import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from app.categories import category_catalog_text
from app.config import settings
from app.models import LegalMetadata


STORE_REGISTRY_PATH = Path("/work/file_search_stores.json")
MAX_CUSTOM_METADATA_STRING_LENGTH = 256

CATEGORY_METADATA_CODES = {
    "Vinculos Conyugales y Patrimoniales": "vcp",
    "Relaciones Paterno-Filiales y Menores": "rpfm",
    "Proteccion a Personas Vulnerables": "ppv",
    "Sucesiones y Herencia": "sh",
    "Procesos Legales y Resolucion Alternativa": "plra",
    "Generales": "gen",
}

SUBCATEGORY_METADATA_CODES = {
    "Divorcio y Separacion": "divorcio_separacion",
    "Union de Hecho": "union_hecho",
    "Sociedad de Gananciales": "sociedad_gananciales",
    "Filiacion e Identidad": "filiacion_identidad",
    "Patria Potestad": "patria_potestad",
    "Tenencia y Custodia": "tenencia_custodia",
    "Pension de Alimentos": "pension_alimentos",
    "Adopcion": "adopcion",
    "Violencia Familiar": "violencia_familiar",
    "Centro Emergencia Mujer": "centro_emergencia_mujer",
    "Proteccion de Ninos y Adolescentes": "proteccion_ninos_adolescentes",
    "Tutela y Curatela": "tutela_curatela",
    "Capacidad Juridica": "capacidad_juridica",
    "Conciliacion": "conciliacion",
    "Procedimiento Civil": "procedimiento_civil",
    "Derecho de Familia": "derecho_familia",
    "Otros": "otros",
}


def extract_metadata_with_gemini(filename: str, markdown: str, fuente: str | None) -> LegalMetadata:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY no esta configurado")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = build_metadata_prompt(filename, markdown, fuente)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )
    data = parse_json_response(response.text or "")
    return LegalMetadata.model_validate(data)


def build_metadata_prompt(filename: str, markdown: str, fuente: str | None) -> str:
    clipped_markdown = markdown[:180000]
    source_hint = fuente or filename
    return f"""
Eres un extractor juridico para documentos peruanos de derecho de familia.

Devuelve solamente JSON valido. No incluyas markdown ni explicaciones.

Reglas:
- No inventes datos. Si un campo no tiene evidencia clara, usa null.
- Puede haber una o varias categorias por documento.
- Cada categoria debe aparecer una sola vez.
- Si una categoria aplica a varias subcategorias, agrupalas en el arreglo subcategorias de esa categoria.
- Cada categoria/subcategoria debe salir exactamente del catalogo.
- Si una categoria no tiene subcategoria, usa [] en subcategorias.
- Devuelve al menos una categoria. Usa Generales con subcategorias ["Otros"] si no hay una clasificacion juridica clara.
- Mantener fechas como aparecen en el documento o en formato ISO si es inequivoco.
- confidence debe ser un numero entre 0 y 1.

Catalogo cerrado:
{category_catalog_text()}

Schema exacto:
{{
  "titulo": string|null,
  "identificador": string|null,
  "entidad": string|null,
  "fecha": string|null,
  "materia": string|null,
  "expediente": string|null,
  "articulo": string|null,
  "norma": string|null,
  "pleno": string|null,
  "protocolo": string|null,
  "fuente": string|null,
  "categorias": [
    {{
      "categoria": string,
      "subcategorias": string[]
    }}
  ],
  "confidence": number,
  "observaciones": string|null
}}

Archivo: {filename}
Fuente sugerida: {source_hint}

Documento Markdown:
---
{clipped_markdown}
---
""".strip()


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    return json.loads(cleaned)


def upload_to_file_search(filename: str, markdown: str, metadata: LegalMetadata) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY no esta configurado")

    return upload_to_file_search_store(
        filename=filename,
        markdown=markdown,
        metadata=metadata,
        file_search_store_name=settings.gemini_file_search_store,
        create_store_if_missing=True,
        wait_until_done=True,
        max_wait_seconds=600,
    )


def upload_to_file_search_store(
    filename: str,
    markdown: str,
    metadata: LegalMetadata,
    file_search_store_name: str | None,
    create_store_if_missing: bool,
    wait_until_done: bool,
    max_wait_seconds: int,
) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY no esta configurado")
    if not file_search_store_name:
        raise RuntimeError("file_search_store_name o GEMINI_FILE_SEARCH_STORE no esta configurado")

    import tempfile
    from pathlib import Path

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    store_name = _resolve_file_search_store_name(
        client=client,
        types=types,
        requested_name=file_search_store_name,
        create_store_if_missing=create_store_if_missing,
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / filename
        path.write_text(markdown, encoding="utf-8")
        custom_metadata = _build_custom_metadata(types, metadata)
        _validate_custom_metadata_lengths(custom_metadata)
        try:
            operation = client.file_search_stores.upload_to_file_search_store(
                file_search_store_name=store_name,
                file=path,
                config=types.UploadToFileSearchStoreConfig(
                    display_name=filename,
                    mime_type="text/markdown",
                    custom_metadata=custom_metadata,
                ),
            )
        except Exception as exc:
            debug_metadata = _custom_metadata_debug(custom_metadata)
            raise RuntimeError(
                f"{exc}; custom_metadata_sent={json.dumps(debug_metadata, ensure_ascii=False)}"
            ) from exc

    final_operation = _wait_for_operation(client, operation, wait_until_done, max_wait_seconds)

    return {
        "enabled": True,
        "uploaded": True,
        "file_search_store": store_name,
        "file_uri": None,
        "raw_response": {
            "operation_name": getattr(final_operation, "name", None),
            "operation_done": getattr(final_operation, "done", None),
            "metadata": metadata.model_dump(),
        },
        "message": "Archivo enviado a Gemini File Search Store.",
    }


def _resolve_file_search_store_name(client, types, requested_name: str, create_store_if_missing: bool) -> str:
    if requested_name.startswith("fileSearchStores/"):
        return requested_name

    registry = _load_store_registry()
    if requested_name in registry:
        return registry[requested_name]

    stores = client.file_search_stores.list()
    for store in stores:
        if getattr(store, "display_name", None) == requested_name or getattr(store, "name", None) == requested_name:
            if getattr(store, "display_name", None) == requested_name:
                _save_store_registry_entry(requested_name, store.name)
            return store.name

    if not create_store_if_missing:
        raise RuntimeError(f"No existe File Search Store con display_name '{requested_name}'")

    store = client.file_search_stores.create(
        config=types.CreateFileSearchStoreConfig(display_name=requested_name)
    )
    _save_store_registry_entry(requested_name, store.name)
    return store.name


def resolve_file_search_store(display_name: str, create_if_missing: bool) -> dict[str, Any]:
    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY no esta configurado")
    if not display_name.strip():
        raise RuntimeError("display_name no puede estar vacio")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    registry = _load_store_registry()
    if display_name in registry:
        store_name = registry[display_name]
        created = False
    else:
        store_name = ""
        for store in client.file_search_stores.list():
            if getattr(store, "display_name", None) == display_name or getattr(store, "name", None) == display_name:
                store_name = store.name
                created = False
                break
        else:
            if not create_if_missing:
                raise RuntimeError(f"No existe File Search Store con display_name '{display_name}'")
            store = client.file_search_stores.create(
                config=types.CreateFileSearchStoreConfig(display_name=display_name)
            )
            store_name = store.name
            created = True

        _save_store_registry_entry(display_name, store_name)

    return {
        "display_name": display_name,
        "name": store_name,
        "created": created,
        "saved": True,
    }


def _build_custom_metadata(types, metadata: LegalMetadata) -> list[Any]:
    custom_metadata: list[Any] = []
    for key, value in metadata.model_dump().items():
        if value is None or value == "":
            continue
        if key == "categorias":
            custom_metadata.extend(_build_category_custom_metadata(types, value))
            continue
        metadata_key = "observaciones_resumen" if key == "observaciones" else key
        custom_metadata.append(
            types.CustomMetadata(
                key=metadata_key,
                string_value=_fit_custom_metadata_value(str(value)),
            )
        )
    return custom_metadata


def _validate_custom_metadata_lengths(custom_metadata: list[Any]) -> None:
    oversized = [
        item
        for item in _custom_metadata_debug(custom_metadata)
        if item["length"] > MAX_CUSTOM_METADATA_STRING_LENGTH
    ]
    if oversized:
        raise RuntimeError(
            "custom_metadata contiene string_value mayor a "
            f"{MAX_CUSTOM_METADATA_STRING_LENGTH} caracteres: "
            f"{json.dumps(oversized, ensure_ascii=False)}"
        )


def _custom_metadata_debug(custom_metadata: list[Any]) -> list[dict[str, Any]]:
    debug: list[dict[str, Any]] = []
    for index, item in enumerate(custom_metadata):
        key = getattr(item, "key", None)
        value = getattr(item, "string_value", None)
        if isinstance(item, dict):
            key = item.get("key")
            value = item.get("string_value")
        value = "" if value is None else str(value)
        debug.append(
            {
                "index": index,
                "key": key,
                "length": len(value),
                "value": value,
            }
        )
    return debug


def _build_category_custom_metadata(types, categories: list[dict[str, Any]]) -> list[Any]:
    entries = [_compact_category_entry(category) for category in categories]
    entries = [entry for entry in entries if entry]
    if not entries:
        return []

    custom_metadata = [
        types.CustomMetadata(
            key="categorias_count",
            string_value=str(len(entries)),
        )
    ]
    chunks = _split_metadata_entries(entries)
    if len(chunks) == 1:
        custom_metadata.append(types.CustomMetadata(key="categorias_compact", string_value=chunks[0]))
        return custom_metadata

    for index, chunk in enumerate(chunks, start=1):
        custom_metadata.append(types.CustomMetadata(key=f"categorias_compact_{index}", string_value=chunk))
    return custom_metadata


def _compact_category_entry(category: dict[str, Any]) -> str:
    category_name = str(category.get("categoria") or "").strip()
    if not category_name:
        return ""

    category_code = CATEGORY_METADATA_CODES.get(category_name, _metadata_code(category_name))
    subcategories = category.get("subcategorias") or []
    subcategory_codes = [
        SUBCATEGORY_METADATA_CODES.get(str(subcategory), _metadata_code(str(subcategory)))
        for subcategory in subcategories
        if str(subcategory).strip()
    ]
    if not subcategory_codes:
        return category_code
    return f"{category_code}:{','.join(subcategory_codes)}"


def _split_metadata_entries(entries: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""

    for entry in entries:
        entry = _fit_custom_metadata_value(entry)
        candidate = entry if not current else f"{current}|{entry}"
        if len(candidate) <= MAX_CUSTOM_METADATA_STRING_LENGTH:
            current = candidate
            continue

        if current:
            chunks.append(current)
        current = entry

    if current:
        chunks.append(current)
    return chunks


def _fit_custom_metadata_value(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= MAX_CUSTOM_METADATA_STRING_LENGTH:
        return compact

    sentence_end = compact.find(". ")
    if 0 < sentence_end + 1 <= MAX_CUSTOM_METADATA_STRING_LENGTH:
        return compact[: sentence_end + 1]

    return compact[: MAX_CUSTOM_METADATA_STRING_LENGTH - 3].rstrip() + "..."


def _metadata_code(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    code = re.sub(r"[^a-z0-9]+", "_", without_accents.casefold()).strip("_")
    return code or "unknown"


def _wait_for_operation(client, operation, wait_until_done: bool, max_wait_seconds: int):
    if not wait_until_done:
        return operation

    deadline = time.time() + max_wait_seconds
    current = operation
    while not getattr(current, "done", False):
        if time.time() >= deadline:
            raise TimeoutError(f"Gemini File Search upload no termino en {max_wait_seconds} segundos")
        time.sleep(1)
        current = client.operations.get(current)
    return current


def _load_store_registry() -> dict[str, str]:
    if not STORE_REGISTRY_PATH.exists():
        return {}
    try:
        data = json.loads(STORE_REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _save_store_registry_entry(display_name: str, store_name: str) -> None:
    STORE_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    registry = _load_store_registry()
    registry[display_name] = store_name
    STORE_REGISTRY_PATH.write_text(json.dumps(registry, indent=2, ensure_ascii=False), encoding="utf-8")
