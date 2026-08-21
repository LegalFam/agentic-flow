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
_ARTICULO_RE = re.compile(
    r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}art[ií]culo\s+(\d+(?:[\s\-–]?[a-z])?[°º]?)\s*(?:[.\-–—:)º°]|$)",
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

    folded_query = fold(query)

    position = index.folded.find(folded_query)
    if position >= 0:
        return index.offset_map[position], "exact"

    tokens = query.split(" ")
    for size in (20, 15, 10, 6):
        if len(tokens) < size:
            continue
        prefix = fold(" ".join(tokens[:size]))
        position = index.folded.find(prefix)
        if position >= 0:
            return index.offset_map[position], "prefix"

    position = _fuzzy_offset(index, folded_query)
    if position is not None:
        return index.offset_map[position], "fuzzy"

    return None, "none"


def _fuzzy_offset(index: DocumentIndex, folded_query: str) -> int | None:
    from difflib import SequenceMatcher

    window = len(folded_query)
    if window < 40:
        return None

    # Anclas: los tokens mas largos del inicio del snippet, que son los mas discriminantes.
    anchors = sorted(
        {token for token in folded_query[: window // 2].split(" ") if len(token) >= 7},
        key=len,
        reverse=True,
    )[:5]
    if not anchors:
        return None

    candidates: list[int] = []
    for anchor in anchors:
        start = 0
        while len(candidates) < 200:
            found = index.folded.find(anchor, start)
            if found < 0:
                break
            candidates.append(max(0, found - 40))
            start = found + len(anchor)
        if len(candidates) >= 200:
            break

    best_position: int | None = None
    best_ratio = settings.locator_fuzzy_threshold
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(folded_query)

    for candidate in candidates:
        chunk = index.folded[candidate : candidate + window]
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

    match = _SNIPPET_ARTICULO_RE.search(text)
    if not match:
        return EMPTY_LOCATOR

    label = f"Art. {match.group(1)}"

    # Solo se adjunta el inciso cuando es inequivoco: si el snippet cita varios, elegir
    # uno seria enganoso.
    incisos = {found.group(1) for found in _INCISO_RE.finditer(text)}
    if len(incisos) == 1:
        label = f"{label}, inc. {incisos.pop()}"

    return Locator(label=label, breadcrumb=label, page=None, source="snippet_regex")


def resolve(index: DocumentIndex | None, snippet: str) -> Locator:
    """Punto de entrada: indice si hay documento, regex sobre el snippet si no."""
    if index is not None:
        offset, strategy = find_offset(index, snippet)
        if offset is not None:
            locator = build_locator(index, offset, strategy)
            if not locator.is_empty():
                return locator

    return locator_from_snippet(snippet)
