from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    max_upload_mb: int = 75
    min_text_chars_for_native_pdf: int = 800

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    # Razonamiento de la busqueda con grounding (/rag-search). Lo que razona ahi no lo ve
    # nadie: el RAG Agent y el XAI Agent rehacen la orientacion. Medido en vivo, el thinking
    # dinamico le sumaba varios segundos por turno sin cambiar que fragmentos se recuperan.
    # En modelos 2.5 se aplica como presupuesto de tokens (0 lo apaga, -1 lo deja dinamico);
    # en Gemini 3 o superior, como nivel (minimal, low, medium, high; vacio = el del modelo).
    gemini_rag_thinking_budget: int = 0
    gemini_rag_thinking_level: str = "minimal"
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
    locator_min_articulado_articles: int = 20
    locator_min_excerpt_chars: int = 25
    locator_require_excerpt_when_ambiguous: bool = True
    locator_max_combined_articles: int = 5
    locator_align_min_coverage: float = 0.8

    # Actualizacion de normas desde El Peruano: registro de normas vigiladas, propuestas,
    # copias de seguridad e historial. Van junto al corpus porque se mueven con el.
    amendments_dir: str = "/work/amendments"
    amendments_registry_path: str = "/work/normas_vigiladas.json"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
