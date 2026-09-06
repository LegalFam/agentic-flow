"""Un chunk de File Search no respeta el articulado.

El chunk que motivo esto abarcaba los articulos 561, 562 y 563 del Codigo Procesal Civil:
el locator se quedaba con el 561 por ser el primero, mientras la respuesta se apoyaba en
el 562. La cita salia bien redactada y con el articulo equivocado.

La ubicacion se recalcula ahora sobre el fragmento que el agente XAI declara haber usado
(`original_snippet`), verificado contra el chunk guardado al recuperarlo.
"""

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

# Lo que devuelve File Search: arranca en el 561 y termina dentro del 563.
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

# El excerpt tampoco respeta el articulado: el agente copio desde el final del 562 y
# siguio dentro del 563. Atribuirlo al 562 por ser el primero es el mismo error que el
# excerpt vino a corregir, un nivel mas abajo.
EXCERPT_562_563 = (
    "La resolución que lo concede es inimpugnable. Articulo 563.- A pedido de parte y "
    "cuando se acredite de manera indubitable el vínculo familiar"
)

# Dos articulos que no comparten padre: el tramo cruza de un titulo al siguiente.
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
    """El comportamiento anterior, que es justo lo que el excerpt viene a corregir."""
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
    """Aunque exista en el documento: si no salio del chunk, el agente lo invento."""
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
    """Corpus desincronizado: el chunk no existe tal cual, pero el excerpt se verifico."""
    stale_chunk = "Texto que no aparece en este markdown. Articulo 562.- El demandante goza."
    found = L.resolve_excerpt(index, stale_chunk, "Articulo 562.- El demandante goza.")
    assert found.label == "Art. 562"
    assert found.source == "snippet_regex"


def test_single_article_chunk_reports_one_article(index):
    _, articles = L.resolve_chunk(index, EXCERPT_562)
    assert articles == ["Art. 562"]


def test_article_span_ignores_an_article_that_is_too_far_behind(index, monkeypatch):
    """Mismo criterio que build_locator: un articulo lejano ya no gobierna el texto."""
    monkeypatch.setattr(settings, "locator_max_article_span", 1)
    _, articles = L.resolve_chunk(index, EXCERPT_563)
    assert articles == []


# --- excerpt que cruza articulos -------------------------------------------------

def test_excerpt_that_crosses_two_articles_cites_both(index):
    """La cita real: el fragmento arranca en el 562 y termina dentro del 563."""
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


def test_excerpt_that_crosses_parents_is_rejected():
    """Cruzar de un titulo a otro ya no ubica nada: mejor una cita sin ubicacion."""
    index = L.build_index(DOS_TITULOS)
    chunk = L.clean_user_text(DOS_TITULOS)
    excerpt = (
        "habitacion, vestido, educacion y asistencia medica. TITULO II PATRIA POTESTAD "
        "Articulo 473.- El mayor de dieciocho anos solo puede pedir alimentos"
    )
    found, articles = L.resolve_excerpt_span(index, chunk, excerpt)
    assert articles == ["Art. 472", "Art. 473"]
    assert found.is_empty()


def test_snippet_regex_fallback_refuses_a_multi_article_excerpt():
    """Sin corpus no hay forma de saber donde termina un articulo: no se elige ninguno."""
    chunk = (
        "Articulo 562.- El demandante goza de Auxilio Judicial. Articulo 563.- A pedido "
        "de parte el Juez puede prohibir al demandado ausentarse del pais."
    )
    assert L.resolve_excerpt(None, chunk, chunk).is_empty()
