import hashlib
import re
import tempfile
from pathlib import Path

from app.config import settings
from app.ocr import DocumentAiOcr


def build_document_id(filename: str, content: bytes) -> str:
    stem = Path(filename).stem.lower()
    clean_stem = re.sub(r"[^a-z0-9]+", "-", stem).strip("-") or "documento"
    digest = hashlib.sha256(content).hexdigest()[:12]
    return f"{clean_stem}-{digest}"


def normalize_markdown(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = text.strip()
    return f"{text}\n" if text else ""


def plain_text_from_markdown(markdown: str) -> str:
    text = re.sub(r"`{3}.*?`{3}", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[#>*_`|~-]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def convert_pdf_to_markdown(filename: str, content: bytes) -> dict:
    warnings: list[str] = []
    document_id = build_document_id(filename, content)
    native_markdown = ""
    pages: int | None = None

    with tempfile.TemporaryDirectory() as temp_dir:
        pdf_path = Path(temp_dir) / filename
        pdf_path.write_bytes(content)

        try:
            native_markdown, pages = _convert_with_docling(pdf_path)
        except Exception as exc:
            warnings.append(f"docling_failed: {type(exc).__name__}: {exc}")
            try:
                native_markdown = _convert_with_pymupdf4llm(pdf_path)
            except Exception as fallback_exc:
                warnings.append(f"pymupdf4llm_failed: {type(fallback_exc).__name__}: {fallback_exc}")

    markdown = normalize_markdown(native_markdown)
    plain_text = plain_text_from_markdown(markdown)
    ocr_used = False

    if len(plain_text) < settings.min_text_chars_for_native_pdf:
        try:
            markdown = DocumentAiOcr().extract_markdown(content)
            markdown = normalize_markdown(markdown)
            plain_text = plain_text_from_markdown(markdown)
            ocr_used = True
        except Exception as exc:
            warnings.append(f"ocr_skipped_or_failed: {type(exc).__name__}: {exc}")

    if not markdown:
        raise ValueError("No se pudo extraer contenido Markdown del PDF")

    return {
        "filename": filename,
        "document_id": document_id,
        "markdown": markdown,
        "plain_text": plain_text,
        "pages": pages,
        "ocr_used": ocr_used,
        "warnings": warnings,
    }


def _convert_with_docling(pdf_path: Path) -> tuple[str, int | None]:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    markdown = result.document.export_to_markdown()
    pages = None
    if getattr(result, "pages", None) is not None:
        pages = len(result.pages)
    return markdown, pages


def _convert_with_pymupdf4llm(pdf_path: Path) -> str:
    import pymupdf4llm

    return pymupdf4llm.to_markdown(str(pdf_path))
