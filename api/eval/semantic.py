import argparse
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

from app.config import settings
from eval.corpus_articles import deaccent

MODEL = "gemini-embedding-001"
CACHE = Path(__file__).resolve().parent / "runs" / ".embeddings.jsonl"

THRESHOLD = 0.71

MIN_WORDS = 4

_cache: dict[str, list[float]] | None = None
_pending: dict[str, list[float]] = {}
_client = None


def _key(text: str) -> str:
    return hashlib.blake2s(text.encode("utf-8"), digest_size=12).hexdigest()


def _load_cache() -> dict[str, list[float]]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    if CACHE.exists():
        for line in CACHE.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                _cache[row["k"]] = row["v"]
    return _cache


def _flush() -> None:
    if not _pending:
        return
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("a", encoding="utf-8") as handle:
        for key, vector in _pending.items():
            handle.write(json.dumps({"k": key, "v": vector}) + "\n")
    _pending.clear()


def _embed_uncached(texts: list[str]) -> list[list[float]]:
    global _client
    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY no esta configurado: sin el no se pueden calcular embeddings "
            "nuevos. Los ya cacheados si funcionan."
        )
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=settings.gemini_api_key)

    vectors: list[list[float]] = []
    for start in range(0, len(texts), 32):
        batch = texts[start : start + 32]
        response = _client.models.embed_content(model=MODEL, contents=batch)
        vectors.extend(list(item.values) for item in response.embeddings)
    return vectors


def embed(texts: list[str]) -> list[list[float]]:
    cache = _load_cache()
    missing = [text for text in dict.fromkeys(texts) if _key(text) not in cache]
    if missing:
        for text, vector in zip(missing, _embed_uncached(missing)):
            cache[_key(text)] = vector
            _pending[_key(text)] = vector
        _flush()
    return [cache[_key(text)] for text in texts]


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


_SENTENCE = re.compile(r"(?<=[.!?:;])\s+|\n+")
_WORD = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+")


def sentences(text: str) -> list[str]:
    plain = re.sub(r"[*_#`>\[\]]", " ", text or "")
    out = []
    for piece in _SENTENCE.split(plain):
        piece = piece.strip(" -•\t")
        if len(_WORD.findall(piece)) >= MIN_WORDS:
            out.append(piece)
    return out


def mentions(
    answer: str, term: str, proposition: str | None = None, threshold: float = THRESHOLD
) -> bool:
    if not answer or not term:
        return False
    if deaccent(term) in deaccent(answer):
        return True
    if not proposition:
        return False

    pieces = sentences(answer)
    if not pieces:
        return False

    vectors = embed([proposition] + pieces)
    target, rest = vectors[0], vectors[1:]
    return any(cosine(target, vector) >= threshold for vector in rest)


def best_score(answer: str, term: str) -> float:
    pieces = sentences(answer)
    if not pieces:
        return 0.0
    vectors = embed([term] + pieces)
    return max(cosine(vectors[0], vector) for vector in vectors[1:])




def calibrate(run: Path, dataset: Path, limit: int | None = None) -> int:
    import random

    items = {}
    for line in dataset.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            items[item["id"]] = item

    props_path = dataset.parent / "propositions.json"
    props = json.loads(props_path.read_text(encoding="utf-8")) if props_path.exists() else {}

    positives: list[float] = []
    negatives: list[float] = []
    rng = random.Random(20260909)

    rows = []
    for line in (run / "full.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = json.loads(line)
            if record.get("ok") and record.get("repeat", 0) == 0:
                rows.append(record)
    if limit:
        rows = rows[:limit]

    for record in rows:
        item = items.get(record["id"])
        if not item:
            continue
        answer = (record["response"] or {}).get("message") or ""
        if not sentences(answer):
            continue

        propuestas = props.get(item["id"], {})
        for term in item.get("must_mention", []):
            texto = propuestas.get(term) or term
            if deaccent(term) in deaccent(answer):
                positives.append(best_score(answer, texto))

        otros = [
            other
            for other in items.values()
            if other["category"] != item["category"] and other.get("must_mention")
        ]
        if otros:
            ajeno = rng.choice(otros)
            term = rng.choice(ajeno["must_mention"])
            texto = (props.get(ajeno["id"]) or {}).get(term) or term
            if deaccent(term) not in deaccent(answer):
                negatives.append(best_score(answer, texto))

    if not positives or not negatives:
        print("no hay suficientes pares para calibrar")
        return 2

    positives.sort()
    negatives.sort()

    def cut(threshold: float) -> tuple[float, float]:
        tpr = sum(1 for s in positives if s >= threshold) / len(positives)
        fpr = sum(1 for s in negatives if s >= threshold) / len(negatives)
        return tpr, fpr

    mejor = max(
        (round(t / 100, 2) for t in range(40, 96)),
        key=lambda t: (lambda tpr, fpr: tpr - fpr)(*cut(t)),
    )

    print(f"positivos (termino presente literalmente) : n={len(positives)}")
    print(f"  p05={positives[len(positives)//20]:.3f}  mediana={positives[len(positives)//2]:.3f}")
    print(f"negativos (termino de otra categoria)     : n={len(negatives)}")
    print(f"  mediana={negatives[len(negatives)//2]:.3f}  p95={negatives[int(len(negatives)*0.95)]:.3f}")
    print(f"\numbral que mejor separa: {mejor:.2f}")
    for t in (mejor - 0.05, mejor, mejor + 0.05):
        tpr, fpr = cut(t)
        print(f"  umbral {t:.2f} -> detecta {tpr:.1%} de los presentes, {fpr:.1%} de falsos positivos")
    print(f"\nEl modulo usa THRESHOLD = {THRESHOLD}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Coincidencia semantica de terminos")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--run", type=Path, default=Path("eval/runs/ablation"))
    parser.add_argument(
        "--dataset", type=Path, default=Path("eval/dataset/family_law_v1.jsonl")
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--check", nargs=2, metavar=("RESPUESTA", "TERMINO"))
    args = parser.parse_args(argv)

    if args.check:
        answer, term = args.check
        print(f"  presente: {mentions(answer, term)}   similitud: {best_score(answer, term):.3f}")
        return 0

    if args.calibrate:
        return calibrate(args.run, args.dataset, args.limit)

    cache = _load_cache()
    print(f"cache: {len(cache)} vectores en {CACHE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
