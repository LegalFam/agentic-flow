import uuid

NAMESPACE = uuid.UUID("6f0a2d4e-6d2b-5c1a-9f3e-8a1b2c3d4e5f")

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
    pass


def stable_id(arm: str, seed: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"legalfam-eval:{arm}:{seed}"))


def node(workflow: dict, name: str) -> dict:
    for candidate in workflow["nodes"]:
        if candidate["name"] == name:
            return candidate
    raise PatchError(f"el workflow no tiene el nodo '{name}'")


def drop_node(workflow: dict, name: str) -> None:
    node(workflow, name)
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
    begin = text.find(start)
    finish = text.find(end, begin + 1) if begin >= 0 else -1
    if begin < 0 or finish < 0:
        raise PatchError(f"{label}: no se encontro el tramo entre las anclas")
    return text[:begin] + new + text[finish:]




def apply_common(workflow: dict, arm: str) -> None:
    workflow["name"] = f"LegalFam Eval - {arm}"
    workflow["id"] = stable_id(arm, "workflow")
    workflow["versionId"] = stable_id(arm, "version")
    workflow["active"] = False
    workflow["tags"] = []

    webhook = node(workflow, "Webhook")
    webhook["parameters"]["path"] = f"chat-process-eval-{arm}"
    webhook["webhookId"] = stable_id(arm, "webhook")
    webhook["parameters"].pop("authentication", None)
    webhook.pop("credentials", None)

    for item in workflow["nodes"]:
        item["id"] = stable_id(arm, item["name"])
        if item["type"].endswith("lmChatGoogleGemini"):
            item["parameters"].setdefault("options", {})["temperature"] = 0




_NO_RAG_TOOL_LINE = (
    "No tienes ninguna herramienta de busqueda disponible: responde unicamente con tu "
    "propio conocimiento de Derecho de Familia peruano."
)

_NO_RAG_PROCESS = """Proceso obligatorio:
1. Responde la consulta de Derecho de Familia con la mejor orientacion prudente que puedas dar con tu propio conocimiento.
2. No dispones de documentos recuperados: citations es siempre [] y citationSupportStatus es siempre NONE.
"""


def apply_no_rag(workflow: dict) -> None:
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




_XAI_SECTIONS_TO_DROP = (
    "Contrato de salida obligatorio para el parser:",
    "Campos estructurados obligatorios:",
    "Preguntas para afinar la orientacion:",
)

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
    agent = node(workflow, "XAI Agent")
    reduced = _strip_xai_sections(agent["parameters"]["options"]["systemMessage"])
    agent["parameters"]["options"]["systemMessage"] = f"{reduced}\n\n{_XAI_REDUCED_CONTRACT}"

    node(workflow, "XAI Output Parser")["parameters"]["inputSchema"] = _XAI_REDUCED_SCHEMA

    drop_node(workflow, "Resolve Locators")
    drop_node(workflow, "Attach Locators")
    connect(workflow, "XAI Agent", "Build Response XAI")

    node(workflow, "Build Response XAI")["parameters"]["jsCode"] = _XAI_REDUCED_BUILDER




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
    "no_xai_inline": {"rag": True, "xai": False, "patches": (apply_no_xai_inline,)},
}

FACTORIAL = ("full", "no_rag", "no_xai", "base")


def build(workflow: dict, arm: str) -> dict:
    spec = ARMS[arm]
    apply_common(workflow, arm)
    for patch in spec["patches"]:
        patch(workflow)
    verify(workflow, arm)
    return workflow


def verify(workflow: dict, arm: str) -> None:
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
    return [
        source
        for source, outputs in workflow["connections"].items()
        for branch in outputs.get("ai_tool", []) or []
        for link in branch or []
        if link.get("node") == agent
    ]


XAI_NODES = ("XAI Agent", "XAI Output Parser")


def _prompt_text(workflow: dict) -> str:
    chunks: list[str] = []
    for name in XAI_NODES:
        parameters = node(workflow, name).get("parameters", {})
        options = parameters.get("options", {})
        if isinstance(options, dict) and options.get("systemMessage"):
            chunks.append(str(options["systemMessage"]))
        if parameters.get("inputSchema"):
            chunks.append(str(parameters["inputSchema"]))
    return "\n".join(chunks)
