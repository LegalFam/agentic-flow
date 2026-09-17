import argparse
import json
import random
import re
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from pathlib import Path

from app import corpus, locator
from app.config import settings

_TAG_RE = re.compile(r"<!--.*?-->|</?[a-zA-Z][^<>\n]{0,40}>", re.DOTALL)
_LINE_MARKUP_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)

_HEADING_NUMBER_RE = re.compile(
    r"^[\s*_#>\-–—•\"'“”‘’]{0,12}art[ií]culo\s+(\d+)\s*[°º]?\s*"
    r"(?:[-–]\s*([a-z])(?![a-záéíóúüñ])|([a-z])(?![a-záéíóúüñ])(?=\s*(?:[.\-–:(]|$)))?",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING_WINDOW = 400

MODES = ("clean", "trimmed", "ellipsis", "punctuation", "typos")
NEGATIVES = ("outside_chunk", "shuffled")


def render(markdown: str) -> str:
    text = _TAG_RE.sub(" ", markdown)
    text = _LINE_MARKUP_RE.sub("", text)
    text = text.replace("*", "").replace("_", " ").replace("|", " ")
    return re.sub(r"\s+", " ", text).strip()


def _snap(base: str, position: int) -> int:
    for tag in _TAG_RE.finditer(base, max(0, position - 60), position + 60):
        if tag.start() < position < tag.end():
            position = tag.end()
    while position < len(base) and not base[position].isspace():
        position += 1
    return position


def read_heading_number(index: locator.DocumentIndex, heading: locator.Heading) -> str | None:
    match = _HEADING_NUMBER_RE.search(index.base, heading.offset, heading.offset + _HEADING_WINDOW)
    if match is None:
        return None
    return match.group(1) + (match.group(2) or match.group(3) or "").upper()


def heading_numbers(index: locator.DocumentIndex) -> tuple[dict[int, str], list[dict]]:
    numbers: dict[int, str] = {}
    disagreements: list[dict] = []
    for heading in index.articles:
        read = read_heading_number(index, heading)
        numbers[heading.offset] = read or ""
        if read != locator.article_key(heading.label):
            line_end = index.base.find("\n", heading.offset)
            disagreements.append({
                "offset": heading.offset,
                "label": heading.label,
                "read": read,
                "line": index.base[heading.offset : line_end if line_end >= 0 else None][:120],
            })
    return numbers, disagreements


def truth_articles(
    index: locator.DocumentIndex, start: int, end: int, numbers: dict[int, str]
) -> set[str]:
    base = _TAG_RE.sub(lambda match: " " * len(match.group(0)), index.base)

    def real(char: str) -> bool:
        return char.isalnum() and char not in "ºª"

    first = next((i for i in range(start, end) if real(base[i])), None)
    if first is None:
        return set()
    last = next(i for i in range(end - 1, start - 1, -1) if real(base[i]))

    owners: list[tuple[int, locator.Heading | None]] = []
    for heading in index.headings:
        if heading.level > locator.LEVEL_ARTICULO:
            continue
        position = heading.offset
        while position < len(base) and not real(base[position]):
            position += 1
        owners.append((position, heading if heading.kind == "articulo" else None))
    owners.sort(key=lambda item: item[0])

    covered: set[str] = set()
    for position, (real, heading) in enumerate(owners):
        region_end = owners[position + 1][0] if position + 1 < len(owners) else len(base)
        if heading is None or region_end <= first or real > last:
            continue
        if real <= first and first - heading.offset > settings.locator_max_article_span:
            continue
        covered.add(numbers.get(heading.offset) or f"?{heading.offset}")
    return covered


def visible_truth(
    index: locator.DocumentIndex, start: int, end: int, text: str, numbers: dict[int, str]
) -> set[str]:
    pieces = text.split(" ... ")
    if len(pieces) == 1:
        return truth_articles(index, start, end, numbers)

    covered: set[str] = set()
    position = bisect_left(index.skeleton_map, start)
    limit = bisect_left(index.skeleton_map, end)
    for piece in pieces:
        skeleton = locator.skeletonize(piece)
        found = index.skeleton.find(skeleton, position, limit)
        if not skeleton or found < 0:
            return truth_articles(index, start, end, numbers)
        position = found + len(skeleton)
        covered |= truth_articles(
            index, index.skeleton_map[found], index.skeleton_map[position - 1] + 1, numbers
        )
    return covered


def perturb(text: str, mode: str, rng: random.Random) -> str:
    words = text.split(" ")
    if mode == "trimmed" and len(words) > 6:
        head = words[0][rng.randint(1, max(1, len(words[0]) - 1)) :]
        tail = words[-1][: rng.randint(1, max(1, len(words[-1]) - 1))]
        head = head if any(char.isalnum() for char in head) else words[0]
        tail = tail if any(char.isalnum() for char in tail) else words[-1]
        return " ".join([head, *words[1:-1], tail]).strip()
    if mode == "ellipsis" and len(words) > 24:
        cut = rng.randint(6, len(words) - 12)
        size = rng.randint(3, min(40, len(words) - cut - 6))
        return " ".join(words[:cut]) + " ... " + " ".join(words[cut + size :])
    if mode == "punctuation":
        text = re.sub(r"[\"'“”‘’°º]", "", text)
        text = text.replace(".-", ". ").replace(";", ",")
        return re.sub(r"\s+", " ", text).strip()
    if mode == "typos":
        chars = list(text)
        letters = [i for i, char in enumerate(chars) if char.isalpha()]
        for i in rng.sample(letters, k=min(len(letters), max(1, len(chars) // 120))):
            chars[i] = "x" if chars[i] != "x" else "z"
        return "".join(chars)
    return text


def classify(found: locator.Locator, truth: set[str]) -> str:
    declared = {locator.article_key(n) for n in re.findall(r"\d+(?:\s?-?\s?[A-Z]\b)?", found.label)}
    if not found.label.startswith("Art"):
        declared = set()
    if not declared:
        return "abstained" if truth or found.is_empty() else "correct_no_article"
    if declared == truth:
        return "correct"
    if declared & truth:
        return "partial"
    return "wrong"


def articulated_documents(minimum: int) -> list[tuple[Path, locator.DocumentIndex]]:
    documents = []
    for path in corpus.iter_corpus_files():
        index = corpus.load_index(path)
        if index is not None and len(index.articles) >= minimum:
            documents.append((path, index))
    return documents


def run(per_document: int, seed: int, minimum_articles: int) -> dict:
    rng = random.Random(seed)
    outcomes: dict[str, Counter] = defaultdict(Counter)
    failures: list[dict] = []
    headings = Counter()
    disagreements: list[dict] = []

    for path, index in articulated_documents(minimum_articles):
        base = index.base
        numbers, disagreeing = heading_numbers(index)
        headings["total"] += len(index.articles)
        headings["disagree"] += len(disagreeing)
        disagreements.extend({"document": path.name, **item} for item in disagreeing)
        for _ in range(per_document):
            chunk_start = _snap(base, rng.randrange(0, max(1, len(base) - 3000)))
            chunk_end = _snap(base, min(len(base), chunk_start + rng.randint(900, 2600)))
            chunk_md = base[chunk_start:chunk_end]
            chunk = render(chunk_md) if rng.random() < 0.5 else re.sub(r"\s+", " ", chunk_md).strip()
            if len(render(chunk_md)) < 300:
                continue

            excerpt_start = _snap(base, rng.randrange(chunk_start, max(chunk_start + 1, chunk_end - 150)))
            excerpt_end = _snap(base, min(chunk_end, excerpt_start + rng.randint(60, 700)))
            excerpt_md = base[excerpt_start:excerpt_end]
            excerpt = render(excerpt_md)
            if len(excerpt) < 40:
                continue
            truth = truth_articles(index, excerpt_start, excerpt_end, numbers)

            for mode in MODES:
                altered = perturb(excerpt, mode, rng)
                found, _ = locator.resolve_excerpt_span(index, chunk, altered)
                verdict = classify(found, visible_truth(index, excerpt_start, excerpt_end, altered, numbers))
                outcomes[mode][verdict] += 1
                if verdict in ("wrong", "partial"):
                    failures.append({
                        "document": path.name, "mode": mode, "verdict": verdict,
                        "declared": found.label,
                        "truth": sorted(visible_truth(index, excerpt_start, excerpt_end, altered, numbers)),
                        "excerpt": altered[:300], "base_offset": excerpt_start,
                    })

            other = _snap(base, rng.randrange(0, max(1, len(base) - 800)))
            if other + 600 < chunk_start or other > chunk_end:
                outside = render(base[other : _snap(base, other + rng.randint(80, 400))])
                if len(outside) >= 40 and locator.skeletonize(outside) not in locator.skeletonize(chunk):
                    found, _ = locator.resolve_excerpt_span(index, chunk, outside)
                    outcomes["outside_chunk"]["abstained" if found.is_empty() else "false_accept"] += 1
                    if not found.is_empty():
                        failures.append({
                            "document": path.name, "mode": "outside_chunk", "verdict": "false_accept",
                            "declared": found.label, "excerpt": outside[:300], "chunk": chunk[:600],
                        })
            words = excerpt.split(" ")
            if len(words) >= 12:
                shuffled = words[:]
                rng.shuffle(shuffled)
                found, _ = locator.resolve_excerpt_span(index, chunk, " ".join(shuffled))
                outcomes["shuffled"]["abstained" if found.is_empty() else "false_accept"] += 1
                if not found.is_empty():
                    failures.append({
                        "document": path.name, "mode": "shuffled", "verdict": "false_accept",
                        "declared": found.label, "excerpt": " ".join(shuffled)[:300],
                    })

    return {
        "outcomes": {mode: dict(counter) for mode, counter in outcomes.items()},
        "failures": failures,
        "headings": dict(headings),
        "disagreements": disagreements,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Banco del localizador con verdad construida")
    parser.add_argument("--per-document", type=int, default=300)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--minimum-articles", type=int, default=20)
    parser.add_argument("--failures", type=Path, help="Escribe aqui los casos wrong/partial")
    args = parser.parse_args(argv)

    result = run(args.per_document, args.seed, args.minimum_articles)
    print(f"{'modo':<14} {'n':>6} {'correct':>8} {'partial':>8} {'wrong':>6} {'abstain':>8} {'false_acc':>9}")
    for mode, counter in result["outcomes"].items():
        total = sum(counter.values())
        print(
            f"{mode:<14} {total:>6} {counter.get('correct', 0) + counter.get('correct_no_article', 0):>8} "
            f"{counter.get('partial', 0):>8} {counter.get('wrong', 0):>6} "
            f"{counter.get('abstained', 0):>8} {counter.get('false_accept', 0):>9}"
        )
    headings = result["headings"]
    print(
        f"\nencabezados de articulo: {headings.get('total', 0)}, "
        f"etiqueta distinta a la linea original: {headings.get('disagree', 0)}"
    )
    for item in result["disagreements"]:
        print(f"  {item['document'][:40]:<40} {item['label']!r:>14} vs {item['read']!r:>8}  {item['line']!r}")
    if args.failures:
        args.failures.write_text(json.dumps(result["failures"], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"fallos: {len(result['failures'])} -> {args.failures}")
    return 1 if result["disagreements"] else 0


if __name__ == "__main__":
    sys.exit(main())
