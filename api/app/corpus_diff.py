"""Cruza los documentos indexados en un File Search Store contra el corpus local.

La llave de union del citation locator es el nombre del archivo: la API sube con
`display_name=filename` y Gemini devuelve ese mismo valor en `retrieved_context.title`.
Si los markdown del corpus fueron renombrados respecto a lo indexado, nada casa y el
locator cae siempre al fallback por regex.

Este script responde tres preguntas antes de copiar nada:
  - que documentos hay realmente en el store
  - cuales de esos resuelven contra el corpus local
  - que entradas de manifest harian falta para los que no

    python -m app.corpus_diff --list-stores
    python -m app.corpus_diff --store "fileSearchStores/familylaw-k3uggnk7czvu"
    python -m app.corpus_diff --store "..." --write-manifest
"""

import argparse
import difflib
import json
import sys

from app import corpus
from app.config import settings

# Umbral alto: una sugerencia equivocada en el manifest es peor que ninguna.
SUGGESTION_CUTOFF = 0.72


def build_client():
    if not settings.gemini_api_key:
        raise SystemExit("GEMINI_API_KEY no esta configurado (revisa .env)")
    from google import genai

    return genai.Client(api_key=settings.gemini_api_key)


def list_stores(client) -> int:
    registry = _load_store_registry()
    print("Stores en la cuenta:\n")
    found = False
    for store in client.file_search_stores.list():
        found = True
        display = getattr(store, "display_name", "") or "(sin display_name)"
        print(f"  {display}")
        print(f"    {store.name}")
    if not found:
        print("  (ninguno)")
    if registry:
        print("\nRegistry local (work/file_search_stores.json):")
        for display, name in registry.items():
            print(f"  {display} -> {name}")
    return 0


def _load_store_registry() -> dict:
    from app.gemini_client import _load_store_registry as loader

    return loader()


def store_documents(client, store: str) -> dict[str, int]:
    """display_name -> size_bytes del markdown que se subio.

    El tamano confirma que el archivo local es la misma revision indexada, no solo que
    el nombre coincide.
    """
    documents = {}
    for document in client.file_search_stores.documents.list(parent=store):
        display = getattr(document, "display_name", "") or ""
        if display:
            documents[display] = getattr(document, "size_bytes", 0) or 0
    return dict(sorted(documents.items()))


def compare_size(path, expected: int) -> tuple[str, str]:
    """Devuelve (estado, detalle). Distingue contenido distinto de un simple CRLF."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return "error", str(exc)

    if len(raw) == expected:
        return "ok", ""

    # Un checkout en Windows pudo convertir los saltos de linea sin tocar el texto.
    if len(raw.replace(b"\r\n", b"\n")) == expected:
        return "crlf", f"{len(raw)} en disco vs {expected} indexados (solo saltos de linea)"

    return "distinto", f"{len(raw)} en disco vs {expected} indexados"


def resolve_store(client, requested: str | None) -> str:
    if requested and requested.startswith("fileSearchStores/"):
        return requested

    registry = _load_store_registry()
    if requested:
        if requested in registry:
            return registry[requested]
        for store in client.file_search_stores.list():
            if getattr(store, "display_name", None) == requested:
                return store.name
        raise SystemExit(f"No se encontro un store llamado '{requested}'. Usa --list-stores.")

    if settings.gemini_file_search_store:
        return resolve_store(client, settings.gemini_file_search_store)

    raise SystemExit(
        "No hay store indicado. Pasa --store o define GEMINI_FILE_SEARCH_STORE.\n"
        "Corre --list-stores para ver las opciones."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diff entre File Search Store y corpus local")
    parser.add_argument("--store", help="Nombre completo o display_name del store")
    parser.add_argument("--list-stores", action="store_true", help="Solo listar los stores disponibles")
    parser.add_argument("--write-manifest", action="store_true", help="Escribir las sugerencias al manifest")
    args = parser.parse_args(argv)

    client = build_client()

    if args.list_stores:
        return list_stores(client)

    store = resolve_store(client, args.store)
    store_docs = store_documents(client, store)
    documents = list(store_docs)

    corpus_files = corpus.iter_corpus_files()
    corpus_names = [path.name for path in corpus_files]

    print(f"store      : {store}")
    print(f"documentos : {len(documents)}")
    print(f"corpus_dir : {settings.corpus_dir}")
    print(f"markdowns  : {len(corpus_names)}\n")

    if not documents:
        print("El store no tiene documentos. Revisa que sea el store correcto.")
        return 1

    if not corpus_names:
        print("El corpus esta vacio. Estos son los nombres que deben tener los .md:\n")
        for name in documents:
            print(f"  {name}  ({store_docs[name]} bytes)")
        return 1

    matched: list[tuple[str, str]] = []
    unmatched: list[str] = []
    size_issues: list[tuple[str, str, str]] = []
    for display in documents:
        path = corpus.resolve_document_path(display, None)
        if path is None:
            unmatched.append(display)
            continue
        matched.append((display, path.name))
        state, detail = compare_size(path, store_docs[display])
        if state != "ok":
            size_issues.append((display, state, detail))

    used = {name for _, name in matched}
    orphans = [name for name in corpus_names if name not in used]

    identical = len(matched) - len(size_issues)
    print(f"CASAN            : {len(matched)}/{len(documents)}")
    print(f"  identicos      : {identical}")
    print(f"SIN CASAR        : {len(unmatched)}")
    print(f"SOBRAN EN CORPUS : {len(orphans)}\n")

    if size_issues:
        crlf = [row for row in size_issues if row[1] == "crlf"]
        real = [row for row in size_issues if row[1] != "crlf"]
        if crlf:
            print(f"Difieren solo en saltos de linea ({len(crlf)}), inofensivo:\n")
            for display, _, detail in crlf[:10]:
                print(f"  {display}")
                print(f"      {detail}")
            print()
        if real:
            print(f"CONTENIDO DISTINTO AL INDEXADO ({len(real)}):\n")
            for display, _, detail in real:
                print(f"  {display}")
                print(f"      {detail}")
            print("  -> se edito despues de subirlo, o es otra version.")
            print("     El locator fallara ahi: el snippet no existira tal cual.")
            print()

    suggestions: dict[str, str] = {}
    if unmatched:
        # Comparar sobre el stem sin hash: los hashes se parecen entre si y generan
        # sugerencias espurias.
        keys = {
            corpus.normalize_key(corpus.strip_document_id_hash(name.rsplit(".", 1)[0])): name
            for name in corpus_names
        }
        print("Documentos del store que NO resuelven:\n")
        for display in unmatched:
            key = corpus.normalize_key(corpus.strip_document_id_hash(display.rsplit(".", 1)[0]))
            close = difflib.get_close_matches(key, list(keys), n=1, cutoff=SUGGESTION_CUTOFF)
            if close:
                candidate = keys[close[0]]
                suggestions[display] = candidate
                print(f"  {display}")
                print(f"      -> sugerido: {candidate}")
            else:
                print(f"  {display}")
                print("      -> sin candidato parecido en el corpus")
        print()

    if orphans:
        print("Markdowns del corpus que ningun documento del store reclama:\n")
        for name in orphans:
            print(f"  {name}  ({store_docs[name]} bytes)")
        print()

    if suggestions:
        print("Entradas de manifest sugeridas:\n")
        print(json.dumps(suggestions, indent=2, ensure_ascii=False))
        print()
        if args.write_manifest:
            path = corpus.manifest_path()
            merged = {**corpus.load_manifest(), **suggestions}
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Escrito {path} con {len(merged)} entradas.")
            print("Revisa las sugerencias a mano antes de confiar en ellas.")
        else:
            print("Corre otra vez con --write-manifest para guardarlas.")

    if unmatched:
        return 2
    return 3 if any(row[1] != "crlf" for row in size_issues) else 0


if __name__ == "__main__":
    sys.exit(main())
