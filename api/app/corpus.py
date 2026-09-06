"""Carga los markdown originales de los documentos indexados en Gemini File Search.

La llave de union es el nombre del archivo: al subir se usa `display_name=filename`
(`upload_to_file_search_store`), y eso es lo que Gemini devuelve luego en
`retrieved_context.title`. Cuando los nombres del corpus no coinciden con los del store,
`corpus_manifest.json` resuelve el mapeo sin renombrar nada.

El corpus se monta como carpeta: bind mount `./work:/work` en local, Cloud Storage volume
en Cloud Run. Por eso aca solo hay lectura de filesystem, sin SDK de GCS.
"""

import json
import re
import threading
import unicodedata
from pathlib import Path

from app.config import settings
from app.locator import DocumentIndex, build_index

_CACHE: dict[str, tuple[tuple[int, int], DocumentIndex]] = {}
_LOCK = threading.Lock()


def corpus_path() -> Path:
    return Path(settings.corpus_dir)


def manifest_path() -> Path:
    return corpus_path() / settings.corpus_manifest_filename


def iter_corpus_files() -> list[Path]:
    root = corpus_path()
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("**/*.md") if path.is_file())


def load_manifest() -> dict[str, str]:
    """Mapea `display_name` del store -> archivo del corpus.

    Tolerante igual que `_load_store_registry` en `gemini_client`: si no existe o esta
    corrupto devuelve {} en vez de romper la busqueda.
    """
    path = manifest_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


# `build_document_id` en converter.py sube los documentos como
# "<stem-normalizado>-<sha256(pdf)[:12]>.md", asi que el display_name del store no es el
# nombre original del archivo. El hash se calcula sobre el PDF y no se puede recomputar
# desde el markdown, asi que al comparar se ignora.
_DOCUMENT_ID_HASH_RE = re.compile(r"-[0-9a-f]{12}$")


def strip_document_id_hash(stem: str) -> str:
    return _DOCUMENT_ID_HASH_RE.sub("", stem)


def normalize_key(value: str) -> str:
    """Misma normalizacion que `_metadata_code` en `gemini_client`."""
    normalized = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", without_accents.casefold()).strip("_")


def resolve_document_path(title: str | None, file_id: str | None) -> Path | None:
    files = iter_corpus_files()
    if not files:
        return None

    by_name = {path.name: path for path in files}
    by_key = {normalize_key(path.stem): path for path in files}
    # Por si el corpus si trae los nombres con hash y el store no.
    for path in files:
        by_key.setdefault(normalize_key(strip_document_id_hash(path.stem)), path)

    manifest = load_manifest()
    for candidate in (title, file_id):
        if not candidate:
            continue
        mapped = manifest.get(candidate)
        if mapped and mapped in by_name:
            return by_name[mapped]

    for candidate in (title, file_id):
        if not candidate:
            continue
        if candidate in by_name:
            return by_name[candidate]
        if f"{candidate}.md" in by_name:
            return by_name[f"{candidate}.md"]

    for candidate in (title, file_id):
        if not candidate:
            continue
        key = normalize_key(Path(candidate).stem)
        if key and key in by_key:
            return by_key[key]

    # Ultimo intento: el mismo stem sin el hash de document_id.
    for candidate in (title, file_id):
        if not candidate:
            continue
        key = normalize_key(strip_document_id_hash(Path(candidate).stem))
        if key and key in by_key:
            return by_key[key]

    return None


def load_index(path: Path) -> DocumentIndex | None:
    """Indice cacheado por (mtime, size): reemplazar un .md invalida solo ese documento."""
    try:
        stat = path.stat()
    except OSError:
        return None

    stamp = (stat.st_mtime_ns, stat.st_size)
    key = str(path)

    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]

    try:
        markdown = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    index = build_index(markdown)
    with _LOCK:
        _CACHE[key] = (stamp, index)
    return index


def get_index(title: str | None, file_id: str | None) -> DocumentIndex | None:
    path = resolve_document_path(title, file_id)
    if path is None:
        return None
    return load_index(path)


def clear_cache() -> int:
    with _LOCK:
        size = len(_CACHE)
        _CACHE.clear()
    return size


def sync_replacement(filename: str, markdown: str, removed_display_names: list[str]) -> dict:
    """Deja el corpus local en el mismo estado que el store tras un reemplazo.

    Sin esto queda el peor caso silencioso que describe el README: el nombre viejo sigue
    casando, el locator indexa un texto que ya no es el indexado, y las citas de ese
    documento pierden ubicacion sin ningun error visible.

    En Cloud Run el corpus es un volumen de Cloud Storage de solo lectura, asi que no
    poder escribir es un resultado esperado y no una excepcion: se reporta `synced: False`
    con el motivo y el operador sincroniza el bucket.
    """
    root = corpus_path()
    if not root.is_dir():
        return {"synced": False, "reason": f"{root} no existe"}

    target = root / Path(filename).name
    if target.suffix != ".md":
        target = target.with_suffix(".md")

    # Los viejos se resuelven ANTES de escribir el nuevo: los dos normalizan a la misma
    # llave (es lo que los emparejo), asi que despues de escribirlo `resolve_document_path`
    # del nombre viejo devolveria el archivo nuevo y lo borrariamos recien escrito.
    stale = []
    for display_name in removed_display_names:
        path = resolve_document_path(display_name, None)
        if path is not None and path != target and path not in stale:
            stale.append(path)

    try:
        target.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        return {"synced": False, "reason": f"no se pudo escribir {target}: {exc}"}

    removed: list[str] = []
    failed: list[str] = []
    for path in stale:
        try:
            path.unlink()
            removed.append(path.name)
        except OSError as exc:
            failed.append(f"{path.name}: {exc}")

    manifest_pruned = _prune_manifest(removed_display_names, removed)
    clear_cache()

    return {
        "synced": True,
        "written": target.name,
        "removed": removed,
        "remove_failed": failed,
        "manifest_pruned": manifest_pruned,
    }


def _prune_manifest(removed_display_names: list[str], removed_files: list[str]) -> list[str]:
    """Saca del manifest las entradas que apuntan a lo que ya no existe.

    Una entrada huerfana no rompe la resolucion, pero sobrevive a varias revisiones y
    termina mapeando un nombre viejo a un archivo que alguien recreo con otro contenido.
    """
    manifest = load_manifest()
    if not manifest:
        return []

    pruned = [
        key
        for key, value in manifest.items()
        if key in set(removed_display_names) or value in set(removed_files)
    ]
    if not pruned:
        return []

    remaining = {key: value for key, value in manifest.items() if key not in set(pruned)}
    try:
        manifest_path().write_text(json.dumps(remaining, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        return []
    return pruned


def status() -> dict:
    files = iter_corpus_files()
    root = corpus_path()
    with _LOCK:
        cached = len(_CACHE)
    return {
        "corpus_dir": str(root),
        "exists": root.is_dir(),
        "documents": len(files),
        "indexed_cached": cached,
        "manifest_present": manifest_path().exists(),
        "manifest_entries": len(load_manifest()),
        "locator_enabled": settings.enable_citation_locator,
        "sample": [path.name for path in files[:20]],
    }
