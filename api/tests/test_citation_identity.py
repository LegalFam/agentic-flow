"""Identidad y deduplicacion de citas.

El `citation_id` es lo unico que los agentes LLM transportan: el locator nunca viaja por
el prompt. Estos tests fijan las dos propiedades de las que depende ese diseno — que el id
sea estable y que la unidad de deduplicacion sea el articulo.
"""

from types import SimpleNamespace as NS

import pytest

from app import corpus
from app.config import settings
from app.gemini_client import _extract_grounding_citations, citation_id, citation_identity

CC = "https://spij.gob.pe/codigo-civil"


# --- identidad -------------------------------------------------------------------

def test_same_article_is_one_identity_regardless_of_fragment():
    # Dos fragmentos distintos del mismo articulo: una sola cita.
    assert citation_identity(CC, "Art. 333", "El adulterio.") == citation_identity(
        CC, "Art. 333", "La violencia fisica o psicologica."
    )


def test_different_articles_of_the_same_document_are_distinct():
    assert citation_identity(CC, "Art. 333", "x") != citation_identity(CC, "Art. 481", "x")


def test_same_article_in_different_documents_is_distinct():
    assert citation_identity(CC, "Art. 333", "x") != citation_identity(
        "https://otro.gob.pe/ley", "Art. 333", "x"
    )


def test_without_locator_the_fragment_is_the_identity():
    # Las resoluciones no tienen articulado: ahi la unidad vuelve a ser el fragmento.
    assert citation_identity(CC, "", "primer fragmento") != citation_identity(
        CC, "", "segundo fragmento"
    )


def test_identity_ignores_text_beyond_the_prefix():
    long_a = "mismo comienzo " * 20 + "final A"
    long_b = "mismo comienzo " * 20 + "final B"
    assert citation_identity(CC, "", long_a) == citation_identity(CC, "", long_b)


# --- id opaco --------------------------------------------------------------------

def test_id_is_deterministic():
    """Derivado y no aleatorio: el RAG Agent reintenta con consultas mas amplias y el
    mismo articulo debe volver con el mismo id."""
    identity = citation_identity(CC, "Art. 333", "El adulterio.")
    assert citation_id(identity) == citation_id(identity)


def test_id_is_short_and_url_safe():
    value = citation_id(citation_identity(CC, "Art. 333", "x"))
    assert len(value) == 10
    assert value.isalnum()


def test_id_differs_per_identity():
    ids = {
        citation_id(citation_identity(CC, f"Art. {n}", "x"))
        for n in range(50)
    }
    assert len(ids) == 50


def test_id_handles_non_ascii():
    # Los titulos y locators reales llevan acentos; encode('utf-8') no debe reventar.
    assert citation_id(citation_identity(CC, "Art. 333 Ñ", "sección")) != ""


# --- deduplicacion en la extraccion ----------------------------------------------

DOC = """LIBRO III
DERECHO DE FAMILIA

Articulo 333.- Son causas de separacion de cuerpos:
1. El adulterio.
2. La violencia fisica o psicologica, que el juez apreciara segun las circunstancias.

Articulo 481.- La pension alimenticia se regula por el juez en proporcion a las
necesidades de quien los pide.
"""


def _response(*snippets: str):
    """Respuesta de Gemini con un grounding chunk por snippet, todos del mismo archivo."""
    def chunk(text):
        meta = [
            NS(key="titulo", string_value="Codigo Civil"),
            NS(key="fuente", string_value=CC),
            NS(key="identificador", string_value="cc-peru"),
        ]
        return NS(retrieved_context=NS(title="codigo-civil.md", text=text, custom_metadata=meta))

    return NS(candidates=[NS(grounding_metadata=NS(grounding_chunks=[chunk(s) for s in snippets]))])


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    (tmp_path / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    corpus.clear_cache()
    yield tmp_path
    corpus.clear_cache()


def test_two_fragments_of_one_article_collapse(corpus_dir):
    citations = _extract_grounding_citations(
        _response(
            "Son causas de separacion de cuerpos: 1. El adulterio.",
            "La violencia fisica o psicologica, que el juez apreciara segun las circunstancias.",
        )
    )
    assert len(citations) == 1
    assert citations[0]["locator"] == "Art. 333"


def test_two_articles_of_one_document_stay_separate(corpus_dir):
    citations = _extract_grounding_citations(
        _response(
            "Son causas de separacion de cuerpos: 1. El adulterio.",
            "La pension alimenticia se regula por el juez en proporcion",
        )
    )
    assert len(citations) == 2
    assert {c["locator"] for c in citations} == {"Art. 333", "Art. 481"}
    # Ids distintos: es lo que permite volver a unirlos despues de los dos saltos de LLM.
    assert len({c["citation_id"] for c in citations}) == 2


def test_every_citation_carries_an_id(corpus_dir):
    citations = _extract_grounding_citations(
        _response("Son causas de separacion de cuerpos: 1. El adulterio.")
    )
    assert all(c["citation_id"] for c in citations)


def test_unlocated_fragments_are_not_collapsed(corpus_dir, monkeypatch):
    # Sin locator la unidad es el fragmento, asi que dos textos distintos son dos citas.
    monkeypatch.setattr(settings, "enable_citation_locator", False)
    citations = _extract_grounding_citations(
        _response("Un texto sin referencia normativa.", "Otro texto distinto sin referencia.")
    )
    assert len(citations) == 2


# --- registro autoritativo --------------------------------------------------------

def test_extraction_registers_every_locator(corpus_dir):
    from app import locator_registry

    locator_registry.clear()
    citations = _extract_grounding_citations(
        _response(
            "Son causas de separacion de cuerpos: 1. El adulterio.",
            "La pension alimenticia se regula por el juez en proporcion",
        )
    )

    # Cada cita debe poder resolverse despues por su id, que es lo unico que los
    # agentes transportan.
    for citation in citations:
        stored = locator_registry.resolve(citation["citation_id"])
        assert stored is not None
        assert stored["locator"] == citation["locator"]
