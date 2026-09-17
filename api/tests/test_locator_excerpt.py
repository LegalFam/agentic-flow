import pytest

from app import locator as L
from app.config import settings

PROCESAL = """# Texto Unico Ordenado del Codigo Procesal Civil

SECCION QUINTA
PROCESOS CONTENCIOSOS

TITULO III
PROCESO SUMARISIMO

CAPITULO II
DISPOSICIONES ESPECIALES

SUBCAPITULO 1
ALIMENTOS

Articulo 561.- Son competentes para conocer los procesos de alimentos los jueces de paz
letrado y los directores de los establecimientos de menores.

Articulo 562.- El demandante goza de Auxilio Judicial sin trámite ni prestar caución
juratoria. La resolución que lo concede es inimpugnable.

Articulo 563.- A pedido de parte y cuando se acredite de manera indubitable el vínculo
familiar, el Juez puede prohibir al demandado ausentarse del país.
"""

CHUNK = (
    "Son competentes para conocer los procesos de alimentos los jueces de paz letrado y "
    "los directores de los establecimientos de menores. Articulo 562.- El demandante goza "
    "de Auxilio Judicial sin trámite ni prestar caución juratoria. La resolución que lo "
    "concede es inimpugnable. Articulo 563.- A pedido de parte y cuando se acredite de "
    "manera indubitable el vínculo familiar, el Juez puede prohibir al demandado "
    "ausentarse del país."
)

EXCERPT_562 = "El demandante goza de Auxilio Judicial sin trámite ni prestar caución juratoria."
EXCERPT_563 = "el Juez puede prohibir al demandado ausentarse del país"

EXCERPT_562_563 = (
    "La resolución que lo concede es inimpugnable. Articulo 563.- A pedido de parte y "
    "cuando se acredite de manera indubitable el vínculo familiar"
)

DOS_TITULOS = """TITULO I
ALIMENTOS

Articulo 472.- Se entiende por alimentos lo que es indispensable para el sustento del
menor, y comprende habitacion, vestido, educacion y asistencia medica.

TITULO II
PATRIA POTESTAD

Articulo 473.- El mayor de dieciocho anos solo puede pedir alimentos cuando no se
encuentre en aptitud de atender a su subsistencia.
"""


@pytest.fixture(scope="module")
def index():
    return L.build_index(PROCESAL)


def test_chunk_locator_keeps_the_first_article(index):
    found, articles = L.resolve_chunk(index, CHUNK)
    assert found.label == "Art. 561"
    assert articles == ["Art. 561", "Art. 562", "Art. 563"]


def test_excerpt_moves_the_locator_to_the_article_actually_used(index):
    assert L.resolve_excerpt(index, CHUNK, EXCERPT_562).label == "Art. 562"
    assert L.resolve_excerpt(index, CHUNK, EXCERPT_563).label == "Art. 563"


def test_excerpt_keeps_the_full_breadcrumb(index):
    found = L.resolve_excerpt(index, CHUNK, EXCERPT_562)
    assert found.breadcrumb.startswith("Seccion Quinta")
    assert found.breadcrumb.endswith("Art. 562")


def test_excerpt_tolerates_whitespace_and_case_differences(index):
    noisy = "  EL DEMANDANTE GOZA de   Auxilio Judicial sin trámite ni prestar caución  "
    assert L.resolve_excerpt(index, CHUNK, noisy).label == "Art. 562"


def test_excerpt_that_is_not_in_the_chunk_is_rejected(index):
    invented = "Son competentes los jueces de familia para disolver el vinculo matrimonial."
    assert L.resolve_excerpt(index, CHUNK, invented).is_empty()


def test_excerpt_from_another_part_of_the_document_is_rejected(index):
    outside = (
        "Son competentes para conocer los procesos de alimentos los jueces de paz letrado"
    )
    other_chunk = "El demandante goza de Auxilio Judicial sin trámite ni prestar caución juratoria."
    assert L.resolve_excerpt(index, other_chunk, outside).is_empty()


def test_excerpt_too_short_is_rejected(index):
    assert L.resolve_excerpt(index, CHUNK, "El demandante").is_empty()


def test_empty_inputs_are_rejected(index):
    assert L.resolve_excerpt(index, CHUNK, "").is_empty()
    assert L.resolve_excerpt(index, "", EXCERPT_562).is_empty()


def test_falls_back_to_snippet_regex_without_corpus():
    found = L.resolve_excerpt(None, CHUNK, "Articulo 562.- El demandante goza de Auxilio Judicial")
    assert found.label == "Art. 562"
    assert found.source == "snippet_regex"


def test_falls_back_to_snippet_regex_when_the_chunk_is_not_in_the_document(index):
    stale_chunk = "Texto que no aparece en este markdown. Articulo 562.- El demandante goza."
    found = L.resolve_excerpt(index, stale_chunk, "Articulo 562.- El demandante goza.")
    assert found.label == "Art. 562"
    assert found.source == "snippet_regex"


def test_single_article_chunk_reports_one_article(index):
    _, articles = L.resolve_chunk(index, EXCERPT_562)
    assert articles == ["Art. 562"]


def test_article_span_ignores_an_article_that_is_too_far_behind(index, monkeypatch):
    monkeypatch.setattr(settings, "locator_max_article_span", 1)
    _, articles = L.resolve_chunk(index, EXCERPT_563)
    assert articles == []


def test_excerpt_that_crosses_two_articles_cites_both(index):
    found, articles = L.resolve_excerpt_span(index, CHUNK, EXCERPT_562_563)
    assert articles == ["Art. 562", "Art. 563"]
    assert found.label == "Arts. 562 y 563"
    assert found.breadcrumb.endswith("Arts. 562 y 563")
    assert found.breadcrumb.startswith("Seccion Quinta")


def test_single_article_excerpt_reports_only_that_article(index):
    found, articles = L.resolve_excerpt_span(index, CHUNK, EXCERPT_562)
    assert articles == ["Art. 562"]
    assert found.label == "Art. 562"


def test_excerpt_that_crosses_too_many_articles_is_rejected(index, monkeypatch):
    monkeypatch.setattr(settings, "locator_max_combined_articles", 1)
    found, articles = L.resolve_excerpt_span(index, CHUNK, EXCERPT_562_563)
    assert articles == ["Art. 562", "Art. 563"]
    assert found.is_empty()


def test_excerpt_that_crosses_parents_names_every_article_under_the_common_ancestor():
    index = L.build_index(DOS_TITULOS)
    chunk = L.clean_user_text(DOS_TITULOS)
    excerpt = (
        "habitacion, vestido, educacion y asistencia medica. TITULO II PATRIA POTESTAD "
        "Articulo 473.- El mayor de dieciocho anos solo puede pedir alimentos"
    )
    found, articles = L.resolve_excerpt_span(index, chunk, excerpt)
    assert articles == ["Art. 472", "Art. 473"]
    assert found.label == "Arts. 472 y 473"
    assert found.breadcrumb == "Arts. 472 y 473"


MODIFICADO = """# Codigo Civil

TITULO I
ALIMENTOS

**_Articulo 483.-_** _El obligado a prestar alimentos puede pedir que se le exonere de seguir
prestandolos si disminuyen sus ingresos._

**(*) Articulo modificado por el Articulo 1 de la Ley N 27646, publicada el 23 enero 2002, cuyo texto es el siguiente:**

**Causales de exoneracion de alimentos**

**"Articulo 483.-**

El obligado a prestar alimentos puede pedir que se le exonere si disminuyen sus ingresos, de
modo que no pueda atenderla sin poner en peligro su propia subsistencia.
"""


def test_excerpt_across_a_modified_article_cites_it_once():
    index = L.build_index(MODIFICADO)
    chunk = L.clean_user_text(MODIFICADO)
    excerpt = (
        "publicada el 23 enero 2002, cuyo texto es el siguiente: Causales de exoneracion de "
        "alimentos \"Articulo 483.- El obligado a prestar alimentos puede pedir que se le "
        "exonere si disminuyen sus ingresos"
    )
    found, articles = L.resolve_excerpt_span(index, chunk, excerpt)
    assert articles == ["Art. 483"]
    assert found.label == "Art. 483"


def test_snippet_regex_fallback_refuses_a_multi_article_excerpt():
    chunk = (
        "Articulo 562.- El demandante goza de Auxilio Judicial. Articulo 563.- A pedido "
        "de parte el Juez puede prohibir al demandado ausentarse del pais."
    )
    assert L.resolve_excerpt(None, chunk, chunk).is_empty()


NEAR_DUPLICATES = """Artículo 17.- Notificación de la invitación

c) En caso no pueda realizarse la notificación conforme a los literales a) y b) se dejará aviso del día y hora en que se regresará para realizar la diligencia.

Los plazos se computan en días hábiles desde el día siguiente de recibida la solicitud por el centro de conciliación, y el conciliador deja constancia de cada actuación en el expediente del procedimiento, bajo responsabilidad del director del centro.

Artículo 30.- Notificación en el procedimiento

c. En caso no pueda realizarse la notificación conforme a los literales a) y b) se deja aviso del día y hora en que se regresa para realizar la diligencia.
"""


def test_passage_copied_from_another_article_is_not_placed_in_its_near_duplicate():
    index = L.build_index(NEAR_DUPLICATES)
    chunk = NEAR_DUPLICATES[NEAR_DUPLICATES.index("Artículo 30") :]
    excerpt = "se dejará aviso del día y hora en que se regresará para realizar la diligencia"
    assert L.resolve_excerpt(index, chunk, excerpt).is_empty()


def test_passage_with_a_typo_is_still_placed_when_no_verbatim_copy_exists():
    index = L.build_index(NEAR_DUPLICATES)
    chunk = NEAR_DUPLICATES[NEAR_DUPLICATES.index("Artículo 30") :]
    excerpt = "se deja aviso del día y hxra en que se regresa para realizar la diligencia"
    assert L.resolve_excerpt(index, chunk, excerpt).label == "Art. 30"
