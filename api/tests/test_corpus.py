import json

import pytest

from app import corpus
from app.config import settings

DOC = """LIBRO III
DERECHO DE FAMILIA

Articulo 333.- Son causas de separacion de cuerpos: el adulterio.
"""


@pytest.fixture
def corpus_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", str(tmp_path))
    corpus.clear_cache()
    yield tmp_path
    corpus.clear_cache()


def test_missing_corpus_directory_is_safe(monkeypatch):
    monkeypatch.setattr(settings, "corpus_dir", "/no/existe/aqui")
    assert corpus.iter_corpus_files() == []
    assert corpus.get_index("cualquiera.md", None) is None
    assert corpus.status()["exists"] is False


def test_empty_corpus_directory_is_safe(corpus_dir):
    assert corpus.iter_corpus_files() == []
    assert corpus.get_index("codigo-civil.md", None) is None


def test_resolves_by_exact_filename(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    assert corpus.resolve_document_path("codigo-civil.md", None).name == "codigo-civil.md"


def test_resolves_when_title_has_no_extension(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    assert corpus.resolve_document_path("codigo-civil", None).name == "codigo-civil.md"


def test_resolves_by_normalized_stem(corpus_dir):
    (corpus_dir / "codigo-civil-peru.md").write_text(DOC, encoding="utf-8")
    assert corpus.resolve_document_path("Código Civil Perú.md", None).name == "codigo-civil-peru.md"


def test_resolves_by_file_id_when_title_fails(corpus_dir):
    (corpus_dir / "ley-30364.md").write_text(DOC, encoding="utf-8")
    assert corpus.resolve_document_path("Titulo que no casa", "ley-30364").name == "ley-30364.md"


def test_manifest_overrides_name_matching(corpus_dir):
    (corpus_dir / "documento-a.md").write_text(DOC, encoding="utf-8")
    (corpus_dir / settings.corpus_manifest_filename).write_text(
        json.dumps({"Nombre Raro En El Store.md": "documento-a.md"}), encoding="utf-8"
    )
    resolved = corpus.resolve_document_path("Nombre Raro En El Store.md", None)
    assert resolved.name == "documento-a.md"


def test_corrupt_manifest_falls_back_to_name_matching(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    (corpus_dir / settings.corpus_manifest_filename).write_text("{no es json", encoding="utf-8")
    assert corpus.load_manifest() == {}
    assert corpus.resolve_document_path("codigo-civil.md", None).name == "codigo-civil.md"


def test_unknown_document_returns_none(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    assert corpus.resolve_document_path("otra-cosa.md", None) is None


def test_index_is_cached_and_invalidated_on_change(corpus_dir):
    path = corpus_dir / "codigo-civil.md"
    path.write_text(DOC, encoding="utf-8")

    first = corpus.load_index(path)
    assert corpus.load_index(path) is first

    path.write_text(DOC.replace("333", "334"), encoding="utf-8")
    updated = corpus.load_index(path)
    assert updated is not first
    assert any(h.label == "Art. 334" for h in updated.headings)


def test_clear_cache_reports_entries(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    corpus.get_index("codigo-civil.md", None)
    assert corpus.clear_cache() == 1
    assert corpus.clear_cache() == 0


def test_status_reports_documents(corpus_dir):
    (corpus_dir / "a.md").write_text(DOC, encoding="utf-8")
    (corpus_dir / "b.md").write_text(DOC, encoding="utf-8")
    report = corpus.status()
    assert report["documents"] == 2
    assert report["exists"] is True
    assert sorted(report["sample"]) == ["a.md", "b.md"]


def test_ignores_document_id_hash_suffix(corpus_dir):
    (corpus_dir / "codigo-civil.md").write_text(DOC, encoding="utf-8")
    resolved = corpus.resolve_document_path("codigo-civil-97fe57ba02fb.md", None)
    assert resolved is not None and resolved.name == "codigo-civil.md"


def test_ignores_hash_suffix_in_the_other_direction(corpus_dir):
    (corpus_dir / "codigo-civil-97fe57ba02fb.md").write_text(DOC, encoding="utf-8")
    resolved = corpus.resolve_document_path("Codigo Civil.md", None)
    assert resolved is not None and resolved.name == "codigo-civil-97fe57ba02fb.md"


def test_does_not_strip_a_non_hash_suffix(corpus_dir):
    (corpus_dir / "resolucion-224.md").write_text(DOC, encoding="utf-8")
    assert corpus.resolve_document_path("resolucion-224-2016.md", None) is None


RULING_QUOTING_ARTICLES = """## CASACIÓN 2067-2010

## Tenencia y custodia de menor

Artículo 82. Variación de la Tenencia. Si resulta necesaria la variación de la Tenencia, el Juez ordenará que se efectúe en forma progresiva.

La Sala considera que la variación de la tenencia no debe producir daño ni trastorno al niño, por mandato legal.

- Artículo 85. Opinión. El juez especializado debe escuchar la opinión del niño.

Por tales razones declararon infundado el recurso de casación interpuesto por la demandante.
"""

RESOLUCION_MINISTERIAL = """## Resolución Ministerial N° 100-2021-MIMP

Artículo 1.- Aprobar el protocolo de actuación.

Artículo 2.- Disponer la publicación del protocolo en el portal institucional.

Artículo 3.- Encargar el cumplimiento de la presente resolución a la Dirección General.
"""


@pytest.fixture
def real_articulado_threshold(monkeypatch):
    monkeypatch.setattr(settings, "locator_min_articulado_articles", 20)


def test_ruling_that_quotes_articles_gets_no_article_locator(corpus_dir, real_articulado_threshold):
    from app import locator

    (corpus_dir / "cas-2067-2010.md").write_text(RULING_QUOTING_ARTICLES, encoding="utf-8")
    index = corpus.get_index("cas-2067-2010.md", None)
    assert index.articulated is False
    assert index.articles == []
    for passage in (
        "Si resulta necesaria la variación de la Tenencia",
        "no debe producir daño ni trastorno al niño",
        "declararon infundado el recurso de casación",
    ):
        found, articles = locator.resolve_chunk(index, passage)
        assert not found.label.startswith("Art"), passage
        assert articles == []
        assert not locator.resolve_excerpt(index, passage, passage).label.startswith("Art")


def test_ruling_does_not_fall_back_to_article_numbers_in_the_snippet(corpus_dir, real_articulado_threshold):
    from app import locator

    (corpus_dir / "cas-2067-2010.md").write_text(RULING_QUOTING_ARTICLES, encoding="utf-8")
    index = corpus.get_index("cas-2067-2010.md", None)
    assert locator.resolve_chunk(index, "texto ajeno al documento según el artículo 82")[0].is_empty()


def test_short_norm_numbered_from_one_keeps_its_articles(corpus_dir, real_articulado_threshold):
    from app import locator

    (corpus_dir / "rm-100-2021.md").write_text(RESOLUCION_MINISTERIAL, encoding="utf-8")
    index = corpus.get_index("rm-100-2021.md", None)
    assert index.articulated is True
    assert locator.resolve(index, "Disponer la publicación del protocolo").label == "Art. 2"
