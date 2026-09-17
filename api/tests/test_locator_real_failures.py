from app import locator as L
from app.config import settings

CODIGO = """**TITULO I**

**Alimentos**

**Noción de alimentos**

**_Artículo 472.-_** _Se entiende por alimentos lo que es indispensable para el sustento,
habitación, vestido y asistencia médica, según la situación y posibilidades de la familia._

**Artículo 473.-  El mayor de dieciocho años sólo tiene derecho a alimentos cuando no se
encuentre en aptitud de atender a su subsistencia por causas de incapacidad física o mental.**

**Artículo 474.-  Se deben alimentos recíprocamente los cónyuges.**
"""

CHUNK_WITH_MARKUP = " ".join(CODIGO.split())


def _index():
    return L.build_index(CODIGO)


def test_plain_excerpt_is_located_against_a_chunk_with_markup():
    excerpt = (
        "Artículo 472.- Se entiende por alimentos lo que es indispensable para el sustento, "
        "habitación, vestido y asistencia médica, según la situación y posibilidades de la familia."
    )
    found, articles = L.resolve_excerpt_span(_index(), CHUNK_WITH_MARKUP, excerpt)
    assert articles == ["Art. 472"]
    assert found.label == "Art. 472"
    assert found.source == "exact"


def test_excerpt_ending_at_the_article_boundary_does_not_take_the_next_article():
    excerpt = (
        "El mayor de dieciocho años sólo tiene derecho a alimentos cuando no se encuentre en "
        "aptitud de atender a su subsistencia por causas de incapacidad física o mental."
    )
    found, articles = L.resolve_excerpt_span(_index(), CHUNK_WITH_MARKUP, excerpt)
    assert articles == ["Art. 473"]
    assert found.label == "Art. 473"


def test_excerpt_starting_at_a_heading_does_not_take_the_previous_article():
    excerpt = "Artículo 474.- Se deben alimentos recíprocamente los cónyuges."
    found, _ = L.resolve_excerpt_span(_index(), CHUNK_WITH_MARKUP, excerpt)
    assert found.label == "Art. 474"


def test_sumilla_above_the_article_belongs_to_it():
    excerpt = "Noción de alimentos Artículo 472.- Se entiende por alimentos lo que es indispensable"
    found, articles = L.resolve_excerpt_span(_index(), CHUNK_WITH_MARKUP, excerpt)
    assert articles == ["Art. 472"]
    assert found.label == "Art. 472"


MODIFICADO = """**Artículo 348.-  El divorcio disuelve el vínculo del matrimonio.**

**_Artículo 349.- Puede demandarse el divorcio por las causales señaladas en el artículo 333, incisos 1 al 10.(*)_**

**(*) Artículo modificado por el Artículo 5 de la Ley N° 27495, publicada el 07 julio 2001, cuyo texto es el siguiente:**

**"Artículo 349.- Causales de divorcio**

Puede demandarse el divorcio por las causales señaladas en el artículo 333, incisos del 1 al 12.

**Artículo 350.-  Por el divorcio cesa la obligación alimenticia entre marido y mujer.**
"""


def test_short_heading_excerpt_in_a_chunk_with_a_duplicated_article():
    index = L.build_index(MODIFICADO)
    chunk = " ".join(MODIFICADO.split())
    found, articles = L.resolve_excerpt_span(index, chunk, "Artículo 348.- El divorcio disuelve el vínculo del matrimonio.")
    assert articles == ["Art. 348"]
    assert found.label == "Art. 348"


def test_passage_repeated_in_two_articles_cannot_be_attributed_by_the_evaluator_to_the_wrong_one():
    index = L.build_index(MODIFICADO)
    readings = L.passage_readings(
        index, "Puede demandarse el divorcio por las causales señaladas en el artículo 333"
    )
    assert ["Art. 349"] in readings
    assert all(reading == ["Art. 349"] for reading in readings)


def test_numbers_are_never_matched_approximately():
    doc = (
        "**Artículo 58.- Texto del artículo cincuenta y ocho sobre la representación procesal.**\n\n"
        "CONCORDANCIAS AL ARTÍCULO 58 DEL CÓDIGO PROCESAL CIVIL SENTENCIA EN CASACIÓN DE LA "
        "CORTE SUPREMA DE JUSTICIA CAS. 3248-2013-LIMA\n"
    )
    index = L.build_index(doc)
    chunk = " ".join(doc.split())
    invented = (
        "CONCORDANCIAS AL ARTÍCULO 387 DEL CÓDIGO PROCESAL CIVIL SENTENCIA EN CASACIÓN DE LA "
        "CORTE SUPREMA DE JUSTICIA CAS. 5123-2007-LIMA"
    )
    assert L.resolve_excerpt_span(index, chunk, invented)[0].is_empty()


def test_shuffled_words_are_not_accepted_as_coming_from_the_chunk():
    index = _index()
    words = "Se entiende por alimentos lo que es indispensable para el sustento habitación vestido".split()
    shuffled = " ".join(reversed(words))
    assert L.resolve_excerpt_span(index, CHUNK_WITH_MARKUP, shuffled)[0].is_empty()


def test_small_typos_still_locate_the_right_article():
    excerpt = (
        "El mayor de dieciocho anos solo tiene derecho a alimentos cuando no se encuentre en "
        "aptitud de atender a su subsistensia por causas de incapacidad fisica o mental"
    )
    found, _ = L.resolve_excerpt_span(_index(), CHUNK_WITH_MARKUP, excerpt)
    assert found.label == "Art. 473"


def test_excerpt_with_an_ellipsis_is_located_by_both_ends():
    excerpt = "Se entiende por alimentos lo que es indispensable ... según la situación y posibilidades de la familia."
    found, _ = L.resolve_excerpt_span(_index(), CHUNK_WITH_MARKUP, excerpt)
    assert found.label == "Art. 472"


NINOS = """## CAPÍTULO II TENENCIA DEL NIÑO Y DEL ADOLESCENTE

## Artículo 84º

## Facultad del juez

En caso de disponer la tenencia exclusiva, el Juez debe señalar un régimen de visitas.

## CAPÍTULO II TENENCIA DEL NIÑO Y DEL ADOLESCENTE

- (*)  Artículo  modificado  por  el  Artículo  2  de  la  Ley  Nº

<!-- image -->

## Artículo 85º

## Opinión

El juez especializado debe escuchar la opinión del niño.
"""


def test_modification_note_is_not_taken_as_the_chapter_name():
    index = L.build_index(NINOS)
    chunk = L.clean_user_text(NINOS)
    excerpt = (
        "el Juez debe señalar un régimen de visitas. CAPÍTULO II TENENCIA DEL NIÑO Y DEL ADOLESCENTE "
        "Artículo 85º Opinión El juez especializado debe escuchar la opinión del niño."
    )
    found, articles = L.resolve_excerpt_span(index, chunk, excerpt)
    assert articles == ["Art. 84", "Art. 85"]
    assert found.label == "Arts. 84 y 85"
    assert found.breadcrumb == "Capitulo II tenencia del niño y del adolescente > Arts. 84 y 85"


def test_wrapped_prose_line_is_not_a_structural_heading():
    doc = (
        "**Artículo 816.- Son herederos del primer orden los hijos, salvo lo previsto en la\n"
        "disposición que lo instituye.**\n\n"
        "Los herederos del segundo orden son los padres, conforme a la\n"
        "Titulo II de la SECCION SEGUNDA de este Código.\n"
        "y a lo que dispone la ley especial.\n"
    )
    index = L.build_index(doc)
    assert [h.kind for h in index.headings if h.level < L.LEVEL_ARTICULO] == []
    assert L.resolve(index, "y a lo que dispone la ley especial").label == "Art. 816"


def test_titulo_in_the_sense_of_a_deed_is_not_a_structural_heading():
    doc = (
        "**Artículo 2011.- Los registradores califican la legalidad de los documentos.**\n\n"
        "**Titulo que da mérito a la inscripción**\n\n"
        "Lo dispuesto en el parrafo anterior no se aplica cuando se trate de parte notarial.\n"
    )
    index = L.build_index(doc)
    assert [h.kind for h in index.headings if h.level < L.LEVEL_ARTICULO] == []
    assert L.resolve(index, "no se aplica cuando se trate de parte notarial").label == "Art. 2011"


def test_numbered_structural_headings_are_still_detected():
    for line, kind in (
        ("**TITULO II**", "titulo"),
        ("CAPITULO PRIMERO", "capitulo"),
        ("## Título Preliminar", "titulo"),
        ("SECCION QUINTA", "seccion"),
        ("SUBCAPITULO 1", "subcapitulo"),
        ("LIBRO III", "libro"),
    ):
        heading = L._classify_line(line, 0)
        assert heading is not None and heading.kind == kind, line


def test_article_span_limit_still_applies(monkeypatch):
    monkeypatch.setattr(settings, "locator_max_article_span", 10)
    index = _index()
    found, articles = L.resolve_excerpt_span(
        index, CHUNK_WITH_MARKUP, "según la situación y posibilidades de la familia."
    )
    assert articles == []
    assert "Art." not in found.label


ALIMENTOS_PROCESS = """## Artículo 168º (*)

## Traslado de la demanda

Admitida la demanda, el Juez dará por ofrecidos los  medios  probatorios  y  correrá  traslado  de  ella al  demandado,  con  conocimiento  del  Fiscal,  por el término perentorio de cinco (5) días para que el demandado la conteste.

En el  proceso  de  alimentos,  el  Juez  no  admite  la contestación  de  la  demanda  si  el  demandado  no cumple lo establecido en el literal b) del artículo 167-A  y  ejecuta  el  apercibimiento,  continuando con el proceso.

## Artículo 169º

## Tachas u oposiciones

Las  tachas  u  oposiciones  que  se  formulen  deben acreditarse con medios probatorios y actuarse durante la audiencia única.

## CAPÍTULO II PROCESO ÚNICO

## Artículo 170º

## Audiencia (*)

Contestada la  demanda o transcurrido el término para su contestación, el Juez fijará una fecha inaplazable  para  la  audiencia.

## Artículo 170-A

## Audiencia única (*)

En los procesos de alimentos, la audiencia única se rige por las siguientes reglas:

a) El Juez puede realizar la audiencia única de manera presencial o virtual, privilegiando en todos los casos la vigencia de los principios de oralidad.
"""


def test_ellipsis_does_not_take_the_articles_skipped_between_segments():
    excerpt = (
        "alimentos, el Juez no admite la contestación de la demanda si el demandado no cumple lo "
        "establecido en el literal b) del artículo 167-A y ejecuta el apercibimiento, continuando "
        "con el proceso. [...] Artículo 170-A Audiencia única (*) En los procesos de alimentos, la "
        "audiencia única se rige por las siguientes reglas: a) El Juez puede realizar la audiencia "
        "única de manera presencial o virtual"
    )
    _, articles = L.resolve_excerpt_span(L.build_index(ALIMENTOS_PROCESS), " ".join(ALIMENTOS_PROCESS.split()), excerpt)
    assert articles == ["Art. 168", "Art. 170-A"]
