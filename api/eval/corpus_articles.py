import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from app import corpus, locator
from eval.norms import NORMS, Norm, normalize_article


def deaccent(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


@dataclass
class NormIndex:
    norm: Norm
    path: Path
    index: locator.DocumentIndex
    articles: dict[str, tuple[int, int]]

    def has(self, article: str) -> bool:
        return normalize_article(article) in self.articles

    def text_of(self, article: str) -> str:
        span = self.articles.get(normalize_article(article))
        if span is None:
            return ""
        return self.index.base[span[0] : span[1]]


def _article_spans(index: locator.DocumentIndex) -> dict[str, tuple[int, int]]:
    boundaries = sorted(
        heading.offset
        for heading in index.headings
        if heading.kind == "articulo" or heading.level <= locator.LEVEL_ARTICULO
    )
    spans: dict[str, tuple[int, int]] = {}
    for heading in index.headings:
        if heading.kind != "articulo":
            continue
        key = normalize_article(heading.label)
        if key in spans:
            continue
        end = next((offset for offset in boundaries if offset > heading.offset), len(index.base))
        spans[key] = (heading.offset, end)
    return spans


@lru_cache(maxsize=1)
def build_registry() -> dict[str, NormIndex]:
    files = corpus.iter_corpus_files()
    registry: dict[str, NormIndex] = {}

    for norm in NORMS:
        matches = [path for path in files if norm.stem_contains in path.stem.lower()]
        if not matches:
            continue
        path = max(matches, key=lambda candidate: candidate.stat().st_size)
        index = corpus.load_index(path)
        if index is None:
            continue
        registry[norm.key] = NormIndex(
            norm=norm, path=path, index=index, articles=_article_spans(index)
        )

    return registry


_MENTION_RE = re.compile(
    r"\bart(?:iculos?|s?\.|\b)\s*"
    r"((?:\d+(?:\s?[-–]\s?[a-z])?[°º]?)"
    r"(?:\s*(?:,|;|\sy\s|\se\s)\s*\d+(?:\s?[-–]\s?[a-z])?[°º]?)*)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"\d+(?:\s?[-–]\s?[a-z])?[°º]?", re.IGNORECASE)

_LOOKAHEAD = 90
_LOOKBEHIND = 320


@dataclass(frozen=True)
class Mention:
    article: str
    norm_key: str | None
    raw: str
    position: int
    attribution: str = "none"


_ADJACENT_BEFORE_GAP_RE = re.compile(r"[\s(\[,:;]*")
_ATTACHED_AFTER_GAP_RE = re.compile(r"[\s)\],]*(?:del|de la|de los|de)\s+(?:(?:el|la|los)\s+)?")


def _find_alias(window: str, direction: str) -> tuple[int, str, int] | None:
    best: tuple[int, str, int] | None = None
    for norm in NORMS:
        for alias in norm.aliases:
            position = window.find(alias) if direction == "after" else window.rfind(alias)
            if position < 0:
                continue
            if direction == "after":
                distance, gap = position, position
            else:
                gap = position + len(alias)
                distance = len(window) - gap
            if best is None or distance < best[0]:
                best = (distance, norm.key, gap)
    return best


def detect_mentions(text: str) -> list[Mention]:
    folded = deaccent(text)
    matches = list(_MENTION_RE.finditer(folded))
    mentions: list[Mention] = []

    for position, match in enumerate(matches):
        previous_end = matches[position - 1].end() if position else 0
        next_start = matches[position + 1].start() if position + 1 < len(matches) else len(folded)

        after_window = folded[match.end() : min(next_start, match.end() + _LOOKAHEAD)]
        before_window = folded[max(previous_end, match.start() - _LOOKBEHIND) : match.start()]
        after = _find_alias(after_window, "after") if after_window else None
        before = _find_alias(before_window, "before") if before_window else None

        before_adjacent = before is not None and _ADJACENT_BEFORE_GAP_RE.fullmatch(
            before_window[before[2] :]
        )
        after_attached = after is not None and _ATTACHED_AFTER_GAP_RE.fullmatch(
            after_window[: after[2]]
        )

        if before_adjacent and not after_attached:
            norm_key, attribution = before[1], "explicit"
        elif after is not None:
            norm_key, attribution = after[1], "explicit"
        elif before is not None:
            norm_key, attribution = before[1], "inferred"
        else:
            norm_key, attribution = None, "none"

        for number in _NUMBER_RE.finditer(match.group(1)):
            mentions.append(
                Mention(
                    article=normalize_article(number.group(0)),
                    norm_key=norm_key,
                    raw=number.group(0).strip(),
                    position=match.start(),
                    attribution=attribution,
                )
            )

    return mentions


def norm_of_document(file_name: str, file_url: str = "") -> str | None:
    haystack = deaccent(f"{file_name} {file_url}")
    for norm in NORMS:
        if norm.stem_contains.replace("-", " ") in haystack.replace("-", " "):
            return norm.key
        if any(alias in haystack for alias in norm.aliases):
            return norm.key
    return None


_CASE_RE = re.compile(r"\b(\d{1,6})\s*(?:[-–]\s*|\s+)((?:19|20)\d{2})\b")


def case_numbers(text: str) -> set[str]:
    found = set()
    for number, year in _CASE_RE.findall(text or ""):
        found.add(f"{number.lstrip('0') or '0'}-{year}")
    return found


@lru_cache(maxsize=1)
def _case_index() -> dict[str, Path]:
    index: dict[str, Path] = {}
    for path in corpus.iter_corpus_files():
        for case in case_numbers(path.stem):
            index.setdefault(case, path)
    return index


def find_corpus_path(file_name: str, file_url: str = "") -> Path | None:
    norm_key = norm_of_document(file_name, file_url)
    if norm_key:
        entry = build_registry().get(norm_key)
        if entry is not None:
            return entry.path

    by_stem = _stem_index().get(_stem_key(Path(urlparse(file_url).path).stem)) if file_url else None
    if by_stem is not None:
        return by_stem

    searchable = f"{file_name} {unquote(file_url)}".replace("_", " ")
    index = _case_index()
    for number, year in _CASE_RE.findall(searchable):
        case = f"{number.lstrip('0') or '0'}-{year}"
        if case in index:
            return index[case]
    return None


_DOCUMENT_HASH_RE = re.compile(r"-[0-9a-f]{12}$")


def _stem_key(stem: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", unquote(stem).lower())).strip("-")


@lru_cache(maxsize=1)
def _stem_index() -> dict[str, Path]:
    return {
        _stem_key(_DOCUMENT_HASH_RE.sub("", path.stem)): path for path in corpus.iter_corpus_files()
    }


def articles_in_locator(label: str) -> list[str]:
    if not label:
        return []
    return [normalize_article(number) for number in _NUMBER_RE.findall(label)]


def classify_mention(mention: Mention, registry: dict[str, NormIndex]) -> str:
    if mention.norm_key is None:
        return "unattributed"
    entry = registry.get(mention.norm_key)
    if entry is None or not entry.norm.articulated:
        return "unknown_norm"
    return "exists" if entry.has(mention.article) else "hallucinated"


def verify_dataset(path: Path) -> int:
    registry = build_registry()
    problems: list[str] = []
    items = 0
    articles = 0

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"linea {line_number}: JSON invalido ({exc})")
            continue
        items += 1

        for expected in item.get("expected_articles", []):
            articles += 1
            norm_key = expected.get("norm")
            article = expected.get("article", "")
            entry = registry.get(norm_key)
            if entry is None:
                problems.append(f"{item.get('id')}: norma ausente del corpus: {norm_key}")
            elif not entry.has(article):
                problems.append(f"{item.get('id')}: {norm_key} no tiene articulo {article}")

        for norm_key in item.get("expected_norms", []):
            if norm_key not in registry:
                problems.append(
                    f"{item.get('id')}: expected_norms cita {norm_key}, que no esta en el corpus"
                )

    print(f"dataset   : {path}")
    print(f"preguntas : {items}")
    print(f"articulos : {articles}")

    if problems:
        print(f"\nProblemas ({len(problems)}):")
        for problem in problems:
            print(f"  {problem}")
        print("\n-> corrige el ground truth: un articulo que no existe en el corpus mide")
        print("   el error del dataset y no el del sistema.")
        return 2

    print("\nTodo el ground truth existe en el corpus.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Registro de articulos del corpus")
    parser.add_argument("--list", metavar="NORMA", help="Lista los articulos de una norma")
    parser.add_argument("--verify-dataset", metavar="RUTA", type=Path)
    parser.add_argument("--detect", metavar="TEXTO", help="Prueba el detector de menciones")
    args = parser.parse_args(argv)

    if args.verify_dataset:
        return verify_dataset(args.verify_dataset)

    registry = build_registry()

    if args.detect:
        for mention in detect_mentions(args.detect):
            verdict = classify_mention(mention, registry)
            print(f"  Art. {mention.article:<8} {str(mention.norm_key):<28} {mention.attribution:<10} {verdict}")
        return 0

    if args.list:
        entry = registry.get(args.list)
        if entry is None:
            print(f"norma desconocida: {args.list}")
            print(f"disponibles: {', '.join(sorted(registry))}")
            return 2
        print(f"{entry.norm.display}  ({entry.path.name})")
        print(f"articulos: {len(entry.articles)}")
        print("  " + ", ".join(sorted(entry.articles, key=lambda value: (len(value), value))))
        return 0

    print(f"corpus : {corpus.corpus_path()}")
    print(f"normas reconocidas: {len(registry)} de {len(NORMS)}\n")
    for norm in NORMS:
        entry = registry.get(norm.key)
        if entry is None:
            print(f"  {norm.key:<28} AUSENTE DEL CORPUS  (stem ~ {norm.stem_contains})")
            continue
        kind = "articulos" if norm.articulated else "sin articulado"
        count = len(entry.articles) if norm.articulated else ""
        print(f"  {norm.key:<28} {str(count):>5} {kind:<14} {entry.path.name}")

    missing = [norm.key for norm in NORMS if norm.key not in registry]
    return 2 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
