"""Metricas deterministas sobre las respuestas guardadas por `run_ablation`.

    python -m eval.score --run eval/runs/<timestamp>

Todo se verifica contra el corpus (`work/corpus/`), no contra lo que el sistema dice de
si mismo: el localizador de cada cita se recalcula desde cero sobre el markdown original,
y los articulos citados se contrastan con el articulado real. Una metrica que se creyera
la salida del sistema no mediria nada.

Escribe `per_question.jsonl` (una fila por pregunta y brazo, para inspeccion manual) y
`results.json` (agregados por brazo, que es lo que consume `report.py`).
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from app import corpus, locator
from eval import corpus_articles
from eval.corpus_articles import (
    articles_in_locator,
    build_registry,
    classify_mention,
    deaccent,
    detect_mentions,
    norm_of_document,
)
from eval.norms import normalize_article

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "dataset" / "family_law_v1.jsonl"


# --------------------------------------------------------------------------------------
# Eje RAG: que articulos le llegan al usuario, y si existen


def surfaced_articles(message: str, citations: list[dict], registry: dict) -> dict:
    """Articulos que el usuario acaba viendo, por los dos canales posibles.

    Se separan los canales a proposito. El prompt del XAI Agent prohibe nombrar fuentes
    dentro de `answer` porque la interfaz las muestra aparte, asi que contar solo el texto
    castigaria al brazo completo por cumplir su propio contrato; y contar solo las citas
    dejaria en cero a los brazos que no tienen capa de citas aunque nombren la norma
    correcta en la respuesta. La union es lo que el usuario ve; el desglose es lo que
    permite decir de donde vino.
    """
    from_text: set[tuple[str, str]] = set()
    for mention in detect_mentions(message):
        if mention.norm_key:
            from_text.add((mention.norm_key, mention.article))

    from_citations: set[tuple[str, str]] = set()
    for citation in citations:
        norm_key = norm_of_document(citation.get("file_name", ""), citation.get("file_url", ""))
        if not norm_key:
            continue
        for article in articles_in_locator(citation.get("locator", "")):
            from_citations.add((norm_key, article))

    return {
        "from_text": from_text,
        "from_citations": from_citations,
        "all": from_text | from_citations,
    }


def hallucination_counts(message: str, registry: dict) -> dict:
    """Articulos nombrados en el texto que no existen en el corpus.

    Se reportan por separado los de atribucion explicita ("art. 1481 del Codigo Civil")
    y los inferidos de la frase anterior. Solo los explicitos permiten afirmar sin
    discusion que el numero esta inventado; contar los dos juntos inflaria la metrica
    justo en el brazo que mas interesa que salga mal.
    """
    counts = Counter()
    for mention in detect_mentions(message):
        verdict = classify_mention(mention, registry)
        counts[f"{verdict}_{mention.attribution}"] += 1
        counts[verdict] += 1
        counts["total"] += 1
    return dict(counts)


# --------------------------------------------------------------------------------------
# Eje XAI: si la cita se sostiene contra el documento


def _index_for(citation: dict, registry: dict):
    """El markdown original del documento que la cita dice haber usado."""
    index = corpus.get_index(citation.get("file_name"), citation.get("file_id"))
    if index is not None:
        return index
    norm_key = norm_of_document(citation.get("file_name", ""), citation.get("file_url", ""))
    entry = registry.get(norm_key) if norm_key else None
    return entry.index if entry else None


def audit_citation(citation: dict, registry: dict) -> dict:
    """Verifica una cita contra el documento, sin creerle nada al sistema.

    Devuelve si el pasaje es literal, si trae ubicacion, y si esa ubicacion es la correcta.
    La correccion se decide recalculando el articulo que contiene el pasaje sobre el
    markdown: comparar el locator entregado contra si mismo no probaria nada.
    """
    snippet = citation.get("original_snippet") or ""
    reported = citation.get("locator") or ""
    audit = {
        "has_locator": bool(reported),
        "locator_source": citation.get("locator_source") or "",
        "locator_scope": citation.get("locator_scope") or "",
        "verbatim": False,
        "locator_verdict": "unverifiable",
        "document_resolved": False,
    }

    index = _index_for(citation, registry)
    if index is None or not snippet:
        return audit

    audit["document_resolved"] = True
    offset, strategy = locator.find_offset(index, snippet)
    # Solo `exact` y `prefix` prueban que el pasaje esta copiado tal cual. `fuzzy` acepta
    # un parecido alto, que es util para ubicar pero no para afirmar literalidad.
    audit["verbatim"] = offset is not None and strategy in ("exact", "prefix")
    audit["match_strategy"] = strategy

    if offset is None:
        return audit

    recomputed = locator.build_locator(index, offset, strategy)
    expected = set(articles_in_locator(recomputed.label))
    declared = set(articles_in_locator(reported))

    if not declared:
        audit["locator_verdict"] = "missing"
    elif not expected:
        audit["locator_verdict"] = "unverifiable"
    elif declared == expected:
        audit["locator_verdict"] = "correct"
    elif declared & expected:
        audit["locator_verdict"] = "partial"
    else:
        audit["locator_verdict"] = "wrong"

    audit["recomputed_locator"] = recomputed.label
    return audit


# --------------------------------------------------------------------------------------
# Puntuacion por pregunta


def score_record(record: dict, item: dict, registry: dict) -> dict:
    response = record.get("response") or {}
    message = str(response.get("message") or "")
    citations = response.get("citations") or []

    expected = {(entry["norm"], normalize_article(entry["article"])) for entry in item.get("expected_articles", [])}
    surfaced = surfaced_articles(message, citations, registry)
    hit = expected & surfaced["all"]

    folded = deaccent(message)
    must = [term for term in item.get("must_mention", [])]
    covered = [term for term in must if deaccent(term) in folded]
    violations = [term for term in item.get("must_not_mention", []) if deaccent(term) in folded]

    audits = [audit_citation(citation, registry) for citation in citations]
    hallucination = hallucination_counts(message, registry)

    specialist = bool(response.get("specialistSupportRecommended"))
    expects_specialist = bool(item.get("expects_specialist_support"))

    return {
        "id": item["id"],
        "arm": record["arm"],
        "repeat": record.get("repeat", 0),
        "category": item.get("category"),
        "ok": bool(record.get("ok")),
        "latency_ms": record.get("latency_ms"),
        "answer_chars": len(message),
        "agent_token_cost": response.get("agentTokenCost"),
        # --- eje RAG
        "expected_articles": len(expected),
        "articles_hit": len(hit),
        "article_recall": (len(hit) / len(expected)) if expected else None,
        "articles_surfaced": len(surfaced["all"]),
        "articles_from_text": len(surfaced["from_text"]),
        "articles_from_citations": len(surfaced["from_citations"]),
        "article_precision": (len(hit) / len(surfaced["all"])) if surfaced["all"] else None,
        "mentions_total": hallucination.get("total", 0),
        "hallucinated_explicit": hallucination.get("hallucinated_explicit", 0),
        "hallucinated_inferred": hallucination.get("hallucinated_inferred", 0),
        "unattributed_mentions": hallucination.get("unattributed", 0),
        "must_mention_total": len(must),
        "must_mention_covered": len(covered),
        "must_mention_coverage": (len(covered) / len(must)) if must else None,
        "must_not_violations": len(violations),
        # --- eje XAI
        "citations": len(citations),
        "citations_verbatim": sum(1 for audit in audits if audit["verbatim"]),
        "citations_with_locator": sum(1 for audit in audits if audit["has_locator"]),
        "locator_correct": sum(1 for audit in audits if audit["locator_verdict"] == "correct"),
        "locator_partial": sum(1 for audit in audits if audit["locator_verdict"] == "partial"),
        "locator_wrong": sum(1 for audit in audits if audit["locator_verdict"] == "wrong"),
        "locator_unverifiable": sum(1 for audit in audits if audit["locator_verdict"] == "unverifiable"),
        "locator_scopes": Counter(audit["locator_scope"] for audit in audits if audit["locator_scope"]),
        "locator_sources": Counter(audit["locator_source"] for audit in audits if audit["locator_source"]),
        # Una respuesta es trazable si al menos una cita esta copiada literalmente y
        # ubicada correctamente: es lo minimo para que el usuario pueda ir a comprobarla.
        "traceable": any(
            audit["verbatim"] and audit["locator_verdict"] == "correct" for audit in audits
        ),
        "citation_support_status": response.get("citationSupportStatus"),
        "confidence_status": response.get("confidenceStatus"),
        "next_steps": len(response.get("nextSteps") or []),
        "clarifying_questions": len(response.get("clarifyingQuestions") or []),
        # --- seguridad
        "specialist_support": specialist,
        "expects_specialist_support": expects_specialist,
        "specialist_true_positive": specialist and expects_specialist,
        "specialist_false_negative": expects_specialist and not specialist,
        "specialist_false_positive": specialist and not expects_specialist,
        # --- correccion, para la calibracion
        "correct": hallucination.get("hallucinated_explicit", 0) == 0
        and (len(covered) / len(must) >= 0.5 if must else True),
    }


# --------------------------------------------------------------------------------------
# Agregacion


NUMERIC = (
    "article_recall",
    "article_precision",
    "must_mention_coverage",
    "latency_ms",
    "answer_chars",
)
COUNTS = (
    "citations",
    "citations_verbatim",
    "citations_with_locator",
    "locator_correct",
    "locator_partial",
    "locator_wrong",
    "locator_unverifiable",
    "articles_surfaced",
    "articles_from_text",
    "articles_from_citations",
    "mentions_total",
    "hallucinated_explicit",
    "hallucinated_inferred",
    "unattributed_mentions",
    "next_steps",
    "clarifying_questions",
)
RATES = ("traceable", "correct", "specialist_true_positive", "specialist_false_negative", "specialist_false_positive")


def _mean(values: list) -> float | None:
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _percentile(values: list[int], fraction: float) -> int | None:
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return None
    return clean[min(len(clean) - 1, int(len(clean) * fraction))]


def aggregate(rows: list[dict]) -> dict:
    """Agregados de un brazo. Solo las respuestas que llegaron: una llamada caida es un
    fallo operativo y contarla como respuesta vacia mezclaria dos cosas distintas."""
    answered = [row for row in rows if row["ok"]]
    summary: dict = {
        "calls": len(rows),
        "answered": len(answered),
        "failure_rate": 1 - len(answered) / len(rows) if rows else None,
    }

    if not answered:
        return summary

    for field in NUMERIC + COUNTS:
        summary[field] = _mean([row.get(field) for row in answered])
    for field in RATES:
        summary[field] = sum(1 for row in answered if row.get(field)) / len(answered)

    summary["latency_p50"] = _percentile([row["latency_ms"] for row in answered], 0.5)
    summary["latency_p95"] = _percentile([row["latency_ms"] for row in answered], 0.95)

    # Tasas por cita y no por respuesta: una respuesta con seis citas y otra con una no
    # pesan igual en la media por respuesta, y aca interesa la calidad de la cita.
    total_citations = sum(row["citations"] for row in answered)
    summary["total_citations"] = total_citations
    if total_citations:
        summary["verbatim_rate"] = sum(row["citations_verbatim"] for row in answered) / total_citations
        summary["locator_resolution_rate"] = (
            sum(row["citations_with_locator"] for row in answered) / total_citations
        )
        summary["locator_correctness_rate"] = (
            sum(row["locator_correct"] for row in answered) / total_citations
        )
    else:
        summary["verbatim_rate"] = None
        summary["locator_resolution_rate"] = None
        summary["locator_correctness_rate"] = None

    summary["citation_support"] = dict(Counter(row["citation_support_status"] for row in answered))
    summary["confidence"] = dict(Counter(row["confidence_status"] for row in answered))
    summary["locator_scopes"] = dict(sum((Counter(row["locator_scopes"]) for row in answered), Counter()))
    summary["locator_sources"] = dict(sum((Counter(row["locator_sources"]) for row in answered), Counter()))

    # Calibracion: cuanto acierta el sistema en cada nivel de confianza que declara. Una
    # confianza HIGH con correccion baja es peor que no declarar confianza.
    calibration: dict[str, dict] = {}
    for level in ("HIGH", "MEDIUM", "LOW"):
        bucket = [row for row in answered if row["confidence_status"] == level]
        if bucket:
            calibration[level] = {
                "n": len(bucket),
                "correct_rate": sum(1 for row in bucket if row["correct"]) / len(bucket),
            }
    summary["calibration"] = calibration

    expected_specialist = [row for row in answered if row["expects_specialist_support"]]
    flagged = [row for row in answered if row["specialist_support"]]
    summary["specialist_recall"] = (
        sum(1 for row in expected_specialist if row["specialist_support"]) / len(expected_specialist)
        if expected_specialist
        else None
    )
    summary["specialist_precision"] = (
        sum(1 for row in flagged if row["expects_specialist_support"]) / len(flagged) if flagged else None
    )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puntua una corrida de la ablacion")
    parser.add_argument("--run", type=Path, required=True, help="Directorio runs/<timestamp>")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args(argv)

    if not args.run.exists():
        print(f"no existe la corrida: {args.run}")
        return 2

    dataset = {}
    for line in args.dataset.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            item = json.loads(line)
            dataset[item["id"]] = item

    registry = build_registry()
    print(f"corpus : {corpus.corpus_path()} ({len(registry)} normas)")

    rows: list[dict] = []
    for path in sorted(args.run.glob("*.jsonl")):
        if path.name == "per_question.jsonl":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            item = dataset.get(record["id"])
            if item is None:
                print(f"aviso: {record['id']} no esta en el dataset, se omite")
                continue
            rows.append(score_record(record, item, registry))

    if not rows:
        print("la corrida no tiene respuestas puntuables")
        return 2

    # Los Counter no son serializables tal cual y solo sirven para agregar.
    output = args.run / "per_question.jsonl"
    with output.open("w", encoding="utf-8") as handle:
        for row in rows:
            flat = {key: (dict(value) if isinstance(value, Counter) else value) for key, value in row.items()}
            handle.write(json.dumps(flat, ensure_ascii=False) + "\n")

    results = {
        "run": str(args.run),
        "questions": len({row["id"] for row in rows}),
        "arms": {},
    }
    for arm in sorted({row["arm"] for row in rows}):
        results["arms"][arm] = aggregate([row for row in rows if row["arm"] == arm])

    (args.run / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\nfilas puntuadas: {len(rows)}")
    print(f"escrito: {output}")
    print(f"escrito: {args.run / 'results.json'}")
    print("\n-> `python -m eval.report --run <corrida>` para las tablas 2x2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
