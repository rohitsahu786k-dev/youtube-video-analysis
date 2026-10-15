"""Durable background jobs (data download, backtests, optimisation, library rebuilds, source sync).

Jobs live in the `jobs` collection of the owning user. A job found RUNNING at start-up was interrupted and is retried
(up to MAX_ATTEMPTS) so work resumes after a restart instead of being silently lost.
"""
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from uuid import uuid4

from pyapp.db import db

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
HANDLERS: dict = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def handler(kind):
    def register(fn):
        HANDLERS[kind] = fn
        return fn
    return register


async def enqueue(kind: str, params: dict, label: str = "") -> dict:
    if kind == "rebuild_library":                  # one pending rebuild covers every change made before it starts
        pending = await db.query("jobs", {"status": "QUEUED"})
        existing = next((j for j in pending if j["kind"] == kind), None)
        if existing:
            return existing
    job = {"id": uuid4().hex, "kind": kind, "label": label or kind, "params": params, "status": "QUEUED", "progress": 0, "message": "Queued",
           "attempts": 0, "createdAt": now(), "result": None, "error": None}
    await db.save("jobs", job)
    return job


async def update(job: dict, progress: float | None = None, message: str | None = None, **extra):
    if progress is not None:
        job["progress"] = round(max(0.0, min(100.0, progress)), 1)
    if message is not None:
        job["message"] = message
    job.update(extra)
    await db.save("jobs", job)


async def recover():
    for job in await db.list("jobs"):
        if job["status"] == "RUNNING":
            job.update(status="QUEUED" if job["attempts"] < MAX_ATTEMPTS else "FAILED", message="Resumed after restart" if job["attempts"] < MAX_ATTEMPTS else "Interrupted too many times")
            await db.save("jobs", job)


async def run_job(job: dict):
    fn = HANDLERS.get(job["kind"])
    job.update(status="RUNNING", startedAt=now(), attempts=job["attempts"] + 1, error=None)
    await db.save("jobs", job)
    try:
        if fn is None:
            raise RuntimeError(f"No handler for job kind '{job['kind']}'")
        result = await fn(job)
        job.update(status="COMPLETED", progress=100, finishedAt=now(), result=result, message="Completed")
    except asyncio.CancelledError:
        job.update(status="QUEUED", message="Interrupted; will resume")
        await db.save("jobs", job)
        raise
    except Exception as exc:
        logger.exception("Job %s failed", job["id"])
        job.update(status="FAILED", finishedAt=now(), error=str(exc)[:2000], message="Failed", trace=traceback.format_exc()[-3000:])
    await db.save("jobs", job)


async def next_queued():
    queued = await db.query("jobs", {"status": "QUEUED"})          # indexed; newest first, so the oldest waiting job is last
    return queued[-1] if queued else None
