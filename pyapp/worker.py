"""Restart-safe local queue. Run one web process; per-user queues are served fairly with global concurrency limits."""
import asyncio
import logging
import time
from datetime import datetime, timezone
from pyapp.config import settings
from pyapp.db import db, owner_var
from pyapp.media import media_processor, blocking_call
from pyapp.pipeline import process_video_pipeline
from pyapp import budget, jobs, limits, maintenance
from pyapp.sources import expand_ingestion  # noqa: F401  (re-exported for callers and tests)

logger = logging.getLogger(__name__)
JOB_CONCURRENCY = 2
MAINTENANCE_EVERY = 6 * 3600
_paused_logged: set = set()


async def recover_owner():
    for video in await db.get_videos():
        if video["status"] not in ("COMPLETED", "FAILED", "QUEUED", "UNAVAILABLE", "AWAITING_PERMISSION", "SKIPPED"):
            video["status"] = "QUEUED"
            await db.save_video(video)
    for job in await db.list("ingestions"):
        if job["status"] == "RESOLVING":
            job["status"] = "QUEUED"
            await db.save("ingestions", job)
    await jobs.recover()


async def tick_owner(tasks, owner):
    for job in reversed(await db.query("ingestions", {"status": "QUEUED"})):
        await expand_ingestion(job)
        break
    paused = await budget.blocked("video") or limits.storage_problem()      # running work finishes; nothing new starts until the cap clears
    if paused and owner not in _paused_logged:
        logger.warning("New analysis paused for %r: %s", owner, paused)
        _paused_logged.add(owner)
    elif not paused:
        _paused_logged.discard(owner)
    running_videos = sum(1 for key in tasks if key[1] == "video")
    if not paused:
        for video in reversed(await db.query("videos", {"status": "QUEUED"})):
            if running_videos >= settings.VIDEO_PROCESS_CONCURRENCY:
                break
            key = (owner, "video", video["id"])
            if key not in tasks and video.get("notBefore", 0) <= time.time():
                tasks[key] = asyncio.create_task(process_video_pipeline(video["id"], video["sourceUrl"]))
                running_videos += 1
    running_jobs = sum(1 for key in tasks if key[1] == "job")
    while running_jobs < JOB_CONCURRENCY:
        job = await jobs.next_queued()
        key = (owner, "job", job["id"]) if job else None
        if not job or key in tasks:
            break
        job["status"] = "RUNNING"          # claim immediately so the next tick does not start it twice
        await db.save("jobs", job)
        tasks[key] = asyncio.create_task(jobs.run_job(job))
        running_jobs += 1


async def run_worker():
    tasks, owners, refreshed, last_maintenance = {}, [""], 0.0, 0.0
    for owner in await db.owners():
        token = owner_var.set(owner)
        try:
            await recover_owner()
        finally:
            owner_var.reset(token)
    try:
        while True:
            for key, task in list(tasks.items()):
                if task.done():
                    if not task.cancelled() and task.exception():
                        logger.error("Worker task %s failed: %s", key, task.exception())
                    del tasks[key]
            if time.time() - refreshed > 10:
                owners, refreshed = await db.owners(), time.time()
            if time.time() - last_maintenance > MAINTENANCE_EVERY:
                last_maintenance = time.time()
                try:
                    await maintenance.run_once()
                except Exception:
                    logger.exception("Maintenance run failed")
            for owner in owners:
                token = owner_var.set(owner)
                try:
                    await tick_owner(tasks, owner)
                except Exception:
                    logger.exception("Worker tick failed for owner %r", owner)
                finally:
                    owner_var.reset(token)
            await asyncio.sleep(1)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
