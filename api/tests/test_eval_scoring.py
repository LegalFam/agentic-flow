"""Deteccion de articulos citados, auditoria de citas contra el corpus y estadistica."""

import pytest

from app import corpus
from app.config import settings
from eval import corpus_articles, report, score

CODIGO_CIVIL = """LIBRO III
DERECHO DE FAMILIA

Articulo 472.- Se entiende por alimentos lo que es indispensable para el sustento,
habitacion, vestido, educacion, instruccion y capacitacion para el trabajo.

Articulo 481.- Los alimentos se regulan por el juez en proporcion a las necesidades
de quien los pide y a las posibilidades del que debe darlos.

Articulo 482.- La pension alimenticia se incrementa o reduce segun el aumento o la
disminucion que experimenten las necesidades del alimentista.
"""


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    (tmp_path / "codigo-civil-abcdef123456.md").write_text(CODIGO_CIVIL, encoding="utf-8")
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    corpus.clear_cache()
    corpus_articles.build_registry.cache_clear()
    yield tmp_path
    corpus.clear_cache()
    corpus_articles.build_registry.cache_clear()


@pytest.fixture
def registry(corpus_dir):
    return corpus_articles.build_registry()


# --------------------------------------------------------------------------------------
# Deteccion de menciones


def test_article_is_attributed_to_the_nearest_norm():
    mentions = corpus_articles.detect_mentions(
        "El articulo 92 del Codigo de los Ninos y Adolescentes. El articulo 481 del Codigo Civil."
    )
    assert [(item.article, item.norm_key) for item in mentions] == [
        ("92", "codigo_ninos_adolescentes"),
        ("481", "codigo_civil"),
    ]


def test_enumeration_expands_to_every_article():
    mentions = corpus_articles.detect_mentions("Los articulos 472, 474 y 481 del Codigo Civil.")
    assert [item.article for item in mentions] == ["472", "474", "481"]
    assert {item.norm_key for item in mentions} == {"codigo_civil"}


def test_attribution_is_marked_as_inferred_when_carried_over():
    mentions = corpus_articles.detect_mentions("Segun el Codigo Civil, el articulo 481 dispone.")
    assert mentions[0].attribution == "inferred"


def test_article_without_a_norm_is_not_counted_as_invented(registry):
    mention = corpus_articles.detect_mentions("Revisa el articulo 481 antes de decidir.")[0]
    assert mention.attribution == "none"
    assert corpus_articles.classify_mention(mention, registry) == "unattributed"


def test_nonexistent_article_is_hallucinated(registry):
    mention = corpus_articles.detect_mentions("El articulo 9999 del Codigo Civil lo regula.")[0]
    assert corpus_articles.classify_mention(mention, registry) == "hallucinated"


def test_existing_article_is_verified(registry):
    mention = corpus_articles.detect_mentions("El articulo 481 del Codigo Civil lo regula.")[0]
    assert corpus_articles.classify_mention(mention, registry) == "exists"


def test_prose_reference_without_a_number_is_ignored():
    assert corpus_articles.detect_mentions("conforme al articulo pertinente") == []


def test_locator_label_expands_to_its_articles():
    assert corpus_articles.articles_in_locator("Arts. 562 y 563") == ["562", "563"]
    assert corpus_articles.articles_in_locator("Art. 481") == ["481"]
    assert corpus_articles.articles_in_locator("") == []


def test_document_name_maps_to_its_norm():
    assert corpus_articles.norm_of_document("Codigo Civil") == "codigo_civil"
    assert corpus_articles.norm_of_document("codigo-civil-abcdef.md") == "codigo_civil"
    assert corpus_articles.norm_of_document("Un documento cualquiera") is None


# --------------------------------------------------------------------------------------
# Auditoria de citas


def _citation(snippet: str, locator: str) -> dict:
    return {
        "original_snippet": snippet,
        "locator": locator,
        "file_name": "Codigo Civil",
        "file_url": "https://ejemplo/codigo-civil",
        "locator_source": "exact",
        "locator_scope": "excerpt",
    }


def test_verbatim_snippet_with_the_right_article_is_correct(registry):
    audit = score.audit_citation(
        _citation("Los alimentos se regulan por el juez en proporcion", "Art. 481"), registry
    )
    assert audit["verbatim"] is True
    assert audit["locator_verdict"] == "correct"


def test_verbatim_snippet_with_the_wrong_article_is_caught(registry):
    """El fallo que la metrica existe para encontrar: cita bien redactada, mal ubicada."""
    audit = score.audit_citation(
        _citation("Los alimentos se regulan por el juez en proporcion", "Art. 472"), registry
    )
    assert audit["verbatim"] is True
    assert audit["locator_verdict"] == "wrong"


def test_invented_snippet_is_not_verbatim(registry):
    audit = score.audit_citation(
        _citation("Este pasaje no aparece en ningun documento del corpus", "Art. 481"), registry
    )
    assert audit["verbatim"] is False
    assert audit["locator_verdict"] == "unverifiable"


def test_citation_without_locator_is_reported_as_missing(registry):
    audit = score.audit_citation(
        _citation("Los alimentos se regulan por el juez en proporcion", ""), registry
    )
    assert audit["has_locator"] is False
    assert audit["locator_verdict"] == "missing"


def test_citation_from_an_unknown_document_is_set_apart(registry):
    """No supe que documento es: es carencia del evaluador, no sospecha sobre el sistema.

    Se separa de `unverifiable` —el pasaje no aparece en un documento que si resolvi—
    porque solo el segundo dice algo sobre la calidad de la cita.
    """
    citation = _citation("cualquier texto", "Art. 481")
    citation["file_name"] = "Documento desconocido"
    citation["file_url"] = ""
    audit = score.audit_citation(citation, registry)
    assert audit["document_resolved"] is False
    assert audit["locator_verdict"] == "document_unknown"


def test_passage_missing_from_a_resolved_document_is_unverifiable(registry):
    """Esta si es la senal real: el documento existe y el pasaje declarado no esta en el."""
    audit = score.audit_citation(
        _citation("Este pasaje no aparece en ningun documento del corpus", "Art. 481"), registry
    )
    assert audit["document_resolved"] is True
    assert audit["verbatim"] is False
    assert audit["locator_verdict"] == "unverifiable"


# --------------------------------------------------------------------------------------
# Puntuacion de una respuesta


def _record(message: str, citations: list[dict], arm: str = "full") -> dict:
    return {
        "id": "alim-001",
        "arm": arm,
        "ok": True,
        "latency_ms": 1000,
        "response": {
            "message": message,
            "citations": citations,
            "citationSupportStatus": "GOOD" if citations else "NONE",
            "confidenceStatus": "HIGH" if citations else "LOW",
            "nextSteps": [],
            "clarifyingQuestions": [],
            "specialistSupportRecommended": False,
            "agentTokenCost": 3,
        },
    }


ITEM = {
    "id": "alim-001",
    "category": "pension de alimentos",
    "expected_articles": [{"norm": "codigo_civil", "article": "481"}],
    "must_mention": ["proporcion", "necesidades"],
    "must_not_mention": [],
    "expects_specialist_support": False,
}


def test_article_reaches_the_user_through_the_citation_channel(registry):
    row = score.score_record(
        _record(
            "La pension se fija en proporcion a las necesidades.",
            [_citation("Los alimentos se regulan por el juez en proporcion", "Art. 481")],
        ),
        ITEM,
        registry,
    )
    assert row["article_recall"] == 1.0
    assert row["articles_from_citations"] == 1
    assert row["articles_from_text"] == 0
    assert row["traceable"] is True


def test_article_reaches_the_user_through_the_text_channel(registry):
    row = score.score_record(
        _record("Segun el articulo 481 del Codigo Civil, se fija en proporcion.", []),
        ITEM,
        registry,
    )
    assert row["article_recall"] == 1.0
    assert row["articles_from_text"] == 1
    assert row["traceable"] is False


def test_answer_without_sources_has_no_traceability(registry):
    row = score.score_record(
        _record("La pension se fija segun las necesidades y posibilidades.", []), ITEM, registry
    )
    assert row["article_recall"] == 0.0
    assert row["traceable"] is False
    assert row["citations"] == 0


def test_invented_article_is_counted_and_marks_the_answer_incorrect(registry):
    row = score.score_record(
        _record("El articulo 9999 del Codigo Civil regula la proporcion y las necesidades.", []),
        ITEM,
        registry,
    )
    assert row["hallucinated_explicit"] == 1
    assert row["correct"] is False


def test_must_mention_coverage_ignores_accents_and_case(registry):
    row = score.score_record(
        _record("Se fija en PROPORCIÓN a las NECESIDADES del menor.", []), ITEM, registry
    )
    assert row["must_mention_coverage"] == 1.0


# --------------------------------------------------------------------------------------
# Estadistica del reporte


def test_mcnemar_only_counts_discordant_pairs():
    wins, losses, p_value = report.mcnemar([(True, False)] * 10 + [(True, True)] * 40)
    assert (wins, losses) == (10, 0)
    assert p_value < 0.01


def test_mcnemar_without_disagreement_is_not_significant():
    _, _, p_value = report.mcnemar([(True, True)] * 30 + [(False, False)] * 30)
    assert p_value == 1.0


def test_wilcoxon_detects_a_consistent_shift():
    pairs = [(value + 1.0, value) for value in range(20)]
    assert report.wilcoxon(pairs) < 0.01


def test_wilcoxon_needs_enough_pairs():
    import math

    assert math.isnan(report.wilcoxon([(1.0, 0.0), (2.0, 1.0)]))


def test_bootstrap_interval_brackets_the_mean():
    low, high = report.bootstrap_ci([0.5] * 20 + [0.7] * 20)
    assert low <= 0.6 <= high


def test_factorial_table_separates_both_effects():
    """Un componente que aporta 1.0 y otro que no aporta nada tienen que salir separados."""
    table = {
        "q1": {"full": 1.0, "no_rag": 0.0, "no_xai": 1.0, "base": 0.0},
        "q2": {"full": 1.0, "no_rag": 0.0, "no_xai": 1.0, "base": 0.0},
    }
    stats = report.factorial_table(table)
    assert stats["rag_effect"] == pytest.approx(1.0)
    assert stats["xai_effect"] == pytest.approx(0.0)
    assert stats["interaction"] == pytest.approx(0.0)


def test_pivot_averages_repeats_of_the_same_question():
    rows = [
        {"id": "q1", "arm": "full", "ok": True, "traceable": True},
        {"id": "q1", "arm": "full", "ok": True, "traceable": False},
    ]
    assert report.pivot(rows, "traceable")["q1"]["full"] == pytest.approx(0.5)


def test_pivot_drops_failed_calls():
    rows = [
        {"id": "q1", "arm": "full", "ok": False, "traceable": True},
        {"id": "q1", "arm": "full", "ok": True, "traceable": True},
    ]
    assert report.pivot(rows, "traceable")["q1"]["full"] == 1.0


# --------------------------------------------------------------------------------------
# Reanudacion: un corte de infraestructura no es una tasa de fallo del sistema


def _write_run(tmp_path, records):
    import json as _json

    (tmp_path / "full.jsonl").write_text(
        "\n".join(_json.dumps(record) for record in records), encoding="utf-8"
    )
    return tmp_path


def test_successful_retry_replaces_the_failed_attempt(tmp_path, capsys):
    run = _write_run(
        tmp_path,
        [
            {"id": "alim-001", "arm": "full", "repeat": 0, "ok": False, "error": "URLError"},
            {"id": "alim-001", "arm": "full", "repeat": 0, "ok": True, "response": {"message": "x"}},
        ],
    )
    records = score.load_records(run, {"alim-001": ITEM})
    assert len(records) == 1
    assert records[0]["ok"] is True


def test_attempt_that_never_succeeded_stays_a_failure(tmp_path, capsys):
    run = _write_run(
        tmp_path,
        [
            {"id": "alim-001", "arm": "full", "repeat": 0, "ok": False, "error": "timeout"},
            {"id": "alim-001", "arm": "full", "repeat": 0, "ok": False, "error": "timeout"},
        ],
    )
    records = score.load_records(run, {"alim-001": ITEM})
    assert len(records) == 1
    assert records[0]["ok"] is False


def test_repeats_are_kept_apart(tmp_path, capsys):
    run = _write_run(
        tmp_path,
        [
            {"id": "alim-001", "arm": "full", "repeat": 0, "ok": True, "response": {"message": "a"}},
            {"id": "alim-001", "arm": "full", "repeat": 1, "ok": True, "response": {"message": "b"}},
        ],
    )
    assert len(score.load_records(run, {"alim-001": ITEM})) == 2


def test_questions_outside_the_dataset_are_skipped(tmp_path, capsys):
    run = _write_run(
        tmp_path,
        [{"id": "no-existe", "arm": "full", "repeat": 0, "ok": True, "response": {"message": "x"}}],
    )
    assert score.load_records(run, {"alim-001": ITEM}) == []


def test_snippet_spanning_two_articles_accepts_the_combined_locator(registry):
    """Un pasaje que cruza dos articulos se cita con los dos, y eso es correcto.

    Comparar el locator combinado contra el articulo del primer caracter marcaba como
    fallo citas que estaban bien, y hundia artificialmente la tasa de acierto.
    """
    spanning = (
        "disminucion que experimenten las necesidades del alimentista"
    )
    audit = score.audit_citation(_citation(spanning, "Art. 482"), registry)
    assert audit["locator_verdict"] == "correct"


def test_combined_locator_that_overreaches_is_partial(registry):
    """Declarar dos articulos cuando el pasaje solo cubre uno no es correcto del todo."""
    audit = score.audit_citation(
        _citation("Los alimentos se regulan por el juez en proporcion", "Arts. 481 y 482"),
        registry,
    )
    assert audit["locator_verdict"] == "partial"
