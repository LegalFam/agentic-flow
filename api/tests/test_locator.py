import pytest

from app import locator as L

CODIGO = """# Codigo Civil del Peru

LIBRO III
DERECHO DE FAMILIA

SECCION SEGUNDA
SOCIEDAD CONYUGAL

TITULO IV
DECAIMIENTO Y DISOLUCION DEL VINCULO

CAPITULO PRIMERO
Separacion de cuerpos

Articulo 332.- La separacion de cuerpos suspende los deberes relativos al lecho y
habitacion y pone fin al regimen patrimonial de sociedad de gananciales, dejando
subsistente el vinculo matrimonial.

Articulo 333.- Son causas de separacion de cuerpos:
1. El adulterio.
2. La violencia fisica o psicologica, que el juez apreciara segun las circunstancias.
3. El atentado contra la vida del conyuge.

TITULO V
DISPOSICIONES GENERALES

Articulo 340.- Al declararse la separacion se aplica lo dispuesto en este titulo.
"""

OCR = """## Pagina 1

Resolucion administrativa sobre pension de alimentos.

## Pagina 12

Articulo 481.- La pension alimenticia se regula por el juez en proporcion a las
necesidades de quien los pide y a las posibilidades del que debe darlos.
"""


@pytest.fixture(scope="module")
def index():
    return L.build_index(CODIGO)


# --- mapa de offsets -------------------------------------------------------------

def test_collapse_maps_back_to_base_positions():
    base = "Articulo 1.-\n\n   Texto   con    espacios\n"
    collapsed, offsets = L.collapse_with_map(base)
    assert collapsed == "Articulo 1.- Texto con espacios"
    assert len(collapsed) == len(offsets)
    # Cada indice colapsado debe apuntar al mismo caracter en el texto base.
    for position, char in enumerate(collapsed):
        if char != " ":
            assert base[offsets[position]] == char


def test_collapse_strips_leading_and_trailing_whitespace():
    collapsed, offsets = L.collapse_with_map("\n\n  hola  \n\n")
    assert collapsed == "hola"
    assert len(offsets) == 4


def test_collapse_handles_empty_document():
    collapsed, offsets = L.collapse_with_map("")
    assert collapsed == ""
    assert len(offsets) == 0


def test_offset_map_is_a_compact_array():
    """list[int] costaba ~36 bytes por caracter; array('i') cuesta 4."""
    from array import array

    _, offsets = L.collapse_with_map("Articulo 1.- texto")
    assert isinstance(offsets, array)
    assert offsets.itemsize == 4


def test_fold_preserves_length():
    text = "ARTÍCULO 333 Ñandú"
    assert len(L.fold(text)) == len(text)


# --- deteccion de encabezados ----------------------------------------------------

def test_detects_full_legal_hierarchy(index):
    kinds = [heading.kind for heading in index.headings]
    for expected in ("libro", "seccion", "titulo", "capitulo", "articulo"):
        assert expected in kinds


def test_two_line_heading_keeps_the_number(index):
    # "TITULO V" + "DISPOSICIONES GENERALES" es un solo encabezado, no dos del mismo nivel.
    titulos = [h.label for h in index.headings if h.kind == "titulo"]
    assert "Titulo V - Disposiciones generales" in titulos


def test_all_caps_names_are_sentence_cased(index):
    libros = [h.label for h in index.headings if h.kind == "libro"]
    assert libros == ["Libro III - Derecho de familia"]


def test_article_in_running_prose_is_not_a_heading():
    index = L.build_index("Texto que menciona el articulo 333 dentro de una oracion larga.\n")
    assert [h for h in index.headings if h.kind == "articulo"] == []


def test_detects_ocr_page_markers():
    index = L.build_index(OCR)
    pages = [h.page for h in index.headings if h.kind == "pagina"]
    assert pages == [1, 12]


# --- cascada de resolucion -------------------------------------------------------

def test_exact_match(index):
    found = L.resolve(index, "Son causas de separacion de cuerpos: 1. El adulterio.")
    assert found.source == "exact"
    assert found.label == "Art. 333"


def test_match_across_line_breaks(index):
    # El snippet llega con el whitespace ya colapsado; el markdown tiene saltos de linea.
    found = L.resolve(index, "suspende los deberes relativos al lecho y habitacion y pone fin al regimen")
    assert found.source == "exact"
    assert found.label == "Art. 332"


def test_prefix_match_when_snippet_is_truncated(index):
    truncated = "La separacion de cuerpos suspende los deberes relativos al lecho y habitacion y pone fin XXX"
    found = L.resolve(index, truncated)
    assert found.source in {"prefix", "fuzzy"}
    assert found.label == "Art. 332"


def test_fuzzy_match_tolerates_small_drift(index):
    drifted = (
        "Son causas de la separacion de los cuerpos: 1. El aduterio. "
        "2. La violencia fisica o psicologica que el juez apreciara"
    )
    found = L.resolve(index, drifted)
    assert found.source == "fuzzy"
    assert found.label == "Art. 333"


def test_higher_level_heading_closes_lower_ones(index):
    # Art. 340 esta bajo TITULO V, no debe arrastrar el CAPITULO PRIMERO del titulo anterior.
    found = L.resolve(index, "Al declararse la separacion se aplica lo dispuesto en este titulo.")
    assert "Titulo V" in found.breadcrumb
    assert "Capitulo Primero" not in found.breadcrumb


def test_breadcrumb_is_ordered_from_general_to_specific(index):
    found = L.resolve(index, "Son causas de separacion de cuerpos: 1. El adulterio.")
    parts = found.breadcrumb.split(" > ")
    assert parts[0].startswith("Libro")
    assert parts[-1] == "Art. 333"


def test_page_is_reported_for_ocr_documents():
    index = L.build_index(OCR)
    found = L.resolve(index, "La pension alimenticia se regula por el juez en proporcion")
    assert found.page == 12
    assert found.label == "Art. 481"


def test_unmatched_snippet_without_article_reference_yields_nothing(index):
    found = L.resolve(index, "Texto que no aparece en ninguna parte de este documento.")
    assert found.is_empty()
    assert found.source == "none"


def test_empty_snippet_is_safe(index):
    assert L.resolve(index, "").is_empty()
    assert L.resolve(index, "    ").is_empty()


# --- fallback sin corpus ---------------------------------------------------------

def test_snippet_regex_fallback_without_index():
    found = L.resolve(None, "Articulo 333.- Son causas de separacion de cuerpos.")
    assert found.source == "snippet_regex"
    assert found.label == "Art. 333"


def test_snippet_regex_attaches_unambiguous_inciso():
    found = L.resolve(None, "Conforme al articulo 481 y su inciso 2, la pension se fija por el juez.")
    assert found.label == "Art. 481, inc. 2"


def test_snippet_regex_skips_ambiguous_inciso():
    found = L.resolve(None, "Segun el articulo 333, inciso 1 y el inciso 3 de la misma norma.")
    assert found.label == "Art. 333"


def test_snippet_regex_without_reference_is_empty():
    assert L.resolve(None, "Un texto sin ninguna referencia normativa.").is_empty()


def test_far_away_article_is_not_attributed(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "locator_max_article_span", 50)
    index = L.build_index(OCR)
    # El texto de la pagina 12 esta a mas de 50 caracteres del encabezado del articulo.
    found = L.resolve(index, "a las posibilidades del que debe darlos.")
    assert found.label != "Art. 481"


def test_markdown_heading_is_labelled_apart():
    """En resoluciones sin articulado el encabezado markdown no es una ubicacion juridica:
    se devuelve, pero etiquetado para que el consumidor pueda descartarlo."""
    resolucion = (
        "# Casacion 864-2014 Ica\n\n"
        "## Impugnacion de reconocimiento de paternidad\n\n"
        "La Sala Civil Transitoria de la Corte Suprema de Justicia de la Republica, "
        "vista la causa en audiencia publica de la fecha, emite la siguiente sentencia.\n"
    )
    index = L.build_index(resolucion)
    found = L.resolve(index, "vista la causa en audiencia publica de la fecha")
    assert found.source == "markdown_heading"
    assert found.label == "Impugnacion de reconocimiento de paternidad"


def test_legal_hierarchy_keeps_its_own_source(index):
    # Un documento con articulado conserva la estrategia de busqueda como source.
    found = L.resolve(index, "Son causas de separacion de cuerpos: 1. El adulterio.")
    assert found.source == "exact"


def test_bold_italic_article_heading_is_detected():
    """El Codigo Civil real usa "**_Articulo 333.- ...". Cuando el patron solo aceptaba
    asteriscos, 223 articulos quedaban invisibles y su texto se atribuia al articulo
    anterior: un error de una unidad, con total confianza."""
    doc = (
        "**Artículo 332.-**\n\n"
        "La separación de cuerpos suspende los deberes relativos al lecho y habitación.\n\n"
        "**_Artículo 333.- Son causas de separación de cuerpos:_**\n\n"
        "_1. El adulterio._\n"
    )
    index = L.build_index(doc)
    assert L.resolve(index, "El adulterio").label == "Art. 333"
    assert L.resolve(index, "suspende los deberes relativos al lecho").label == "Art. 332"


def test_article_number_with_spaced_letter_suffix():
    doc = "**Artículo 659 F.- Designación de apoyos a futuro**\n\nEl apoyo se designa por escritura.\n"
    index = L.build_index(doc)
    found = L.resolve(index, "El apoyo se designa por escritura")
    assert found.label == "Art. 659 F"


def test_prose_reference_to_an_article_is_still_not_a_heading():
    """Ampliar el enfasis no debe convertir una cita en prosa en un encabezado."""
    for prose in (
        "Artículo 326 del Código Civil.",
        "_artículo 402, inciso 4, cuando fueren varios los autores._",
        "**Artículo 2 de la Resolución N°**",
        "artículo 44 en los numerales 4 al 7 sin declaración judicial.",
        # Las mismas referencias en prosa, ahora con las decoraciones que el prefijo
        # aprendio a aceptar: lo que las deja fuera es el separador, no el prefijo.
        "\"Artículo 23 de este Código.\"",
        "“artículo 8, de conformidad con la ley de la materia” .",
        "- Artículo 310, en lo que fuera aplicable.",
        "## \"Artículo 465 El juez puede autorizar a los hijos.",
    ):
        assert L._ARTICULO_RE.match(prose) is None, prose


def test_quoted_article_heading_is_detected():
    """El texto unico ordenado transcribe entre comillas los articulos sustituidos por
    leyes posteriores. Con el prefijo antiguo esos 595 encabezados se perdian y su texto
    pasaba a colgar del articulo anterior: el Art. 345-A del Codigo Civil se leia como 345."""
    doc = (
        "**Artículo 345.- Patria potestad en separación convencional**\n\n"
        "En caso de separación convencional el juez fija el régimen.\n\n"
        "**\"Artículo 345-A.- Indemnización en caso de perjuicio\"**\n\n"
        "El juez velará por la estabilidad económica del cónyuge perjudicado.\n"
    )
    index = L.build_index(doc)
    assert L.resolve(index, "velará por la estabilidad económica").label == "Art. 345-A"
    assert L.resolve(index, "el juez fija el régimen").label == "Art. 345"


@pytest.mark.parametrize(
    "heading",
    (
        '**“ Artículo 21.- Regulación de la capacidad jurídica**',
        '**"Artículo 345-A.- Indemnización en caso de perjuicio"**',
        '## "Artículo 7. Sujetos de protección de la Ley',
        "## 'Artículo 6.- Carácter obligatorio",
        '**“** **Artículo 66.- Falta del representante**',
        '#       "Artículo 386. Procedencia',
        "- Artículo 93º",
        '- "Artículo 22.- Requisitos para ser acreditado como conciliador',
    ),
)
def test_decorated_article_headings_from_the_corpus(heading):
    """Formas reales del corpus, una por documento de origen."""
    assert L._ARTICULO_RE.match(heading) is not None


def test_bulleted_article_heading_is_detected():
    """El Codigo de los Ninos y Adolescentes numera con vineta y pone el nombre debajo."""
    doc = (
        "- Artículo 92º\n\n"
        "## Definición\n\n"
        "Se considera alimentos lo necesario para el sustento.\n\n"
        "- Artículo 93º\n\n"
        "## Obligados a prestar alimentos\n\n"
        "Es obligación de los padres prestar alimentos a sus hijos.\n"
    )
    index = L.build_index(doc)
    assert L.resolve(index, "obligación de los padres prestar alimentos").label == "Art. 93º"
    assert L.resolve(index, "lo necesario para el sustento").label == "Art. 92º"
