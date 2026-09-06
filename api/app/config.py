from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    max_upload_mb: int = 75
    min_text_chars_for_native_pdf: int = 800

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_file_search_store: str | None = None
    enable_gemini_file_search_upload: bool = False

    google_cloud_project: str | None = None
    document_ai_location: str = "us"
    document_ai_processor_id: str | None = None

    corpus_dir: str = "/work/corpus"
    corpus_manifest_filename: str = "corpus_manifest.json"
    enable_citation_locator: bool = True
    locator_registry_ttl_seconds: int = 900
    locator_registry_max_entries: int = 5000
    locator_fuzzy_threshold: float = 0.82
    locator_max_article_span: int = 20000
    # Debajo de esto un excerpt no discrimina un articulo de otro dentro del chunk.
    locator_min_excerpt_chars: int = 25
    # Un chunk que abarca varios articulos y sin excerpt verificable sale sin ubicacion:
    # quedarse con el primero seria adivinar cual sustenta la respuesta.
    locator_require_excerpt_when_ambiguous: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
