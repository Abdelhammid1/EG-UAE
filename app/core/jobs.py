"""Lightweight background jobs for large/slow exports (async reports).

A big report (full general ledger, whole audit trail, a year of journal entries)
can take long enough to time out a web request. Instead of blocking, the request
*queues* the work and returns immediately; the job runs in a background thread,
writes its output file under ``instance/exports/``, and the user downloads it once
it is ready. Progress is tracked in an in-process registry.

This is the single-process dev/single-tenant implementation. In production (see
requirements: redis + rq) the same ``submit`` boundary is backed by a real queue
so it survives across worker processes; the calling code does not change.
"""
from __future__ import annotations

import os
import threading
import traceback
import uuid
from datetime import datetime

from flask import current_app

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _exports_dir(app) -> str:
    d = os.path.join(app.instance_path, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def submit(title: str, builder, *, user_id=None, download_name="report.xlsx"):
    """Queue *builder* (a function taking no args, returning bytes) as a job.

    Returns the job id. The job runs in a background thread with its own app
    context so it can touch the database and settings just like a request."""
    app = current_app._get_current_object()
    job_id = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[job_id] = {
            "id": job_id, "title": title, "status": "running",
            "user_id": user_id, "download_name": download_name,
            "created_at": datetime.utcnow(), "finished_at": None,
            "path": None, "error": None,
        }

    def _run():
        with app.app_context():
            try:
                data = builder()
                path = os.path.join(_exports_dir(app), f"{job_id}.bin")
                with open(path, "wb") as fh:
                    fh.write(data)
                with _LOCK:
                    _JOBS[job_id].update(status="done", path=path,
                                         finished_at=datetime.utcnow())
            except Exception as exc:  # noqa: BLE001
                with _LOCK:
                    _JOBS[job_id].update(status="error", error=str(exc),
                                         finished_at=datetime.utcnow())
                app.logger.error("async report %s failed: %s\n%s", job_id, exc,
                                 traceback.format_exc())

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return job_id


def get(job_id):
    with _LOCK:
        j = _JOBS.get(job_id)
        return dict(j) if j else None


def for_user(user_id):
    """Jobs for a user (or all when user_id is None), newest first."""
    with _LOCK:
        rows = [dict(j) for j in _JOBS.values()
                if user_id is None or j.get("user_id") == user_id]
    rows.sort(key=lambda j: j["created_at"], reverse=True)
    return rows
