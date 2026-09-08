"""Genera los cuatro workflows de la ablacion desde el de produccion.

    python -m eval.ablation.build_workflows --dry-run    # solo el diff, no escribe nada
    python -m eval.ablation.build_workflows

Los ficheros salen en `n8n/workflows/eval/`, que NO entra en el deploy: el import del
pipeline de Cloud Run apunta a `n8n/workflows/` y pisaria produccion con estos brazos.
"""

import argparse
import copy
import json
import sys
from pathlib import Path

from eval.ablation import arms

# api/eval/ablation/build_workflows.py -> agentic-flow/
REPO = Path(__file__).resolve().parents[3]
SOURCE = REPO / "n8n" / "workflows" / "LegalFam Message Flow.json"
TARGET = REPO / "n8n" / "workflows" / "eval"


def load_source(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"no se encontro el workflow de produccion: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(original: dict, variant: dict, arm: str) -> str:
    before = {item["name"] for item in original["nodes"]}
    after = {item["name"] for item in variant["nodes"]}

    lines = [f"--- {arm} " + "-" * (66 - len(arm))]
    lines.append(f"  webhook           /{arms.node(variant, 'Webhook')['parameters']['path']}")
    lines.append(f"  nodos             {len(after)} (produccion: {len(before)})")

    removed = sorted(before - after)
    if removed:
        lines.append(f"  nodos eliminados  {', '.join(removed)}")

    for name in sorted(after & before):
        source_node = next(item for item in original["nodes"] if item["name"] == name)
        variant_node = next(item for item in variant["nodes"] if item["name"] == name)
        changes = _changed_fields(source_node, variant_node)
        if changes:
            lines.append(f"  {name:<32} {', '.join(changes)}")

    return "\n".join(lines)


def _changed_fields(before: dict, after: dict) -> list[str]:
    """Que se toco de un nodo, en terminos legibles y no como un diff de JSON."""
    changes: list[str] = []
    old = before.get("parameters", {})
    new = after.get("parameters", {})

    old_message = (old.get("options") or {}).get("systemMessage", "")
    new_message = (new.get("options") or {}).get("systemMessage", "")
    if old_message != new_message:
        delta = len(new_message) - len(old_message)
        changes.append(f"systemMessage {delta:+d} chars")

    if old.get("inputSchema") != new.get("inputSchema"):
        changes.append("inputSchema reescrito")
    if old.get("jsCode") != new.get("jsCode"):
        changes.append("jsCode reescrito")
    if old.get("path") != new.get("path"):
        changes.append(f"path -> {new.get('path')}")

    old_temperature = (old.get("options") or {}).get("temperature")
    new_temperature = (new.get("options") or {}).get("temperature")
    if old_temperature != new_temperature:
        changes.append(f"temperature -> {new_temperature}")

    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Genera los workflows de la ablacion")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--out", type=Path, default=TARGET)
    parser.add_argument("--dry-run", action="store_true", help="Muestra el diff sin escribir")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Falla si los brazos en disco no coinciden con el workflow de produccion",
    )
    parser.add_argument("--arm", action="append", help="Genera solo estos brazos")
    args = parser.parse_args(argv)

    original = load_source(args.source)
    selected = args.arm or list(arms.FACTORIAL)

    print(f"origen : {args.source}")
    print(f"destino: {args.out}\n")

    variants: list[tuple[str, dict]] = []
    for arm in selected:
        if arm not in arms.ARMS:
            print(f"brazo desconocido: {arm} (disponibles: {', '.join(arms.ARMS)})")
            return 2
        try:
            variant = arms.build(copy.deepcopy(original), arm)
        except arms.PatchError as exc:
            print(f"FALLO el brazo {arm}: {exc}")
            print("\n-> el workflow de produccion cambio y el parche ya no encaja.")
            print("   Revisa eval/ablation/arms.py antes de correr nada: un parche que no")
            print("   aplica produce un brazo que no esta ablacionado y no se nota.")
            return 2
        variants.append((arm, variant))
        if not args.check:
            print(summarize(original, variant, arm))

    if args.check:
        return check(variants, args.out)

    if args.dry_run:
        print("\n(dry-run: no se escribio nada)")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for arm, variant in variants:
        serialize(args.out / f"legalfam-eval-{arm}.json", variant)
        print(f"\nescrito: {args.out / f'legalfam-eval-{arm}.json'}")

    return 0


def serialize(path: Path, variant: dict) -> str:
    text = json.dumps(variant, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def check(variants: list[tuple[str, dict]], out: Path) -> int:
    """Compara los brazos en disco contra los que saldrian del workflow de produccion ahora.

    Es lo que hace seguro versionarlos. El riesgo de un artefacto generado no es que se
    vea en el arbol, es que se quede viejo sin que nadie lo note: alguien toca un prompt
    de produccion, nadie regenera, y la corrida siguiente compara contra una version del
    sistema que ya no existe. Esto convierte ese silencio en un fallo.
    """
    stale: list[str] = []
    for arm, variant in variants:
        path = out / f"legalfam-eval-{arm}.json"
        expected = json.dumps(variant, ensure_ascii=False, indent=2) + "\n"
        if not path.exists():
            stale.append(f"{arm}: no esta generado ({path.name})")
        elif path.read_text(encoding="utf-8") != expected:
            stale.append(f"{arm}: quedo viejo respecto al workflow de produccion")
        else:
            print(f"  {arm:<16} al dia")

    if stale:
        print("\nDesincronizados:")
        for problem in stale:
            print(f"  {problem}")
        print("\n-> regenera con `python -m eval.ablation.build_workflows` y vuelve a")
        print("   importarlos en n8n. Los ficheros en disco no son la verdad: el workflow")
        print("   de produccion lo es.")
        return 2

    print("\nLos brazos coinciden con el workflow de produccion.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
