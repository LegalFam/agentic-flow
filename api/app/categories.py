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
