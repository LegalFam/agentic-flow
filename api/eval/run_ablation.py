"""Corre el dataset contra los cuatro webhooks de la ablacion y guarda las respuestas crudas.

    python -m eval.run_ablation --limit 1                 # humo: una pregunta por brazo
    python -m eval.run_ablation                           # corrida completa
    python -m eval.run_ablation --repeat 3 --repeat-sample 10

Aca no se calcula ninguna metrica. Lo unico que hace es guardar lo que devolvio cada
brazo, entero y sin tocar, en `runs/<timestamp>/<brazo>.jsonl`. El scoring va despues y
sobre esos ficheros: reprocesar una metrica no puede costar otras 250 llamadas al modelo.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from eval.ablation.arms import ARMS, FACTORIAL

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_DATASET = EVAL_DIR / "dataset" / "family_law_v1.jsonl"
DEFAULT_RUNS = EVAL_DIR / "runs"


def load_dataset(path: Path) -> list[dict]:
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(json.loads(line))
    return items


def call_webhook(url: str, token: str | None, header: str, message: str, timeout: int) -> dict:
    """Mismo contrato que `N8nWebhookClient.sendMessage` del backend.

    `session_id` es nuevo en cada llamada a proposito: el flujo cambia de registro cuando
    detecta mensajes previos —el XAI Agent deja de saludar, el Parser Agent hereda hechos—
    y reutilizar la sesion haria que la respuesta a una pregunta dependiera del orden del
    dataset.
    """
    payload = {
        "message": message,
        "session_id": str(uuid.uuid4()),
        "language": "es",
        "previous_messages": [],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **({header: token} if token else {})},
        method="POST",
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except Exception as exc:  # timeout, conexion rechazada, DNS
        return {
            "ok": False,
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    latency = int((time.monotonic() - started) * 1000)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "status": status, "error": "respuesta no es JSON", "raw": body[:2000], "latency_ms": latency}

    return {"ok": 200 <= status < 300, "status": status, "response": parsed, "latency_ms": latency}


def already_done(path: Path) -> set[tuple[str, int]]:
    """Que (pregunta, repeticion) ya se contestaron, para poder reanudar.

    Una corrida completa son varios cientos de llamadas al modelo; si se corta a mitad,
    volver a empezar cuesta dinero y no aporta nada.
    """
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("ok"):
            done.add((record["id"], record.get("repeat", 0)))
    return done


def write_manifest(run_dir: Path, run_id: str, args, questions: int) -> None:
    """Registra la fase que se acaba de lanzar, sin borrar las anteriores.

    Una corrida completa son horas, asi que se hace por fases sobre el mismo `--run-id`.
    Si el manifiesto se sobrescribiera, al final diria "16 preguntas" —el tamano de la
    ultima fase— y no lo que de verdad se corrio.
    """
    path = run_dir / "manifest.json"
    manifest = {"run_id": run_id, "dataset": str(args.dataset), "phases": []}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest.setdefault("phases", [])
        except json.JSONDecodeError:
            pass

    manifest["phases"].append(
        {
            "started_at": datetime.now(timezone.utc).isoformat(),
            "questions": questions,
            "limit": args.limit,
            "arms": args.arms,
            "repeat": args.repeat,
            "repeat_sample": args.repeat_sample,
            "base_url": args.base_url,
        }
    )
    manifest["questions"] = max(
        (phase.get("questions") or 0) for phase in manifest["phases"]
    )
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Corre la ablacion contra los webhooks de n8n")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--base-url", default=os.environ.get("EVAL_N8N_BASE_URL", "http://localhost:5678"))
    parser.add_argument("--token", default=os.environ.get("N8N_AUTH_TOKEN"))
    parser.add_argument("--auth-header", default=os.environ.get("N8N_AUTH_HEADER_NAME", "X-N8N-Token"))
    parser.add_argument("--arms", nargs="*", default=list(FACTORIAL))
    parser.add_argument("--limit", type=int, help="Solo las primeras N preguntas")
    parser.add_argument("--timeout", type=int, default=300, help="Segundos por llamada")
    parser.add_argument("--repeat", type=int, default=1, help="Repeticiones para estimar varianza")
    parser.add_argument("--repeat-sample", type=int, default=10, help="Sobre cuantas preguntas se repite")
    parser.add_argument("--out", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--run-id", help="Reanuda una corrida existente")
    parser.add_argument("--pause", type=float, default=0.0, help="Segundos entre llamadas")
    args = parser.parse_args(argv)

    if not args.dataset.exists():
        print(f"no existe el dataset: {args.dataset}")
        return 2

    items = load_dataset(args.dataset)
    if args.limit:
        items = items[: args.limit]

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.out / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    write_manifest(run_dir, run_id, args, len(items))

    print(f"run      : {run_dir}")
    print(f"dataset  : {len(items)} preguntas")
    print(f"brazos   : {', '.join(args.arms)}")
    if not args.token:
        print("aviso    : sin token; si el webhook exige headerAuth las llamadas daran 403")
    print()

    for arm in args.arms:
        if arm not in ARMS:
            print(f"brazo desconocido: {arm}")
            return 2

    handles = {arm: (run_dir / f"{arm}.jsonl").open("a", encoding="utf-8") for arm in args.arms}
    done = {arm: already_done(run_dir / f"{arm}.jsonl") for arm in args.arms}
    for arm, previous in done.items():
        if previous:
            print(f"[{arm}] reanudando: {len(previous)} respuestas ya guardadas")

    failures = 0
    completed: list[str] = []
    try:
        # Pregunta por fuera, brazo por dentro: una corrida que se corta a mitad —cuota
        # agotada, contenedor caido— deja preguntas con los cuatro brazos hechos en vez de
        # un brazo entero y los otros a cero. Lo primero sigue siendo un experimento
        # pareado valido sobre menos preguntas; lo segundo no sirve para comparar nada.
        for position, item in enumerate(items):
            repeats = args.repeat if position < args.repeat_sample else 1
            answered = 0
            for attempt in range(repeats):
                for arm in args.arms:
                    if (item["id"], attempt) in done[arm]:
                        answered += 1
                        continue

                    result = call_webhook(
                        f"{args.base_url.rstrip('/')}/webhook/chat-process-eval-{arm}",
                        args.token,
                        args.auth_header,
                        item["question"],
                        args.timeout,
                    )
                    handles[arm].write(
                        json.dumps(
                            {
                                "id": item["id"],
                                "arm": arm,
                                "repeat": attempt,
                                "category": item.get("category"),
                                "question": item["question"],
                                **result,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    handles[arm].flush()

                    if result["ok"]:
                        answered += 1
                    else:
                        failures += 1
                    mark = "ok " if result["ok"] else "FAIL"
                    detail = "" if result["ok"] else f"  {result.get('error') or result.get('status')}"
                    print(f"[{arm:<7}] {mark} {item['id']:<10} {result['latency_ms']:>6} ms{detail}")

                    if args.pause:
                        time.sleep(args.pause)

            if answered == repeats * len(args.arms):
                completed.append(item["id"])
    finally:
        for handle in handles.values():
            handle.close()

    print(f"\nguardado en {run_dir}")
    print(f"preguntas con todos los brazos completos: {len(completed)}/{len(items)}")
    if completed:
        print(f"  ultima completa: {completed[-1]}")
    if failures:
        print(f"llamadas fallidas: {failures} (vuelve a correr con --run-id {run_id} para reintentarlas)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
