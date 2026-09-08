"""Los cuatro brazos del experimento, como parches sobre el workflow de produccion.

Se parchea y no se copia a mano porque el workflow de produccion se sigue editando: una
copia editada a mano deja de ser comparable en cuanto alguien toca un prompt, y el
experimento pasaria a medir la diferencia entre dos versiones del sistema en vez del
efecto del componente ablacionado.

Cada parche esta anclado a texto literal del workflow y **falla ruidosamente** si el ancla
ya no aparece. Es deliberado: un parche que no encuentra su ancla y sigue adelante produce
un brazo que dice estar ablacionado y no lo esta, y eso no se nota en los resultados, solo
los invalida.
"""

import uuid

NAMESPACE = uuid.UUID("6f0a2d4e-6d2b-5c1a-9f3e-8a1b2c3d4e5f")

# Terminos que no pueden sobrevivir a la ablacion de cada eje. Se comprueban al final:
# es la red que atrapa un parche que aplico a medias.
RAG_FORBIDDEN = ("SearchStore",)
XAI_FORBIDDEN = (
    "citations",
    "citation_id",
    "original_snippet",
    "summary_snippet",
    "citationSupportStatus",
    "confidenceStatus",
    "confidenceReason",
    "nextSteps",
    "clarifyingQuestions",
    "specialistSupportRecommended",
)


class PatchError(RuntimeError):
    """El workflow de produccion cambio y el parche ya no encaja. Hay que revisarlo."""


def stable_id(arm: str, seed: str) -> str:
    """Id derivado, no aleatorio: reimportar el mismo brazo actualiza su workflow en vez
    de crear uno nuevo cada vez."""
    return str(uuid.uuid5(NAMESPACE, f"legalfam-eval:{arm}:{seed}"))


def node(workflow: dict, name: str) -> dict:
    for candidate in workflow["nodes"]:
        if candidate["name"] == name:
            return candidate
    raise PatchError(f"el workflow no tiene el nodo '{name}'")


def drop_node(workflow: dict, name: str) -> None:
    node(workflow, name)  # falla si no existe
    workflow["nodes"] = [item for item in workflow["nodes"] if item["name"] != name]
    workflow["connections"].pop(name, None)
    for outputs in workflow["connections"].values():
        for branches in outputs.values():
            for branch in branches or []:
                if branch is None:
                    continue
                branch[:] = [link for link in branch if link.get("node") != name]


def connect(workflow: dict, source: str, target: str) -> None:
    workflow["connections"].setdefault(source, {}).setdefault("main", [[]])
    workflow["connections"][source]["main"][0] = [{"node": target, "type": "main", "index": 0}]


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise PatchError(f"{label}: el ancla no aparece exactamente una vez")
    return text.replace(old, new)


def cut_between(text: str, start: str, end: str, new: str, label: str) -> str:
    """Sustituye el tramo `[start, end)`, conservando `end`."""
    begin = text.find(start)
    finish = text.find(end, begin + 1) if begin >= 0 else -1
    if begin < 0 or finish < 0:
        raise PatchError(f"{label}: no se encontro el tramo entre las anclas")
    return text[:begin] + new + text[finish:]


# --------------------------------------------------------------------------------------
# Parche comun


def apply_common(workflow: dict, arm: str) -> None:
    """Identidad propia, webhook propio y temperatura fija.

    La temperatura se fija en los cuatro brazos por igual. No hace la corrida
    determinista, pero sin ella la diferencia entre brazos incluye el ruido de muestreo
    de cinco nodos distintos, y ese ruido no es el efecto que se quiere medir.
    """
    workflow["name"] = f"LegalFam Eval - {arm}"
    workflow["id"] = stable_id(arm, "workflow")
    workflow["versionId"] = stable_id(arm, "version")
    workflow["active"] = False
    workflow["tags"] = []

    webhook = node(workflow, "Webhook")
    webhook["parameters"]["path"] = f"chat-process-eval-{arm}"
    webhook["webhookId"] = stable_id(arm, "webhook")

    for item in workflow["nodes"]:
        item["id"] = stable_id(arm, item["name"])
        if item["type"].endswith("lmChatGoogleGemini"):
            item["parameters"].setdefault("options", {})["temperature"] = 0


# --------------------------------------------------------------------------------------
# Eje RAG


_NO_RAG_TOOL_LINE = (
    "No tienes ninguna herramienta de busqueda disponible: responde unicamente con tu "
    "propio conocimiento de Derecho de Familia peruano."
)

_NO_RAG_PROCESS = """Proceso obligatorio:
1. Responde la consulta de Derecho de Familia con la mejor orientacion prudente que puedas dar con tu propio conocimiento.
2. No dispones de documentos recuperados: citations es siempre [] y citationSupportStatus es siempre NONE.
"""


def apply_no_rag(workflow: dict) -> None:
    """Quita la recuperacion: el agente responde de memoria.

    Se elimina la herramienta y se reescribe solo la parte del prompt que la nombra. El
    resto del system message queda literalmente igual, para que la diferencia entre este
    brazo y el completo sea la recuperacion y no una redaccion distinta del encargo.
    """
    drop_node(workflow, "SearchStore")

    agent = node(workflow, "RAG Agent")
    message = agent["parameters"]["options"]["systemMessage"]

    message = replace_once(
        message,
        "Debes usar siempre la herramienta SearchStore antes de responder.",
        _NO_RAG_TOOL_LINE,
        "RAG Agent / linea de herramienta",
    )
    message = cut_between(
        message,
        "Proceso obligatorio:",
        "\n\nContrato de salida obligatorio:",
        _NO_RAG_PROCESS,
        "RAG Agent / proceso",
    )
    message = replace_once(
        message,
        "- Usa GOOD si hay citas utiles y directas; WEAK si hay citas debiles o indirectas"
        " y debes devolverlas; NONE si no hay citas utiles y citations debe ser [].",
        "- citationSupportStatus debe ser siempre NONE.",
        "RAG Agent / estado de respaldo",
    )
    message = replace_once(
        message,
        "- citations debe ser el arreglo de citas validas devuelto por SearchStore; si"
        " citationSupportStatus es WEAK devuelve las citas debiles disponibles; si es"
        " NONE usa [].",
        "- citations debe ser siempre [].",
        "RAG Agent / arreglo de citas",
    )
    message = replace_once(
        message,
        "- Si SearchStore devuelve texto plano, extrae su respuesta a answer, usa"
        " citations: [] y citationSupportStatus: NONE.\n",
        "",
        "RAG Agent / texto plano",
    )

    agent["parameters"]["options"]["systemMessage"] = message


# --------------------------------------------------------------------------------------
# Eje XAI


# Secciones del system message del XAI Agent que definen la capa de explicabilidad.
_XAI_SECTIONS_TO_DROP = (
    "Contrato de salida obligatorio para el parser:",
    "Campos estructurados obligatorios:",
    "Preguntas para afinar la orientacion:",
)

# Vinetas sueltas que sobreviven al corte por secciones pero solo tienen sentido con la
# capa de explicabilidad puesta. Se listan literalmente para que el parche falle si el
# prompt de produccion las reescribe.
_XAI_BULLETS_TO_DROP = (
    "- Si no puedes copiar un pasaje literal de la cita, no la incluyas.",
)

_XAI_REDUCED_CONTRACT = """Contrato de salida obligatorio para el parser:
- Devuelve exclusivamente el objeto JSON final en la raiz.
- No incluyas una propiedad "output".
- No uses Markdown, bloque ```json, ni texto antes o despues del JSON.
- La unica clave permitida en la raiz es answer.
- answer es el texto en espanol, en Markdown seguro, con la orientacion para el usuario.
"""

_XAI_REDUCED_SCHEMA = """{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "PlainAnswer",
  "type": "object",
  "properties": {
    "answer": {
      "type": "string",
      "description": "Final user-facing Spanish Markdown answer with personalized orientation and prudent caveats."
    }
  },
  "required": [
    "answer"
  ],
  "additionalProperties": false
}"""

# El nodo que arma la respuesta cuando no hay capa de explicabilidad. Los campos XAI
# salen vacios y no ausentes: el backend y el frontend esperan las claves, y un brazo que
# rompiera el contrato de transporte estaria midiendo un error de integracion.
_XAI_REDUCED_BUILDER = """const output = $json.output || {};
// Brazo de ablacion sin capa de explicabilidad: la respuesta viaja sin citas, sin
// localizador, sin confianza y sin pasos. Los campos se envian vacios y no ausentes para
// que el contrato con el backend siga siendo el mismo y la comparacion mida la
// explicabilidad y no un fallo de transporte.
const normalized = $('Normalized Input').first().json.body || {};
const language = normalized.language || 'es';
const languageRequested = normalized.language_requested || language;
return [{
  json: {
    language,
    languageRequested,
    payload: {
      message: output.answer || $json.answer || 'Con lo que cuentas hasta ahora, puedo darte una orientacion general de Derecho de Familia.',
      citations: [],
      confidenceStatus: null,
      confidenceReason: null,
      nextSteps: [],
      specialistSupportRecommended: false,
      citationSupportStatus: 'NONE',
      clarifyingQuestions: [],
      agentTokenCost: 3,
    },
  },
}];"""


def _strip_xai_sections(message: str) -> str:
    """Deja el encargo de redaccion y borra el contrato de explicabilidad.

    Se corta por secciones y no por el nodo entero a proposito. Si se borrara el XAI
    Agent completo, este brazo perderia tambien al redactor —la respuesta cruda del RAG
    Agent es visiblemente peor prosa— y el efecto medido mezclaria explicabilidad con
    calidad de redaccion. Lo que se ablaciona aca es exactamente la verificabilidad.
    """
    lines = message.split("\n")
    kept: list[str] = []
    dropped_bullets: set[str] = set()
    dropping = False

    for line in lines:
        stripped = line.strip()
        is_header = stripped.endswith(":") and not stripped.startswith("-")

        if is_header:
            dropping = stripped in _XAI_SECTIONS_TO_DROP
            if dropping:
                continue

        if dropping:
            continue

        # Dentro de las secciones que si se conservan quedan vinetas sueltas sobre citas
        # ("No incluyas una seccion de fuentes...", que ahi es una regla de redaccion y
        # se queda: quitarla cambiaria el formato de la respuesta y no su explicabilidad).
        # Se van solo las que nombran un campo del contrato XAI.
        if stripped.startswith("-") and any(token in line for token in XAI_FORBIDDEN):
            continue

        if stripped in _XAI_BULLETS_TO_DROP:
            dropped_bullets.add(stripped)
            continue

        kept.append(line)

    missing = set(_XAI_BULLETS_TO_DROP) - dropped_bullets
    if missing:
        raise PatchError(
            "XAI Agent: estas vinetas ya no estan en el prompt y el parche quedo obsoleto: "
            + " | ".join(sorted(missing))
        )

    trimmed = "\n".join(_drop_orphan_headers(kept))
    while "\n\n\n" in trimmed:
        trimmed = trimmed.replace("\n\n\n", "\n\n")
    return trimmed.strip()


def _drop_orphan_headers(lines: list[str]) -> list[str]:
    """Quita las vinetas que solo encabezaban sub-vinetas ya eliminadas.

    "- Cada cita lleva dos textos distintos y ninguno reemplaza al otro:" se queda sin
    sus dos sub-vinetas al ablacionar, y una instruccion que termina en dos puntos y no
    introduce nada es ruido que el modelo intenta cumplir igual.
    """
    result: list[str] = []
    for position, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("-") and stripped.endswith(":"):
            indent = len(line) - len(line.lstrip())
            following = lines[position + 1] if position + 1 < len(lines) else ""
            has_children = following.strip().startswith("-") and (
                len(following) - len(following.lstrip())
            ) > indent
            if not has_children:
                continue
        result.append(line)
    return result


def apply_no_xai(workflow: dict) -> None:
    """Quita la capa de explicabilidad, conservando la generacion de la respuesta."""
    agent = node(workflow, "XAI Agent")
    reduced = _strip_xai_sections(agent["parameters"]["options"]["systemMessage"])
    agent["parameters"]["options"]["systemMessage"] = f"{reduced}\n\n{_XAI_REDUCED_CONTRACT}"

    node(workflow, "XAI Output Parser")["parameters"]["inputSchema"] = _XAI_REDUCED_SCHEMA

    # Sin citas no hay nada que reanclar contra el corpus.
    drop_node(workflow, "Resolve Locators")
    drop_node(workflow, "Attach Locators")
    connect(workflow, "XAI Agent", "Build Response XAI")

    node(workflow, "Build Response XAI")["parameters"]["jsCode"] = _XAI_REDUCED_BUILDER


# --------------------------------------------------------------------------------------
# Registro de brazos


_SOURCES_BULLET = (
    "- No incluyas una seccion de fuentes, bibliografia, enlaces, documentos usados ni"
    " citas dentro de answer. Las fuentes se muestran fuera del mensaje en la interfaz."
)
_INLINE_SOURCES_BULLET = (
    "- Cuando te apoyes en una norma, nombra el articulo y la norma dentro de answer. En"
    " este modo no hay interfaz que muestre las fuentes aparte, asi que si no las nombras"
    " ahi el usuario no las ve."
)


def apply_no_xai_inline(workflow: dict) -> None:
    """Como `no_xai`, pero dejando que la respuesta nombre sus fuentes en el texto.

    Existe para responder a la objecion evidente contra `no_xai`: ese brazo conserva la
    instruccion de no citar dentro de `answer` —correcta en produccion, donde la interfaz
    muestra las fuentes al lado— pero se queda sin la interfaz que la justifica, asi que
    parte de su caida en trazabilidad podria venir de la instruccion y no de la ablacion.
    Este brazo separa las dos cosas: si aqui la trazabilidad sigue baja, la perdida es de
    la capa de explicabilidad y no del formato que se le pidio a la respuesta.
    """
    apply_no_xai(workflow)
    agent = node(workflow, "XAI Agent")
    agent["parameters"]["options"]["systemMessage"] = replace_once(
        agent["parameters"]["options"]["systemMessage"],
        _SOURCES_BULLET,
        _INLINE_SOURCES_BULLET,
        "XAI Agent / fuentes en el texto",
    )


ARMS: dict[str, dict] = {
    "full": {"rag": True, "xai": True, "patches": ()},
    "no_rag": {"rag": False, "xai": True, "patches": (apply_no_rag,)},
    "no_xai": {"rag": True, "xai": False, "patches": (apply_no_xai,)},
    "base": {"rag": False, "xai": False, "patches": (apply_no_rag, apply_no_xai)},
    # Control opcional, fuera del 2x2: no se corre por defecto.
    "no_xai_inline": {"rag": True, "xai": False, "patches": (apply_no_xai_inline,)},
}

# Los cuatro brazos del diseno factorial. `no_xai_inline` se pide explicitamente.
FACTORIAL = ("full", "no_rag", "no_xai", "base")


def build(workflow: dict, arm: str) -> dict:
    """Aplica el brazo sobre una copia ya deserializada del workflow de produccion."""
    spec = ARMS[arm]
    apply_common(workflow, arm)
    for patch in spec["patches"]:
        patch(workflow)
    verify(workflow, arm)
    return workflow


def verify(workflow: dict, arm: str) -> None:
    """Comprueba que la ablacion se aplico de verdad.

    Un brazo mal parcheado no falla al ejecutarse: responde normal y contamina los
    resultados sin dejar rastro. Por eso la comprobacion es una asercion y no un aviso.
    """
    spec = ARMS[arm]

    if not spec["rag"]:
        tools = _tools_of(workflow, "RAG Agent")
        if tools:
            raise PatchError(f"brazo {arm}: el RAG Agent conserva herramientas ({', '.join(tools)})")

        message = node(workflow, "RAG Agent")["parameters"]["options"]["systemMessage"]
        if any(token in message for token in RAG_FORBIDDEN):
            raise PatchError(f"brazo {arm}: el prompt del RAG Agent sigue pidiendo recuperar")

    if not spec["xai"]:
        prompts = _prompt_text(workflow)
        leaked = [token for token in XAI_FORBIDDEN if token in prompts]
        if leaked:
            raise PatchError(
                f"brazo {arm}: el contrato de explicabilidad sigue en los prompts "
                f"({', '.join(leaked)})"
            )


def _tools_of(workflow: dict, agent: str) -> list[str]:
    """Nodos enchufados como herramienta a un agente.

    La comprobacion es sobre las conexiones y no sobre el texto del JSON: el nombre del
    recurso de Gemini ("fileSearchStores/...") contiene la palabra que se busca, y un
    chequeo textual daba por fallado un brazo correctamente ablacionado.
    """
    return [
        source
        for source, outputs in workflow["connections"].items()
        for branch in outputs.get("ai_tool", []) or []
        for link in branch or []
        if link.get("node") == agent
    ]


XAI_NODES = ("XAI Agent", "XAI Output Parser")


def _prompt_text(workflow: dict) -> str:
    """Lo que la capa XAI le pide al modelo: su system message y su esquema de salida.

    Se mira solo esos dos nodos, y no todo el workflow, porque el resto sigue hablando de
    citas con razon. El RAG Agent las recupera igual en este brazo —ablacionar tambien la
    recuperacion seria el brazo `base`, no este— y los nodos `Build Response *` nombran
    los campos XAI justamente para enviarlos vacios. Lo que no puede quedar es una
    instruccion que le pida al modelo producir explicabilidad.
    """
    chunks: list[str] = []
    for name in XAI_NODES:
        parameters = node(workflow, name).get("parameters", {})
        options = parameters.get("options", {})
        if isinstance(options, dict) and options.get("systemMessage"):
            chunks.append(str(options["systemMessage"]))
        if parameters.get("inputSchema"):
            chunks.append(str(parameters["inputSchema"]))
    return "\n".join(chunks)
