from datetime import date, timedelta
from json import JSONDecodeError

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from app import amendments, corpus, elperuano, locator, locator_registry, store_documents
from app.config import settings
from app.converter import convert_pdf_to_markdown
from app.conversion_jobs import get_conversion_job, start_conversion_job
from app.gemini_client import (
    extract_metadata_with_gemini,
    resolve_file_search_store,
    search_legal_rag,
    upload_to_file_search_store,
)
from app.models import (
    AmendmentDecisionRequest,
    AmendmentRollbackRequest,
    AmendmentScanRequest,
    AmendmentSnapshotRequest,
    ConversionJobResponse,
    ConversionResponse,
    CorpusReloadResponse,
    CorpusStatusResponse,
    LocatorProbeRequest,
    LocatorProbeResponse,
    LocatorResolveRequest,
    LocatorResolveResponse,
    LocatorResolveResponseCitation,
    FileSearchDocumentDeleteRequest,
    FileSearchDocumentDeleteResponse,
    FileSearchDocumentListRequest,
    FileSearchDocumentListResponse,
    FileSearchReplacePlanRequest,
    FileSearchReplacePlanResponse,
    FileSearchReplaceRequest,
    FileSearchReplaceResponse,
    FileSearchUploadRequest,
    FileSearchUploadResponse,
    FileSearchStoreResolveRequest,
    FileSearchStoreResolveResponse,
    MetadataRequest,
    MetadataResponse,
    RagSearchRequest,
    RagSearchResponse,
)

app = FastAPI(title="Legal PDF Processing API", version="0.1.0")


def error_detail(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def classify_processing_error(exc: Exception) -> tuple[int, dict[str, str]]:
    text = str(exc)
    normalized = text.lower()

    if isinstance(exc, TimeoutError) or "timeout" in normalized or "no termino" in normalized:
        return 504, error_detail("UPSTREAM_TIMEOUT", "El procesamiento tardo mas de lo esperado.")
    if isinstance(exc, (JSONDecodeError, ValidationError)) or "json" in normalized or "valid" in normalized:
        return 422, error_detail("AGENT_VALIDATION_FAILED", "No se pudo validar la respuesta generada.")
    if "api_key" in normalized or "no esta configurado" in normalized or "no está configurado" in normalized:
        return 503, error_detail("UPSTREAM_NOT_CONFIGURED", "El servicio de IA no esta configurado.")
    if "file search" in normalized or "store" in normalized:
        return 503, error_detail("UPSTREAM_UNAVAILABLE", "No se pudo consultar la base de conocimiento.")
    if "gemini" in normalized:
        return 503, error_detail("UPSTREAM_UNAVAILABLE", "El servicio de IA no esta disponible en este momento.")
    return 422, error_detail("PROCESSING_FAILED", "No se pudo completar el procesamiento.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/convert", response_model=ConversionResponse)
async def convert(file: UploadFile = File(...)) -> ConversionResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail=error_detail("INVALID_FILE_TYPE", "Solo se aceptan archivos PDF."))

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=error_detail("FILE_TOO_LARGE", f"Archivo supera {settings.max_upload_mb} MB."))

    try:
        return ConversionResponse.model_validate(convert_pdf_to_markdown(file.filename, content))
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/convert-jobs", response_model=ConversionJobResponse)
async def start_convert_job(file: UploadFile = File(...)) -> ConversionJobResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail=error_detail("INVALID_FILE_TYPE", "Solo se aceptan archivos PDF."))

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=error_detail("FILE_TOO_LARGE", f"Archivo supera {settings.max_upload_mb} MB."))

    return ConversionJobResponse.model_validate(start_conversion_job(file.filename, content))


@app.get("/convert-jobs/{job_id}", response_model=ConversionJobResponse)
def convert_job_status(job_id: str) -> ConversionJobResponse:
    job = get_conversion_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=error_detail("JOB_NOT_FOUND", "Conversion job no existe."))
    return ConversionJobResponse.model_validate(job)


@app.post("/extract-metadata", response_model=MetadataResponse)
async def extract_metadata(payload: MetadataRequest) -> MetadataResponse:
    try:
        metadata = extract_metadata_with_gemini(
            filename=payload.filename,
            markdown=payload.markdown,
            fuente=payload.fuente,
        )
        return MetadataResponse(filename=payload.filename, metadata=metadata)
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/upload-gemini-file-search", response_model=FileSearchUploadResponse)
async def upload_file_search(payload: FileSearchUploadRequest) -> FileSearchUploadResponse:
    try:
        return FileSearchUploadResponse.model_validate(
            upload_to_file_search_store(
                filename=payload.filename,
                markdown=payload.markdown,
                metadata=payload.metadata,
                file_search_store_name=payload.file_search_store_name,
                create_store_if_missing=payload.create_store_if_missing,
                wait_until_done=payload.wait_until_done,
                max_wait_seconds=payload.max_wait_seconds,
            )
        )
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/file-search-stores/resolve", response_model=FileSearchStoreResolveResponse)
async def resolve_store(payload: FileSearchStoreResolveRequest) -> FileSearchStoreResolveResponse:
    try:
        return FileSearchStoreResolveResponse.model_validate(
            resolve_file_search_store(
                display_name=payload.display_name,
                create_if_missing=payload.create_if_missing,
            )
        )
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/file-search-stores/documents", response_model=FileSearchDocumentListResponse)
async def list_store_documents(payload: FileSearchDocumentListRequest) -> FileSearchDocumentListResponse:
    try:
        return FileSearchDocumentListResponse.model_validate(
            store_documents.list_store_documents(payload.file_search_store_name)
        )
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/file-search-stores/documents/delete", response_model=FileSearchDocumentDeleteResponse)
async def delete_store_document(payload: FileSearchDocumentDeleteRequest) -> FileSearchDocumentDeleteResponse:
    try:
        return FileSearchDocumentDeleteResponse.model_validate(
            store_documents.delete_store_document(
                document=payload.document,
                file_search_store_name=payload.file_search_store_name,
            )
        )
    except store_documents.ReplaceRefused as exc:
        raise HTTPException(
            status_code=409, detail=error_detail("DELETE_NEEDS_DECISION", str(exc))
        ) from exc
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/file-search-stores/documents/plan-replace", response_model=FileSearchReplacePlanResponse)
async def plan_replace_document(payload: FileSearchReplacePlanRequest) -> FileSearchReplacePlanResponse:
    try:
        return FileSearchReplacePlanResponse.model_validate(
            store_documents.plan_replacement(
                filename=payload.filename,
                file_search_store_name=payload.file_search_store_name,
            )
        )
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/file-search-stores/documents/replace", response_model=FileSearchReplaceResponse)
async def replace_store_document(payload: FileSearchReplaceRequest) -> FileSearchReplaceResponse:
    try:
        return FileSearchReplaceResponse.model_validate(
            store_documents.replace_document(
                filename=payload.filename,
                markdown=payload.markdown,
                metadata=payload.metadata,
                file_search_store_name=payload.file_search_store_name,
                supersedes=payload.supersedes,
                allow_new=payload.allow_new,
                allow_multiple=payload.allow_multiple,
                sync_corpus=payload.sync_corpus,
                wait_until_done=payload.wait_until_done,
                max_wait_seconds=payload.max_wait_seconds,
            )
        )
    except store_documents.ReplaceRefused as exc:
        raise HTTPException(
            status_code=409, detail=error_detail("REPLACE_NEEDS_DECISION", str(exc))
        ) from exc
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.post("/rag-search", response_model=RagSearchResponse)
async def rag_search(payload: RagSearchRequest) -> RagSearchResponse:
    try:
        return RagSearchResponse.model_validate(
            search_legal_rag(
                query=payload.query,
                original_message=payload.original_message,
                legal_category=payload.legal_category,
                canonical_category=payload.canonical_category,
                canonical_subcategories=payload.canonical_subcategories,
                metadata_hints=payload.metadata_hints,
                search_terms=payload.search_terms,
                file_search_store_name=payload.file_search_store_name,
            )
        )
    except Exception as exc:
        status_code, detail = classify_processing_error(exc)
        raise HTTPException(status_code=status_code, detail=detail) from exc


@app.get("/corpus/status", response_model=CorpusStatusResponse)
def corpus_status() -> CorpusStatusResponse:
    return CorpusStatusResponse.model_validate(corpus.status())


@app.post("/corpus/reload", response_model=CorpusReloadResponse)
def corpus_reload() -> CorpusReloadResponse:
    cleared = corpus.clear_cache()
    return CorpusReloadResponse(cleared=cleared, documents=len(corpus.iter_corpus_files()))


@app.post("/resolve-locators", response_model=LocatorResolveResponse)
def resolve_locators(payload: LocatorResolveRequest) -> LocatorResolveResponse:
    citations = []
    resolved = 0
    from_excerpt = 0
    ambiguous = 0

    for requested in payload.citations:
        entry = locator_registry.resolve_entry(requested.citation_id)
        if entry is None:
            citations.append(
                LocatorResolveResponseCitation(
                    citation_id=requested.citation_id,
                    resolved=False,
                    locator_scope="unknown",
                    **locator_registry.EMPTY,
                )
            )
            continue

        resolved += 1
        fields = entry["fields"]
        context = entry["context"]
        articles = context.get("articles") or []

        excerpt_fields, excerpt_articles = _locator_from_excerpt(context, requested.original_snippet)
        if excerpt_fields is not None:
            fields = excerpt_fields
            scope = "excerpt_multi" if len(excerpt_articles) > 1 else "excerpt"
            from_excerpt += 1
        elif len(excerpt_articles) > 1 or (
            len(articles) > 1 and settings.locator_require_excerpt_when_ambiguous
        ):
            fields = locator_registry.EMPTY
            scope = "ambiguous"
            ambiguous += 1
        else:
            scope = "chunk"

        citations.append(
            LocatorResolveResponseCitation(
                citation_id=requested.citation_id,
                resolved=True,
                locator_scope=scope,
                chunk_articles=articles,
                excerpt_articles=excerpt_articles,
                file_name=context.get("file_name") or "",
                file_url=context.get("file_url") or "",
                **fields,
            )
        )

    return LocatorResolveResponse(
        citations=citations,
        resolved=resolved,
        unknown=len(citations) - resolved,
        from_excerpt=from_excerpt,
        ambiguous=ambiguous,
    )


def _locator_from_excerpt(context: dict, excerpt: str) -> tuple[dict | None, list[str]]:
    if not settings.enable_citation_locator or not excerpt or not context.get("snippet"):
        return None, []

    try:
        index = corpus.get_index(context.get("title"), context.get("file_id"))
        found, articles = locator.resolve_excerpt_span(index, context["snippet"], excerpt)
    except Exception:
        return None, []

    if found.is_empty():
        return None, articles

    return {
        "locator": found.label,
        "breadcrumb": found.breadcrumb,
        "page": found.page,
        "locator_source": found.source,
    }, articles


@app.post("/locator/probe", response_model=LocatorProbeResponse)
def locator_probe(payload: LocatorProbeRequest) -> LocatorProbeResponse:
    path = corpus.resolve_document_path(payload.title, payload.file_id)
    index = corpus.load_index(path) if path is not None else None
    found = locator.resolve(index, payload.snippet)
    return LocatorProbeResponse(
        matched_document=path.name if path is not None else None,
        locator=found.label,
        breadcrumb=found.breadcrumb,
        page=found.page,
        locator_source=found.source,
    )


@app.post("/metadata-from-form", response_model=MetadataResponse)
async def metadata_from_form(
    filename: str = Form(...),
    markdown: str = Form(...),
    fuente: str | None = Form(None),
) -> MetadataResponse:
    payload = MetadataRequest(filename=filename, markdown=markdown, fuente=fuente)
    return await extract_metadata(payload)


# ----------------------------------------------------------------------------- amendments
# Actualizacion de normas desde El Peruano. Los endpoints son `def` (no async) a proposito:
# un escaneo hace decenas de llamadas bloqueantes a El Peruano y a Gemini, y asi FastAPI
# los corre en su threadpool sin frenar el resto de la API.


def _amendment_error(exc: Exception) -> HTTPException:
    if isinstance(exc, amendments.BackupError):
        return HTTPException(status_code=409, detail=error_detail("BACKUP_FAILED", str(exc)))
    if isinstance(exc, (amendments.AmendmentError, store_documents.ReplaceRefused)):
        return HTTPException(status_code=409, detail=error_detail("AMENDMENT_REFUSED", str(exc)))
    if isinstance(exc, elperuano.ElPeruanoFormatError):
        return HTTPException(status_code=502, detail=error_detail("SOURCE_FORMAT_CHANGED", str(exc)))
    if isinstance(exc, elperuano.ElPeruanoError):
        return HTTPException(status_code=502, detail=error_detail("SOURCE_UNAVAILABLE", str(exc)))
    status_code, detail = classify_processing_error(exc)
    return HTTPException(status_code=status_code, detail=detail)


@app.get("/amendments/registry")
def amendments_registry() -> dict:
    return {"normas": [vars(norm) for norm in amendments.load_registry()]}


@app.post("/amendments/scan")
def amendments_scan(payload: AmendmentScanRequest) -> dict:
    today = date.today()
    try:
        fecha_fin = date.fromisoformat(payload.fecha_fin) if payload.fecha_fin else today
        fecha_ini = date.fromisoformat(payload.fecha_ini) if payload.fecha_ini else fecha_fin - timedelta(days=1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=error_detail("INVALID_DATE", str(exc))) from exc
    if fecha_ini > fecha_fin:
        raise HTTPException(status_code=400, detail=error_detail("INVALID_DATE", "fecha_ini es posterior a fecha_fin"))
    try:
        return amendments.scan(fecha_ini, fecha_fin, payload.documents)
    except Exception as exc:
        raise _amendment_error(exc) from exc


@app.get("/amendments/proposals")
def amendments_proposals(status: str | None = "pending") -> dict:
    return {"proposals": amendments.list_proposals(status or None)}


@app.get("/amendments/proposals/{proposal_id}")
def amendments_proposal(proposal_id: str) -> dict:
    try:
        return amendments.load_proposal(proposal_id)
    except Exception as exc:
        raise _amendment_error(exc) from exc


@app.post("/amendments/decide")
def amendments_decide(payload: AmendmentDecisionRequest) -> dict:
    try:
        return amendments.decide(
            payload.proposal_id, payload.decision, payload.file_search_store_name, payload.decided_by
        )
    except Exception as exc:
        raise _amendment_error(exc) from exc


@app.get("/amendments/backups")
def amendments_backups(document: str | None = None) -> dict:
    return {"backups": amendments.list_backups(document)}


@app.post("/amendments/backups")
def amendments_snapshot(payload: AmendmentSnapshotRequest) -> dict:
    try:
        return amendments.snapshot(payload.documents, payload.file_search_store_name, payload.reason)
    except Exception as exc:
        raise _amendment_error(exc) from exc


@app.post("/amendments/rollback")
def amendments_rollback(payload: AmendmentRollbackRequest) -> dict:
    try:
        return amendments.rollback(payload.backup_id, payload.file_search_store_name, payload.decided_by)
    except Exception as exc:
        raise _amendment_error(exc) from exc


@app.get("/amendments/history")
def amendments_history(document: str | None = None) -> dict:
    entries = amendments.load_history()
    if document:
        entries = [entry for entry in entries if document in (entry.get("document"), entry.get("new_document"))]
    return {"history": entries}
