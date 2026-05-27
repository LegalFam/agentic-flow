import unicodedata


CATEGORY_TREE: dict[str, list[str]] = {
    "Vinculos Conyugales y Patrimoniales": [
        "Divorcio y Separacion",
        "Union de Hecho",
        "Sociedad de Gananciales",
    ],
    "Relaciones Paterno-Filiales y Menores": [
        "Filiacion e Identidad",
        "Patria Potestad",
        "Tenencia y Custodia",
        "Pension de Alimentos",
        "Adopcion",
    ],
    "Proteccion a Personas Vulnerables": [
        "Violencia Familiar",
        "Centro Emergencia Mujer",
        "Proteccion de Ninos y Adolescentes",
        "Tutela y Curatela",
        "Capacidad Juridica",
    ],
    "Sucesiones y Herencia": [],
    "Procesos Legales y Resolucion Alternativa": [
        "Conciliacion",
        "Procedimiento Civil",
    ],
    "Generales": [
        "Derecho de Familia",
        "Otros",
    ],
}


def _normalize_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join(without_accents.casefold().split())


_CATEGORY_ALIASES = {
    _normalize_label(category): category
    for category in CATEGORY_TREE
}

_SUBCATEGORY_ALIASES = {
    (category, _normalize_label(subcategory)): subcategory
    for category, subcategories in CATEGORY_TREE.items()
    for subcategory in subcategories
}


def category_catalog_text() -> str:
    lines: list[str] = []
    for category, subcategories in CATEGORY_TREE.items():
        if subcategories:
            lines.append(f"- {category}: {', '.join(subcategories)}")
        else:
            lines.append(f"- {category}: sin subcategoria")
    return "\n".join(lines)


def validate_category_pair(category: str, subcategory: str | None) -> bool:
    if category not in CATEGORY_TREE:
        return False
    allowed = CATEGORY_TREE[category]
    if not allowed:
        return subcategory is None
    return subcategory in allowed


def canonicalize_category_pair(category: str, subcategory: str | None) -> tuple[str, str | None]:
    canonical_category = _CATEGORY_ALIASES.get(_normalize_label(category), category)
    if canonical_category not in CATEGORY_TREE:
        return category, subcategory

    allowed = CATEGORY_TREE[canonical_category]
    if not allowed:
        return canonical_category, None
    if subcategory is None:
        return canonical_category, None

    canonical_subcategory = _SUBCATEGORY_ALIASES.get(
        (canonical_category, _normalize_label(subcategory)),
        subcategory,
    )
    return canonical_category, canonical_subcategory
