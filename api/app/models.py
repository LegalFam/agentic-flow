from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.categories import canonicalize_category_pair, validate_category_pair


class CategoryGroup(BaseModel):
    categoria: str
    subcategorias: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_pair_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "subcategorias" in data:
            return data

        subcategory = data.get("subcategoria")
        subcategories = [] if subcategory is None else [subcategory]
        return {
            **data,
            "subcategorias": subcategories,
        }

    @field_validator("subcategorias", mode="before")
    @classmethod
    def normalize_subcategories_input(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

    @model_validator(mode="before")
    @classmethod
    def canonicalize_group(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        category = data.get("categoria")
        subcategories = data.get("subcategorias")
        if not isinstance(category, str) or not isinstance(subcategories, list):
            return data

        canonical_subcategories: list[str] = []
        canonical_category = category
        for subcategory in subcategories:
            if not isinstance(subcategory, str) or not subcategory.strip():
                continue
            canonical_category, canonical_subcategory = canonicalize_category_pair(category, subcategory)
            if canonical_subcategory is not None and canonical_subcategory not in canonical_subcategories:
                canonical_subcategories.append(canonical_subcategory)
        if not canonical_subcategories:
            canonical_category, _ = canonicalize_category_pair(category, None)

        return {
            **data,
            "categoria": canonical_category,
            "subcategorias": canonical_subcategories,
        }

    @model_validator(mode="after")
    def validate_group(self) -> "CategoryGroup":
        if not self.subcategorias:
            if not validate_category_pair(self.categoria, None):
                raise ValueError("categoria/subcategorias fuera del catalogo permitido")
            return self

        for subcategory in self.subcategorias:
            if not validate_category_pair(self.categoria, subcategory):
                raise ValueError("categoria/subcategorias fuera del catalogo permitido")
        return self


def _group_category_entries(entries: Any) -> Any:
    if not isinstance(entries, list):
        return entries

    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            return entries

        category = entry.get("categoria")
        if not isinstance(category, str):
            return entries

        if "subcategorias" in entry:
            raw_subcategories = entry.get("subcategorias")
            if raw_subcategories is None:
                subcategories: list[Any] = []
            elif isinstance(raw_subcategories, list):
                subcategories = raw_subcategories
            else:
                return entries
        else:
            raw_subcategory = entry.get("subcategoria")
            subcategories = [] if raw_subcategory is None else [raw_subcategory]

        canonical_category, _ = canonicalize_category_pair(category, None)
        current = grouped.setdefault(canonical_category, {"categoria": canonical_category, "subcategorias": []})
        for subcategory in subcategories:
            if not isinstance(subcategory, str) or not subcategory.strip():
                continue
            _, canonical_subcategory = canonicalize_category_pair(category, subcategory)
            if canonical_subcategory is not None and canonical_subcategory not in current["subcategorias"]:
                current["subcategorias"].append(canonical_subcategory)

    return list(grouped.values())


class CategoryAssignment(BaseModel):
    categoria: str
    subcategoria: str | None = None

    @model_validator(mode="before")
    @classmethod
    def from_group_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        subcategories = data.get("subcategorias")
        if isinstance(subcategories, list):
            subcategory = subcategories[0] if subcategories else None
            return {
                **data,
                "subcategoria": subcategory,
            }
        return data

    @model_validator(mode="after")
    def validate_pair(self) -> "CategoryAssignment":
        if not validate_category_pair(self.categoria, self.subcategoria):
            raise ValueError("categoria/subcategoria fuera del catalogo permitido")
        return self


class ConversionResponse(BaseModel):
    filename: str
    document_id: str
    markdown: str
    plain_text: str
    pages: int | None = None
    ocr_used: bool = False
    warnings: list[str] = Field(default_factory=list)


class ConversionJobResponse(BaseModel):
    job_id: str
    status: str
    filename: str
    result: ConversionResponse | None = None
    error: str | None = None


class MetadataRequest(BaseModel):
    filename: str
    markdown: str
    fuente: str | None = None


class LegalMetadata(BaseModel):
    titulo: str | None = None
    identificador: str | None = None
    entidad: str | None = None
    fecha: str | None = None
    materia: str | None = None
    expediente: str | None = None
    articulo: str | None = None
    norma: str | None = None
    pleno: str | None = None
    protocolo: str | None = None
    fuente: str | None = None
    categorias: list[CategoryGroup] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    observaciones: str | None = None

    @model_validator(mode="before")
    @classmethod
    def group_duplicate_categories(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "categorias" not in data:
            return data
        return {
            **data,
            "categorias": _group_category_entries(data["categorias"]),
        }

    @field_validator("titulo", "identificador", "entidad", "fecha", "materia", "expediente", "articulo", "norma", "pleno", "protocolo", "fuente", "observaciones", mode="before")
    @classmethod
    def empty_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

class MetadataResponse(BaseModel):
    filename: str
    metadata: LegalMetadata


class FileSearchUploadRequest(BaseModel):
    filename: str
    markdown: str
    metadata: LegalMetadata
    file_search_store_name: str | None = None
    create_store_if_missing: bool = True
    wait_until_done: bool = True
    max_wait_seconds: int = Field(default=600, ge=1, le=3600)


class FileSearchUploadResponse(BaseModel):
    enabled: bool
    uploaded: bool
    file_search_store: str | None = None
    file_uri: str | None = None
    raw_response: dict[str, Any] | None = None
    message: str


class FileSearchStoreResolveRequest(BaseModel):
    display_name: str
    create_if_missing: bool = True


class FileSearchStoreResolveResponse(BaseModel):
    display_name: str
    name: str
    created: bool
    saved: bool


class FileSearchDocument(BaseModel):
    name: str
    display_name: str = ""
    size_bytes: int = 0
    state: str = ""
    mime_type: str = ""
    create_time: str | None = None


class FileSearchDocumentListRequest(BaseModel):
    file_search_store_name: str | None = None


class FileSearchDocumentListResponse(BaseModel):
    file_search_store: str
    documents: list[FileSearchDocument] = Field(default_factory=list)


class FileSearchReplacePlanRequest(BaseModel):
    filename: str
    file_search_store_name: str | None = None


class FileSearchReplacePlanResponse(BaseModel):
    file_search_store: str
    filename: str
    replacement_key: str
    documents: int
    superseded: list[FileSearchDocument] = Field(default_factory=list)
    already_indexed: list[FileSearchDocument] = Field(default_factory=list)
    # "replace" (uno solo, caso normal), "new" (nada que reemplazar), "ambiguous" (varios
    # candidatos) o "identical" (mismo display_name, o sea el mismo PDF).
    verdict: str
    message: str


class FileSearchReplaceRequest(BaseModel):
    filename: str
    markdown: str
    metadata: LegalMetadata
    file_search_store_name: str | None = None
    # Nombres completos o display_name de los documentos a borrar. Manda sobre lo que
    # deduzca el plan: es la unica forma de resolver un caso ambiguo.
    supersedes: list[str] = Field(default_factory=list)
    allow_new: bool = False
    allow_multiple: bool = False
    sync_corpus: bool = True
    wait_until_done: bool = True
    max_wait_seconds: int = Field(default=900, ge=1, le=3600)


class FileSearchReplaceResponse(BaseModel):
    file_search_store: str
    filename: str
    uploaded: bool
    verdict: str = ""
    superseded: list[FileSearchDocument] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)
    delete_failed: list[dict[str, str]] = Field(default_factory=list)
    corpus: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    message: str


class RagSearchRequest(BaseModel):
    query: str
    original_message: str | None = None
    legal_category: str | None = None
    canonical_category: str | None = None
    canonical_subcategories: list[str] = Field(default_factory=list)
    metadata_hints: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    file_search_store_name: str | None = None


class RagCitation(BaseModel):
    citation_id: str = ""
    file_id: str = ""
    file_name: str = "Fuente legal"
    snippet: str = ""
    file_url: str = ""
    locator: str = ""
    breadcrumb: str = ""
    page: int | None = None
    locator_source: str = ""


class RagSearchResponse(BaseModel):
    answer: str
    citations: list[RagCitation] = Field(default_factory=list)
    valid: bool
    retry_suggestion: str | None = None


class LocatorResolveRequestCitation(BaseModel):
    citation_id: str = ""
    # Copia textual del fragmento del chunk que el agente XAI dice haber usado. Cuando se
    # verifica contra el chunk recuperado, la ubicacion sale de aca y no del chunk entero.
    original_snippet: str = ""


class LocatorResolveRequest(BaseModel):
    citations: list[LocatorResolveRequestCitation] = Field(default_factory=list)


class LocatorResolveResponseCitation(BaseModel):
    citation_id: str = ""
    locator: str = ""
    breadcrumb: str = ""
    page: int | None = None
    locator_source: str = ""
    resolved: bool = False
    # De donde salio la ubicacion: "excerpt" (el fragmento citado, un solo articulo),
    # "excerpt_multi" (el fragmento cruza articulos y se citan todos), "chunk" (el chunk
    # completo), "ambiguous" (abarca varios articulos que no se pudieron combinar y no
    # hubo excerpt verificable: se devuelve vacio) o "unknown" (citation_id que no esta
    # en el registro).
    locator_scope: str = ""
    chunk_articles: list[str] = Field(default_factory=list)
    excerpt_articles: list[str] = Field(default_factory=list)
    # Documento de la cita. Viaja por el registro y no por el prompt, igual que el
    # locator: asi la cita llega completa aunque el agente no copie estos campos.
    file_name: str = ""
    file_url: str = ""


class LocatorResolveResponse(BaseModel):
    citations: list[LocatorResolveResponseCitation] = Field(default_factory=list)
    resolved: int = 0
    unknown: int = 0
    from_excerpt: int = 0
    ambiguous: int = 0


class CorpusStatusResponse(BaseModel):
    corpus_dir: str
    exists: bool
    documents: int
    indexed_cached: int
    manifest_present: bool
    manifest_entries: int
    locator_enabled: bool
    sample: list[str] = Field(default_factory=list)


class CorpusReloadResponse(BaseModel):
    cleared: int
    documents: int


class LocatorProbeRequest(BaseModel):
    snippet: str
    title: str | None = None
    file_id: str | None = None


class LocatorProbeResponse(BaseModel):
    matched_document: str | None = None
    locator: str = ""
    breadcrumb: str = ""
    page: int | None = None
    locator_source: str = ""
