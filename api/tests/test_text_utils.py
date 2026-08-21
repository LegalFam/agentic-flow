from app.text_utils import clean_user_text, repair_mojibake


def test_none_and_empty():
    assert clean_user_text(None) == ""
    assert clean_user_text("") == ""
    assert clean_user_text("   ") == ""


def test_collapses_whitespace_and_strips():
    assert clean_user_text("  hola   mundo \n\t legal  ") == "hola mundo legal"


def test_replaces_hard_space():
    assert clean_user_text("pension alimenticia") == "pension alimenticia"


def test_removes_control_characters():
    assert clean_user_text("art\x07iculo\x00 333") == "articulo 333"


def test_preserves_accents():
    assert clean_user_text("Artículo 333 de la Sección Segunda") == "Artículo 333 de la Sección Segunda"


def test_repairs_mojibake_roundtrip():
    broken = "café".encode("utf-8").decode("latin1")
    assert repair_mojibake(broken) == "café"


def test_leaves_clean_text_untouched():
    # El guard de la funcion evita tocar texto que no tiene marcadores de mojibake.
    assert repair_mojibake("Sección Segunda") == "Sección Segunda"


def test_non_string_input_is_coerced():
    assert clean_user_text(333) == "333"
