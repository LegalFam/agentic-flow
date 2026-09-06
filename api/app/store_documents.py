"""Reemplazar un documento ya indexado en un Gemini File Search Store.

File Search no tiene update in-place: un documento solo se puede borrar y volver a
subir. Si la version nueva entra sin sacar la vieja, el store queda con las dos y el RAG
recupera texto contradictorio del mismo cuerpo legal sin forma de saber cual rige.

El caso que motiva este modulo es el PDF actualizado. `build_document_id` en
`converter.py` nombra los documentos como `<stem>-<sha256(pdf)[:12]>.md`, asi que un PDF
nuevo produce un `display_name` distinto: la version vieja no se encuentra por igualdad
de nombre. Se encuentra por la misma llave que usa el locator para casar corpus y store,
el stem normalizado sin el hash (`corpus.normalize_key` sobre
`corpus.strip_document_id_hash`).

Esa llave puede casar con mas de un documento, o con ninguno. En ambos casos el modulo
se niega y levanta `ReplaceRefused` en vez de elegir: borrar el documento equivocado no
se deshace, y la lista de documentos del store es lo unico que se tiene para decidir.
Quien llama resuelve la ambiguedad pasando `supersedes` explicito.
"""

from pathlib import Path
from typing import Any

from app import corpus
from app.config import settings
from app.models import LegalMetadata

# Mismo patron que `corpus_diff`: los helpers de store viven en `gemini_client`, que es
# quien construye el cliente del SDK y arma el custom_metadata.
from app.gemini_client import (
    _resolve_file_search_store_name,
    build_client,
    upload_document,
)


class ReplaceRefused(RuntimeError):
    """El reemplazo necesita una decision humana, no un reintento.

    Se distingue de un fallo de Gemini porque la respuesta HTTP es distinta: 409 y no
    503. Un 503 invita a reintentar; aca reintentar da exactamente el mismo resultado.
    """


def replacement_key(display_name: str) -> str:
    """Llave que identifica al documento a traves de versiones.

    Ignora el hash del `document_id` (se calcula sobre los bytes del PDF, cambia con cada
    revision) y los acentos y separadores, igual que `corpus.resolve_document_path`.
    """
    return corpus.normalize_key(corpus.strip_document_id_hash(Path(display_name).stem))


def _document_summary(document: Any) -> dict[str, Any]:
    state = getattr(document, "state", None)
    return {
        "name": getattr(document, "name", "") or "",
        "display_name": getattr(document, "display_name", "") or "",
        "size_bytes": getattr(document, "size_bytes", 0) or 0,
        "state": getattr(state, "name", None) or (str(state) if state else ""),
        "mime_type": getattr(document, "mime_type", "") or "",
        "create_time": _as_text(getattr(document, "create_time", None)),
    }


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _resolve_store(client, types, requested: str | None) -> str:
    name = requested or settings.gemini_file_search_store
    if not name:
        raise RuntimeError("file_search_store_name o GEMINI_FILE_SEARCH_STORE no esta configurado")
    # Nunca se crea: reemplazar dentro de un store recien creado y vacio significaria que
    # el store pedido estaba mal escrito, y el documento terminaria donde nadie lo busca.
    return _resolve_file_search_store_name(
        client=client,
        types=types,
        requested_name=name,
        create_store_if_missing=False,
    )


def list_store_documents(file_search_store_name: str | None) -> dict[str, Any]:
    client, types = build_client()
    store = _resolve_store(client, types, file_search_store_name)
    documents = [
        _document_summary(item) for item in client.file_search_stores.documents.list(parent=store)
    ]
    documents.sort(key=lambda item: item["display_name"])
    return {"file_search_store": store, "documents": documents}


def plan_replacement(filename: str, file_search_store_name: str | None) -> dict[str, Any]:
    """Que se borraria si se subiera `filename`. No toca el store."""
    listed = list_store_documents(file_search_store_name)
    return _plan_from_documents(listed["file_search_store"], filename, listed["documents"])


def _plan_from_documents(store: str, filename: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    key = replacement_key(filename)
    superseded = [item for item in documents if replacement_key(item["display_name"]) == key]
    identical = [item for item in superseded if item["display_name"] == filename]

    if identical:
        # Mismo display_name = mismo sha256 del PDF = mismo archivo. No hay texto que
        # actualizar; lo que cambie aca seria la metadata, no el documento.
        verdict = "identical"
        message = (
            f"'{filename}' ya esta indexado con ese mismo nombre, o sea el mismo PDF byte a byte. "
            "Si el PDF es realmente nuevo, revisa que no hayas tomado el archivo viejo."
        )
    elif not superseded:
        verdict = "new"
        message = (
            f"Ningun documento del store comparte la llave '{key}'. Esto seria un alta nueva, "
            "no una actualizacion. Usa /upload-gemini-file-search, o allow_new si de verdad "
            "quieres darla de alta por aca."
        )
    elif len(superseded) > 1:
        verdict = "ambiguous"
        names = ", ".join(item["display_name"] for item in superseded)
        message = (
            f"La llave '{key}' casa con {len(superseded)} documentos ({names}). "
            "Indica en supersedes cual o cuales se reemplazan."
        )
    else:
        verdict = "replace"
        message = f"Se reemplaza '{superseded[0]['display_name']}' por '{filename}'."

    return {
        "file_search_store": store,
        "filename": filename,
        "replacement_key": key,
        "documents": len(documents),
        "superseded": superseded,
        "already_indexed": identical,
        "verdict": verdict,
        "message": message,
    }


def replace_document(
    filename: str,
    markdown: str,
    metadata: LegalMetadata,
    file_search_store_name: str | None,
    supersedes: list[str],
    allow_new: bool,
    allow_multiple: bool,
    sync_corpus: bool,
    wait_until_done: bool,
    max_wait_seconds: int,
) -> dict[str, Any]:
    """Sube la version nueva y despues borra la vieja.

    Ese orden no es casual. Si falla la subida, el store queda como estaba. Si en cambio
    se borrara primero y fallara la subida, el corpus perderia el documento entero, que
    es peor que tenerlo duplicado unos segundos.
    """
    client, types = build_client()
    store = _resolve_store(client, types, file_search_store_name)
    documents = [
        _document_summary(item) for item in client.file_search_stores.documents.list(parent=store)
    ]
    plan = _plan_from_documents(store, filename, documents)

    targets = _decide_targets(plan, documents, supersedes, allow_new, allow_multiple)

    upload_document(
        client=client,
        types=types,
        store_name=store,
        filename=filename,
        markdown=markdown,
        metadata=metadata,
        wait_until_done=wait_until_done,
        max_wait_seconds=max_wait_seconds,
    )

    deleted: list[str] = []
    delete_failed: list[dict[str, str]] = []
    for target in targets:
        try:
            client.file_search_stores.documents.delete(name=target["name"])
            deleted.append(target["display_name"])
        except Exception as exc:
            delete_failed.append(
                {"document": target["display_name"], "name": target["name"], "error": str(exc)}
            )

    warnings: list[str] = []
    if delete_failed:
        # No se revierte la subida: dejar el store sin ninguna version por un borrado a
        # medias seria peor. Pero el estado si tiene que salir dicho.
        warnings.append(
            "El store quedo con la version nueva Y con "
            f"{len(delete_failed)} version(es) vieja(s) que no se pudieron borrar. "
            "El RAG puede recuperar texto desactualizado hasta que se borren a mano."
        )

    corpus_result: dict[str, Any] = {"synced": False, "reason": "sync_corpus=false"}
    if sync_corpus:
        corpus_result = corpus.sync_replacement(filename, markdown, deleted)
        if not corpus_result.get("synced"):
            warnings.append(
                f"El corpus local no se sincronizo ({corpus_result.get('reason')}). "
                "Hasta que se copie el markdown nuevo, las citas de este documento "
                "saldran sin ubicacion."
            )

    return {
        "file_search_store": store,
        "filename": filename,
        "uploaded": True,
        "verdict": plan["verdict"],
        "superseded": targets,
        "deleted": deleted,
        "delete_failed": delete_failed,
        "corpus": corpus_result,
        "warnings": warnings,
        "message": (
            f"'{filename}' indexado; {len(deleted)} version(es) anterior(es) borrada(s)."
            if not delete_failed
            else f"'{filename}' indexado, pero quedaron {len(delete_failed)} version(es) vieja(s) en el store."
        ),
    }


def _decide_targets(
    plan: dict[str, Any],
    documents: list[dict[str, Any]],
    supersedes: list[str],
    allow_new: bool,
    allow_multiple: bool,
) -> list[dict[str, Any]]:
    """Que documentos se borran. Explicito manda; si no, el plan; nunca se adivina."""
    if supersedes:
        by_name = {item["name"]: item for item in documents}
        by_display = {item["display_name"]: item for item in documents}
        targets: list[dict[str, Any]] = []
        for requested in supersedes:
            found = by_name.get(requested) or by_display.get(requested)
            if found is None:
                raise ReplaceRefused(
                    f"supersedes menciona '{requested}', que no esta en el store. "
                    "Lista los documentos antes de decidir."
                )
            if found not in targets:
                targets.append(found)
        return targets

    verdict = plan["verdict"]
    if verdict == "new":
        if not allow_new:
            raise ReplaceRefused(plan["message"])
        return []
    if verdict == "ambiguous" and not allow_multiple:
        raise ReplaceRefused(plan["message"])
    return plan["superseded"]
