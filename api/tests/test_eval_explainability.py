"""Suficiencia, comprensibilidad, accionabilidad y fidelidad causal de la explicacion."""

import pytest

from eval import explainability


# --------------------------------------------------------------------------------------
# Legibilidad


def test_syllable_count_handles_diphthongs_and_hiatus():
    assert explainability._syllables("agua") == 2       # a-gua, diptongo
    assert explainability._syllables("caos") == 2       # ca-os, hiato de fuertes
    assert explainability._syllables("dia") == 1        # dia, diptongo
    assert explainability._syllables("día") == 2        # di-a, hiato por tilde
    assert explainability._syllables("pension") == 2
    assert explainability._syllables("alimentos") == 4


def test_short_text_has_no_readability_score():
    """Los indices no significan nada en una frase de ocho palabras."""
    assert explainability.readability("La pension se fija segun el caso.") is None


def test_plain_language_scores_higher_than_legalese():
    legal = (
        "Los alimentos se regulan por el juez en proporcion a las necesidades de quien "
        "los pide y a las posibilidades del que debe darlos, atendiendo ademas a las "
        "circunstancias personales de ambos, especialmente a las obligaciones a que se "
        "halle sujeto el deudor."
    )
    plain = (
        "El juez mira dos cosas. Primero, cuanto necesita tu hijo. Segundo, cuanto puede "
        "pagar el padre. Con eso fija el monto. No hay una tabla fija para todos los casos."
    )
    assert explainability.readability(plain)["szigriszt"] > explainability.readability(legal)["szigriszt"]


def test_readability_gap_rewards_a_summary_that_simplifies():
    citation = {
        "original_snippet": (
            "Los alimentos se regulan por el juez en proporcion a las necesidades de quien "
            "los pide y a las posibilidades del que debe darlos, atendiendo ademas a las "
            "circunstancias personales de ambos."
        ),
        "summary_snippet": (
            "Este articulo dice que el juez fija el monto mirando dos cosas: lo que hace "
            "falta para el menor y lo que puede pagar el padre o la madre."
        ),
    }
    gap = explainability.readability_gap([citation])
    assert gap["pairs"] == 1
    assert gap["gap"] > 0


def test_readability_gap_is_none_without_usable_pairs():
    assert explainability.readability_gap([{"original_snippet": "corto", "summary_snippet": ""}]) is None


# --------------------------------------------------------------------------------------
# Suficiencia


def test_normative_claim_needs_both_a_duty_marker_and_legal_vocabulary():
    claims = explainability.normative_claims(
        "El juez debe fijar la pension segun las posibilidades del obligado. "
        "Puedes reunir tus recibos y ordenarlos por fecha antes de la audiencia."
    )
    assert len(claims) == 1
    assert "pension" in claims[0]


def test_practical_advice_alone_is_not_a_normative_claim():
    """Sin esta condicion, un consejo practico inflaria el denominador de la densidad."""
    assert explainability.normative_claims(
        "Puedes reunir los documentos que tengas. Debes guardarlos en un lugar seguro."
    ) == []


def test_support_density_counts_claims_backed_by_the_cited_article():
    message = (
        "El juez debe fijar la pension de alimentos en proporcion a las necesidades de "
        "quien los pide y a las posibilidades del que debe darlos."
    )
    article = (
        "Articulo 481.- Los alimentos se regulan por el juez en proporcion a las "
        "necesidades de quien los pide y a las posibilidades del que debe darlos."
    )
    result = explainability.support_density(message, [{"locator": "Art. 481"}], [article])
    assert result["claims"] == 1
    assert result["density"] == 1.0


def test_support_density_is_zero_when_the_citation_is_about_something_else():
    message = (
        "El juez debe fijar la tenencia del menor considerando su opinion y el tiempo de "
        "convivencia con cada progenitor."
    )
    unrelated = (
        "Articulo 318.- Fenece el regimen de la sociedad de gananciales por invalidacion "
        "del matrimonio, separacion de patrimonios, divorcio o muerte de un conyuge."
    )
    result = explainability.support_density(message, [{"locator": "Art. 318"}], [unrelated])
    assert result["claims"] == 1
    assert result["density"] == 0.0


def test_support_density_is_none_without_normative_claims():
    """Un saludo no tiene afirmaciones que respaldar: cero seria un juicio, no un dato."""
    result = explainability.support_density("Hola, buenos dias.", [], [])
    assert result["claims"] == 0
    assert result["density"] is None


# --------------------------------------------------------------------------------------
# Accionabilidad


def test_steps_starting_with_an_action_verb_count_as_actionable():
    result = explainability.actionability(
        ["Reune las boletas de pago de los ultimos seis meses", "Ordena los recibos por fecha"],
        [],
        expects_referral=False,
    )
    assert result["actionable_rate"] == 1.0


def test_vague_step_is_not_actionable():
    result = explainability.actionability(
        ["Es importante que tengas claro tu caso"], [], expects_referral=False
    )
    assert result["actionable"] == 0


def test_step_that_repeats_a_clarifying_question_is_flagged():
    result = explainability.actionability(
        ["Confirma si el padre tiene ingresos fijos declarados"],
        ["¿El padre tiene ingresos fijos declarados?"],
        expects_referral=False,
    )
    assert result["duplicated"] == 1


def test_institutional_referral_without_an_emergency_is_flagged():
    """Derivar toda consulta ordinaria a una comisaria vacia la senal para las urgentes."""
    result = explainability.actionability(
        ["Acude a la comisaria mas cercana"], [], expects_referral=False
    )
    assert result["generic_referral"] == 1


def test_institutional_referral_is_fine_when_the_case_calls_for_it():
    result = explainability.actionability(
        ["Acude al CEM mas cercano para pedir medidas de proteccion"], [], expects_referral=True
    )
    assert result["generic_referral"] == 0


def test_no_steps_reports_none_instead_of_zero():
    assert explainability.actionability([], [], expects_referral=False)["actionable_rate"] is None


# --------------------------------------------------------------------------------------
# Fidelidad causal


def _response(message: str, locators: list[str]) -> dict:
    return {
        "message": message,
        "citations": [{"file_name": "Codigo Civil", "locator": item} for item in locators],
    }


def test_stable_answer_with_stable_citations_is_not_decorative():
    responses = [_response("La pension se fija en proporcion.", ["Art. 481"])] * 3
    result = explainability.stability(responses)
    assert result["citation_similarity"] == 1.0
    assert result["decorative"] is False


def test_stable_answer_with_shifting_citations_is_decorative():
    """El sintoma de una cita que acompana en vez de sostener: la respuesta no cambia y
    la cita si."""
    responses = [
        _response("La pension se fija en proporcion a las necesidades del menor.", ["Art. 481"]),
        _response("La pension se fija en proporcion a las necesidades del menor.", ["Art. 92"]),
        _response("La pension se fija en proporcion a las necesidades del menor.", ["Art. 472"]),
    ]
    result = explainability.stability(responses)
    assert result["answer_similarity"] >= explainability.ANSWER_STABLE
    assert result["citation_similarity"] <= explainability.CITATIONS_UNSTABLE
    assert result["decorative"] is True


def test_changing_answer_is_not_judged_decorative():
    """Si la respuesta tambien cambia, que la cita cambie no dice nada."""
    responses = [
        _response("La pension se fija en proporcion a lo que necesita el menor.", ["Art. 481"]),
        _response("Para pedir tenencia hay que acudir al juzgado de familia del distrito.", ["Art. 81"]),
    ]
    assert explainability.stability(responses)["decorative"] is False


def test_answer_without_citations_is_never_decorative():
    responses = [_response("Orientacion general sin fuentes.", [])] * 3
    assert explainability.stability(responses)["decorative"] is False


def test_single_response_cannot_be_compared():
    assert explainability.stability([_response("una sola", ["Art. 481"])]) is None


# --------------------------------------------------------------------------------------
# Coincidencia semantica


def test_literal_match_needs_no_embedding():
    """Si la palabra esta, el concepto esta: no se gasta una llamada a la API."""
    from eval import semantic

    assert semantic.mentions("Se fija en proporción a las necesidades", "proporción") is True


def test_literal_match_ignores_accents():
    from eval import semantic

    assert semantic.mentions("Se fija en PROPORCION al ingreso", "proporción") is True


def test_without_a_proposition_it_stays_literal():
    """La comparacion semantica sobre el termino suelto separa mal (margen 0.03 medido),
    asi que no se aplica: sin proposicion en el dataset, coincidencia literal."""
    from eval import semantic

    assert semantic.mentions("la obligación recae en los abuelos", "ascendientes") is False


def test_sentences_drops_fragments_too_short_to_compare():
    from eval import semantic

    pieces = semantic.sentences("Sí. El juez fija el monto mirando las necesidades del menor.")
    assert all(len(p.split()) >= semantic.MIN_WORDS for p in pieces)
