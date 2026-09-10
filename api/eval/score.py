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
    """El markdown original del documento que la cita dice haber usado.

    Tres intentos, de mas a menos preciso: el manifiesto del corpus, la tabla de normas, y
    el numero de expediente para la jurisprudencia, que es lo unico que se escribe igual
    en la caratula de la cita y en el nombre del fichero.
    """
    index = corpus.get_index(citation.get("file_name"), citation.get("file_id"))
    if index is not None:
        return index

    path = corpus_articles.find_corpus_path(
        citation.get("file_name", ""), citation.get("file_url", "")
    )
    return corpus.load_index(path) if path is not None else None


def _has_articles(citation: dict) -> bool:
    """Si el documento citado tiene articulado que un localizador pueda señalar."""
    norm_key = norm_of_document(citation.get("file_name", ""), citation.get("file_url", ""))
    norm = BY_KEY.get(norm_key) if norm_key else None
    return bool(norm and norm.articulated)


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
        # No es lo mismo "el pasaje no esta" que "no supe que documento es": lo segundo es
        # una carencia del evaluador y contarlo como sospecha del sistema seria injusto.
        audit["locator_verdict"] = "document_unknown"
        return audit

    audit["document_resolved"] = True
    query = clean_user_text(snippet)
    position, strategy = locator.find_in_folded(index.folded, query)
    # Solo `exact` y `prefix` prueban que el pasaje esta copiado tal cual. `fuzzy` acepta
    # un parecido alto, que es util para ubicar pero no para afirmar literalidad.
    audit["verbatim"] = position is not None and strategy in ("exact", "prefix")
    audit["match_strategy"] = strategy

    if position is None:
        return audit

    # Los articulos que cubre el pasaje entero, no solo el de su primer caracter.
    #
    # Un fragmento recuperado cruza a menudo dos articulos seguidos, y el sistema lo
    # etiqueta con los dos ("Arts. 478 y 479"), que es lo correcto. Comparar eso contra el
    # articulo del offset inicial marcaba como fallo 31 de 157 citas que estaban bien, y
    # convertia una tasa de acierto real del 85% en un 47% inventado por la metrica.
    start = index.offset_map[position]
    last = min(position + max(1, len(query)) - 1, len(index.offset_map) - 1)
    end = index.offset_map[last] + 1
    expected = {normalize_article(label) for label in locator.articles_in_span(index, start, end)}
    declared = set(articles_in_locator(reported))

    articulated = _has_articles(citation)

    if not articulated:
        # Una casacion o un protocolo no tienen articulado al que apuntar, traigan o no
        # una etiqueta: el sistema les pone el encabezado del documento, y el propio
        # codigo de produccion ya advierte que eso "no es una ubicacion juridica".
        # Exigirles un articulo mide la composicion del corpus, no el sistema.
        audit["locator_verdict"] = "not_applicable"
    elif not declared:
        audit["locator_verdict"] = "missing"
    elif not expected:
        audit["locator_verdict"] = "unverifiable"
    elif declared == expected:
        audit["locator_verdict"] = "correct"
    elif declared & expected:
        audit["locator_verdict"] = "partial"
    else:
        audit["locator_verdict"] = "wrong"

    audit["recomputed_articles"] = sorted(expected)
    return audit


# --------------------------------------------------------------------------------------
# Puntuacion por pregunta


def cited_article_texts(citations: list[dict], registry: dict) -> list[str]:
    """El texto real de los articulos que respaldan la respuesta.

    Se toma del corpus a partir del documento y el localizador de cada cita, no del
    pasaje que la cita trae: el pasaje es un recorte que el modelo eligio, y medir el
    respaldo contra un recorte suyo seria dejarle marcar su propio examen.
    """
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

    # `agentTokenCost: 1` marca las respuestas que salieron por la via de aclaracion: el
    # flujo pidio datos en vez de responder, sin llegar a recuperar ni a explicar. Ahi la
    # ausencia de los terminos exigidos no mide una respuesta incompleta, mide que no hubo
    # respuesta, y contarla como incorrecta mezcla dos cosas distintas.
    substantive = response.get("agentTokenCost") != 1

    folded = deaccent(message)
    must = [term for term in item.get("must_mention", [])]
    # El dataset puede traer una proposicion por termino; con ella la comparacion es
    # semantica, sin ella se queda en literal. Ver `eval/semantic.py`.
    props = item.get("must_mention_propositions") or {}
    covered = [term for term in must if semantic.mentions(message, term, props.get(term))]
    violations = [term for term in item.get("must_not_mention", []) if deaccent(term) in folded]

    audits = [audit_citation(citation, registry) for citation in citations]
    hallucination = hallucination_counts(message, registry)

    specialist = bool(response.get("specialistSupportRecommended"))
    expects_specialist = bool(item.get("expects_specialist_support"))

    # --- explicabilidad mas alla de si la cita se sostiene
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
        "must_mention_coverage": (len(covered) / len(must)) if (must and substantive) else None,
        "must_not_violations": len(violations),
        # --- eje XAI
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
        # Una respuesta es trazable si al menos una cita esta copiada literalmente y
        # ubicada correctamente: es lo minimo para que el usuario pueda ir a comprobarla.
        "traceable": any(
            audit["verbatim"] and audit["locator_verdict"] == "correct" for audit in audits
        ),
        "citation_support_status": response.get("citationSupportStatus"),
        "confidence_status": response.get("confidenceStatus"),
        "next_steps": len(response.get("nextSteps") or []),
        "clarifying_questions": len(response.get("clarifyingQuestions") or []),
        # --- suficiencia de la explicacion (proxy lexico, ver explainability.py)
        "normative_claims": support["claims"],
        "claims_supported": support["supported"],
        "support_density": support["density"],
        # --- comprensibilidad
        "answer_readability": answer_readability["szigriszt"] if answer_readability else None,
        "answer_words_per_sentence": (
            answer_readability["words_per_sentence"] if answer_readability else None
        ),
        "snippet_readability": gap["original"] if gap else None,
        "summary_readability": gap["summary"] if gap else None,
        "readability_gap": gap["gap"] if gap else None,
        # Lo que el usuario lee frente al articulado citado. Es la medida que responde a
        # si la respuesta reestructura en lenguaje accesible; `readability_gap` mide solo
        # la glosa de cada cita, que es otra cosa.
        "answer_vs_sources_gap": answer_gap,
        # --- accionabilidad de los pasos
        "steps_actionable_rate": steps["actionable_rate"],
        "steps_duplicated": steps["duplicated"],
        "steps_generic_referral": steps["generic_referral"],
        # --- seguridad
        "specialist_support": specialist,
        "expects_specialist_support": expects_specialist,
        "specialist_true_positive": specialist and expects_specialist,
        "specialist_false_negative": expects_specialist and not specialist,
        "specialist_false_positive": specialist and not expects_specialist,
        # --- correccion, para la calibracion
        # Sin respuesta sustantiva no hay nada que juzgar como correcto o incorrecto.
        "correct": None
        if not substantive
        else (
            hallucination.get("hallucinated_explicit", 0) == 0
            and (len(covered) / len(must) >= 0.5 if must else True)
        ),
        "substantive": substantive,
    }


# --------------------------------------------------------------------------------------
# Agregacion


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
        # Excluye los `None`: una respuesta que salio por la via de aclaracion no es un
        # fallo de la metrica, es una respuesta que no se puede juzgar con ella.
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

    # Tasas por cita y no por respuesta: una respuesta con seis citas y otra con una no
    # pesan igual en la media por respuesta, y aca interesa la calidad de la cita.
    total_citations = sum(row["citations"] for row in answered)
    summary["total_citations"] = total_citations
    # Las citas a documentos sin articulado no pueden llevar localizador de articulo, asi
    # que se sacan del denominador de la tasa de ubicacion: incluirlas mediria cuanta
    # jurisprudencia hay en el corpus.
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


def load_records(run: Path, dataset: dict) -> list[dict]:
    """Las respuestas de la corrida, con un solo intento por (brazo, pregunta, repeticion).

    Una corrida larga se hace por fases y se reanuda, asi que un mismo intento puede
    aparecer varias veces: primero fallido —se cayo el contenedor, venció el timeout— y
    despues correcto. Gana el intento correcto.

    Sin esto, un corte de infraestructura quedaria registrado como tasa de fallo del
    sistema evaluado, que es una cosa completamente distinta. Un intento que nunca llego a
    salir bien si se conserva como fallo: eso si es un resultado.
    """
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
    """Fidelidad causal de la cita, sobre las preguntas que se repitieron.

    Devuelve `None` cuando la corrida no llevaba repeticiones: sin al menos dos respuestas
    a la misma pregunta no hay nada que comparar, y un cero ahi se leeria como un
    resultado en vez de como ausencia de datos.
    """
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
        # Respuesta estable con citas inestables: la cita acompana a la respuesta en vez
        # de sostenerla.
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

    # Las proposiciones viven en su propio fichero: son una capa de interpretacion sobre
    # el ground truth —que debe transmitir una respuesta para dar por cubierto un
    # termino— y conviene poder revisarlas y versionarlas aparte de las preguntas.
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
