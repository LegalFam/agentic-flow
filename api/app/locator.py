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

LEVEL_MARKDOWN = 100

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_ARTICULO_RE = re.compile(
    r"^\s{0,8}(?:[-*+•]\s+)?(?:#{1,6}\s*)?[*_\"'“”‘’ ]{0,10}"
    r"art[ií]culo\s+(\d+(?:[\s\-–]?[a-z])?[°º]?)\s*(?:[.\-–—:)]|[º°](?!\s*[,a-záéíóúüñ])|$)",
    re.IGNORECASE,
)

_PAGINA_RE = re.compile(r"^\s{0,8}#{1,6}\s*p[áa]gina\s+(\d+)\s*$", re.IGNORECASE)

_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")

_WHOLE_LINE_PATTERNS = (
    (LEVEL_LIBRO, "libro", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}libro\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_SECCION, "seccion", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}secci[óo]n\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_SUBCAPITULO, "subcapitulo", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}sub\s?-?\s?cap[ií]tulo\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_CAPITULO, "capitulo", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}cap[ií]tulo\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_TITULO, "titulo", re.compile(r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}t[ií]tulo\s+(.{1,60}?)[\s*_]*$", re.IGNORECASE)),
    (LEVEL_TITULO, "disposiciones", re.compile(
        r"^\s{0,8}(?:#{1,6}\s*)?[*_]{0,4}(disposici[óo]n(?:es)?\s+"
        r"(?:complementari|final|transitori|derogatori|modificatori|sustitutori|generales|especiales|preliminar)"
        r"[^.;,\n]{0,60}?)[\s*_]*$",
        re.IGNORECASE,
    )),
)

_NAMED_KINDS = frozenset({"libro", "seccion", "titulo", "capitulo", "subcapitulo"})

_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")
_NUMERAL_RE = re.compile(
    r"^(?:[IVXLCDM]+\b|\d|primer|segund|tercer|cuart|quint|sext|s[eé]p?tim|octav|noven|"
    r"d[eé]cim|und[eé]cim|duod[eé]cim|preliminar|final|[uú]nic)",
    re.IGNORECASE,
)
_INCISO_RE = re.compile(r"\binciso\s+(\d+)", re.IGNORECASE)
_SNIPPET_ARTICULO_RE = re.compile(r"\bart[ií]culo\s+(\d+[\-–]?[a-z]?)", re.IGNORECASE)

_SUMILLA_RE = re.compile(r"^\s{0,3}\*\*_?\s*([^*\n]{3,90}?)\s*_?\*\*\s*$")

_MARKUP_RE = re.compile(r"<!--.*?-->|</?[a-zA-Z][^<>\n]{0,40}>", re.DOTALL)

_ELLIPSIS_RE = re.compile(r"\[\s*(?:\.{3,}|…)\s*\]|\(\s*(?:\.{3,}|…)\s*\)|\.{3,}|…")

_NOT_SKELETON = frozenset("ºª")
_SKELETON_CHARS: dict[str, str] = {}

_WINDOW_SLACK = 200

_KGRAM = 12
_MAX_KGRAM_OCCURRENCES = 30
_MAX_CANDIDATES = 40
_MIN_ALIGNED_CHARS = 30
_EDGE_BLOCK = 8
_MIN_SEGMENT = 12
_ELLIPSIS_GAP = 6000


@dataclass(frozen=True)
class Heading:
    offset: int
    level: int
    kind: str
    label: str
    page: int | None = None
    anchor: int = -1


@dataclass
class DocumentIndex:
    base: str
    collapsed: str
    folded: str
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
    start: int
    end: int
    core_start: int
    core_end: int
    strategy: str


def fold(text: str) -> str:
    out = []
    for char in text:
        lowered = char.lower()
        out.append(lowered if len(lowered) == 1 else char)
    return "".join(out)


def collapse_with_map(base: str) -> tuple[str, array]:
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False

    for index, char in enumerate(base):
        if _CONTROL_CHARS.match(char):
            continue
        if char.isspace():
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
    cleaned = label.upper().replace("ARTS.", "").replace("ART.", "")
    return re.sub(r"[\s\-–—.°º]+", "", cleaned)


def _pretty_tail(raw: str) -> str:
    words = [word.strip("*_#") for word in raw.split()]
    words = [word for word in words if word]
    if not words:
        return ""

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
    for offset in (1, 2):
        index = position + offset
        if index >= len(lines):
            break
        candidate = lines[index].strip().strip("*_#").strip()
        if not candidate:
            continue
        if len(candidate) > 80 or candidate.endswith((".", ";", ",")):
            break
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
    return not re.search(r"\b[IVXLCDM]+\b|\d", candidate)


def _classify_line(line: str, offset: int) -> Heading | None:
    if not line.strip():
        return None

    page_match = _PAGINA_RE.match(line)
    if page_match:
        number = int(page_match.group(1))
        return Heading(offset=offset, level=LEVEL_MARKDOWN, kind="pagina", label=f"Pagina {number}", page=number)

    articulo_match = _ARTICULO_RE.match(line)
    if articulo_match:
        return Heading(
            offset=offset,
            level=LEVEL_ARTICULO,
            kind="articulo",
            label=_build_label("articulo", LEVEL_ARTICULO, articulo_match.group(1)),
        )

    if line.rstrip(" *_").endswith((".", ",", ";")):
        return _markdown_heading(line, offset)

    for level, kind, pattern in _WHOLE_LINE_PATTERNS:
        match = pattern.match(line)
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


def find_offset(index: DocumentIndex, snippet: str) -> tuple[int | None, str]:
    query = clean_user_text(snippet)
    if not query or not index.folded:
        return None, "none"

    position, strategy = find_in_folded(index.folded, query)
    if position is None:
        return None, "none"
    return index.offset_map[position], strategy


def find_in_folded(haystack: str, query: str) -> tuple[int | None, str]:
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


def find_spans(haystack: str, query: str, lo: int = 0, hi: int | None = None) -> list[Span]:
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
    covered = bytearray(len(query))
    for block in blocks:
        covered[block.b : block.b + block.size] = b"\x01" * block.size
    return all(covered[position] or not char.isdigit() for position, char in enumerate(query))


def passage_segments(text: str) -> list[str]:
    parts = [skeleton_with_map(part)[0] for part in _ELLIPSIS_RE.split(clean_user_text(text))]
    parts = [part for part in parts if part]
    if len(parts) > 1:
        parts = [part for part in parts if len(part) >= _MIN_SEGMENT]
    return parts


def locate_segments(haystack: str, segments: list[str], lo: int = 0, hi: int | None = None) -> list[Span]:
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
    spans = locate_segments(index.skeleton, passage_segments(text))
    readings, _ = readings_of(index, spans)
    return [[heading.label for heading in reading.articles] for reading in readings]


def build_locator(index: DocumentIndex, offset: int, source: str) -> Locator:
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
        for level in list(nearest):
            if level > heading.level:
                del nearest[level]

    articulo = nearest.get(LEVEL_ARTICULO)
    if articulo is not None and offset - articulo.offset > settings.locator_max_article_span:
        del nearest[LEVEL_ARTICULO]

    chain = [nearest[level].label for level in sorted(nearest)]

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
    text = clean_user_text(snippet)
    if not text:
        return EMPTY_LOCATOR

    numbers = [found.group(1) for found in _SNIPPET_ARTICULO_RE.finditer(text)]
    if not numbers:
        return EMPTY_LOCATOR

    if len({number.upper() for number in numbers}) > 1:
        return EMPTY_LOCATOR

    label = f"Art. {numbers[0]}"

    incisos = {found.group(1) for found in _INCISO_RE.finditer(text)}
    if len(incisos) == 1:
        label = f"{label}, inc. {incisos.pop()}"

    return Locator(label=label, breadcrumb=label, page=None, source="snippet_regex")


def headings_in_span(index: DocumentIndex, start: int, end: int) -> list[Heading]:
    return skeleton_articles(
        index, bisect_left(index.skeleton_map, start), bisect_left(index.skeleton_map, end)
    )


def articles_in_span(index: DocumentIndex, start: int, end: int) -> list[str]:
    return [heading.label for heading in headings_in_span(index, start, end)]


def _parent_chain(breadcrumb: str, label: str) -> str:
    if breadcrumb == label:
        return ""
    suffix = f" > {label}"
    return breadcrumb[: -len(suffix)] if breadcrumb.endswith(suffix) else breadcrumb


def _combined_label(labels: list[str]) -> str:
    numbers = [label[len("Art. ") :] if label.startswith("Art. ") else label for label in labels]
    if len(numbers) == 1:
        return f"Art. {numbers[0]}"
    return "Arts. " + ", ".join(numbers[:-1]) + f" y {numbers[-1]}"


def combine_locator(index: DocumentIndex, headings: list[Heading], source: str) -> Locator:
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
    seen: dict[str, Heading] = {}
    for reading in readings:
        for heading in reading.articles:
            seen.setdefault(article_key(heading.label), heading)
    return [heading.label for heading in sorted(seen.values(), key=lambda heading: heading.anchor)]


def _locator_for(index: DocumentIndex, reading: Reading) -> Locator:
    if reading.articles:
        return combine_locator(index, reading.articles, reading.span.strategy)
    return build_locator(index, _base_offset(index, reading.span.start), reading.span.strategy)


def resolve_chunk(index: DocumentIndex | None, snippet: str) -> tuple[Locator, list[str]]:
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
    return resolve_excerpt_span(index, chunk, excerpt)[0]


def resolve_excerpt_span(
    index: DocumentIndex | None, chunk: str, excerpt: str
) -> tuple[Locator, list[str]]:
    chunk_text = clean_user_text(chunk)
    excerpt_text = clean_user_text(excerpt)
    if not chunk_text or not excerpt_text:
        return EMPTY_LOCATOR, []

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
    return resolve_chunk(index, snippet)[0]
