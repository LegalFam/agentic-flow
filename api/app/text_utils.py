import re
import unicodedata
from typing import Any

# Cuidado al editar: varias de estas secuencias contienen caracteres invisibles
# (la segunda termina en U+009D, un caracter de control). Copiarlas a mano las rompe.
# El orden importa: las mas largas van antes que sus prefijos.
_MOJIBAKE_REPLACEMENTS = (
    ("�", ""),
    ("â€œ", "\""),
    ("â€", "\""),
    ("â€˜", "'"),
    ("â€™", "'"),
    ("â€“", "-"),
    ("â€”", "-"),
)


def clean_user_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = repair_mojibake(text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def repair_mojibake(text: str) -> str:
    if "Ã" not in text and "Â" not in text and "â" not in text:
        return text
    try:
        repaired = text.encode("latin1").decode("utf-8")
    except UnicodeError:
        repaired = text
    for broken, fixed in _MOJIBAKE_REPLACEMENTS:
        repaired = repaired.replace(broken, fixed)
    return repaired
