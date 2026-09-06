"""Registro autoritativo de locators, indexado por `citation_id`.

Las citas atraviesan dos agentes LLM antes de llegar al backend. Si el numero de articulo
viajara como texto por esos saltos, el modelo podria alterarlo sin que nadie lo note. En
vez de eso los agentes transportan un id opaco, y el locator real se recupera de aqui.

La propiedad que importa: un id corrupto no encuentra nada y la cita sale sin ubicacion.
Nunca sale con una ubicacion inventada.

El almacenamiento es en proceso porque el servicio corre con `--max-instances 1` (ver
`deploy-n8n.yml`): no hay varias instancias que desincronizar. Si el contenedor recicla
entre la busqueda y la resolucion, hay miss y la cita degrada, que es el modo seguro.
Si algun dia se sube ese limite, esto tiene que mudarse a un almacen compartido.
"""

import threading
import time

from app.config import settings

_ENTRIES: dict[str, tuple[float, dict]] = {}
_LOCK = threading.Lock()

EMPTY: dict = {"locator": "", "breadcrumb": "", "page": None, "locator_source": ""}

# Junto al locator se guarda de donde salio. Sin esto, `/resolve-locators` no podria
# reanclar el fragmento que el agente XAI dice haber usado: no sabria contra que documento
# ni contra que chunk verificarlo.
EMPTY_CONTEXT: dict = {"title": "", "file_id": "", "snippet": "", "articles": []}


def register(citation_id: str, fields: dict, context: dict | None = None) -> None:
    if not citation_id:
        return

    expires_at = time.monotonic() + settings.locator_registry_ttl_seconds
    entry = {"fields": dict(fields), "context": dict(context or EMPTY_CONTEXT)}
    with _LOCK:
        _ENTRIES[citation_id] = (expires_at, entry)
        if len(_ENTRIES) > settings.locator_registry_max_entries:
            _evict_locked()


def resolve(citation_id: str) -> dict | None:
    """Solo los campos del locator, que es lo que viaja en la respuesta de la cita."""
    entry = resolve_entry(citation_id)
    return None if entry is None else entry["fields"]


def resolve_entry(citation_id: str) -> dict | None:
    """Locator + contexto de resolucion (`{"fields": ..., "context": ...}`)."""
    if not citation_id:
        return None

    now = time.monotonic()
    with _LOCK:
        entry = _ENTRIES.get(citation_id)
        if entry is None:
            return None
        expires_at, stored = entry
        if expires_at <= now:
            del _ENTRIES[citation_id]
            return None
        return {"fields": dict(stored["fields"]), "context": dict(stored["context"])}


def _evict_locked() -> None:
    """Primero lo vencido; si aun sobra, lo mas proximo a vencer, que es lo mas viejo."""
    now = time.monotonic()
    for key in [key for key, (expires_at, _) in _ENTRIES.items() if expires_at <= now]:
        del _ENTRIES[key]

    excess = len(_ENTRIES) - settings.locator_registry_max_entries
    if excess <= 0:
        return

    oldest = sorted(_ENTRIES.items(), key=lambda item: item[1][0])[:excess]
    for key, _ in oldest:
        del _ENTRIES[key]


def clear() -> int:
    with _LOCK:
        size = len(_ENTRIES)
        _ENTRIES.clear()
    return size


def size() -> int:
    with _LOCK:
        return len(_ENTRIES)
