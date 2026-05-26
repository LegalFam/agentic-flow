from app.config import settings


class DocumentAiOcr:
    def __init__(self) -> None:
        self.project_id = settings.google_cloud_project
        self.location = settings.document_ai_location
        self.processor_id = settings.document_ai_processor_id

    def configured(self) -> bool:
        return bool(self.project_id and self.location and self.processor_id)

    def extract_markdown(self, pdf_content: bytes) -> str:
        if not self.configured():
            raise RuntimeError("Document AI OCR no esta configurado")

        from google.api_core.client_options import ClientOptions
        from google.cloud import documentai

        endpoint = f"{self.location}-documentai.googleapis.com"
        client = documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=endpoint)
        )
        name = client.processor_path(self.project_id, self.location, self.processor_id)
        request = documentai.ProcessRequest(
            name=name,
            raw_document=documentai.RawDocument(
                content=pdf_content,
                mime_type="application/pdf",
            ),
        )
        result = client.process_document(request=request)
        document = result.document
        return self._document_to_markdown(document)

    def _document_to_markdown(self, document) -> str:
        if not getattr(document, "pages", None):
            return document.text or ""

        chunks: list[str] = []
        full_text = document.text or ""
        for index, page in enumerate(document.pages, start=1):
            chunks.append(f"\n\n## Pagina {index}\n")
            lines = getattr(page, "lines", None) or []
            for line in lines:
                segment_text = self._layout_text(full_text, line.layout)
                if segment_text:
                    chunks.append(segment_text)
            if not lines:
                chunks.append(full_text)
        return "\n".join(chunks).strip()

    def _layout_text(self, full_text: str, layout) -> str:
        parts: list[str] = []
        for segment in layout.text_anchor.text_segments:
            start = int(segment.start_index or 0)
            end = int(segment.end_index)
            parts.append(full_text[start:end])
        return "".join(parts).strip()
