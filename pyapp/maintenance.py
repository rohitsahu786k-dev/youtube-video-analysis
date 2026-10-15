"""Periodic housekeeping so the server never silently fills its disk or inodes: stale temp files, old finished jobs, WAL growth."""
import logging
import os
import shutil
import time
from pathlib import Path

from pyapp import limits
from pyapp.config import settings
from pyapp.db import db

logger = logging.getLogger(__name__)
TMP_MAX_AGE = 24 * 3600


def purge_old_files(directory: Path, max_age: float) -> int:
    """Delete files (and then empty folders) older than max_age seconds. Returns the number of files removed."""
    removed, cutoff = 0, time.time() - max_age
    if not directory.is_dir():
        return 0
    for root, dirs, files in os.walk(directory, topdown=False):
        for name in files:
            path = Path(root) / name
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        for name in dirs:
            try:
                (Path(root) / name).rmdir()          # only succeeds when empty
            except OSError:
                pass
    return removed


async def run_once() -> dict:
    storage = Path(settings.STORAGE_PATH)
    report = {"tmpFilesRemoved": sum(purge_old_files(storage / folder, TMP_MAX_AGE) for folder in ("tmp", "downloads", "chunks")), "storage": limits.storage_status()}
    try:
        report["database"] = await db.maintain(settings.JOB_RETENTION_DAYS)
    except Exception:
        logger.exception("Database maintenance failed")
    problem = limits.storage_problem()
    if problem:
        logger.warning("Storage guard active: %s", problem)
    logger.info("Maintenance: %s", report)
    return report
