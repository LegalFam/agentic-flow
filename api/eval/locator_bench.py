import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from app import corpus, locator
from app.config import settings

_TAG_RE = re.compile(r"<!--.*?-->|</?[a-zA-Z][^<>\n]{0,40}>", re.DOTALL)
_LINE_MARKUP_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)

MODES = ("clean", "trimmed", "ellipsis", "punctuation", "typos")
NEGATIVES = ("outside_chunk", "shuffled")


def render(markdown: str) -> str:
    text = _TAG_RE.sub(" ", markdown)
    text = _LINE_MARKUP_RE.sub("", text)
    text = text.replace("*", "").replace("_", " ").replace("|", " ")
    return re.sub(r"\s+", " ", text).strip()


def _snap(base: str, position: int) -> int:
    while position < len(base) and not base[position].isspace():
        position += 1
    return position


def truth_articles(index: locator.DocumentIndex, start: int, end: int) -> set[str]:
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
        covered.add(locator.article_key(heading.label))
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

    for path, index in articulated_documents(minimum_articles):
        base = index.base
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
            truth = truth_articles(index, excerpt_start, excerpt_end)

            for mode in MODES:
                altered = perturb(excerpt, mode, rng)
                found, _ = locator.resolve_excerpt_span(index, chunk, altered)
                verdict = classify(found, truth)
                outcomes[mode][verdict] += 1
                if verdict in ("wrong", "partial"):
                    failures.append({
                        "document": path.name, "mode": mode, "verdict": verdict,
                        "declared": found.label, "truth": sorted(truth),
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

    return {"outcomes": {mode: dict(counter) for mode, counter in outcomes.items()}, "failures": failures}


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
    if args.failures:
        args.failures.write_text(json.dumps(result["failures"], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"fallos: {len(result['failures'])} -> {args.failures}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
