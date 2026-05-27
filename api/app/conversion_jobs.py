from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from app.converter import convert_pdf_to_markdown


_executor = ThreadPoolExecutor(max_workers=1)
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = Lock()


def start_conversion_job(filename: str, content: bytes) -> dict[str, Any]:
    job_id = str(uuid4())
    now = _utc_now()
    job = {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    with _jobs_lock:
        _jobs[job_id] = job

    _executor.submit(_run_conversion_job, job_id, filename, content)
    return _public_job(job)


def get_conversion_job(job_id: str) -> dict[str, Any] | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return _public_job(job)


def _run_conversion_job(job_id: str, filename: str, content: bytes) -> None:
    _update_job(job_id, status="running")
    try:
        result = convert_pdf_to_markdown(filename, content)
    except Exception as exc:
        _update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        return

    _update_job(job_id, status="succeeded", result=result)


def _update_job(job_id: str, **changes: Any) -> None:
    with _jobs_lock:
        job = _jobs[job_id]
        job.update(changes)
        job["updated_at"] = _utc_now()


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "filename": job["filename"],
        "result": job["result"],
        "error": job["error"],
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
