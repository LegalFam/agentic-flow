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
"""

import re
import unicodedata
from array import array
from dataclasses import dataclass, field

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
    (LEVEL_TITULO, "disposiciones", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}(disposici[óo]n(?:es)?\s+.{1,60}?)[\s*_]*$", re.IGNORECASE)),
)

# Niveles estructurales cuyo nombre suele venir en la linea siguiente.
_NAMED_KINDS = frozenset({"libro", "seccion", "titulo", "capitulo", "subcapitulo"})

_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")
_INCISO_RE = re.compile(r"\binciso\s+(\d+)", re.IGNORECASE)
_SNIPPET_ARTICULO_RE = re.compile(r"\bart[ií]culo\s+(\d+[\-–]?[a-z]?)", re.IGNORECASE)

# Holgura al buscar el excerpt dentro del chunk: absorbe el desfase de un chunk que solo
# caso por prefijo o difuso, sin llegar a alcanzar el articulo siguiente.
_WINDOW_SLACK = 200


@dataclass(frozen=True)
class Heading:
    offset: int
    level: int
    kind: str
    label: str
    page: int | None = None


@dataclass
class DocumentIndex:
    base: str
    collapsed: str
    folded: str
    # array en vez de list: una list[int] cuesta ~36 bytes por caracter (un objeto int
    # por posicion) y hacia que 6.5 MB de corpus ocuparan 225 MB de RAM. Con 'i' son 4.
    offset_map: array
    headings: list[Heading] = field(default_factory=list)


@dataclass(frozen=True)
class Locator:
    label: str = ""
    breadcrumb: str = ""
    page: int | None = None
    source: str = "none"

    def is_empty(self) -> bool:
        return not self.label and not self.breadcrumb and self.page is None


EMPTY_LOCATOR = Locator()


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
        # Sin _pretty_tail: bajaria a minuscula el sufijo de "Art. 659 F".
        return "Art. " + " ".join(raw.strip("*_# ").split()).upper()
    tail = _pretty_tail(raw)
    if kind == "disposiciones":
        return tail
    return f"{LEVEL_NAMES.get(level, '')} {tail}".strip()


def build_index(markdown: str) -> DocumentIndex:
    base = unicodedata.normalize("NFC", markdown.replace("\r\n", "\n").replace("\r", "\n"))
    collapsed, offset_map = collapse_with_map(base)
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
                heading = Heading(
                    offset=heading.offset,
                    level=heading.level,
                    kind=heading.kind,
                    label=f"{heading.label} - {name}",
                )
        headings.append(heading)

    return DocumentIndex(
        base=base,
        collapsed=collapsed,
        folded=fold(collapsed),
        offset_map=offset_map,
        headings=headings,
    )


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
        if _classify_line(lines[index], 0) is not None and not _is_bare_name(candidate):
            break
        if not any(char.isalpha() for char in candidate):
            break
        return index, _pretty_tail(candidate)
    return -1, ""


def _is_bare_name(candidate: str) -> bool:
    """Un nombre suelto como "DISPOSICIONES GENERALES" no lleva numeracion propia."""
    return not re.search(r"\b(?:[IVXLCDM]+|\d+)\b", candidate)


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

    for level, kind, pattern in _WHOLE_LINE_PATTERNS:
        match = pattern.match(line)
        if match:
            return Heading(offset=offset, level=level, kind=kind, label=_build_label(kind, level, match.group(1)))

    markdown_match = _MARKDOWN_HEADING_RE.match(line)
    if markdown_match:
        return Heading(
            offset=offset,
            level=LEVEL_MARKDOWN + len(markdown_match.group(1)),
            kind="markdown",
            label=_pretty_tail(markdown_match.group(2)),
        )

    return None


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

    La posicion esta en las coordenadas del haystack, no en `base`. Se separo de
    `find_offset` porque el mismo escalonado exacto -> prefijo -> difuso hace falta
    tambien contra un chunk suelto, que no tiene indice ni `offset_map`.
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
    from difflib import SequenceMatcher

    window = len(folded_query)
    if window < 40:
        return None

    # Anclas: los tokens del inicio del snippet que menos aparecen en el documento, que son
    # los que de verdad discriminan. Ordenar solo por largo dejaba los empates al orden de
    # iteracion del set, que depende de PYTHONHASHSEED: segun el proceso entraban anclas
    # como "articulo" (miles de apariciones), llenaban el tope de 200 candidatos y la
    # ventana buena nunca se evaluaba. La misma cita salia con otros articulos en cada
    # corrida. Frecuencia, largo y texto dejan un orden total.
    tokens = {token for token in folded_query[: window // 2].split(" ") if len(token) >= 7}
    ranked = sorted(tokens, key=lambda token: (haystack.count(token), -len(token), token))
    anchors = [token for token in ranked if token in haystack][:5]
    if not anchors:
        return None

    candidates: set[int] = set()
    for anchor in anchors:
        # Cada aparicion del ancla fija donde empezaria el snippet: se alinea con la
        # posicion del ancla dentro del query, no con un margen fijo que desplaza la
        # ventana y la hace cruzar al articulo siguiente.
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

    # En orden de posicion y con `>` estricto: a igual parecido gana la primera aparicion.
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
    spanned: list[Heading] = []
    current: Heading | None = None

    for heading in index.headings:
        if heading.kind != "articulo":
            continue
        if heading.offset <= start:
            current = heading
            continue
        if heading.offset >= end:
            break
        spanned.append(heading)

    # El articulo abierto antes del tramo tambien lo cubre, salvo que quede tan lejos que
    # ya no lo gobierne (mismo criterio que build_locator).
    if current is not None and start - current.offset <= settings.locator_max_article_span:
        spanned.insert(0, current)

    return spanned


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
    """Un tramo que cruza articulos se ubica con todos ellos, o con ninguno.

    Atribuirlo al primero es lo que producia citas bien redactadas y mal ubicadas. Citar
    los dos solo es honesto si cuelgan del mismo padre: un tramo que cruza de un titulo a
    otro ya no ubica nada, y ahi es mejor una cita sin ubicacion.
    """
    if not headings or len(headings) > max(1, settings.locator_max_combined_articles):
        return EMPTY_LOCATOR

    parents = set()
    page: int | None = None
    for heading in headings:
        found = build_locator(index, heading.offset, source)
        if found.is_empty():
            return EMPTY_LOCATOR
        parents.add(_parent_chain(found.breadcrumb, heading.label))
        if page is None:
            page = found.page

    if len(parents) != 1:
        return EMPTY_LOCATOR

    parent = parents.pop()
    label = _combined_label([heading.label for heading in headings])
    return Locator(
        label=label,
        breadcrumb=f"{parent} > {label}" if parent else label,
        page=page,
        source=source,
    )


def resolve_chunk(index: DocumentIndex | None, snippet: str) -> tuple[Locator, list[str]]:
    """Ubica el chunk recuperado y reporta que articulos abarca."""
    if index is None:
        return locator_from_snippet(snippet), []

    query = clean_user_text(snippet)
    position, strategy = find_in_folded(index.folded, query)
    if position is None:
        return locator_from_snippet(snippet), []

    start = index.offset_map[position]
    end_position = min(position + len(query), len(index.offset_map) - 1)
    end = index.offset_map[end_position]

    found = build_locator(index, start, strategy)
    if found.is_empty():
        return locator_from_snippet(snippet), []

    return found, articles_in_span(index, start, end + 1)


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

    El excerpt tampoco tiene por que caer dentro de un solo articulo: un fragmento que
    arranca en el 562 y sigue dentro del 563 se cita con los dos, y si no se pueden
    combinar sale sin ubicacion. Quedarse con el primero reintroduce exactamente la
    atribucion falsa que este modulo existe para evitar.

    Devuelve `(EMPTY_LOCATOR, articulos)` cuando el excerpt no se puede verificar contra
    el chunk o cruza articulos incombinables: el caller decide como degradar, pero nunca
    se ubica texto que el modelo pudo inventar ni se elige un articulo a ciegas.
    """
    chunk_text = clean_user_text(chunk)
    excerpt_text = clean_user_text(excerpt)
    if not chunk_text or not excerpt_text:
        return EMPTY_LOCATOR, []

    # Un excerpt muy corto ("El demandante") casa en cualquier parte y no discrimina un
    # articulo de otro, que es justo lo que se quiere resolver.
    if len(excerpt_text) < settings.locator_min_excerpt_chars:
        return EMPTY_LOCATOR, []

    folded_chunk = fold(chunk_text)
    inner, strategy = find_in_folded(folded_chunk, excerpt_text)
    if inner is None:
        return EMPTY_LOCATOR, []

    if index is None:
        return locator_from_snippet(excerpt_text), []

    chunk_start, _ = find_in_folded(index.folded, chunk_text)
    if chunk_start is None:
        return locator_from_snippet(excerpt_text), []

    # Buscar el excerpt directamente dentro de la ventana del chunk es lo mas preciso;
    # la suma de posiciones es el respaldo cuando el chunk solo caso por prefijo o difuso
    # y por lo tanto arrastra un desfase de unos pocos caracteres.
    window_end = chunk_start + len(folded_chunk) + _WINDOW_SLACK
    direct = index.folded.find(fold(excerpt_text), chunk_start, window_end)
    if direct >= 0:
        position, strategy = direct, "exact"
    else:
        position = min(chunk_start + inner, len(index.offset_map) - 1)

    start = index.offset_map[position]
    end_position = min(position + len(excerpt_text), len(index.offset_map) - 1)
    end = index.offset_map[end_position]

    spanned = headings_in_span(index, start, end + 1)
    articles = [heading.label for heading in spanned]

    if len(spanned) > 1:
        return combine_locator(index, spanned, strategy), articles

    found = build_locator(index, start, strategy)
    if found.is_empty():
        return locator_from_snippet(excerpt_text), articles
    return found, articles


def resolve(index: DocumentIndex | None, snippet: str) -> Locator:
    """Punto de entrada: indice si hay documento, regex sobre el snippet si no."""
    return resolve_chunk(index, snippet)[0]
