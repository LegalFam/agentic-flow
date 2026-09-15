import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from app import corpus, locator
from app.text_utils import clean_user_text
from eval import corpus_articles, explainability, semantic
from eval.corpus_articles import (
    articles_in_locator,
    build_registry,
    classify_mention,
    deaccent,
    detect_mentions,
    norm_of_document,
)
from eval.norms import BY_KEY, normalize_article

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "dataset" / "family_law_v1.jsonl"
DEFAULT_PROPOSITIONS = EVAL_DIR / "dataset" / "propositions.json"




def surfaced_articles(message: str, citations: list[dict], registry: dict) -> dict:
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
    counts = Counter()
    for mention in detect_mentions(message):
        verdict = classify_mention(mention, registry)
        counts[f"{verdict}_{mention.attribution}"] += 1
        counts[verdict] += 1
        counts["total"] += 1
    return dict(counts)




def _index_for(citation: dict, registry: dict):
    index = corpus.get_index(citation.get("file_name"), citation.get("file_id"))
    if index is not None:
        return index

    path = corpus_articles.find_corpus_path(
        citation.get("file_name", ""), citation.get("file_url", "")
    )
    return corpus.load_index(path) if path is not None else None


def _has_articles(citation: dict) -> bool:
    norm_key = norm_of_document(citation.get("file_name", ""), citation.get("file_url", ""))
    norm = BY_KEY.get(norm_key) if norm_key else None
    return bool(norm and norm.articulated)


def audit_citation(citation: dict, registry: dict) -> dict:
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
        audit["locator_verdict"] = "document_unknown"
        return audit

    audit["document_resolved"] = True
    query = clean_user_text(snippet)
    position, strategy = locator.find_in_folded(index.folded, query)
    audit["verbatim"] = position is not None and strategy in ("exact", "prefix")
    audit["match_strategy"] = strategy

    readings = [
        {normalize_article(label) for label in labels}
        for labels in locator.passage_readings(index, query)
    ]
    if position is None and not readings:
        return audit

    expected = set().union(*readings) if readings else set()
    declared = set(articles_in_locator(reported))

    articulated = _has_articles(citation)

    if not articulated:
        audit["locator_verdict"] = "not_applicable"
    elif not declared:
        audit["locator_verdict"] = "missing"
    elif not expected:
        audit["locator_verdict"] = "unverifiable"
    elif declared in readings:
        audit["locator_verdict"] = "correct"
    elif declared & expected:
        audit["locator_verdict"] = "partial"
    else:
        audit["locator_verdict"] = "wrong"

    audit["recomputed_articles"] = sorted(expected)
    return audit




def cited_article_texts(citations: list[dict], registry: dict) -> list[str]:
    texts: list[str] = []
    for citation in citations:
        norm_key = norm_of_document(citation.get("file_name", ""), citation.get("file_url", ""))
        entry = registry.get(norm_key) if norm_key else None
        if entry is None:
            continue
        for article in articles_in_locator(citation.get("locator", "")):
            text = entry.text_of(article)
            if text:
                texts.append(text)
    return texts


def score_record(record: dict, item: dict, registry: dict) -> dict:
    response = record.get("response") or {}
    message = str(response.get("message") or "")
    citations = response.get("citations") or []

    expected = {(entry["norm"], normalize_article(entry["article"])) for entry in item.get("expected_articles", [])}
    surfaced = surfaced_articles(message, citations, registry)
    hit = expected & surfaced["all"]

    substantive = response.get("agentTokenCost") != 1

    folded = deaccent(message)
    must = [term for term in item.get("must_mention", [])]
    props = item.get("must_mention_propositions") or {}
    covered = [term for term in must if semantic.mentions(message, term, props.get(term))]
    violations = [term for term in item.get("must_not_mention", []) if deaccent(term) in folded]

    audits = [audit_citation(citation, registry) for citation in citations]
    hallucination = hallucination_counts(message, registry)

    specialist = bool(response.get("specialistSupportRecommended"))
    expects_specialist = bool(item.get("expects_specialist_support"))

    support = explainability.support_density(
        message, citations, cited_article_texts(citations, registry)
    )
    steps = explainability.actionability(
        response.get("nextSteps") or [],
        response.get("clarifyingQuestions") or [],
        expects_specialist,
    )
    answer_readability = explainability.readability(message)
    gap = explainability.readability_gap(citations)
    answer_gap = explainability.answer_vs_sources(message, citations)

    return {
        "id": item["id"],
        "arm": record["arm"],
        "repeat": record.get("repeat", 0),
        "category": item.get("category"),
        "ok": bool(record.get("ok")),
        "latency_ms": record.get("latency_ms"),
        "answer_chars": len(message),
        "agent_token_cost": response.get("agentTokenCost"),
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
        "must_mention_coverage": (len(covered) / len(must)) if (must and substantive) else None,
        "must_not_violations": len(violations),
        "citations": len(citations),
        "citations_verbatim": sum(1 for audit in audits if audit["verbatim"]),
        "citations_with_locator": sum(1 for audit in audits if audit["has_locator"]),
        "locator_correct": sum(1 for audit in audits if audit["locator_verdict"] == "correct"),
        "locator_partial": sum(1 for audit in audits if audit["locator_verdict"] == "partial"),
        "locator_wrong": sum(1 for audit in audits if audit["locator_verdict"] == "wrong"),
        "locator_unverifiable": sum(1 for audit in audits if audit["locator_verdict"] == "unverifiable"),
        "locator_not_applicable": sum(
            1 for audit in audits if audit["locator_verdict"] == "not_applicable"
        ),
        "locator_document_unknown": sum(
            1 for audit in audits if audit["locator_verdict"] == "document_unknown"
        ),
        "locator_scopes": Counter(audit["locator_scope"] for audit in audits if audit["locator_scope"]),
        "locator_sources": Counter(audit["locator_source"] for audit in audits if audit["locator_source"]),
        "traceable": any(
            audit["verbatim"] and audit["locator_verdict"] == "correct" for audit in audits
        ),
        "citation_support_status": response.get("citationSupportStatus"),
        "confidence_status": response.get("confidenceStatus"),
        "next_steps": len(response.get("nextSteps") or []),
        "clarifying_questions": len(response.get("clarifyingQuestions") or []),
        "normative_claims": support["claims"],
        "claims_supported": support["supported"],
        "support_density": support["density"],
        "answer_readability": answer_readability["szigriszt"] if answer_readability else None,
        "answer_words_per_sentence": (
            answer_readability["words_per_sentence"] if answer_readability else None
        ),
        "snippet_readability": gap["original"] if gap else None,
        "summary_readability": gap["summary"] if gap else None,
        "readability_gap": gap["gap"] if gap else None,
        "answer_vs_sources_gap": answer_gap,
        "steps_actionable_rate": steps["actionable_rate"],
        "steps_duplicated": steps["duplicated"],
        "steps_generic_referral": steps["generic_referral"],
        "specialist_support": specialist,
        "expects_specialist_support": expects_specialist,
        "specialist_true_positive": specialist and expects_specialist,
        "specialist_false_negative": expects_specialist and not specialist,
        "specialist_false_positive": specialist and not expects_specialist,
        "correct": None
        if not substantive
        else (
            hallucination.get("hallucinated_explicit", 0) == 0
            and (len(covered) / len(must) >= 0.5 if must else True)
        ),
        "substantive": substantive,
    }




NUMERIC = (
    "article_recall",
    "article_precision",
    "must_mention_coverage",
    "latency_ms",
    "answer_chars",
    "support_density",
    "answer_readability",
    "answer_words_per_sentence",
    "snippet_readability",
    "summary_readability",
    "readability_gap",
    "answer_vs_sources_gap",
    "steps_actionable_rate",
)
COUNTS = (
    "normative_claims",
    "claims_supported",
    "steps_duplicated",
    "steps_generic_referral",
    "citations",
    "citations_verbatim",
    "citations_with_locator",
    "locator_correct",
    "locator_partial",
    "locator_wrong",
    "locator_unverifiable",
    "locator_not_applicable",
    "locator_document_unknown",
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
        aplicables = [row for row in answered if row.get(field) is not None]
        summary[field] = (
            sum(1 for row in aplicables if row[field]) / len(aplicables) if aplicables else None
        )
        summary[f"{field}_n"] = len(aplicables)

    summary["clarification_rate"] = 1 - sum(
        1 for row in answered if row.get("substantive")
    ) / len(answered)

    summary["latency_p50"] = _percentile([row["latency_ms"] for row in answered], 0.5)
    summary["latency_p95"] = _percentile([row["latency_ms"] for row in answered], 0.95)

    total_citations = sum(row["citations"] for row in answered)
    summary["total_citations"] = total_citations
    locatable = total_citations - sum(
        row.get("locator_not_applicable", 0) + row.get("locator_document_unknown", 0)
        for row in answered
    )
    summary["locatable_citations"] = locatable
    if total_citations:
        summary["verbatim_rate"] = sum(row["citations_verbatim"] for row in answered) / total_citations
        summary["locator_resolution_rate"] = (
            sum(row["citations_with_locator"] for row in answered) / total_citations
        )
        summary["locator_correctness_rate"] = (
            sum(row["locator_correct"] for row in answered) / locatable if locatable else None
        )
    else:
        summary["verbatim_rate"] = None
        summary["locator_resolution_rate"] = None
        summary["locator_correctness_rate"] = None

    summary["citation_support"] = dict(Counter(row["citation_support_status"] for row in answered))
    summary["confidence"] = dict(Counter(row["confidence_status"] for row in answered))
    summary["locator_scopes"] = dict(sum((Counter(row["locator_scopes"]) for row in answered), Counter()))
    summary["locator_sources"] = dict(sum((Counter(row["locator_sources"]) for row in answered), Counter()))

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


def load_records(run: Path, dataset: dict) -> list[dict]:
    best: dict[tuple[str, str, int], dict] = {}
    order: list[tuple[str, str, int]] = []
    skipped = 0

    for path in sorted(run.glob("*.jsonl")):
        if path.name == "per_question.jsonl":
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record["id"] not in dataset:
                skipped += 1
                continue
            key = (record["arm"], record["id"], record.get("repeat", 0))
            if key not in best:
                order.append(key)
                best[key] = record
            elif record.get("ok") and not best[key].get("ok"):
                best[key] = record

    if skipped:
        print(f"aviso: {skipped} respuestas de preguntas que no estan en el dataset, omitidas")

    retried = sum(1 for key in order if best[key].get("ok"))
    print(f"intentos unicos: {len(order)}  (correctos: {retried})")
    return [best[key] for key in order]


def aggregate_stability(groups: list[list[dict]]) -> dict | None:
    measured = [stability for stability in map(explainability.stability, groups) if stability]
    if not measured:
        return None

    with_citations = [item for item in measured if item["citation_similarity"] is not None]
    return {
        "questions": len(measured),
        "answer_similarity": round(
            sum(item["answer_similarity"] for item in measured) / len(measured), 3
        ),
        "citation_similarity": round(
            sum(item["citation_similarity"] for item in with_citations) / len(with_citations), 3
        )
        if with_citations
        else None,
        "decorative_citations": sum(1 for item in measured if item["decorative"]),
        "decorative_rate": sum(1 for item in measured if item["decorative"]) / len(measured),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Puntua una corrida de la ablacion")
    parser.add_argument("--run", type=Path, required=True, help="Directorio runs/<timestamp>")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--propositions", type=Path, default=DEFAULT_PROPOSITIONS)
    parser.add_argument(
        "--semantic",
        action="store_true",
        help="Usa las proposiciones para comparar por significado. NO es el modo reportado: "
        "medido sobre esta corrida satura la cobertura en 1.000 y la metrica deja de "
        "discriminar. Ver eval/semantic.py.",
    )
    args = parser.parse_args(argv)
    if not args.semantic:
        args.propositions = None

    if not args.run.exists():
        print(f"no existe la corrida: {args.run}")
        return 2

    dataset = {}
    for line in args.dataset.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            item = json.loads(line)
            dataset[item["id"]] = item

    if args.propositions and args.propositions.exists():
        props = json.loads(args.propositions.read_text(encoding="utf-8"))
        con = 0
        for qid, mapping in props.items():
            if qid.startswith("_") or qid not in dataset:
                continue
            dataset[qid]["must_mention_propositions"] = mapping
            con += 1
        print(f"proposiciones: {con} preguntas con criterio semantico")

    registry = build_registry()
    print(f"corpus : {corpus.corpus_path()} ({len(registry)} normas)")

    records = load_records(args.run, dataset)

    rows: list[dict] = []
    repeats: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        rows.append(score_record(record, dataset[record["id"]], registry))
        if record.get("ok"):
            repeats.setdefault((record["arm"], record["id"]), []).append(
                record.get("response") or {}
            )

    if not rows:
        print("la corrida no tiene respuestas puntuables")
        return 2

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
        results["arms"][arm]["stability"] = aggregate_stability(
            [responses for (candidate, _), responses in repeats.items() if candidate == arm]
        )

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
