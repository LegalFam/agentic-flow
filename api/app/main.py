from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from app.config import settings
from app.converter import convert_pdf_to_markdown
from app.gemini_client import extract_metadata_with_gemini, resolve_file_search_store, upload_to_file_search_store
from app.models import (
    ConversionResponse,
    FileSearchUploadRequest,
    FileSearchUploadResponse,
    FileSearchStoreResolveRequest,
    FileSearchStoreResolveResponse,
    MetadataRequest,
    MetadataResponse,
)

app = FastAPI(title="Legal PDF Processing API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/convert", response_model=ConversionResponse)
async def convert(file: UploadFile = File(...)) -> ConversionResponse:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")

    content = await file.read()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Archivo supera {settings.max_upload_mb} MB")

    try:
        return ConversionResponse.model_validate(convert_pdf_to_markdown(file.filename, content))
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/metadata-from-form", response_model=MetadataResponse)
async def metadata_from_form(
    filename: str = Form(...),
    markdown: str = Form(...),
    fuente: str | None = Form(None),
) -> MetadataResponse:
    payload = MetadataRequest(filename=filename, markdown=markdown, fuente=fuente)
    return await extract_metadata(payload)
