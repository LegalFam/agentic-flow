from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.categories import validate_category_pair


class CategoryAssignment(BaseModel):
    categoria: str
    subcategoria: str | None = None

    @field_validator("subcategoria", mode="before")
    @classmethod
    def empty_subcategory_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
    categorias: list[CategoryAssignment] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    observaciones: str | None = None

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
