"""Escaneo diario de El Peruano (ayer y hoy, hora de Lima) para el Cloud Run Job programado.

El horario de n8n no sirve en Cloud Run: con min-instances 0 no hay instancia despierta a la
hora del disparo. Un error termina el proceso con codigo distinto de cero, asi el job lo marca.
"""
import json
from datetime import datetime, timedelta, timezone

from app import amendments

LIMA = timezone(timedelta(hours=-5))


def main() -> None:
    today = datetime.now(LIMA).date()
    result = amendments.scan(today - timedelta(days=1), today)
    summary = {key: value for key, value in result.items() if key != "proposals"}
    summary["proposals"] = [
        {"id": p.get("id"), "document": p.get("document"), "action": p.get("action")}
        for p in result.get("proposals", [])
    ]
    print(json.dumps(summary, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
