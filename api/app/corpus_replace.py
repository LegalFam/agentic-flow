"""Reemplaza un documento del File Search Store por una revision nueva.

Complemento de `corpus_diff`, que solo mira: este si escribe. El caso tipico es un PDF
actualizado, ya convertido a markdown y con su metadata extraida:

    python -m app.corpus_replace --store "FamilyLaw" --list
    python -m app.corpus_replace --store "FamilyLaw" --plan codigo-civil-<hash>.md
    python -m app.corpus_replace --store "FamilyLaw" \
        --markdown /work/nuevo/codigo-civil-<hash>.md \
        --metadata /work/nuevo/codigo-civil-<hash>.metadata.json

Sin `--apply` no toca nada: imprime que se subiria y que se borraria. El borrado en File
Search no se deshace y no hay version anterior a la que volver, asi que el dry-run es el
default y no una opcion.

Codigos de salida: 0 hecho (o dry-run limpio), 2 hace falta una decision (nada que
reemplazar, o varios candidatos), 3 se aplico pero algo quedo a medias, 1 error.
"""

import argparse
import json
import sys
from pathlib import Path

from app import store_documents
from app.models import LegalMetadata


def print_documents(listed: dict) -> None:
    print(f"store      : {listed['file_search_store']}")
    print(f"documentos : {len(listed['documents'])}\n")
    for document in listed["documents"]:
        print(f"  {document['display_name']}  ({document['size_bytes']} bytes)")
        print(f"      {document['name']}  [{document['state']}]")


def print_plan(plan: dict) -> None:
    print(f"store          : {plan['file_search_store']}")
    print(f"archivo nuevo  : {plan['filename']}")
    print(f"llave          : {plan['replacement_key']}")
    print(f"veredicto      : {plan['verdict']}\n")
    if plan["superseded"]:
        print("Se borraria:\n")
        for document in plan["superseded"]:
            print(f"  {document['display_name']}  ({document['size_bytes']} bytes)")
            print(f"      {document['name']}")
        print()
    print(plan["message"])


def load_metadata(path: Path) -> LegalMetadata:
    data = json.loads(path.read_text(encoding="utf-8"))
    # El workflow de conversion guarda el JSON envuelto: {"filename": ..., "metadata": {...}}
    if isinstance(data, dict) and "metadata" in data and isinstance(data["metadata"], dict):
        data = data["metadata"]
    return LegalMetadata.model_validate(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reemplaza un documento del File Search Store")
    parser.add_argument("--store", help="Nombre completo o display_name del store")
    parser.add_argument("--list", action="store_true", help="Listar los documentos del store")
    parser.add_argument("--plan", metavar="FILENAME", help="Solo mostrar que reemplazaria ese nombre")
    parser.add_argument("--markdown", help="Ruta al .md de la revision nueva")
    parser.add_argument("--metadata", help="Ruta al .metadata.json de la revision nueva")
    parser.add_argument(
        "--filename",
        help="display_name con el que se indexa. Por defecto, el nombre del .md",
    )
    parser.add_argument(
        "--supersedes",
        action="append",
        default=[],
        metavar="DOC",
        help="Documento a borrar (repetible). Manda sobre lo que deduzca el plan",
    )
    parser.add_argument("--allow-new", action="store_true", help="Aceptar que no haya nada que reemplazar")
    parser.add_argument("--allow-multiple", action="store_true", help="Aceptar reemplazar varios documentos")
    parser.add_argument("--no-corpus-sync", action="store_true", help="No tocar el corpus local")
    parser.add_argument("--apply", action="store_true", help="Ejecutar. Sin esto es dry-run")
    args = parser.parse_args(argv)

    try:
        if args.list:
            print_documents(store_documents.list_store_documents(args.store))
            return 0

        if args.plan:
            plan = store_documents.plan_replacement(args.plan, args.store)
            print_plan(plan)
            return 0 if plan["verdict"] == "replace" else 2

        if not args.markdown or not args.metadata:
            parser.error("hacen falta --markdown y --metadata (o usa --list / --plan)")

        markdown_path = Path(args.markdown)
        metadata_path = Path(args.metadata)
        markdown = markdown_path.read_text(encoding="utf-8")
        metadata = load_metadata(metadata_path)
        filename = args.filename or markdown_path.name

        plan = store_documents.plan_replacement(filename, args.store)
        print_plan(plan)
        print()

        if not args.apply:
            print(f"DRY RUN. Nada se subio ni se borro. Se subirian {len(markdown)} caracteres.")
            print("Corre otra vez con --apply cuando lo de arriba sea lo que esperas.")
            return 0 if plan["verdict"] == "replace" else 2

        result = store_documents.replace_document(
            filename=filename,
            markdown=markdown,
            metadata=metadata,
            file_search_store_name=args.store,
            supersedes=args.supersedes,
            allow_new=args.allow_new,
            allow_multiple=args.allow_multiple,
            sync_corpus=not args.no_corpus_sync,
            wait_until_done=True,
            max_wait_seconds=900,
        )
    except store_documents.ReplaceRefused as exc:
        print(f"\nNo se aplico: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"subido    : {result['filename']}")
    print(f"borrados  : {', '.join(result['deleted']) or '(ninguno)'}")
    corpus_result = result["corpus"]
    if corpus_result.get("synced"):
        removed = ", ".join(corpus_result.get("removed") or []) or "(nada)"
        print(f"corpus    : escrito {corpus_result['written']}, borrado {removed}")
    else:
        print(f"corpus    : sin sincronizar ({corpus_result.get('reason')})")
    print(f"\n{result['message']}")

    for warning in result["warnings"]:
        print(f"\nAVISO: {warning}", file=sys.stderr)

    return 3 if result["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
