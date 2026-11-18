"""Mantiene al dia las normas del corpus con lo que publica El Peruano.

Flujo: escanear El Peruano -> detectar normas que afectan a una norma vigilada -> Gemini
clasifica el efecto y senala donde va el cambio -> el CODIGO arma la nota y la inserta ->
verificaciones deterministas -> propuesta en disco -> una persona decide -> copia de
seguridad -> cambio en el store -> historial. Cualquier cambio se deshace con `rollback`.

Dos decisiones de diseno sostienen todo:

* Gemini nunca escribe el documento. Devuelve datos (tipo, ancla, texto nuevo) y el codigo
  compone el bloque con la convencion del SPIJ que ya usan nuestros markdowns:
  "- (*) Articulo modificado por el Articulo 1 de la Ley N 32298, publicada el ..., cuyo
  texto es el siguiente:" seguido del texto entre comillas. Asi no puede reescribir ni
  duplicar texto existente (en la prueba manual, dejarlo reescribir duplico un parrafo).

* Ningun cambio toca el store sin una copia de seguridad verificada: el markdown local tiene
  que medir exactamente lo mismo que el documento indexado, o no se sabe que se esta
  respaldando y el cambio se rechaza.
"""

import difflib
import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from app import corpus, elperuano, store_documents
from app.config import settings

EFFECT_TYPES = {
    "modifica",
    "incorpora",
    "deroga",
    "deroga_total",
    "fe_de_erratas",
    "inconstitucional",
    "nuevo_tuo",
    "ninguno",
}
# Efectos que se aplican insertando una nota en un articulo concreto.
ARTICLE_EFFECTS = {"modifica", "incorpora", "deroga", "fe_de_erratas", "inconstitucional"}
VERBS = {
    "modifica": "modificado",
    "incorpora": "incorporado",
    "deroga": "derogado",
    "fe_de_erratas": "corregido por fe de erratas",
    "inconstitucional": "declarado inconstitucional",
}
SCOPES = {"Artículo", "Extremo", "Literal", "Párrafo", "Numeral", "Inciso"}
MONTHS = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]
DAILY_LISTING_MAX_DAYS = 7


class AmendmentError(RuntimeError):
    pass


class BackupError(AmendmentError):
    pass


# --------------------------------------------------------------------------- rutas


def amendments_dir() -> Path:
    return Path(settings.amendments_dir)


def registry_path() -> Path:
    return Path(settings.amendments_registry_path)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _append_history(entry: dict[str, Any]) -> None:
    path = amendments_dir() / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": _now(), **entry}, ensure_ascii=False) + "\n")


def load_history() -> list[dict[str, Any]]:
    path = amendments_dir() / "history.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- registro


@dataclass
class WatchedNorm:
    """Una norma del corpus que se vigila. Solo normas: la jurisprudencia no cambia."""

    document: str
    identificador: str
    aliases: list[str] = field(default_factory=list)
    version_hasta: str | None = None
    spij_url: str | None = None
    notas: str = ""


SEED_REGISTRY = Path(__file__).parent / "data" / "normas_vigiladas.seed.json"


def load_registry() -> list[WatchedNorm]:
    """El registro vivo (se actualiza al aplicar cambios); si todavia no existe, la semilla."""
    path = registry_path()
    if not path.exists():
        path = SEED_REGISTRY
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [WatchedNorm(**item) for item in data.get("normas", [])]


def save_registry(norms: list[WatchedNorm]) -> None:
    _write_json(registry_path(), {"normas": [asdict(norm) for norm in norms]})


# --------------------------------------------------------------------------- normalizacion


def fold(text: str) -> str:
    """Mayusculas sin tildes ni signos de numero, para comparar menciones de normas."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", text.upper()).strip()


_NORM_TYPES = (
    "DECRETO LEGISLATIVO",
    "DECRETO DE URGENCIA",
    "DECRETO SUPREMO",
    "DECRETO LEY",
    "RESOLUCION MINISTERIAL",
    "RESOLUCION LEGISLATIVA",
    "RESOLUCION ADMINISTRATIVA",
    "LEY",
)
_MENTION = re.compile(
    r"\b(" + "|".join(_NORM_TYPES) + r")\b\s*(?:N\s*[O°\.]*\s*\.?\s*|NRO\.?\s*|NUMERO\s*)?"
    r"(\d+(?:-\d+)*(?:-[A-Z][A-Z0-9/\.]*(?:-[A-Z0-9/\.]+)*)?)"
)
# "el Reglamento de la Ley 30364" no es "la Ley 30364": modificar el reglamento no cambia
# nuestro documento. Lo mismo un TUO, que es otro texto.
_DERIVED = re.compile(r"(REGLAMENTO|TEXTO UNICO ORDENADO|T\.U\.O\.?|TUO)\s+DE\s+(LA|EL|LOS)\s*$")


def canonical_id(tipo: str, numero: str) -> str:
    numero = numero.strip("-.").upper()
    numero = re.sub(r"^0+(?=\d)", "", numero)
    return f"{fold(tipo)} {numero}"


def normalize_norm_id(text: str) -> str | None:
    match = _MENTION.search(fold(text))
    return canonical_id(match.group(1), match.group(2)) if match else None


def _is_derived(folded: str, start: int) -> bool:
    """La mencion es al reglamento o al TUO de la norma, no a la norma.

    Excepcion: "aprueba el Texto Unico Ordenado de la Ley X" SI afecta a nuestro documento
    (hay un texto nuevo que lo reemplaza), y tiene que llegar a la clasificacion.
    """
    before = folded[max(0, start - 70) : start]
    return bool(_DERIVED.search(before[-40:])) and "APRUEB" not in before


def _mention_positions(text: str, norm: WatchedNorm) -> list[int]:
    folded = fold(text)
    positions: list[int] = []
    target = _MENTION.search(fold(norm.identificador))  # un protocolo sin numero no tiene
    if target:
        wanted = canonical_id(target.group(1), target.group(2))
        for match in _MENTION.finditer(folded):
            if canonical_id(match.group(1), match.group(2)) == wanted and not _is_derived(folded, match.start()):
                positions.append(match.start())
    for alias in norm.aliases:
        alias_folded = fold(alias)
        start = folded.find(alias_folded)
        while start >= 0:
            if not _is_derived(folded, start):
                positions.append(start)
            start = folded.find(alias_folded, start + 1)
    return sorted(positions)


def find_mentions(text: str) -> set[str]:
    folded = fold(text)
    found: set[str] = set()
    for match in _MENTION.finditer(folded):
        if _is_derived(folded, match.start()):
            continue
        found.add(canonical_id(match.group(1), match.group(2)))
    return found


def mentions_norm(text: str, norm: WatchedNorm) -> bool:
    return bool(_mention_positions(text, norm))


_CHANGE_VERB = re.compile(
    r"MODIFIC|DEROG|INCORPOR|SUSTITU|SIN EFECTO|ABROG|ADICION|INCONSTITUCIONAL|"
    r"TEXTO UNICO ORDENADO|FE DE ERRATAS|AGREGA|PRECISA"
)
CHANGE_WINDOW = 350


def mentions_with_change(text: str, norm: WatchedNorm) -> bool:
    """La norma aparece con un verbo de cambio cerca ("Modificase el articulo 46 de la Ley 30364").

    Una mencion sola es base legal o cita ("conforme a la Ley 30364") y no cambia nuestro
    texto. En la prueba contra produccion, 23 de 25 normas que citaban la Ley 30364 en su
    parte dispositiva eran de ese tipo; filtrarlas aca ahorra esas llamadas a Gemini.
    """
    folded = fold(text)
    for position in _mention_positions(text, norm):
        window = folded[max(0, position - CHANGE_WINDOW) : position + CHANGE_WINDOW]
        if _CHANGE_VERB.search(window):
            return True
    return False


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace('"', "")
    return re.sub(r"\s+", " ", text).strip()


def literal_in(fragment: str, source: str) -> bool:
    return bool(_norm_text(fragment)) and _norm_text(fragment) in _norm_text(source)


# --------------------------------------------------------------------------- parte dispositiva

_DISPOSITIVE_MARKERS = [
    r"SE\s+RESUELVE\s*:?",
    r"\bRESUELVE\s*:",
    r"\bDECRETA\s*:",
    r"HA\s+DADO\s+LA\s+LEY\s+SIGUIENTE\s*:?",
    r"HA\s+DADO\s+EL\s+DECRETO\s+(?:LEGISLATIVO|DE\s+URGENCIA)\s+SIGUIENTE\s*:?",
    r"HA\s+RESUELTO\s*:?",
    r"\bFALLA\s*:",
    r"\bORDENANZA\s*:",
]


def dispositive_part(texto: str) -> tuple[str, bool]:
    """Lo que la norma DISPONE, sin vistos ni considerandos.

    Los considerandos citan derogaciones ajenas ("la Ley 27293 ... ha sido derogada por el
    D.L. 1252") que no son efecto de esta norma. Un efecto cuya evidencia no esta en la parte
    dispositiva se descarta.
    """
    starts = [
        match.start()
        for pattern in _DISPOSITIVE_MARKERS
        for match in [re.search(pattern, texto, re.IGNORECASE)]
        if match
    ]
    if not starts:
        return texto, False
    return texto[min(starts) :], True


# --------------------------------------------------------------------------- articulos


def _article_header(numero: str) -> re.Pattern:
    """Encabezado del articulo `numero` en cualquiera de las formas que traen los markdowns.

    Formas reales: "## Articulo 46. Titulo", "## Articulo 9º" (sin nada despues: el titulo va en
    otra linea), "## Articulo 3- A" (articulo 3-A con espacio). "9" no debe casar con "90",
    "9-A" ni "9- A". Ojo: Python considera "º" una letra, asi que no sirve un (?!\\w).
    """
    parts = [re.escape(part) for part in re.split(r"\s*-\s*", numero.strip())]
    number = r"\s*-\s*".join(parts)
    return re.compile(
        rf"^\s*(?:#+\s*)?(?:-\s*)?'?\s*Art[ií]culo\s+{number}"
        r"(?!\d)(?!\s*-\s*[A-Za-z]\b)\s*(?:[°º]|\.|:|-|\s|$)",
        re.IGNORECASE,
    )


_ANY_ARTICLE = re.compile(r"^\s*(?:#+\s*)?(?:-\s*)?'?\s*Art[ií]culo\s+(\d+)(-?[A-Z])?\b", re.IGNORECASE)
# Un encabezado entre comillas ("## 'Articulo 15.") es texto citado: la version nueva de ese
# mismo articulo bajo una nota (*), un articulo incorporado (46-E) o uno de otra norma citado
# en una disposicion modificatoria. Solo un articulo distinto cierra el actual.
_QUOTED_HEADER = re.compile(r"^\s*#+\s*(?:-\s*)?['\"“]")
_SECTION_STOP = re.compile(
    r"^\s*#+\s*'?\s*(Art[ií]culo\s+\d|CAP[IÍ]TULO|T[IÍ]TULO|DISPOSICI|SUBCAP)", re.IGNORECASE
)


def find_article(lines: list[str], numero: str) -> tuple[int, int] | None:
    """Rango [inicio, fin) del articulo `numero`, con sus versiones citadas bajo notas (*)."""
    header = _article_header(numero)
    candidates = [index for index, line in enumerate(lines) if header.match(line)]
    if not candidates:
        return None
    headed = [
        index for index in candidates
        if lines[index].lstrip().startswith("#") and not _QUOTED_HEADER.match(lines[index])
    ]
    pool = headed or candidates
    if len(pool) > 1:
        raise AmendmentError(f"el articulo {numero} aparece {len(pool)} veces como encabezado")
    start = pool[0]
    end = next(
        (
            index for index in range(start + 1, len(lines))
            if _SECTION_STOP.match(lines[index])
            and not (_QUOTED_HEADER.match(lines[index]) and header.match(lines[index]))
        ),
        len(lines),
    )
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    # La nota que presenta al articulo siguiente ("Articulo incorporado ..., cuyo texto es el
    # siguiente:") va con ese articulo, no con este.
    if end < start + 1 or end >= len(lines):
        return start, end
    last = lines[end - 1]
    if end - 1 > start and "(*)" in last and last.rstrip().endswith("siguiente:"):
        end -= 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
    return start, end


def _insertion_point_for_new_article(lines: list[str], numero: str) -> int | None:
    """Donde va un articulo incorporado que todavia no existe (p. ej. 46-F)."""
    digits = re.match(r"\d+", numero)
    if not digits:
        return None
    base = int(digits.group(0))
    best: int | None = None
    for index, line in enumerate(lines):
        match = _ANY_ARTICLE.match(line)
        if match and line.lstrip().startswith("#") and int(match.group(1)) <= base:
            best = index
    if best is None:
        return None
    end = next(
        (index for index in range(best + 1, len(lines)) if _SECTION_STOP.match(lines[index])),
        len(lines),
    )
    while end > best + 1 and not lines[end - 1].strip():
        end -= 1
    return end


# --------------------------------------------------------------------------- Gemini

CLASSIFY_PROMPT = """Eres un analista de legislacion peruana. Te doy la PARTE DISPOSITIVA de una norma publicada en El Peruano y una norma de nuestro corpus.

Determina que efectos tiene la norma publicada SOBRE NUESTRA NORMA (no sobre su reglamento, ni sobre su TUO, ni sobre otras normas).

Tipos de efecto:
- "modifica": cambia el texto de articulos existentes (incluye "sustituye").
- "incorpora": agrega articulos, parrafos, literales o numerales.
- "deroga": deroga articulos concretos.
- "deroga_total": deroga la norma completa.
- "fe_de_erratas": corrige errores materiales del texto publicado.
- "inconstitucional": sentencia del Tribunal Constitucional que declara inconstitucional un articulo.
- "nuevo_tuo": aprueba un nuevo Texto Unico Ordenado de nuestra norma.
- "ninguno": solo la menciona o la usa como base legal.

Reglas:
- "afecta_a" es "norma" solo si el efecto recae sobre nuestra norma misma; "reglamento", "tuo" u "otra" en cualquier otro caso.
- "articulos": numeros tal como aparecen ("10", "46-F"). Vacio para deroga_total, nuevo_tuo o ninguno.
- "disposicion": el articulo o disposicion de la norma publicada que produce el efecto, con su nombre completo ("Artículo 1", "Artículo único", "Segunda Disposición Complementaria Modificatoria"), nunca solo el ordinal ("SEGUNDA").
- "evidencia": copia LITERAL y continua, de 5 a 40 palabras, del texto que produce el efecto.
- No inventes efectos. Una mencion en la base legal es "ninguno".

Responde solo JSON: {{"norma_publicada": "tipo y numero, p. ej. Ley N° 32351", "efectos": [{{"tipo": str, "afecta_a": str, "articulos": [str], "disposicion": str, "evidencia": str}}]}}

=== NUESTRA NORMA ===
{identificador} (tambien llamada: {aliases})

=== ENCABEZADO DE LA NORMA PUBLICADA (solo para identificarla) ===
{encabezado}

=== PARTE DISPOSITIVA DE LA NORMA PUBLICADA ===
{dispositiva}
"""

EDIT_PROMPT = """Eres un editor de textos legales peruanos. Debes indicar DONDE y QUE insertar en nuestro articulo para reflejar una norma publicada. No reescribes nada: solo devuelves datos.

Efecto: {tipo} sobre el articulo {articulo} por {disposicion} de {norma}.

Devuelve:
- "alcance": uno de "Artículo", "Extremo", "Literal", "Párrafo", "Numeral", "Inciso". "Artículo" si cambia el articulo completo.
- "ancla_linea": el NUMERO de la linea de nuestro articulo (las lineas vienen numeradas "L12|") despues de la cual va la nota. Elige la ultima linea del bloque afectado; si ese bloque ya tiene notas "(*)" anteriores, la ultima linea de esas notas. 0 si la nota va al final del articulo.
- "texto_nuevo": el texto nuevo COPIADO LITERALMENTE de la norma publicada, sin comillas exteriores y sin "[...]". Solo lo que cambia o se incorpora. Cadena vacia si es una derogacion.
- "articulo_nuevo": true solo si se incorpora un articulo que no existe en nuestro texto.

Responde solo JSON: {{"alcance": str, "ancla_linea": int, "texto_nuevo": str, "articulo_nuevo": bool}}

=== NORMA PUBLICADA (parte dispositiva) ===
{dispositiva}

=== NUESTRO ARTICULO {articulo} ===
{seccion}
"""


def _generate_json(prompt: str) -> dict[str, Any]:
    from app.gemini_client import build_client, parse_json_response

    client, types = build_client()
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
    )
    return parse_json_response(response.text or "")


@dataclass
class Effect:
    tipo: str
    articulos: list[str]
    disposicion: str
    evidencia: str


def classify(
    dispositiva: str, norm: WatchedNorm, generate: Callable = None, encabezado: str = ""
) -> tuple[str, list[Effect], list[str]]:
    """Efectos validados de una norma publicada sobre `norm`, mas los descartes explicados.

    El encabezado (tipo y numero) va aparte: en una ley la parte dispositiva empieza despues
    de "Ha dado la Ley siguiente", y sin el encabezado no hay como nombrarla en la nota.
    """
    generate = generate or _generate_json
    data = generate(
        CLASSIFY_PROMPT.format(
            identificador=norm.identificador,
            aliases=", ".join(norm.aliases) or "-",
            encabezado=encabezado[:1500] or "-",
            dispositiva=dispositiva[:60000],
        )
    )
    effects: list[Effect] = []
    discarded: list[str] = []
    for raw in data.get("efectos") or []:
        tipo = str(raw.get("tipo", "")).strip()
        if tipo not in EFFECT_TYPES or tipo == "ninguno":
            continue
        if str(raw.get("afecta_a", "")).strip() != "norma":
            discarded.append(f"{tipo} sobre {raw.get('afecta_a')} (no es nuestra norma)")
            continue
        evidencia = str(raw.get("evidencia", ""))
        if not literal_in(evidencia, dispositiva):
            discarded.append(f"{tipo}: la evidencia no esta literal en la parte dispositiva")
            continue
        effects.append(
            Effect(
                tipo=tipo,
                articulos=[str(item).strip() for item in raw.get("articulos") or [] if str(item).strip()],
                disposicion=str(raw.get("disposicion", "")).strip() or "la norma",
                evidencia=evidencia,
            )
        )
    return str(data.get("norma_publicada", "")).strip(), effects, discarded


# --------------------------------------------------------------------------- aplicacion


def fecha_larga(fecha: str) -> str:
    """'27/05/2025' -> '27 mayo 2025', como en las notas del SPIJ."""
    day, month, year = fecha.split("/")
    return f"{int(day)} {MONTHS[int(month) - 1]} {year}"


def clean_new_text(texto: str) -> str:
    """El texto nuevo sin envoltorios de la norma modificatoria.

    La norma lo cita entre comillas y cierra con '".' (el punto es de la oracion que cita,
    no del texto citado). Los "[...]" marcan lo que no cambia y no son texto.
    """
    texto = texto.replace("[...]", "").replace("[…]", "").strip()
    texto = re.sub(r'^[\s"“]+', "", texto)
    texto = re.sub(r'[\s"”]+\.?\s*$', "", texto)
    return texto.strip()


_PRETTY_TYPES = [
    "Decreto Legislativo", "Decreto de Urgencia", "Decreto Supremo", "Decreto Ley", "Resolución Ministerial",
    "Resolución Legislativa", "Resolución Administrativa", "Resolución Directoral", "Resolución", "Ley",
]


def pretty_norma(nombre: str) -> str:
    """'LEY N° 32351' -> 'Ley N° 32351', como en las notas existentes del SPIJ."""
    folded = fold(nombre)
    for tipo in _PRETTY_TYPES:
        if folded.startswith(fold(tipo)):
            return tipo + nombre[len(tipo):]
    return nombre


def build_note_block(alcance: str, tipo: str, disposicion: str, norma: str, fecha: str, texto_nuevo: str) -> list[str]:
    if disposicion.isupper():
        disposicion = disposicion.title()
    disposicion = disposicion[0].upper() + disposicion[1:] if disposicion else disposicion
    por = "el" if fold(disposicion).startswith("ART") else "la"
    masculine = fold(norma).startswith("DECRETO")
    de, publicada = ("del", "publicado") if masculine else ("de la", "publicada")
    note = f"- (*) {alcance} {VERBS[tipo]} por {por} {disposicion} {de} {norma}, {publicada} el {fecha_larga(fecha)}"
    if not texto_nuevo.strip():
        return ["", note + "."]
    body = texto_nuevo.strip().strip('"“”').strip()
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n|\n", body) if paragraph.strip()]
    paragraphs[0] = '" ' + paragraphs[0]
    paragraphs[-1] = paragraphs[-1] + ' "'
    block = ["", note + ", cuyo texto es el siguiente:"]
    for paragraph in paragraphs:
        block.extend(["", paragraph])
    return block


@dataclass
class Change:
    norma_id: str
    norma: str
    fecha: str
    url: str
    tipo: str
    articulo: str | None
    status: str  # aplicado | ya_aplicado | aviso | descartado
    detalle: str = ""
    nota: str = ""


def apply_effect_to_article(
    markdown: str,
    articulo: str,
    effect: Effect,
    norma: str,
    fecha: str,
    dispositiva: str,
    generate: Callable = None,
) -> tuple[str, str, str]:
    """Devuelve (markdown, status, detalle). Nunca borra ni reescribe lineas existentes."""
    generate = generate or _generate_json
    lines = markdown.split("\n")
    span = find_article(lines, articulo)

    if span is None and effect.tipo != "incorpora":
        return markdown, "aviso", f"el articulo {articulo} no esta en nuestro texto"

    section_lines = lines[span[0] : span[1]] if span else []
    # Lineas numeradas: Gemini devuelve un numero, no una copia del texto. Copiar la linea
    # fallaba con parrafos partidos en dos lineas por la conversion del PDF (Ley 32298).
    numbered = "\n".join(f"L{index + 1}| {line}" for index, line in enumerate(section_lines))

    # Ya aplicado: la nota de esta norma ya esta en el articulo.
    numero_norma = normalize_norm_id(norma)
    if span and numero_norma and any(
        "(*)" in line and numero_norma in find_mentions(line) for line in section_lines
    ):
        return markdown, "ya_aplicado", f"el articulo {articulo} ya tiene la nota de {norma}"

    plan = generate(
        EDIT_PROMPT.format(
            tipo=effect.tipo,
            articulo=articulo,
            disposicion=effect.disposicion,
            norma=norma,
            dispositiva=dispositiva[:60000],
            seccion=numbered[:40000] or "(el articulo no existe todavia)",
        )
    )
    alcance = str(plan.get("alcance", "Artículo")).strip()
    if alcance not in SCOPES:
        alcance = "Artículo"
    texto_nuevo = clean_new_text(str(plan.get("texto_nuevo", "") or ""))
    try:
        ancla_linea = int(plan.get("ancla_linea") or 0)
    except (TypeError, ValueError):
        ancla_linea = -1

    if effect.tipo in {"modifica", "incorpora", "fe_de_erratas"} and not texto_nuevo:
        return markdown, "aviso", f"{effect.tipo} sin texto nuevo identificable para el articulo {articulo}"
    if texto_nuevo and not literal_in(texto_nuevo, dispositiva):
        return markdown, "aviso", "el texto nuevo propuesto no es literal de la norma publicada"
    if span and texto_nuevo and literal_in(texto_nuevo, "\n".join(section_lines)):
        return markdown, "ya_aplicado", f"el texto nuevo ya esta en el articulo {articulo}"

    if span is None:
        insert_at = _insertion_point_for_new_article(lines, articulo)
        if insert_at is None:
            return markdown, "aviso", f"no se encontro donde incorporar el articulo {articulo}"
        alcance = "Artículo"
    elif alcance == "Artículo" or ancla_linea == 0:
        # Un cambio al articulo completo va al final, despues de sus notas anteriores.
        insert_at = span[1]
    elif 1 <= ancla_linea <= len(section_lines):
        insert_at = _end_of_paragraph(lines, span[0] + ancla_linea, span[1])
    else:
        return markdown, "aviso", f"la linea de insercion propuesta ({plan.get('ancla_linea')}) no existe en el articulo {articulo}"

    block = build_note_block(alcance, effect.tipo, effect.disposicion, norma, fecha, texto_nuevo)
    note = block[1]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    return "\n".join(new_lines), "aplicado", note


_SENTENCE_END = re.compile(r"[\.:;\"”\)]\s*(\(\*\))?\s*$")


def _end_of_paragraph(lines: list[str], index: int, limit: int) -> int:
    """Primer indice despues del parrafo que contiene a `index`, sin cortar oraciones.

    La conversion del PDF parte parrafos en dos con una linea en blanco en medio
    ("...confidencialidad de la" / "" / "victima, acerca de..."). Si una linea no cierra la
    oracion y la siguiente con texto empieza en minuscula, es el mismo parrafo.
    """
    while True:
        while index < limit and lines[index].strip():
            index += 1
        last = next((lines[i] for i in range(index - 1, -1, -1) if lines[i].strip()), "")
        following = next((i for i in range(index, limit) if lines[i].strip()), None)
        if following is None or _SENTENCE_END.search(last) or not lines[following].lstrip()[:1].islower():
            return index
        index = following


def verify_only_insertions(old: str, new: str) -> list[str]:
    problems: list[str] = []
    matcher = difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False)
    for op, i1, i2, _, _ in matcher.get_opcodes():
        if op in ("replace", "delete"):
            problems.append(f"{op} de lineas originales {i1 + 1}-{i2}")
    return problems


def inserted_lines(old: str, new: str) -> list[str]:
    matcher = difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False)
    new_lines = new.split("\n")
    return [line for op, _, _, j1, j2 in matcher.get_opcodes() if op == "insert" for line in new_lines[j1:j2]]


def verify_proposal(old: str, new: str, sources: list[str]) -> list[str]:
    """Las tres reglas que tiene que pasar cualquier propuesta, la haya armado quien sea."""
    problems = verify_only_insertions(old, new)
    source = "\n".join(sources)
    added = [line for line in inserted_lines(old, new) if line.strip()]
    seen: set[str] = set()
    for line in added:
        if line.lstrip().startswith("- (*)"):
            # Dos normas pueden repetir el mismo encabezado al reescribir un articulo dos veces;
            # la duplicacion que importa es dentro de una misma nota.
            seen = set()
            continue
        text = line.strip().strip('"').strip()
        if not literal_in(text, source):
            problems.append(f"texto agregado que no esta literal en El Peruano: {text[:80]!r}")
        key = _norm_text(text)
        if key in seen:
            problems.append(f"parrafo agregado dos veces: {text[:80]!r}")
        seen.add(key)
    return problems


# --------------------------------------------------------------------------- escaneo


def _load_corpus_markdown(document: str) -> tuple[Path, str]:
    """El markdown local EXACTO del documento: nada de coincidencias aproximadas.

    resolve_document_path cae al stem sin hash cuando no encuentra el nombre exacto, y eso
    puede casar con una revision anterior. Para respaldar y editar hace falta el archivo que
    corresponde a lo indexado, no uno parecido.
    """
    root = corpus.corpus_path()
    candidates = [root / document]
    mapped = corpus.load_manifest().get(document)
    if mapped:
        candidates.insert(0, root / mapped)
    for path in candidates:
        if path.is_file():
            return path, path.read_text(encoding="utf-8")
    raise BackupError(f"no hay copia local exacta de '{document}' en {root}")


def _proposals_dir() -> Path:
    return amendments_dir() / "proposals"


def load_proposal(proposal_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{12}", proposal_id or ""):
        raise AmendmentError("proposal_id invalido")
    path = _proposals_dir() / proposal_id / "proposal.json"
    if not path.exists():
        raise AmendmentError(f"la propuesta {proposal_id} no existe")
    return json.loads(path.read_text(encoding="utf-8"))


def list_proposals(status: str | None = None) -> list[dict[str, Any]]:
    root = _proposals_dir()
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.glob("*/proposal.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if status is None or data.get("status") == status:
            items.append({key: value for key, value in data.items() if key != "diff"})
    return items


def _decided_norma_ids(document: str) -> set[str]:
    ids: set[str] = set()
    for entry in load_history():
        if entry.get("document") == document and entry.get("event") in {"applied", "skipped"}:
            ids.update(entry.get("norma_ids", []))
    return ids


def _pending_refs(document: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for proposal in list_proposals("pending"):
        if proposal["document"] == document:
            refs.extend(proposal.get("normas", []))
    return refs


def _date_from_ddmmyyyy(value: str) -> date:
    day, month, year = value.replace(".", "/").split("/")
    return date(int(year), int(month), int(day))


def search_queries(norm: WatchedNorm) -> list[str]:
    """Frases exactas para el buscador de El Peruano.

    Sin comillas el buscador une las palabras con OR: "Decreto Legislativo N 1297" devolvia
    507 normas. Con comillas busca la frase. Las leyes se citan con y sin "N", asi que van
    las variantes.
    """
    queries: list[str] = []
    match = _MENTION.search(fold(norm.identificador))
    if match:
        tipo = norm.identificador[: len(norm.identificador)].split(" N")[0].strip()
        tipo = re.sub(r"\s*N[°º.]*\s*$", "", tipo).strip() or match.group(1).title()
        numero = match.group(2)
        queries += [f'"{tipo} {numero}"', f'"{tipo} N° {numero}"', f'"{tipo} Nº {numero}"']
    queries += [f'"{alias}"' for alias in norm.aliases]
    return list(dict.fromkeys(queries))


# Tipos que pueden modificar normas con rango de ley o reglamentos. En la puesta al dia (que
# usa el buscador, no la lista completa) solo se descarga la ficha de estos, o de normas cuya
# sumilla ya anuncia un cambio. La corrida diaria lee todas las fichas sin este filtro.
_RANKED_TYPES = re.compile(r"^(LEY|DECRETO LEGISLATIVO|DECRETO DE URGENCIA|DECRETO SUPREMO|RESOLUCION LEGISLATIVA)\b")
_SUMILLA_CHANGE = re.compile(r"MODIFIC|DEROG|INCORPOR|SUSTITU|SIN EFECTO|INCONSTITUCIONAL|TEXTO UNICO ORDENADO|FE DE ERRATAS")


def worth_fetching(ref: elperuano.NormaRef) -> bool:
    return bool(_RANKED_TYPES.match(fold(ref.tipo)) or _SUMILLA_CHANGE.search(fold(ref.sumilla)))


def _search_range(query: str, fecha_ini: date, fecha_fin: date, list_normas: Callable) -> list[elperuano.NormaRef]:
    """Busca en el rango; si el sitio devuelve demasiado, parte el rango en dos."""
    try:
        return list_normas(fecha_ini, fecha_fin, query=query)
    except elperuano.ElPeruanoFormatError:
        raise
    except elperuano.ElPeruanoError as exc:
        if "mas de" not in str(exc) or fecha_ini >= fecha_fin:
            raise
        middle = fecha_ini + (fecha_fin - fecha_ini) // 2
        return _search_range(query, fecha_ini, middle, list_normas) + _search_range(
            query, middle + timedelta(days=1), fecha_fin, list_normas
        )


def collect_candidates(
    norms: list[WatchedNorm],
    fecha_ini: date,
    fecha_fin: date,
    list_normas: Callable = elperuano.list_normas,
) -> list[elperuano.NormaRef]:
    """Normas publicadas en el rango que podrian afectar a alguna norma vigilada.

    Rango corto (el uso diario): se leen TODAS las normas publicadas y el filtro lo hace el
    texto completo; no depende del buscador del sitio. Rango largo (puesta al dia): leer
    todo seria decenas de miles de fichas, asi que se usa el buscador con frases exactas y
    solo se descargan las fichas que pueden traer un cambio (ver `worth_fetching`).
    """
    days = (fecha_fin - fecha_ini).days + 1
    if days <= DAILY_LISTING_MAX_DAYS:
        return list_normas(fecha_ini, fecha_fin)
    refs: dict[str, elperuano.NormaRef] = {}
    for norm in norms:
        for query in search_queries(norm):
            for ref in _search_range(query, fecha_ini, fecha_fin, list_normas):
                if worth_fetching(ref):
                    refs.setdefault(ref.id, ref)
    return list(refs.values())


def scan(
    fecha_ini: date,
    fecha_fin: date,
    documents: list[str] | None = None,
    get_dispositivo: Callable = elperuano.get_dispositivo,
    list_normas: Callable = elperuano.list_normas,
    generate: Callable = None,
    persist: bool = True,
) -> dict[str, Any]:
    norms = [norm for norm in load_registry() if documents is None or norm.document in documents]
    if not norms:
        raise AmendmentError("no hay normas vigiladas (revisa normas_vigiladas.json)")

    refs = collect_candidates(norms, fecha_ini, fecha_fin, list_normas)
    warnings: list[str] = []
    dispositivos: dict[str, elperuano.Dispositivo] = {}
    by_document: dict[str, list[tuple[elperuano.NormaRef, elperuano.Dispositivo]]] = {}

    for ref in refs:
        try:
            dispositivo = dispositivos.get(ref.id) or get_dispositivo(ref.id)
        except elperuano.ElPeruanoError as exc:
            warnings.append(f"{ref.id}: {exc}")
            continue
        dispositivos[ref.id] = dispositivo
        dispositiva, _ = dispositive_part(dispositivo.texto)
        haystack = f"{ref.sumilla}\n{dispositiva}"
        for norm in norms:
            if mentions_with_change(haystack, norm):
                by_document.setdefault(norm.document, []).append((ref, dispositivo))

    proposals: list[dict[str, Any]] = []
    for norm in norms:
        decided = _decided_norma_ids(norm.document)
        found = [(ref, disp) for ref, disp in by_document.get(norm.document, []) if ref.id not in decided]
        # Normas que ya esperaban decision en una propuesta pendiente se recalculan junto con
        # las nuevas sobre el texto actual: una propuesta siempre sale del corpus vigente.
        for pending in _pending_refs(norm.document):
            if pending["id"] not in decided and pending["id"] not in {ref.id for ref, _ in found}:
                try:
                    disp = dispositivos.get(pending["id"]) or get_dispositivo(pending["id"])
                except elperuano.ElPeruanoError as exc:
                    warnings.append(f"{pending['id']}: {exc}")
                    continue
                found.append((elperuano.NormaRef(**pending["ref"]), disp))
        if not found:
            continue
        proposal = build_proposal(norm, found, generate=generate)
        if persist:
            _supersede_pending(norm.document, proposal["id"])
            _save_proposal(proposal)
        proposals.append(proposal)

    return {
        "rango": [fecha_ini.isoformat(), fecha_fin.isoformat()],
        "normas_revisadas": len(refs),
        "documentos_vigilados": len(norms),
        "proposals": [{key: value for key, value in item.items() if key != "markdown_nuevo"} for item in proposals],
        "warnings": warnings,
    }


def build_proposal(
    norm: WatchedNorm,
    found: list[tuple[elperuano.NormaRef, elperuano.Dispositivo]],
    generate: Callable = None,
) -> dict[str, Any]:
    _, original = _load_corpus_markdown(norm.document)
    markdown = original
    changes: list[Change] = []
    sources: list[str] = []
    action = "none"

    found = sorted(found, key=lambda item: (_date_from_ddmmyyyy(item[1].fecha_publicacion), item[0].id))
    for ref, dispositivo in found:
        dispositiva, _ = dispositive_part(dispositivo.texto)
        sources.append(dispositiva)
        nombre_ref = f"{ref.tipo.strip().capitalize()} {ref.numero}".strip()
        base = dict(norma_id=ref.id, fecha=dispositivo.fecha_publicacion, url=dispositivo.url)
        try:
            encabezado = dispositivo.texto[: max(0, len(dispositivo.texto) - len(dispositiva))][:1500]
            if not encabezado.strip():
                encabezado = dispositivo.texto[:600]
            nombre, effects, discarded = classify(dispositiva, norm, generate, encabezado)
        except Exception as exc:
            changes.append(Change(**base, norma=nombre_ref, tipo="?", articulo=None, status="aviso",
                                  detalle=f"no se pudo clasificar: {exc}"))
            continue
        nombre = pretty_norma(nombre or nombre_ref)
        for reason in discarded:
            changes.append(Change(**base, norma=nombre, tipo="-", articulo=None, status="descartado", detalle=reason))
        if not effects and not discarded:
            changes.append(Change(**base, norma=nombre, tipo="ninguno", articulo=None, status="descartado",
                                  detalle="la menciona sin modificarla"))
        for effect in effects:
            if effect.tipo == "deroga_total":
                action = "delete"
                changes.append(Change(**base, norma=nombre, tipo=effect.tipo, articulo=None, status="aplicado",
                                      detalle="derogacion total: se propone sacar el documento del store"))
                continue
            if effect.tipo == "nuevo_tuo" or effect.tipo not in ARTICLE_EFFECTS or not effect.articulos:
                changes.append(Change(**base, norma=nombre, tipo=effect.tipo, articulo=None, status="aviso",
                                      detalle="requiere revision manual (nuevo TUO o efecto sin articulo)"))
                continue
            for articulo in effect.articulos:
                try:
                    markdown, status, detalle = apply_effect_to_article(
                        markdown, articulo, effect, nombre, dispositivo.fecha_publicacion, dispositiva, generate
                    )
                except Exception as exc:
                    status, detalle = "aviso", f"error aplicando: {exc}"
                changes.append(Change(**base, norma=nombre, tipo=effect.tipo, articulo=articulo, status=status,
                                      detalle=detalle, nota=detalle if status == "aplicado" else ""))

    problems = verify_proposal(original, markdown, sources)
    if action != "delete" and markdown != original:
        action = "revise"
    if problems:
        action = "blocked"

    diff = "\n".join(
        difflib.unified_diff(original.split("\n"), markdown.split("\n"), "actual", "propuesto", n=2, lineterm="")
    )
    proposal_id = uuid.uuid4().hex[:12]
    return {
        "id": proposal_id,
        "created": _now(),
        "status": "pending",
        "document": norm.document,
        "identificador": norm.identificador,
        "spij_url": norm.spij_url,
        "base_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "action": action,
        "verification_problems": problems,
        "normas": [
            {"id": ref.id, "url": disp.url, "fecha": disp.fecha_publicacion, "ref": asdict(ref)}
            for ref, disp in found
        ],
        "changes": [asdict(change) for change in changes],
        "diff": diff,
        "markdown_nuevo": markdown if action == "revise" else None,
    }


def _save_proposal(proposal: dict[str, Any]) -> None:
    folder = _proposals_dir() / proposal["id"]
    folder.mkdir(parents=True, exist_ok=True)
    data = dict(proposal)
    markdown = data.pop("markdown_nuevo", None)
    if markdown is not None:
        (folder / "propuesto.md").write_text(markdown, encoding="utf-8")
    (folder / "cambios.diff").write_text(data.get("diff", ""), encoding="utf-8")
    _write_json(folder / "proposal.json", data)


def _set_proposal_status(proposal_id: str, status: str, **extra: Any) -> None:
    path = _proposals_dir() / proposal_id / "proposal.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(status=status, **extra)
    _write_json(path, data)


def _supersede_pending(document: str, new_id: str) -> None:
    for proposal in list_proposals("pending"):
        if proposal["document"] == document and proposal["id"] != new_id:
            _set_proposal_status(proposal["id"], "superseded", superseded_by=new_id)


# --------------------------------------------------------------------------- copias y rollback


def _backups_dir() -> Path:
    return amendments_dir() / "backups"


def backup_document(document: str, reason: str, file_search_store_name: str | None, proposal_id: str | None = None) -> dict[str, Any]:
    """Copia exacta de lo indexado: markdown + metadata cruda del store.

    Solo se acepta si el markdown local mide exactamente lo mismo que el documento del
    store. Sin esa igualdad no se sabe que se esta guardando, y un rollback a una copia
    equivocada es peor que no tener copia.
    """
    stored = store_documents.get_store_document(document, file_search_store_name)
    path, markdown = _load_corpus_markdown(document)
    size_local = len(markdown.encode("utf-8"))
    if size_local != int(stored.get("size_bytes") or 0):
        raise BackupError(
            f"la copia local de '{document}' mide {size_local} bytes y la indexada {stored.get('size_bytes')}: "
            "no coinciden, asi que no se puede respaldar con garantia. Sincroniza el corpus antes."
        )
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    folder = _backups_dir() / backup_id
    folder.mkdir(parents=True, exist_ok=False)
    (folder / document).write_text(markdown, encoding="utf-8")
    record = {
        "backup_id": backup_id,
        "created": _now(),
        "document": document,
        "file_search_store": stored["file_search_store"],
        "store_name": stored["name"],
        "size_bytes": size_local,
        "sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "custom_metadata": stored["custom_metadata"],
        "reason": reason,
        "proposal_id": proposal_id,
        "replaced_by": None,
    }
    _write_json(folder / "backup.json", record)
    return record


def load_backup(backup_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{6}", backup_id or ""):
        raise AmendmentError("backup_id invalido")
    path = _backups_dir() / backup_id / "backup.json"
    if not path.exists():
        raise AmendmentError(f"la copia {backup_id} no existe")
    return json.loads(path.read_text(encoding="utf-8"))


def list_backups(document: str | None = None) -> list[dict[str, Any]]:
    root = _backups_dir()
    if not root.is_dir():
        return []
    items = []
    for path in sorted(root.glob("*/backup.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if document is None or data["document"] == document:
            items.append({key: value for key, value in data.items() if key != "custom_metadata"})
    return items


def revision_filename(document: str, markdown: str) -> str:
    stem = corpus.strip_document_id_hash(Path(document).stem)
    return f"{stem}-{hashlib.sha256(markdown.encode('utf-8')).hexdigest()[:12]}.md"


def decide(proposal_id: str, decision: str, file_search_store_name: str | None, decided_by: str = "") -> dict[str, Any]:
    proposal = load_proposal(proposal_id)
    if proposal["status"] != "pending":
        raise AmendmentError(f"la propuesta {proposal_id} esta '{proposal['status']}', no pendiente")
    norma_ids = [item["id"] for item in proposal["normas"]]
    document = proposal["document"]

    if decision == "skip":
        _set_proposal_status(proposal_id, "skipped", decided_by=decided_by, decided=_now())
        _append_history({"event": "skipped", "document": document, "proposal_id": proposal_id,
                         "norma_ids": norma_ids, "decided_by": decided_by})
        return {"proposal_id": proposal_id, "status": "skipped"}
    if decision != "apply":
        raise AmendmentError("decision debe ser 'apply' o 'skip'")
    if proposal["action"] == "blocked":
        raise AmendmentError("la propuesta no paso la verificacion; solo se puede omitir")
    if proposal["action"] == "none":
        _set_proposal_status(proposal_id, "applied", decided_by=decided_by, decided=_now(), result="sin cambios")
        _append_history({"event": "applied", "document": document, "proposal_id": proposal_id,
                         "norma_ids": norma_ids, "decided_by": decided_by, "result": "sin cambios"})
        return {"proposal_id": proposal_id, "status": "applied", "result": "sin cambios en el texto"}

    _, current = _load_corpus_markdown(document)
    if hashlib.sha256(current.encode("utf-8")).hexdigest() != proposal["base_sha256"]:
        raise AmendmentError("el documento cambio desde que se armo la propuesta; vuelve a escanear")

    backup = backup_document(document, f"antes de aplicar la propuesta {proposal_id}",
                             file_search_store_name, proposal_id)

    if proposal["action"] == "delete":
        result = store_documents.swap_document(None, None, None, [document], file_search_store_name)
        new_document = None
    else:
        markdown = (_proposals_dir() / proposal_id / "propuesto.md").read_text(encoding="utf-8")
        new_document = revision_filename(document, markdown)
        result = store_documents.swap_document(
            new_document, markdown, backup["custom_metadata"], [document], file_search_store_name
        )

    record = load_backup(backup["backup_id"])
    record["replaced_by"] = new_document
    _write_json(_backups_dir() / backup["backup_id"] / "backup.json", record)
    _set_proposal_status(proposal_id, "applied", decided_by=decided_by, decided=_now(),
                         backup_id=backup["backup_id"], new_document=new_document)
    _append_history({"event": "applied", "document": document, "new_document": new_document,
                     "proposal_id": proposal_id, "norma_ids": norma_ids, "backup_id": backup["backup_id"],
                     "action": proposal["action"], "decided_by": decided_by, "store_result": result})
    _advance_registry(document, new_document, proposal)
    return {"proposal_id": proposal_id, "status": "applied", "backup_id": backup["backup_id"],
            "new_document": new_document, "store": result}


def _advance_registry(document: str, new_document: str | None, proposal: dict[str, Any]) -> None:
    norms = load_registry()
    latest = max((_date_from_ddmmyyyy(item["fecha"]) for item in proposal["normas"]), default=None)
    for norm in norms:
        if norm.document != document:
            continue
        if new_document:
            norm.document = new_document
        if latest and (not norm.version_hasta or latest.isoformat() > norm.version_hasta):
            norm.version_hasta = latest.isoformat()
    save_registry(norms)


def rollback(backup_id: str, file_search_store_name: str | None, decided_by: str = "") -> dict[str, Any]:
    """Vuelve a poner en el store la version guardada en `backup_id`.

    La version que se saca tambien se respalda antes, asi que un rollback tambien se deshace.
    """
    backup = load_backup(backup_id)
    document = backup["document"]
    markdown = (_backups_dir() / backup_id / document).read_text(encoding="utf-8")
    if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != backup["sha256"]:
        raise BackupError(f"la copia {backup_id} esta alterada: su sha256 no coincide")

    current = backup.get("replaced_by")
    to_delete: list[str] = []
    pre_backup = None
    if current:
        pre_backup = backup_document(current, f"antes del rollback a {backup_id}", file_search_store_name)
        to_delete = [current]

    result = store_documents.swap_document(
        document, markdown, backup["custom_metadata"], to_delete, file_search_store_name
    )
    _append_history({"event": "rollback", "document": current or document, "restored": document,
                     "backup_id": backup_id, "pre_rollback_backup_id": pre_backup and pre_backup["backup_id"],
                     "decided_by": decided_by, "store_result": result})
    norms = load_registry()
    for norm in norms:
        if current and norm.document == current:
            norm.document = document
    save_registry(norms)
    return {"restored": document, "removed": to_delete,
            "pre_rollback_backup_id": pre_backup and pre_backup["backup_id"], "store": result}


def snapshot(documents: list[str] | None, file_search_store_name: str | None, reason: str) -> dict[str, Any]:
    """Copia de seguridad a pedido (por defecto, de todas las normas vigiladas)."""
    targets = documents or [norm.document for norm in load_registry()]
    done, failed = [], []
    for document in targets:
        try:
            done.append(backup_document(document, reason, file_search_store_name)["backup_id"])
        except Exception as exc:
            failed.append({"document": document, "error": str(exc)})
    return {"backups": done, "failed": failed}
