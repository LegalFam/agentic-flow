from json import JSONDecodeError

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from app import corpus, locator, locator_registry, store_documents
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
    ConversionJobResponse,
    ConversionResponse,
    CorpusReloadResponse,
    CorpusStatusResponse,
    LocatorProbeRequest,
    LocatorProbeResponse,
    LocatorResolveRequest,
    LocatorResolveResponse,
    LocatorResolveResponseCitation,
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


@app.post("/file-search-stores/documents/plan-replace", response_model=FileSearchReplacePlanResponse)
async def plan_replace_document(payload: FileSearchReplacePlanRequest) -> FileSearchReplacePlanResponse:
    """Que version quedaria reemplazada, sin tocar el store.

    Es el paso que alimenta la revision manual: el borrado en File Search no se deshace,
    asi que quien aprueba tiene que ver antes el nombre exacto de lo que se va a borrar.
    """
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
    """Sube la version nueva, borra la vieja y sincroniza el corpus local."""
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
        # 409 y no 503: reintentar da el mismo resultado, hace falta que alguien decida.
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
    """Cambia los `citation_id` que trajeron los agentes por el locator autoritativo.

    Un id desconocido devuelve campos vacios, nunca un error: si el modelo altero el id o
    la entrada vencio, la cita debe salir sin ubicacion en vez de tumbar la respuesta del
    chat. Y nunca con la ubicacion que el modelo haya podido inventar.

    El texto que si se acepta del modelo es `original_snippet`, y solo como puntero: se
    verifica contra el chunk que se guardo al recuperarlo, y la ubicacion se recalcula
    sobre el documento. Si no se verifica, se descarta sin mas.
    """
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
            # El fragmento cruza varios articulos y no se pudieron combinar, o el agente
            # no dejo verificable cual uso: el primero es una moneda al aire y una
            # atribucion falsa es peor que ninguna.
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
                # El documento tampoco se acepta del modelo: viaja con el id opaco igual
                # que el locator, para que el backend no dependa de que el agente lo copie.
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
    """Reancla la cita sobre el fragmento citado.

    Devuelve `(campos, articulos que cubre el fragmento)`. Los campos son `None` cuando no
    se pudo verificar el fragmento, y tambien cuando cubre varios articulos que no se
    pudieron combinar: ahi la lista de articulos es lo que le dice al caller que la cita
    es ambigua y no que simplemente no hubo excerpt.
    """
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
    """Diagnostico: muestra que documento caso y con que estrategia se resolvio."""
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
