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
    locator_fuzzy_threshold: float = 0.82
    locator_max_article_span: int = 20000

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
