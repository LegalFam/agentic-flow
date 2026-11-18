"""Escaneo diario de El Peruano (ayer y hoy, hora de Lima) para el Cloud Run Job programado.

El horario de n8n no sirve en Cloud Run: con min-instances 0 no hay instancia despierta a la
hora del disparo. Los avisos por correo salen de alertas de Cloud Monitoring que buscan las
lineas EL_PERUANO_PROPOSALS_PENDING y EL_PERUANO_SCAN_FAILED en el log del job.
"""
import json
import traceback
from datetime import datetime, timedelta, timezone

from app import amendments

LIMA = timezone(timedelta(hours=-5))
DECIDED_BY = "escaneo diario (sin cambios en el texto)"


def _log(severity: str, message: str, **fields) -> None:
    print(json.dumps({"severity": severity, "message": message, **fields}, ensure_ascii=False, default=str), flush=True)


def close_no_change_proposals() -> None:
    for proposal in amendments.list_proposals("pending"):
        if proposal.get("action") == "none":
            amendments.decide(proposal["id"], "apply", None, DECIDED_BY)


def pending_for_review() -> list[dict]:
    return [
        {"id": p["id"], "document": p.get("document"), "action": p.get("action"),
         "normas": [n.get("id") for n in p.get("normas", [])]}
        for p in amendments.list_proposals("pending")
        if p.get("action") != "none"
    ]


def main() -> None:
    today = datetime.now(LIMA).date()
    try:
        result = amendments.scan(today - timedelta(days=1), today)
        close_no_change_proposals()
    except Exception as exc:
        _log("ERROR", f"EL_PERUANO_SCAN_FAILED {type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        raise
    _log("INFO", "EL_PERUANO_SCAN_DONE", rango=result.get("rango"), normas_revisadas=result.get("normas_revisadas"),
         warnings=result.get("warnings"))
    pending = pending_for_review()
    if pending:
        _log("WARNING", f"EL_PERUANO_PROPOSALS_PENDING {len(pending)} propuesta(s) por revisar en Review Norm Updates",
             proposals=pending)


if __name__ == "__main__":
    main()
