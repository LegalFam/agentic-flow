"""Reancla un snippet recuperado por Gemini File Search contra el markdown original
del documento, para saber en que parte del documento esta (articulo, capitulo, pagina).

Gemini File Search no devuelve offset ni seccion del chunk recuperado: lo unico con
informacion posicional es el texto del snippet. Este modulo lo busca dentro del markdown
y camina hacia atras por la jerarquia de encabezados.

Espacios de coordenadas (no mezclarlos):
- `base`      : markdown con saltos normalizados y NFC aplicado. Todos los offsets de
                `Heading.offset` estan en estas coordenadas.
- `collapsed` : `base` con el whitespace colapsado igual que `clean_user_text`, que es el
                tratamiento que ya recibe el snippet. `offset_map[i]` traduce un indice de
                `collapsed` a su indice en `base`.
- `folded`    : `collapsed` en minusculas, garantizado del mismo largo, solo para comparar.
- `skeleton`  : solo letras y digitos de `base`, sin acentos, en minusculas y sin el
                marcado markdown/HTML. `skeleton_map[i]` traduce a `base`. Es donde se
                ubican los pasajes: ver `skeleton_with_map`.
"""

import re
import unicodedata
from array import array
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher

from app.config import settings
from app.text_utils import clean_user_text

LEVEL_LIBRO = 1
LEVEL_SECCION = 2
LEVEL_TITULO = 3
LEVEL_CAPITULO = 4
LEVEL_SUBCAPITULO = 5
LEVEL_ARTICULO = 6

LEVEL_NAMES = {
    LEVEL_LIBRO: "Libro",
    LEVEL_SECCION: "Seccion",
    LEVEL_TITULO: "Titulo",
    LEVEL_CAPITULO: "Capitulo",
    LEVEL_SUBCAPITULO: "Subcapitulo",
    LEVEL_ARTICULO: "Articulo",
}

# Los encabezados markdown viven fuera de la jerarquia juridica para no contaminar el
# breadcrumb; solo se usan como ultimo recurso cuando no hay ningun encabezado legal.
LEVEL_MARKDOWN = 100

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# "Articulo 333.-", "Articulo 12°", "Articulo 4-A:". Exige separador o fin de linea para no
# capturar prosa como "conforme al articulo 333 del codigo".
#
# El prefijo admite dos decoraciones mas porque el corpus las usa en encabezados reales:
# la vineta de lista ("- Articulo 93º", Codigo de los Ninos y Adolescentes) y la comilla de
# apertura, recta o tipografica, con la que el texto unico ordenado transcribe los articulos
# sustituidos por leyes posteriores ('**" Articulo 21.- Regulacion de la capacidad juridica**',
# '## "Articulo 7. Sujetos de proteccion'). Sin ellas esos 631 encabezados se pierden y su
# texto se atribuye al articulo anterior, que es peor que quedarse sin ubicacion.
#
# Lo que separa un encabezado de una referencia en prosa no es el prefijo sino el separador
# exigido tras el numero: "Articulo 23 de este Codigo." y "articulo 8, de conformidad con la
# ley" siguen fuera porque les sigue una palabra o una coma.
_ARTICULO_RE = re.compile(
    r"^\s{0,8}(?:[-*+•]\s+)?(?:#{1,6}\s*)?[*_\"'“”‘’ ]{0,10}"
    r"art[ií]culo\s+(\d+(?:[\s\-–]?[a-z])?[°º]?)\s*(?:[.\-–—:)º°]|$)",
    re.IGNORECASE,
)

_PAGINA_RE = re.compile(r"^\s{0,8}#{1,6}\s*p[áa]gina\s+(\d+)\s*$", re.IGNORECASE)

_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")

# Encabezados de linea completa. El limite de largo evita capturar prosa que empieza con
# la palabra clave ("Titulo que acredita la propiedad ...").
_WHOLE_LINE_PATTERNS = (
    (LEVEL_LIBRO, "libro", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}libro\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_SECCION, "seccion", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}secci[óo]n\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_SUBCAPITULO, "subcapitulo", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}sub\s?-?\s?cap[ií]tulo\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_CAPITULO, "capitulo", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}cap[ií]tulo\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_TITULO, "titulo", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}t[ií]tulo\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    # Solo las disposiciones que son parte de la estructura. Con "disposicion" + cualquier
    # cosa, el corte de linea de un parrafo ("disposición que lo instituye.") y la sumilla
    # "Disposición de los bienes sociales" entraban como TITULO: cerraban el articulo en
    # curso a media frase y ensuciaban el breadcrumb de los siguientes.
    (LEVEL_TITULO, "disposiciones", re.compile(
        r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}(disposici[óo]n(?:es)?\s+"
        r"(?:complementari|final|transitori|derogatori|modificatori|sustitutori|generales|especiales|preliminar)"
        r"[^.;,\n]{0,60}?)[\s*_]*$",
        re.IGNORECASE,
    )),
)

# Niveles estructurales cuyo nombre suele venir en la linea siguiente.
_NAMED_KINDS = frozenset({"libro", "seccion", "titulo", "capitulo", "subcapitulo"})

_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")
_NUMERAL_RE = re.compile(
    r"^(?:[IVXLCDM]+\b|\d|primer|segund|tercer|cuart|quint|sext|s[eé]p?tim|octav|noven|"
    r"d[eé]cim|und[eé]cim|duod[eé]cim|preliminar|final|[uú]nic)",
    re.IGNORECASE,
)
_INCISO_RE = re.compile(r"\binciso\s+(\d+)", re.IGNORECASE)
_SNIPPET_ARTICULO_RE = re.compile(r"\bart[ií]culo\s+(\d+[\-–]?[a-z]?)", re.IGNORECASE)

# La sumilla que el Codigo Civil pone en negrita ENCIMA del articulo modificado
# ("**Causales de exoneracion de alimentos**" y debajo '**"Articulo 483.-**') es parte de
# ese articulo. Sin moverla, un fragmento que arranca en la sumilla se citaba tambien con el
# articulo anterior.
_SUMILLA_RE = re.compile(r"^\s{0,3}\*\*_?\s*([^*\n]{3,90}?)\s*_?\*\*\s*$")

# Marcado que Gemini no transmite en el chunk: comentarios (`<!-- image -->`) y etiquetas
# (`<br>`). Sus letras no pueden entrar al esqueleto o el pasaje dejaria de casar.
_MARKUP_RE = re.compile(r"<!--.*?-->|</?[a-zA-Z][^<>\n]{0,40}>", re.DOTALL)

# "..." / "…" / "[...]" / "(...)": el agente recorta el pasaje por el medio. Cada tramo se
# ubica por separado y en orden.
_ELLIPSIS_RE = re.compile(r"\[\s*(?:\.{3,}|…)\s*\]|\(\s*(?:\.{3,}|…)\s*\)|\.{3,}|…")

# Ordinales: "76º" y "76°" son el mismo numero, y el grado ni siquiera es alfanumerico.
_NOT_SKELETON = frozenset("ºª")
_SKELETON_CHARS: dict[str, str] = {}

# Holgura al buscar el excerpt alrededor del chunk, en caracteres del esqueleto.
_WINDOW_SLACK = 200

# Alineamiento difuso sobre el esqueleto.
_KGRAM = 12
_MAX_KGRAM_OCCURRENCES = 30
_MAX_CANDIDATES = 40
_MIN_ALIGNED_CHARS = 30
# Un bloque comun mas corto que esto no fija el borde del pasaje: "dela" casa en cualquier
# parte de la ventana y correria el inicio hacia el articulo anterior.
_EDGE_BLOCK = 8
# Tramos entre "..." mas cortos que esto no discriminan nada y se descartan.
_MIN_SEGMENT = 12
# Distancia maxima, en el esqueleto, entre dos tramos consecutivos separados por "...".
_ELLIPSIS_GAP = 6000


@dataclass(frozen=True)
class Heading:
    offset: int
    level: int
    kind: str
    label: str
    page: int | None = None
    # Indice en el esqueleto del primer caracter real del encabezado. Un articulo cubre un
    # tramo solo si el tramo contiene texto suyo; con el offset de linea, un pasaje que
    # terminaba justo antes de "**Articulo 473" arrastraba el 473 por los asteriscos.
    anchor: int = -1


@dataclass
class DocumentIndex:
    base: str
    collapsed: str
    folded: str
    # array en vez de list: una list[int] cuesta ~36 bytes por caracter (un objeto int
    # por posicion) y hacia que 6.5 MB de corpus ocuparan 225 MB de RAM. Con 'i' son 4.
    offset_map: array
    headings: list[Heading] = field(default_factory=list)
    skeleton: str = ""
    skeleton_map: array = field(default_factory=lambda: array("i"))
    articles: list[Heading] = field(default_factory=list)
    article_anchors: list[int] = field(default_factory=list)
    structural_anchors: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class Locator:
    label: str = ""
    breadcrumb: str = ""
    page: int | None = None
    source: str = "none"

    def is_empty(self) -> bool:
        return not self.label and not self.breadcrumb and self.page is None


EMPTY_LOCATOR = Locator()


@dataclass(frozen=True)
class Span:
    """Un pasaje ubicado en el esqueleto.

    `core_*` es lo que efectivamente coincidio; `start`/`end` lo extienden con la parte del
    pasaje que no coincidio. En un match exacto son iguales. Cuando difieren y los dos
    tramos no cubren los mismos articulos, la ubicacion depende de texto no verificado.
    """

    start: int
    end: int
    core_start: int
    core_end: int
    strategy: str


def fold(text: str) -> str:
    """Minusculas garantizando el mismo largo, para no invalidar `offset_map`."""
    out = []
    for char in text:
        lowered = char.lower()
        out.append(lowered if len(lowered) == 1 else char)
    return "".join(out)


def collapse_with_map(base: str) -> tuple[str, array]:
    """Colapsa whitespace igual que `clean_user_text` y devuelve el mapa de offsets.

    Sin este mapa el snippet nunca casa: Gemini lo entrega ya colapsado, mientras que el
    markdown conserva saltos de linea e indentacion.
    """
    chars: list[str] = []
    # Se acumula en list (append rapido) y se convierte al final: array.append en
    # bucle es ~5x mas lento, y array solo se necesita para el almacenamiento.
    offsets: list[int] = []
    pending_space = False

    for index, char in enumerate(base):
        if _CONTROL_CHARS.match(char):
            continue
        # isspace() ya cubre el espacio duro U+00A0 que limpia clean_user_text.
        if char.isspace():
            # El espacio se emite recien cuando aparece un caracter real, para que su
            # offset apunte al inicio del texto y no al whitespace previo.
            if chars:
                pending_space = True
            continue
        if pending_space:
            chars.append(" ")
            offsets.append(index)
            pending_space = False
        chars.append(char)
        offsets.append(index)

    return "".join(chars), array("i", offsets)


def _skeleton_char(char: str) -> str:
    mapped = ""
    if char.isalnum() and char not in _NOT_SKELETON:
        stripped = unicodedata.normalize("NFD", char)[0]
        lowered = stripped.lower()
        mapped = lowered if len(lowered) == 1 else stripped
    _SKELETON_CHARS[char] = mapped
    return mapped


def skeleton_with_map(text: str) -> tuple[str, array]:
    """Solo letras y digitos, sin acentos ni mayusculas, con su posicion en `text`.

    Es el espacio donde se ubican los pasajes. El chunk que devuelve Gemini es el texto
    ya renderizado: sin `**`, sin `#`, sin `<!-- image -->`. En el markdown todo eso
    sigue ahi, y buscar el pasaje literal fallaba justo en los articulos con enfasis; el
    respaldo por prefijo o difuso casaba entonces unos caracteres corrido, y ese desfase
    era lo que mandaba la cita al articulo vecino. Sin puntuacion ni marcado, el chunk
    casa exacto contra el markdown.
    """
    masked = _MARKUP_RE.sub(lambda match: " " * len(match.group(0)), text)
    chars: list[str] = []
    offsets: list[int] = []
    cache = _SKELETON_CHARS
    for index, char in enumerate(masked):
        mapped = cache.get(char)
        if mapped is None:
            mapped = _skeleton_char(char)
        if mapped:
            chars.append(mapped)
            offsets.append(index)
    return "".join(chars), array("i", offsets)


def skeletonize(text: str) -> str:
    return skeleton_with_map(clean_user_text(text))[0]


def article_key(label: str) -> str:
    """`"Art. 76°"`, `"Art. 76"` y `"Art. 4-A"`/`"Art. 4 A"` -> una sola forma."""
    cleaned = label.upper().replace("ARTS.", "").replace("ART.", "")
    return re.sub(r"[\s\-–—.°º]+", "", cleaned)


def _pretty_tail(raw: str) -> str:
    words = [word.strip("*_#") for word in raw.split()]
    words = [word for word in words if word]
    if not words:
        return ""

    # "DERECHO DE FAMILIA" -> "Derecho de familia", pero "III" se queda como esta.
    lexical = [word for word in words if any(char.isalpha() for char in word) and not _ROMAN_RE.match(word)]
    all_caps = bool(lexical) and all(word.isupper() for word in lexical)

    pretty = []
    for position, word in enumerate(words):
        if _ROMAN_RE.match(word):
            pretty.append(word)
        elif all_caps:
            pretty.append(word.capitalize() if position == 0 else word.lower())
        else:
            pretty.append(word)
    return " ".join(pretty)


def _build_label(kind: str, level: int, raw: str) -> str:
    if kind == "articulo":
        # Sin _pretty_tail: bajaria a minuscula el sufijo de "Art. 659 F". Sin ordinal: el
        # corpus mezcla "76°" y "77º", y la cita combinada salia "Arts. 76° y 77º".
        number = raw.strip("*_# ").replace("°", "").replace("º", "")
        return "Art. " + " ".join(number.split()).upper()
    tail = _pretty_tail(raw)
    if kind == "disposiciones":
        return tail
    return f"{LEVEL_NAMES.get(level, '')} {tail}".strip()


def build_index(markdown: str) -> DocumentIndex:
    base = unicodedata.normalize("NFC", markdown.replace("\r\n", "\n").replace("\r", "\n"))
    collapsed, offset_map = collapse_with_map(base)
    skeleton, skeleton_map = skeleton_with_map(base)
    headings: list[Heading] = []

    lines = base.split("\n")
    offsets = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1

    consumed: set[int] = set()
    for position, line in enumerate(lines):
        if position in consumed:
            continue
        heading = _classify_line(line, offsets[position])
        if heading is None:
            continue
        if heading.kind in _NAMED_KINDS:
            name_position, name = _lookahead_name(lines, position)
            if name:
                consumed.add(name_position)
                heading = replace(heading, label=f"{heading.label} - {name}")
        elif heading.kind == "articulo":
            sumilla = _sumilla_above(lines, position, consumed)
            if sumilla >= 0 and (not headings or offsets[sumilla] > headings[-1].offset):
                heading = replace(heading, offset=offsets[sumilla])
        headings.append(heading)

    headings = [
        replace(heading, anchor=bisect_left(skeleton_map, heading.offset)) for heading in headings
    ]
    articles = [heading for heading in headings if heading.kind == "articulo"]
    structural = [heading for heading in headings if heading.level < LEVEL_ARTICULO]

    return DocumentIndex(
        base=base,
        collapsed=collapsed,
        folded=fold(collapsed),
        offset_map=offset_map,
        headings=headings,
        skeleton=skeleton,
        skeleton_map=skeleton_map,
        articles=articles,
        article_anchors=[heading.anchor for heading in articles],
        structural_anchors=[heading.anchor for heading in structural],
    )


def _sumilla_above(lines: list[str], position: int, consumed: set[int]) -> int:
    """Linea de la sumilla en negrita justo encima del articulo, o -1."""
    for index in range(position - 1, max(-1, position - 4), -1):
        line = lines[index]
        if not line.strip():
            continue
        if index in consumed or _classify_line(line, 0) is not None:
            return -1
        match = _SUMILLA_RE.match(line)
        if not match:
            return -1
        name = match.group(1).strip("\"'“”‘’ ")
        if not name or not name[0].isupper() or name.endswith((".", ";", ":", ",")) or "(*)" in name:
            return -1
        return index
    return -1


def _lookahead_name(lines: list[str], position: int) -> tuple[int, str]:
    """Los codigos parten el encabezado en dos lineas: "TITULO IV" y su nombre debajo.

    Sin consumir la segunda linea, un nombre como "DISPOSICIONES GENERALES" se clasifica
    como encabezado propio del mismo nivel y pisa al numero en el breadcrumb.
    """
    for offset in (1, 2):
        index = position + offset
        if index >= len(lines):
            break
        candidate = lines[index].strip().strip("*_#").strip()
        if not candidate:
            continue
        if len(candidate) > 80 or candidate.endswith((".", ";", ",")):
            break
        # Una nota "- (*) Articulo modificado por..." o el encabezado del articulo siguiente no
        # son el nombre del capitulo. Tomados como nombre, el mismo CAPITULO II quedaba con
        # dos breadcrumbs distintos y un fragmento de dos articulos suyos salia sin ubicacion.
        name = candidate.lstrip("-•+ ").strip("\"'“”‘’ ")
        if not name or not name[0].isalpha() or re.search(r"\bart[ií]culo\b", name, re.IGNORECASE):
            break
        if _classify_line(lines[index], 0) is not None and not _is_bare_name(candidate):
            break
        if not any(char.isalpha() for char in candidate):
            break
        return index, _pretty_tail(candidate)
    return -1, ""


def _is_bare_name(candidate: str) -> bool:
    """Un nombre suelto como "DISPOSICIONES GENERALES" no lleva numeracion propia."""
    # Cualquier digito cuenta: con `\b\d+\b`, "Articulo 25º" pasaba por nombre suelto porque
    # el ordinal es un caracter de palabra y no deja limite tras el 5.
    return not re.search(r"\b[IVXLCDM]+\b|\d", candidate)


def _classify_line(line: str, offset: int) -> Heading | None:
    if not line.strip():
        return None

    page_match = _PAGINA_RE.match(line)
    if page_match:
        number = int(page_match.group(1))
        return Heading(offset=offset, level=LEVEL_MARKDOWN, kind="pagina", label=f"Pagina {number}", page=number)

    # El articulo va primero: es el unico patron que admite texto despues en la misma linea.
    articulo_match = _ARTICULO_RE.match(line)
    if articulo_match:
        return Heading(
            offset=offset,
            level=LEVEL_ARTICULO,
            kind="articulo",
            label=_build_label("articulo", LEVEL_ARTICULO, articulo_match.group(1)),
        )

    # Un encabezado estructural no termina en punto: "Titulo II de la SECCION SEGUNDA de este
    # Código." es el corte de linea de un parrafo, y tomado por TITULO cerraba a media frase
    # el articulo que lo contiene.
    if line.rstrip(" *_").endswith((".", ",", ";")):
        return _markdown_heading(line, offset)

    for level, kind, pattern in _WHOLE_LINE_PATTERNS:
        match = pattern.match(line)
        # "Titulo que da mérito a la inscripción" es un titulo en sentido de documento, no
        # un TITULO del codigo. De 954 encabezados estructurales del corpus, 952 llevan
        # numeral u ordinal; los 2 que no, son justo esos, y cerraban el articulo en curso.
        if match and kind in _NAMED_KINDS and not _NUMERAL_RE.match(match.group(1).lstrip(" :-.*_")):
            continue
        if match:
            return Heading(offset=offset, level=level, kind=kind, label=_build_label(kind, level, match.group(1)))

    return _markdown_heading(line, offset)


def _markdown_heading(line: str, offset: int) -> Heading | None:
    markdown_match = _MARKDOWN_HEADING_RE.match(line)
    if markdown_match:
        return Heading(
            offset=offset,
            level=LEVEL_MARKDOWN + len(markdown_match.group(1)),
            kind="markdown",
            label=_pretty_tail(markdown_match.group(2)),
        )
    return None


# --------------------------------------------------------------------------------------
# Busqueda literal sobre el texto plegado. Se conserva para la metrica de literalidad del
# evaluador; la ubicacion ya no depende de ella.


def find_offset(index: DocumentIndex, snippet: str) -> tuple[int | None, str]:
    """Devuelve `(offset en coordenadas base, estrategia)`."""
    query = clean_user_text(snippet)
    if not query or not index.folded:
        return None, "none"

    position, strategy = find_in_folded(index.folded, query)
    if position is None:
        return None, "none"
    return index.offset_map[position], strategy


def find_in_folded(haystack: str, query: str) -> tuple[int | None, str]:
    """Busca `query` (ya colapsado) en un texto folded: `(posicion, estrategia)`.

    La posicion esta en las coordenadas del haystack, no en `base`.
    """
    if not haystack or not query:
        return None, "none"

    folded_query = fold(query)

    position = haystack.find(folded_query)
    if position >= 0:
        return position, "exact"

    tokens = query.split(" ")
    for size in (20, 15, 10, 6):
        if len(tokens) < size:
            continue
        prefix = fold(" ".join(tokens[:size]))
        position = haystack.find(prefix)
        if position >= 0:
            return position, "prefix"

    position = _fuzzy_offset(haystack, folded_query)
    if position is not None:
        return position, "fuzzy"

    return None, "none"


def _fuzzy_offset(haystack: str, folded_query: str) -> int | None:
    window = len(folded_query)
    if window < 40:
        return None

    # Anclas: los tokens del inicio del snippet que menos aparecen en el documento, que son
    # los que de verdad discriminan. Frecuencia, largo y texto dejan un orden total, que no
    # depende de PYTHONHASHSEED.
    tokens = {token for token in folded_query[: window // 2].split(" ") if len(token) >= 7}
    ranked = sorted(tokens, key=lambda token: (haystack.count(token), -len(token), token))
    anchors = [token for token in ranked if token in haystack][:5]
    if not anchors:
        return None

    candidates: set[int] = set()
    for anchor in anchors:
        shift = folded_query.find(anchor)
        start = 0
        while len(candidates) < 200:
            found = haystack.find(anchor, start)
            if found < 0:
                break
            candidates.add(max(0, found - shift))
            start = found + len(anchor)
        if len(candidates) >= 200:
            break

    best_position: int | None = None
    best_ratio = settings.locator_fuzzy_threshold
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(folded_query)

    for candidate in sorted(candidates):
        chunk = haystack[candidate : candidate + window]
        matcher.set_seq1(chunk)
        if matcher.real_quick_ratio() < best_ratio or matcher.quick_ratio() < best_ratio:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_position = candidate

    return best_position


# --------------------------------------------------------------------------------------
# Ubicacion de pasajes sobre el esqueleto


def find_spans(haystack: str, query: str, lo: int = 0, hi: int | None = None) -> list[Span]:
    """Todas las apariciones de `query` (un esqueleto) dentro de `haystack[lo:hi]`.

    Devuelve todas y no la primera: el Codigo Civil repite el texto original de un articulo
    junto a su version modificada, y la Ley 30364 repite articulos enteros. Quedarse con la
    primera aparicion es elegir a ciegas; con todas, quien llama puede comprobar si las
    lecturas coinciden.
    """
    hi = len(haystack) if hi is None else max(lo, min(hi, len(haystack)))
    lo = max(0, lo)
    if not query or hi <= lo:
        return []

    spans: list[Span] = []
    position = haystack.find(query, lo, hi)
    while position >= 0 and len(spans) < _MAX_CANDIDATES:
        end = position + len(query)
        spans.append(Span(position, end, position, end, "exact"))
        position = haystack.find(query, position + 1, hi)
    if spans:
        return spans
    return _aligned_spans(haystack, query, lo, hi)


def _aligned_spans(haystack: str, query: str, lo: int, hi: int) -> list[Span]:
    """Alineamiento tolerante: el agente cambia una letra, omite una palabra o corta a medias.

    Las ventanas candidatas salen de los k-gramas mas raros del pasaje. Dentro de cada
    ventana, los bloques comunes de `SequenceMatcher` dan la posicion exacta de lo que
    coincidio, asi que el borde del pasaje no se corre aunque el texto difiera: el desfase
    del difuso anterior, que media ventana contra ventana, era lo que cruzaba de articulo.
    """
    length = len(query)
    if length < _MIN_ALIGNED_CHARS:
        return []

    grams: list[tuple[int, int, str]] = []
    for offset in range(0, length - _KGRAM + 1, _KGRAM // 2):
        gram = query[offset : offset + _KGRAM]
        count = haystack.count(gram, lo, hi)
        if 0 < count <= _MAX_KGRAM_OCCURRENCES:
            grams.append((count, offset, gram))
    if not grams:
        return []
    grams.sort()

    starts: list[int] = []
    for _, offset, gram in grams[:8]:
        position = haystack.find(gram, lo, hi)
        while position >= 0:
            starts.append(position - offset)
            position = haystack.find(gram, position + 1, hi)
    starts.sort()

    slack = length // 4 + _KGRAM
    clusters: list[list[int]] = []
    for start in starts:
        if clusters and start - clusters[-1][-1] <= slack:
            clusters[-1].append(start)
        else:
            clusters.append([start])
    # Primero las ventanas con mas votos; a igual voto, la primera en el documento.
    clusters.sort(key=lambda cluster: (-len(cluster), cluster[0]))

    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(query)
    minimum = settings.locator_align_min_coverage * length
    found: list[Span] = []
    for cluster in clusters[:_MAX_CANDIDATES]:
        anchor = cluster[len(cluster) // 2]
        window_start = max(lo, anchor - slack)
        window_end = min(hi, anchor + length + slack)
        matcher.set_seq1(haystack[window_start:window_end])
        # Solo bloques largos cuentan como coincidencia: con bloques de 4 letras, un pasaje
        # con las palabras barajadas cubria el 80% y se daba por verificado.
        blocks = [block for block in matcher.get_matching_blocks() if block.size >= _EDGE_BLOCK]
        if not blocks or sum(block.size for block in blocks) < minimum:
            continue
        if not _digits_covered(query, blocks):
            continue
        first, last = blocks[0], blocks[-1]
        core_start = window_start + first.a
        core_end = window_start + last.a + last.size
        start = max(lo, core_start - first.b)
        end = min(hi, core_end + (length - last.b - last.size))
        found.append(Span(start, end, core_start, core_end, "fuzzy"))

    found.sort(key=lambda span: span.core_start)
    unique: list[Span] = []
    for span in found:
        if unique and span.core_start < unique[-1].core_end:
            continue
        unique.append(span)
    return unique


def _digits_covered(query: str, blocks: list) -> bool:
    """Los numeros no admiten parecido: "articulo 58" y "articulo 387" son otra norma.

    En texto juridico los digitos son justo lo que distingue un pasaje de su gemelo (las
    concordancias del Codigo Procesal Civil repiten la misma frase con otro numero de
    casacion), asi que un alineamiento con algun digito sin casar no se acepta.
    """
    covered = bytearray(len(query))
    for block in blocks:
        covered[block.b : block.b + block.size] = b"\x01" * block.size
    return all(covered[position] or not char.isdigit() for position, char in enumerate(query))


def passage_segments(text: str) -> list[str]:
    """Esqueletos de los tramos del pasaje, partido por los "..." que puso el agente."""
    parts = [skeleton_with_map(part)[0] for part in _ELLIPSIS_RE.split(clean_user_text(text))]
    parts = [part for part in parts if part]
    if len(parts) > 1:
        parts = [part for part in parts if len(part) >= _MIN_SEGMENT]
    return parts


def locate_segments(haystack: str, segments: list[str], lo: int = 0, hi: int | None = None) -> list[Span]:
    """Ubica los tramos en orden; cada aparicion del primero es una lectura posible."""
    if not segments:
        return []
    hi = len(haystack) if hi is None else min(hi, len(haystack))
    first = find_spans(haystack, segments[0], lo, hi)
    if len(segments) == 1:
        return first

    readings: list[Span] = []
    for span in first:
        current = span
        exact = span.strategy == "exact"
        for segment in segments[1:]:
            following = find_spans(
                haystack, segment, current.end, min(hi, current.end + _ELLIPSIS_GAP + len(segment))
            )
            if not following:
                current = None
                break
            current = following[0]
            exact = exact and current.strategy == "exact"
        if current is None:
            continue
        readings.append(
            Span(span.start, current.end, span.core_start, current.core_end, "exact" if exact else "fuzzy")
        )
    return readings


def skeleton_articles(index: DocumentIndex, start: int, end: int) -> list[Heading]:
    """Articulos con texto dentro del tramo `[start, end)` del esqueleto.

    El articulo que gobierna el inicio cuenta salvo que un encabezado de nivel superior
    (un TITULO nuevo) lo haya cerrado antes, o que quede tan lejos que ya no lo gobierne.
    Los demas cuentan si su primer caracter real cae dentro del tramo.
    """
    anchors = index.article_anchors
    spanned: list[Heading] = []

    inside = bisect_right(anchors, start)
    if inside > 0:
        governing = index.articles[inside - 1]
        structural = bisect_right(index.structural_anchors, start)
        closed = structural > 0 and index.structural_anchors[structural - 1] > governing.anchor
        base_start = _base_offset(index, start)
        if not closed and base_start - governing.offset <= settings.locator_max_article_span:
            spanned.append(governing)

    position = inside
    while position < len(anchors) and anchors[position] < end:
        spanned.append(index.articles[position])
        position += 1

    # Un articulo modificado aparece dos veces seguidas en el corpus: el texto original y,
    # tras la nota "Articulo modificado por ...", el vigente. Son el mismo articulo.
    seen: set[str] = set()
    unique: list[Heading] = []
    for heading in spanned:
        key = article_key(heading.label)
        if key not in seen:
            seen.add(key)
            unique.append(heading)
    return unique


def _base_offset(index: DocumentIndex, skeleton_position: int) -> int:
    if skeleton_position < len(index.skeleton_map):
        return index.skeleton_map[skeleton_position]
    return len(index.base)


def _keys(headings: list[Heading]) -> tuple[str, ...]:
    return tuple(article_key(heading.label) for heading in headings)


@dataclass(frozen=True)
class Reading:
    span: Span
    articles: list[Heading]


def readings_of(index: DocumentIndex, spans: list[Span]) -> tuple[list[Reading], bool]:
    """Lecturas distintas (por conjunto de articulos) y si todas son seguras.

    Una lectura es insegura cuando la parte que coincidio y la extension con el texto no
    verificado cubren articulos distintos: el borde lo decidiria texto que no casa.
    """
    readings: dict[tuple[str, ...], Reading] = {}
    certain = True
    for span in spans:
        articles = skeleton_articles(index, span.start, span.end)
        if (span.core_start, span.core_end) != (span.start, span.end):
            core = skeleton_articles(index, span.core_start, span.core_end)
            if _keys(core) != _keys(articles):
                certain = False
        readings.setdefault(_keys(articles), Reading(span, articles))
    return list(readings.values()), certain


def passage_readings(index: DocumentIndex, text: str) -> list[list[str]]:
    """Conjuntos de articulos que podria cubrir `text` segun donde aparece en el documento.

    Sin chunk que acote la busqueda: es lo que puede recalcular un evaluador externo. Una
    lista vacia significa que el pasaje no aparece.
    """
    spans = locate_segments(index.skeleton, passage_segments(text))
    readings, _ = readings_of(index, spans)
    return [[heading.label for heading in reading.articles] for reading in readings]


# --------------------------------------------------------------------------------------
# Construccion del locator


def build_locator(index: DocumentIndex, offset: int, source: str) -> Locator:
    """Camina hacia atras tomando el encabezado mas cercano de cada nivel juridico."""
    nearest: dict[int, Heading] = {}
    page: int | None = None
    nearest_markdown: Heading | None = None

    for heading in index.headings:
        if heading.offset > offset:
            break
        if heading.kind == "pagina":
            page = heading.page
            continue
        if heading.level >= LEVEL_MARKDOWN:
            nearest_markdown = heading
            continue
        nearest[heading.level] = heading
        # Un encabezado de nivel superior invalida los inferiores ya vistos:
        # un TITULO nuevo cierra el articulo anterior.
        for level in list(nearest):
            if level > heading.level:
                del nearest[level]

    # Nada "cierra" un articulo, asi que un texto lejano heredaria el ultimo articulo visto.
    # En un codigo eso es correcto; en una resolucion con OCR produce una cita falsa.
    articulo = nearest.get(LEVEL_ARTICULO)
    if articulo is not None and offset - articulo.offset > settings.locator_max_article_span:
        del nearest[LEVEL_ARTICULO]

    chain = [nearest[level].label for level in sorted(nearest)]

    # Sin jerarquia juridica solo queda el encabezado markdown, que en resoluciones y
    # casaciones es el asunto del caso o ruido del OCR, no una ubicacion. Se conserva,
    # pero etiquetado aparte para que quien consuma la cita pueda descartarlo.
    if not chain and nearest_markdown is not None:
        chain = [nearest_markdown.label]
        source = "markdown_heading"

    if not chain and page is None:
        return EMPTY_LOCATOR

    return Locator(
        label=chain[-1] if chain else f"Pagina {page}",
        breadcrumb=" > ".join(chain),
        page=page,
        source=source,
    )


def locator_from_snippet(snippet: str) -> Locator:
    """Fallback sin corpus: los chunks suelen arrastrar su propio encabezado.

    Da valor antes de que existan los markdowns y degrada con gracia cuando falta un
    archivo en el corpus.
    """
    text = clean_user_text(snippet)
    if not text:
        return EMPTY_LOCATOR

    numbers = [found.group(1) for found in _SNIPPET_ARTICULO_RE.finditer(text)]
    if not numbers:
        return EMPTY_LOCATOR

    # Sin indice no hay forma de saber donde termina un articulo y empieza el siguiente,
    # asi que un texto que nombra varios no se puede repartir: elegir el primero seria
    # adivinar, y una atribucion falsa es peor que una cita sin ubicacion.
    if len({number.upper() for number in numbers}) > 1:
        return EMPTY_LOCATOR

    label = f"Art. {numbers[0]}"

    # Solo se adjunta el inciso cuando es inequivoco: si el snippet cita varios, elegir
    # uno seria enganoso.
    incisos = {found.group(1) for found in _INCISO_RE.finditer(text)}
    if len(incisos) == 1:
        label = f"{label}, inc. {incisos.pop()}"

    return Locator(label=label, breadcrumb=label, page=None, source="snippet_regex")


def headings_in_span(index: DocumentIndex, start: int, end: int) -> list[Heading]:
    """Encabezados de articulo que cubren el tramo `[start, end)` en coordenadas base."""
    return skeleton_articles(
        index, bisect_left(index.skeleton_map, start), bisect_left(index.skeleton_map, end)
    )


def articles_in_span(index: DocumentIndex, start: int, end: int) -> list[str]:
    """Articulos que cubre el tramo `[start, end)` en coordenadas base.

    Un chunk de File Search no respeta el articulado: puede arrancar a media frase del
    Art. 561 y terminar dentro del 563. Saber cuantos articulos abarca es lo que permite
    distinguir un tramo cuya ubicacion es inequivoca de uno donde quedarse con el primero
    seria adivinar.
    """
    return [heading.label for heading in headings_in_span(index, start, end)]


def _parent_chain(breadcrumb: str, label: str) -> str:
    """El breadcrumb sin su ultimo eslabon, que es el articulo."""
    if breadcrumb == label:
        return ""
    suffix = f" > {label}"
    return breadcrumb[: -len(suffix)] if breadcrumb.endswith(suffix) else breadcrumb


def _combined_label(labels: list[str]) -> str:
    """`["Art. 562", "Art. 563"]` -> `"Arts. 562 y 563"`."""
    numbers = [label[len("Art. ") :] if label.startswith("Art. ") else label for label in labels]
    if len(numbers) == 1:
        return f"Art. {numbers[0]}"
    return "Arts. " + ", ".join(numbers[:-1]) + f" y {numbers[-1]}"


def combine_locator(index: DocumentIndex, headings: list[Heading], source: str) -> Locator:
    """Un tramo que cruza articulos se ubica con todos ellos, nunca con el primero.

    Atribuirlo al primero es lo que producia citas bien redactadas y mal ubicadas. Si los
    articulos cuelgan de padres distintos (el tramo cruza de un capitulo a otro), la
    etiqueta sigue siendo exacta —nombra cada articulo que el tramo toca— y el breadcrumb
    se queda en el ancestro comun. Antes esa cita salia sin ubicacion aunque no hubiera
    nada que adivinar: en ablacion-v1, "Arts. 326 a 329" y "Arts. 817 y 818".
    """
    if not headings or len(headings) > max(1, settings.locator_max_combined_articles):
        return EMPTY_LOCATOR

    chains: list[list[str]] = []
    page: int | None = None
    for heading in headings:
        found = build_locator(index, heading.offset, source)
        if found.is_empty() or found.label != heading.label:
            return EMPTY_LOCATOR
        parent_chain = _parent_chain(found.breadcrumb, heading.label)
        chains.append(parent_chain.split(" > ") if parent_chain else [])
        if page is None:
            page = found.page

    common: list[str] = []
    for links in zip(*chains):
        if len(set(links)) != 1:
            break
        common.append(links[0])

    parent = " > ".join(common)
    label = _combined_label([heading.label for heading in headings])
    return Locator(
        label=label,
        breadcrumb=f"{parent} > {label}" if parent else label,
        page=page,
        source=source,
    )


def _labels(readings: list[Reading]) -> list[str]:
    """Union de los articulos de todas las lecturas, en orden de documento."""
    seen: dict[str, Heading] = {}
    for reading in readings:
        for heading in reading.articles:
            seen.setdefault(article_key(heading.label), heading)
    return [heading.label for heading in sorted(seen.values(), key=lambda heading: heading.anchor)]


def _locator_for(index: DocumentIndex, reading: Reading) -> Locator:
    if reading.articles:
        return combine_locator(index, reading.articles, reading.span.strategy)
    # Sin articulado (preambulo, resoluciones): la jerarquia o el encabezado markdown.
    return build_locator(index, _base_offset(index, reading.span.start), reading.span.strategy)


def resolve_chunk(index: DocumentIndex | None, snippet: str) -> tuple[Locator, list[str]]:
    """Ubica el chunk recuperado y reporta que articulos abarca.

    El locator del chunk es el de su primer articulo: quien lo consume solo lo usa cuando
    el chunk abarca uno solo. Si el chunk aparece en varios sitios con articulos distintos
    no hay locator, y la lista devuelta es la union para que el caller lo trate como
    ambiguo.
    """
    if index is None:
        return locator_from_snippet(snippet), []

    spans = locate_segments(index.skeleton, passage_segments(snippet))
    if not spans:
        return locator_from_snippet(snippet), []

    readings, certain = readings_of(index, spans)
    if len(readings) > 1 or not certain:
        return EMPTY_LOCATOR, _labels(readings)

    reading = readings[0]
    articles = [heading.label for heading in reading.articles]
    if reading.articles:
        found = combine_locator(index, reading.articles[:1], reading.span.strategy)
    else:
        found = _locator_for(index, reading)
    if found.is_empty():
        return locator_from_snippet(snippet) if not articles else EMPTY_LOCATOR, articles
    return found, articles


def resolve_excerpt(index: DocumentIndex | None, chunk: str, excerpt: str) -> Locator:
    """Ubicacion del fragmento citado, sin el detalle de que articulos abarca."""
    return resolve_excerpt_span(index, chunk, excerpt)[0]


def resolve_excerpt_span(
    index: DocumentIndex | None, chunk: str, excerpt: str
) -> tuple[Locator, list[str]]:
    """Ubica el fragmento que el agente dice haber usado, exigiendo que salga del chunk.

    El locator del chunk se queda con el primer articulo que aparece, que no tiene por que
    ser el que sustenta la respuesta cuando el chunk abarca varios. Reanclando sobre el
    texto citado, la ubicacion corresponde a lo que el agente realmente uso.

    Tres garantias, en este orden:
    1. El excerpt tiene que salir del chunk (exacto o casi) o no se ubica: pudo inventarlo.
    2. Se busca en el documento todas las veces que aparece dentro de la zona del chunk. Si
       las apariciones no coinciden en los articulos, o el borde del pasaje depende de
       texto que no casa, no hay ubicacion.
    3. Un fragmento que cruza articulos se cita con todos o con ninguno.

    Devuelve `(EMPTY_LOCATOR, articulos)` cuando la ubicacion no es segura: el caller
    decide como degradar, pero nunca se elige un articulo a ciegas.
    """
    chunk_text = clean_user_text(chunk)
    excerpt_text = clean_user_text(excerpt)
    if not chunk_text or not excerpt_text:
        return EMPTY_LOCATOR, []

    # Un excerpt muy corto ("El demandante") casa en cualquier parte y no discrimina un
    # articulo de otro, que es justo lo que se quiere resolver.
    if len(excerpt_text) < settings.locator_min_excerpt_chars:
        return EMPTY_LOCATOR, []

    segments = passage_segments(excerpt_text)
    chunk_skeleton = skeleton_with_map(chunk_text)[0]
    if not segments or not locate_segments(chunk_skeleton, segments):
        return EMPTY_LOCATOR, []

    if index is None:
        return locator_from_snippet(excerpt_text), []

    chunk_spans = locate_segments(index.skeleton, passage_segments(chunk_text))
    if not chunk_spans:
        # Corpus desincronizado: el chunk no esta en este markdown.
        return locator_from_snippet(excerpt_text), []

    spans: list[Span] = []
    for chunk_span in chunk_spans:
        for span in locate_segments(
            index.skeleton, segments, chunk_span.start - _WINDOW_SLACK, chunk_span.end + _WINDOW_SLACK
        ):
            if span not in spans:
                spans.append(span)
    if not spans:
        return EMPTY_LOCATOR, []

    readings, certain = readings_of(index, spans)
    if len(readings) > 1 or not certain:
        return EMPTY_LOCATOR, _labels(readings)

    reading = readings[0]
    articles = [heading.label for heading in reading.articles]
    return _locator_for(index, reading), articles


def resolve(index: DocumentIndex | None, snippet: str) -> Locator:
    """Punto de entrada: indice si hay documento, regex sobre el snippet si no."""
    return resolve_chunk(index, snippet)[0]
