import json
import re
import time
from pathlib import Path
from typing import Any

from app.categories import category_catalog_text
from app.config import settings
from app.models import LegalMetadata


STORE_REGISTRY_PATH = Path("/work/file_search_stores.json")


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
        operation = client.file_search_stores.upload_to_file_search_store(
            file_search_store_name=store_name,
            file=path,
            config=types.UploadToFileSearchStoreConfig(
                display_name=filename,
                mime_type="text/markdown",
                custom_metadata=_build_custom_metadata(types, metadata),
            ),
        )

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
            custom_metadata.append(
                types.CustomMetadata(
                    key="categorias_json",
                    string_value=json.dumps(value, ensure_ascii=False),
                )
            )
            custom_metadata.append(
                types.CustomMetadata(
                    key="categorias_text",
                    string_value="; ".join(
                        f"{item['categoria']} > {', '.join(item['subcategorias'])}"
                        if item.get("subcategorias")
                        else item["categoria"]
                        for item in value
                    ),
                )
            )
            continue
        custom_metadata.append(types.CustomMetadata(key=key, string_value=str(value)))
    return custom_metadata


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
