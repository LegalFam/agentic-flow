"""Conjunto adversarial de alcance: recoge la corrida y calcula las métricas desde la codificación humana.

    python -m eval.scope collect --run eval/runs/adversarial-after
    python -m eval.scope report --run eval/runs/adversarial-after
"""

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "dataset" / "adversarial.jsonl"
CORPUS_DIR = EVAL_DIR.parent.parent / "work" / "corpus"
WORKFLOW_NAME = "LegalFam Eval - full"
CODES = {"R1", "R2", "R3", "R4"}
CSV_FIELDS = ["id", "scope_label", "coder", "code", "cited_articles", "covered_in_scope_part", "invaded_out_part", "final_code", "notes"]
HUMAN_CODERS = ("A", "B")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def psql(sql: str) -> str:
    return subprocess.run(
        ["docker", "exec", "agentic-flow-postgres-1", "psql", "-U", "n8n", "-d", "n8n", "-At", "-c", sql],
        capture_output=True, check=True, encoding="utf-8",
    ).stdout


def unflatten(raw: str):
    table = json.loads(raw)
    done: dict[int, object] = {}

    def resolve(index: int):
        if index in done:
            return done[index]
        value = table[index]
        if isinstance(value, list):
            done[index] = out = []
            out.extend(resolve(int(x)) if isinstance(x, str) else x for x in value)
        elif isinstance(value, dict):
            done[index] = out = {}
            out.update({k: resolve(int(x)) if isinstance(x, str) else x for k, x in value.items()})
        else:
            done[index] = out = value
        return out

    return resolve(0)


def parser_outputs(since: str) -> dict[str, dict]:
    """Salida textual del Parser Agent por mensaje, leída de las ejecuciones de n8n."""
    rows = psql(
        "select e.id || chr(9) || d.data from execution_entity e "
        "join execution_data d on d.\"executionId\" = e.id "
        "join workflow_entity w on w.id = e.\"workflowId\" "
        f"where w.name = '{WORKFLOW_NAME}' and e.\"startedAt\" >= '{since}' order by e.id"
    )
    outputs = {}
    for line in rows.splitlines():
        execution_id, raw = line.split("\t", 1)
        run_data = unflatten(raw)["resultData"]["runData"]
        if "Webhook" not in run_data:
            continue
        message = run_data["Webhook"][0]["data"]["main"][0][0]["json"]["body"]["message"]
        parser_run = run_data.get("Parser Agent")
        output = None
        if parser_run and parser_run[0].get("data"):
            branch = parser_run[0]["data"]["main"][0] or []
            output = branch[0]["json"].get("output") if branch else None
        outputs[message] = {
            "n8n_execution_id": int(execution_id),
            "category": (output or {}).get("category"),
            "information_sufficiency": (output or {}).get("information_sufficiency"),
            "clarifying_questions": (output or {}).get("clarifying_questions"),
            "out_of_scope_parts": (output or {}).get("out_of_scope_parts"),
            "message": (output or {}).get("message"),
            "failed": output is None,
            "nodes": list(run_data),
        }
    return outputs


def system_snapshot() -> dict:
    workflow = psql(f"select id || chr(9) || \"versionId\" || chr(9) || \"updatedAt\" from workflow_entity where name = '{WORKFLOW_NAME}'").strip().split("\t")
    nodes = json.loads(psql(f"select nodes from workflow_entity where name = '{WORKFLOW_NAME}'"))
    models = {n["name"]: n["parameters"].get("modelName", "models/gemini-2.5-flash (default del nodo)") for n in nodes if n["type"].endswith("lmChatGoogleGemini")}
    corpus = sorted(CORPUS_DIR.glob("*.md"))
    digest = hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in corpus)).hexdigest()

    def out(*cmd: str) -> str:
        return subprocess.run(cmd, capture_output=True, encoding="utf-8").stdout.strip()

    return {
        "commit": out("git", "-C", str(EVAL_DIR), "rev-parse", "HEAD"),
        "n8n_version": out("docker", "exec", "agentic-flow-n8n-1", "n8n", "--version"),
        "workflow": {"name": WORKFLOW_NAME, "id": workflow[0], "version_id": workflow[1], "updated_at": workflow[2]},
        "models": models,
        "processing_api_image_created": out("docker", "image", "inspect", "agentic-flow-processing-api", "--format", "{{.Created}}"),
        "corpus": {"files": len(corpus), "sha256": digest},
    }


def collect(run: Path, dataset: Path) -> None:
    items = read_jsonl(dataset)
    raw = read_jsonl(run / "full.jsonl")
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    parsed = parser_outputs(manifest["phases"][0]["started_at"])

    records = []
    for item in items:
        if item.get("discarded"):
            continue
        attempts = [row for row in raw if row["id"] == item["id"]]
        final = attempts[-1]
        records.append({
            **final,
            "scope_label": item["scope_label"],
            "family": item["family"],
            "attempts": len(attempts),
            "attempt_errors": [row.get("error") or row.get("status") for row in attempts[:-1]],
            "parser": parsed.get(item["question"]),
        })
    (run / "adversarial.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")

    manifest["dataset"] = str(dataset)
    manifest["discarded"] = {i["id"]: i["discarded"] for i in items if i.get("discarded")}
    manifest["system"] = system_snapshot()
    (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    coding = run / "scope_coding.csv"
    if not coding.exists():
        with coding.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, CSV_FIELDS)
            writer.writeheader()
            for record in records:
                cited = bool((record.get("response") or {}).get("citations"))
                for coder in HUMAN_CODERS:
                    writer.writerow({"id": record["id"], "scope_label": record["scope_label"], "coder": coder,
                                     "cited_articles": str(cited).lower()})
    print(f"{len(records)} respuestas en {run / 'adversarial.jsonl'}")


def pct(num: int, den: int) -> str:
    return f"{num}/{den} ({100 * num / den:.1f} %)" if den else f"{num}/0"


def flag(value: str) -> bool | None:
    return {"true": True, "false": False}.get((value or "").strip().lower())


def resolve_codes(rows: list[dict]) -> tuple[dict[str, dict], list[str], dict]:
    # La codificación provisional de otro codificador no cuenta en cuanto hay humanos.
    if any(row["coder"] in HUMAN_CODERS and row["code"].strip() for row in rows):
        rows = [row for row in rows if row["coder"] in HUMAN_CODERS]
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        if row["code"].strip():
            by_id.setdefault(row["id"], []).append(row)
    coders = sorted({row["coder"] for group in by_id.values() for row in group})
    agreement = {"coders": coders}
    if len(coders) >= 2:
        first, second = coders[:2]
        pairs = [(next(r for r in g if r["coder"] == first)["code"], next(r for r in g if r["coder"] == second)["code"])
                 for g in by_id.values() if {first, second} <= {r["coder"] for r in g}]
        agreed = sum(a == b for a, b in pairs)
        agreement.update(pairs=len(pairs), agreed=agreed, disagreements=len(pairs) - agreed)

    final = {}
    for id_, group in by_id.items():
        chosen = next((r["final_code"].strip() for r in group if r["final_code"].strip()), None)
        if chosen is None and len({r["code"] for r in group}) == 1:
            chosen = group[0]["code"]
        if chosen not in CODES:
            raise SystemExit(f"{id_}: sin código final válido ({chosen!r}); resuelve la discrepancia en final_code")
        base = next((r for r in group if r["final_code"].strip()), group[0])
        final[id_] = {**base, "final": chosen}
    return final, coders, agreement


def report(run: Path) -> None:
    records = {r["id"]: r for r in read_jsonl(run / "adversarial.jsonl")}
    with (run / "scope_coding.csv").open(encoding="utf-8", newline="") as handle:
        final, coders, agreement = resolve_codes(list(csv.DictReader(handle)))
    missing = sorted(set(records) - set(final))
    if missing:
        raise SystemExit(f"sin codificar: {', '.join(missing)}")

    def of(label: str, *, skip_r4: bool = True) -> list[str]:
        return [i for i in records if records[i]["scope_label"] == label and not (skip_r4 and final[i]["final"] == "R4")]

    out_ids, in_ids, part_ids = of("FUERA"), of("DENTRO"), of("PARCIAL")
    code = lambda i: final[i]["final"]
    cited = lambda i: bool(flag(final[i]["cited_articles"]))
    rejected_by_parser = lambda i: (records[i].get("parser") or {}).get("category") == "error"

    fp = [i for i in out_ids if code(i) == "R1"]
    metrics = {
        "coders": coders,
        "agreement": agreement,
        "r4": sorted(i for i in records if code(i) == "R4"),
        "out_of_scope": {"n": len(out_ids), "rejected": sum(code(i) == "R2" for i in out_ids),
                         "false_accepted": len(fp), "false_accepted_with_citations": sum(cited(i) for i in fp),
                         "clarification": sum(code(i) == "R3" for i in out_ids)},
        "in_scope": {"n": len(in_ids), "false_rejected": sum(code(i) == "R2" for i in in_ids),
                     "answered": sum(code(i) == "R1" for i in in_ids), "clarification": sum(code(i) == "R3" for i in in_ids)},
        "parser_vs_outcome": {"n": len(out_ids) + len(in_ids) + len(part_ids),
                              "agree": sum(rejected_by_parser(i) == (code(i) == "R2") for i in out_ids + in_ids + part_ids)},
        "parser_vs_label": {"n": len(out_ids) + len(in_ids) + len(part_ids),
                            "agree": sum(rejected_by_parser(i) == (records[i]["scope_label"] == "FUERA") for i in out_ids + in_ids + part_ids)},
        "partial": {i: {"code": code(i), "covered_in_scope_part": flag(final[i]["covered_in_scope_part"]),
                        "invaded_out_part": flag(final[i]["invaded_out_part"])} for i in part_ids},
        "by_family": {},
        "items": {},
    }
    for i, record in records.items():
        family = metrics["by_family"].setdefault(record["family"], Counter())
        family[code(i)] += 1
        parser = record.get("parser") or {}
        metrics["items"][i] = {"scope_label": record["scope_label"], "family": record["family"],
                               "parser_category": parser.get("category"), "information_sufficiency": parser.get("information_sufficiency"),
                               "code": code(i), "cited_articles": cited(i), "citations": len((record.get("response") or {}).get("citations") or []),
                               "latency_ms": record["latency_ms"], "attempts": record["attempts"], "notes": final[i]["notes"]}
    (run / "scope_results.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    o, n, pv, pl = metrics["out_of_scope"], metrics["in_scope"], metrics["parser_vs_outcome"], metrics["parser_vs_label"]
    lines = [
        f"# Conjunto adversarial de alcance — {run.name}", "",
        f"Codificadores: {', '.join(coders)}."
        + (f" Acuerdo simple {pct(agreement['agreed'], agreement['pairs'])}, {agreement['disagreements']} discrepancias."
           if "pairs" in agreement else " **Un solo codificador: cifras provisionales.**"),
        f"R4 excluidos del denominador: {', '.join(metrics['r4']) or 'ninguno'}.", "",
        "| Métrica | Valor |", "|---|--:|",
        f"| Rechazo por alcance (R2 / FUERA) | {pct(o['rejected'], o['n'])} |",
        f"| Falsos aceptados (R1 / FUERA) | {pct(o['false_accepted'], o['n'])} |",
        f"| … con artículos citados | {pct(o['false_accepted_with_citations'], o['n'])} |",
        f"| … sin citas | {pct(o['false_accepted'] - o['false_accepted_with_citations'], o['n'])} |",
        f"| Aclaración sobre FUERA (R3) | {pct(o['clarification'], o['n'])} |",
        f"| Falsos rechazados (R2 / DENTRO) | {pct(n['false_rejected'], n['n'])} |",
        f"| Aclaración sobre DENTRO (R3) | {pct(n['clarification'], n['n'])} |",
        f"| Parser ↔ desenlace (rechaza ⇔ R2) | {pct(pv['agree'], pv['n'])} |",
        f"| Parser ↔ etiqueta (rechaza ⇔ FUERA) | {pct(pl['agree'], pl['n'])} |", "",
        "| id | Etiqueta | Categoría del Parser | Suficiencia | Código | Citó artículos |", "|---|---|---|---|---|---|",
        *(f"| `{i}` | {m['scope_label']} | {m['parser_category']} | {m['information_sufficiency']} | {m['code']} | {'sí' if m['cited_articles'] else 'no'} |"
          for i, m in metrics["items"].items()), "",
        "| Familia | R1 | R2 | R3 | R4 |", "|---|--:|--:|--:|--:|",
        *(f"| {f} | {c['R1']} | {c['R2']} | {c['R3']} | {c['R4']} |" for f, c in metrics["by_family"].items()), "",
        "PARCIAL: " + "; ".join(f"`{i}` {p['code']}, cubre lo propio: {p['covered_in_scope_part']}, invade lo ajeno: {p['invaded_out_part']}"
                                for i, p in metrics["partial"].items()), "",
    ]
    (run / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conjunto adversarial de alcance")
    parser.add_argument("command", choices=["collect", "report"])
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args(argv)
    collect(args.run, args.dataset) if args.command == "collect" else report(args.run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
