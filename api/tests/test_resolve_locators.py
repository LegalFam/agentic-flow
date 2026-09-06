"""`/resolve-locators` es la ultima frontera antes de que la cita llegue al usuario.

Lo que entra por aca lo escribio un LLM. El `citation_id` es opaco y solo sirve de llave;
el `original_snippet` es texto libre y por lo tanto se trata como sospechoso: se acepta
como puntero al chunk guardado, nunca como ubicacion.
"""

import pytest

from app import corpus, locator_registry
from app.config import settings
from app.main import resolve_locators
from app.models import LocatorResolveRequest, LocatorResolveRequestCitation

DOC = """SECCION QUINTA
PROCESOS CONTENCIOSOS

Articulo 561.- Son competentes para conocer los procesos de alimentos los jueces de paz
letrado y los directores de los establecimientos de menores.

Articulo 562.- El demandante goza de Auxilio Judicial sin trámite ni prestar caución
juratoria. La resolución que lo concede es inimpugnable.
"""

CHUNK = (
    "Son competentes para conocer los procesos de alimentos los jueces de paz letrado y "
    "los directores de los establecimientos de menores. Articulo 562.- El demandante goza "
    "de Auxilio Judicial sin trámite ni prestar caución juratoria."
)

EXCERPT_562 = "El demandante goza de Auxilio Judicial sin trámite ni prestar caución juratoria."

# Un excerpt que cruza el corte entre articulos: se cita con los dos o con ninguno.
EXCERPT_561_562 = (
    "los directores de los establecimientos de menores. Articulo 562.- El demandante goza "
    "de Auxilio Judicial"
)

CHUNK_FIELDS = {
    "locator": "Art. 561",
    "breadcrumb": "Seccion Quinta - Procesos contenciosos > Art. 561",
    "page": None,
    "locator_source": "exact",
}


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    (tmp_path / "procesal.md").write_text(DOC, encoding="utf-8")
    corpus.clear_cache()
    locator_registry.clear()
    yield tmp_path
    corpus.clear_cache()
    locator_registry.clear()


def register(citation_id="abc123", articles=("Art. 561", "Art. 562")):
    locator_registry.register(
        citation_id,
        CHUNK_FIELDS,
        {
            "title": "procesal.md",
            "file_id": "procesal",
            "snippet": CHUNK,
            "articles": list(articles),
            "file_name": "Codigo Procesal Civil",
            "file_url": "https://example.test/procesal",
        },
    )


def ask(citation_id="abc123", excerpt=""):
    request = LocatorResolveRequest(
        citations=[
            LocatorResolveRequestCitation(citation_id=citation_id, original_snippet=excerpt)
        ]
    )
    response = resolve_locators(request)
    return response, response.citations[0]


def test_excerpt_wins_over_the_chunk_locator(corpus_dir):
    register()
    response, citation = ask(excerpt=EXCERPT_562)
    assert citation.locator == "Art. 562"
    assert citation.locator_scope == "excerpt"
    assert citation.chunk_articles == ["Art. 561", "Art. 562"]
    assert response.from_excerpt == 1


def test_excerpt_that_crosses_articles_cites_all_of_them(corpus_dir):
    """El caso que reaparecio: el fragmento parte del 561 y sigue dentro del 562."""
    register()
    response, citation = ask(excerpt=EXCERPT_561_562)
    assert citation.locator == "Arts. 561 y 562"
    assert citation.locator_scope == "excerpt_multi"
    assert citation.excerpt_articles == ["Art. 561", "Art. 562"]
    assert response.from_excerpt == 1


def test_uncombinable_multi_article_excerpt_returns_no_location(corpus_dir, monkeypatch):
    monkeypatch.setattr(settings, "locator_max_combined_articles", 1)
    register()
    response, citation = ask(excerpt=EXCERPT_561_562)
    assert citation.locator == ""
    assert citation.locator_scope == "ambiguous"
    assert response.ambiguous == 1


def test_the_document_comes_from_the_registry_not_from_the_agent(corpus_dir):
    """El agente ya no copia file_name ni file_url: se recuperan por el citation_id."""
    register()
    _, citation = ask(excerpt=EXCERPT_562)
    assert citation.file_name == "Codigo Procesal Civil"
    assert citation.file_url == "https://example.test/procesal"


def test_unknown_citation_id_has_no_document_either(corpus_dir):
    register()
    _, citation = ask(citation_id="alterado", excerpt=EXCERPT_562)
    assert citation.file_name == ""
    assert citation.file_url == ""


def test_multi_article_chunk_without_excerpt_returns_no_location(corpus_dir):
    """Quedarse con el primer articulo seria adivinar cual sustenta la respuesta."""
    register()
    response, citation = ask()
    assert citation.locator == ""
    assert citation.locator_scope == "ambiguous"
    assert citation.resolved is True
    assert response.ambiguous == 1


def test_invented_excerpt_falls_back_instead_of_being_trusted(corpus_dir):
    register(articles=("Art. 561",))
    _, citation = ask(excerpt="El juez fijara la pension en un tercio de los ingresos.")
    assert citation.locator == "Art. 561"
    assert citation.locator_scope == "chunk"


def test_single_article_chunk_keeps_the_chunk_locator(corpus_dir):
    register(articles=("Art. 561",))
    _, citation = ask()
    assert citation.locator == "Art. 561"
    assert citation.locator_scope == "chunk"


def test_ambiguity_guard_can_be_disabled(corpus_dir, monkeypatch):
    monkeypatch.setattr(settings, "locator_require_excerpt_when_ambiguous", False)
    register()
    _, citation = ask()
    assert citation.locator == "Art. 561"
    assert citation.locator_scope == "chunk"


def test_unknown_citation_id_degrades_to_empty(corpus_dir):
    register()
    response, citation = ask(citation_id="alterado", excerpt=EXCERPT_562)
    assert citation.locator == ""
    assert citation.resolved is False
    assert citation.locator_scope == "unknown"
    assert response.unknown == 1


def test_a_broken_corpus_does_not_take_down_the_endpoint(corpus_dir, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("disco caido")

    monkeypatch.setattr("app.main.corpus.get_index", explode)
    register(articles=("Art. 561",))
    _, citation = ask(excerpt=EXCERPT_562)
    assert citation.locator == "Art. 561"
    assert citation.locator_scope == "chunk"


def test_disabled_flag_skips_the_excerpt_pass(corpus_dir, monkeypatch):
    monkeypatch.setattr(settings, "enable_citation_locator", False)
    register(articles=("Art. 561",))
    _, citation = ask(excerpt=EXCERPT_562)
    assert citation.locator_scope == "chunk"
