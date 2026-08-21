import pytest

from app import corpus
from app.config import settings
from app.gemini_client import resolve_citation_locator

DOC = """LIBRO III
DERECHO DE FAMILIA

TITULO IV
DECAIMIENTO Y DISOLUCION DEL VINCULO

Articulo 333.- Son causas de separacion de cuerpos:
1. El adulterio.
2. La violencia fisica o psicologica.
"""

SNIPPET = "Son causas de separacion de cuerpos: 1. El adulterio."


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    corpus.clear_cache()
    yield tmp_path
    corpus.clear_cache()


def test_resolves_against_corpus(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    result = resolve_citation_locator("codigo-civil.md", "cc", SNIPPET)
    assert result["locator"] == "Art. 333"
    assert result["locator_source"] == "exact"
    assert "Libro III" in result["breadcrumb"]


def test_falls_back_to_snippet_regex_when_document_is_missing(corpus_dir):
    result = resolve_citation_locator("no-esta.md", "", "Articulo 333.- Son causas de separacion.")
    assert result["locator"] == "Art. 333"
    assert result["locator_source"] == "snippet_regex"


def test_returns_empty_fields_when_nothing_resolves(corpus_dir):
    result = resolve_citation_locator("no-esta.md", "", "Un texto sin referencia normativa.")
    assert result == {"locator": "", "breadcrumb": "", "page": None, "locator_source": ""}


def test_disabled_flag_short_circuits(corpus_dir, monkeypatch):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    monkeypatch.setattr(settings, "enable_citation_locator", False)
    result = resolve_citation_locator("codigo-civil.md", "cc", SNIPPET)
    assert result["locator"] == ""


def test_empty_snippet_short_circuits(corpus_dir):
    assert resolve_citation_locator("codigo-civil.md", "cc", "")["locator"] == ""


def test_never_raises_when_the_corpus_layer_fails(monkeypatch):
    """Una cita sin locator es aceptable; una busqueda legal caida no lo es."""
    def explode(*args, **kwargs):
        raise RuntimeError("disco caido")

    monkeypatch.setattr("app.gemini_client.corpus.get_index", explode)
    result = resolve_citation_locator("codigo-civil.md", "cc", SNIPPET)
    assert result == {"locator": "", "breadcrumb": "", "page": None, "locator_source": ""}
