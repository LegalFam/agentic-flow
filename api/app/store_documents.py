from pathlib import Path
from typing import Any

from app import corpus
from app.config import settings
from app.models import LegalMetadata

from app.gemini_client import (
    _resolve_file_search_store_name,
    build_client,
    upload_document,
)


class ReplaceRefused(RuntimeError):
    pass


def replacement_key(display_name: str) -> str:
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
    return _resolve_file_search_store_name(
        client=client,
        types=types,
        requested_name=name,
        create_store_if_missing=False,
    )


def _delete_document(client, types, name: str) -> None:
    client.file_search_stores.documents.delete(
        name=name,
        config=types.DeleteDocumentConfig(force=True),
    )


def delete_store_document(document: str, file_search_store_name: str | None) -> dict[str, Any]:
    client, types = build_client()
    store = _resolve_store(client, types, file_search_store_name)
    documents = [
        _document_summary(item) for item in client.file_search_stores.documents.list(parent=store)
    ]

    by_name = {item["name"]: item for item in documents}
    by_display = [item for item in documents if item["display_name"] == document]
    target = by_name.get(document)
    if target is None:
        if not by_display:
            raise ReplaceRefused(
                f"'{document}' no esta en el store {store}. Lista los documentos antes de borrar."
            )
        if len(by_display) > 1:
            names = ", ".join(item["name"] for item in by_display)
            raise ReplaceRefused(
                f"'{document}' casa con {len(by_display)} documentos ({names}). "
                "Pasa el name completo para decir cual."
            )
        target = by_display[0]

    _delete_document(client, types, target["name"])
    return {
        "file_search_store": store,
        "deleted": target,
        "message": f"'{target['display_name']}' borrado del store.",
    }


def list_store_documents(file_search_store_name: str | None) -> dict[str, Any]:
    client, types = build_client()
    store = _resolve_store(client, types, file_search_store_name)
    documents = [
        _document_summary(item) for item in client.file_search_stores.documents.list(parent=store)
    ]
    documents.sort(key=lambda item: item["display_name"])
    return {"file_search_store": store, "documents": documents}


def plan_replacement(filename: str, file_search_store_name: str | None) -> dict[str, Any]:
    listed = list_store_documents(file_search_store_name)
    return _plan_from_documents(listed["file_search_store"], filename, listed["documents"])


def _plan_from_documents(store: str, filename: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    key = replacement_key(filename)
    superseded = [item for item in documents if replacement_key(item["display_name"]) == key]
    identical = [item for item in superseded if item["display_name"] == filename]

    if identical:
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
            _delete_document(client, types, target["name"])
            deleted.append(target["display_name"])
        except Exception as exc:
            delete_failed.append(
                {"document": target["display_name"], "name": target["name"], "error": str(exc)}
            )

    warnings: list[str] = []
    if delete_failed:
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


def _raw_custom_metadata(document: Any) -> list[dict[str, str]]:
    """La metadata de un documento indexado, en la forma que acepta upload_document.

    Todo lo que este proyecto sube va como string_value. Si aparece otro tipo, alguien lo
    subio por otra via, y copiarlo como texto le cambiaria el tipo en silencio: se rechaza.
    """
    raw: list[dict[str, str]] = []
    for item in getattr(document, "custom_metadata", None) or []:
        key = getattr(item, "key", None)
        value = getattr(item, "string_value", None)
        if key is None:
            continue
        if value is None:
            raise ReplaceRefused(
                f"El documento {getattr(document, 'display_name', '?')} tiene metadata '{key}' "
                "que no es texto; no se puede copiar sin perderla."
            )
        raw.append({"key": str(key), "string_value": str(value)})
    return raw


def get_store_document(display_name: str, file_search_store_name: str | None) -> dict[str, Any]:
    """Un documento del store con su metadata cruda. Exige que el nombre sea unico."""
    client, types = build_client()
    store = _resolve_store(client, types, file_search_store_name)
    matches = [
        item
        for item in client.file_search_stores.documents.list(parent=store)
        if (getattr(item, "display_name", "") or "") == display_name
    ]
    if not matches:
        raise ReplaceRefused(f"'{display_name}' no esta en el store {store}.")
    if len(matches) > 1:
        raise ReplaceRefused(f"'{display_name}' aparece {len(matches)} veces en el store {store}.")
    document = matches[0]
    return {
        "file_search_store": store,
        **_document_summary(document),
        "custom_metadata": _raw_custom_metadata(document),
    }


def swap_document(
    upload_filename: str | None,
    markdown: str | None,
    raw_custom_metadata: list[dict[str, str]] | None,
    delete_display_names: list[str],
    file_search_store_name: str | None,
    sync_corpus: bool = True,
    wait_until_done: bool = True,
    max_wait_seconds: int = 900,
) -> dict[str, Any]:
    """Sube una version (opcional) y borra otras, en ese orden, con el corpus detras.

    Es la pieza comun de las actualizaciones por normas modificatorias, las derogaciones
    totales y los rollbacks. Respeta el mismo orden que replace_document: primero se sube lo
    nuevo y recien despues se borra lo viejo, porque un store sin ninguna version es peor que
    uno con dos durante unos segundos.
    """
    client, types = build_client()
    store = _resolve_store(client, types, file_search_store_name)
    documents = list(client.file_search_stores.documents.list(parent=store))
    by_display: dict[str, list[Any]] = {}
    for item in documents:
        by_display.setdefault(getattr(item, "display_name", "") or "", []).append(item)

    targets: list[Any] = []
    for display_name in delete_display_names:
        found = by_display.get(display_name, [])
        if len(found) != 1:
            raise ReplaceRefused(
                f"'{display_name}' aparece {len(found)} veces en el store {store}; "
                "se esperaba exactamente una. No se toco nada."
            )
        targets.append(found[0])

    if upload_filename is not None:
        if upload_filename in by_display and upload_filename not in delete_display_names:
            raise ReplaceRefused(f"'{upload_filename}' ya esta indexado en {store}. No se toco nada.")
        upload_document(
            client=client,
            types=types,
            store_name=store,
            filename=upload_filename,
            markdown=markdown or "",
            metadata=None,
            wait_until_done=wait_until_done,
            max_wait_seconds=max_wait_seconds,
            raw_custom_metadata=raw_custom_metadata or [],
        )

    deleted: list[str] = []
    delete_failed: list[dict[str, str]] = []
    for target in targets:
        display_name = getattr(target, "display_name", "")
        try:
            _delete_document(client, types, target.name)
            deleted.append(display_name)
        except Exception as exc:
            delete_failed.append({"document": display_name, "name": target.name, "error": str(exc)})

    warnings: list[str] = []
    if delete_failed:
        warnings.append(
            f"Quedaron {len(delete_failed)} version(es) que no se pudieron borrar; "
            "el RAG puede recuperar texto desactualizado hasta borrarlas a mano."
        )

    corpus_result: dict[str, Any] = {"synced": False, "reason": "sync_corpus=false"}
    if sync_corpus:
        if upload_filename is not None:
            corpus_result = corpus.sync_replacement(upload_filename, markdown or "", deleted)
        else:
            corpus_result = corpus.remove_documents(deleted)
        if not corpus_result.get("synced"):
            warnings.append(f"El corpus local no se sincronizo ({corpus_result.get('reason')}).")

    return {
        "file_search_store": store,
        "uploaded": upload_filename,
        "deleted": deleted,
        "delete_failed": delete_failed,
        "corpus": corpus_result,
        "warnings": warnings,
    }
