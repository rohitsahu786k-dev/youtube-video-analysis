"""Abuse and storage protection: request rate limits, disk/inode guards and per-user queue caps.

The limiter is in-process and per client (user when signed in, otherwise IP). It exists so a stuck script, a refresh loop or a hostile client cannot
fill the disk (uploads, downloads, backtest exports), exhaust inodes (frame/evidence files) or burn the AI budget. nginx adds a coarser limit in front."""
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path

from fastapi import HTTPException
from starlette.responses import JSONResponse

from pyapp.config import settings

# (method, path regex, bucket name, max requests, window seconds). First match wins; unmatched /api paths use the "api" bucket.
RULES = (
    ("POST", re.compile(r"^/api/auth/login$"), "login", 10, 60),
    ("POST", re.compile(r"^/api/qa/conversations/[^/]+/ask$"), "ask", 12, 60),
    ("POST", re.compile(r"^/api/(media|market-data/csv|videos/[^/]+/(media|transcript)|runs/[^/]+/tradingview/compare|research/datasets)(/.*)?$"), "upload", 12, 60),
    ("POST", re.compile(r"^/api/(runs|optimize|compare|market-data/fetch|sources|ingestions|jobs/[^/]+/retry|library/rebuild)(/.*)?$"), "heavy", 20, 60),
    ("POST", re.compile(r"^/api/runs/[^/]+/export/sheets$"), "heavy", 20, 60),
    ("GET", re.compile(r"^/api/runs/[^/]+/export\.(csv|xlsx)$"), "export", 12, 60),
)
GENERAL = ("api", 300, 60)
MAX_KEYS = 50_000
_hits: dict[tuple, deque] = {}
_disk_cache = {"at": 0.0, "value": None}


def _prune(now: float):
    if len(_hits) <= MAX_KEYS:
        return
    for key in [k for k, q in _hits.items() if not q or now - q[-1] > 3600]:
        del _hits[key]
    if len(_hits) > MAX_KEYS:
        _hits.clear()                      # worst case: forget everybody rather than grow without bound


def check_rate(method: str, path: str, client: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds)."""
    if not settings.RATE_LIMIT_ENABLED or not path.startswith("/api/") or path == "/api/health":
        return True, 0
    bucket, limit, window = GENERAL
    for rule_method, pattern, name, max_requests, seconds in RULES:
        if method == rule_method and pattern.match(path):
            bucket, limit, window = name, max_requests, seconds
            break
    now = time.monotonic()
    queue = _hits.setdefault((bucket, client), deque())
    while queue and now - queue[0] > window:
        queue.popleft()
    if len(queue) >= limit:
        return False, max(1, int(window - (now - queue[0])) + 1)
    queue.append(now)
    _prune(now)
    return True, 0


class RateLimitMiddleware:
    """Pure ASGI. Sits inside SecurityMiddleware so the signed-in user is known; anonymous traffic is keyed by IP."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = (scope.get("state") or {}).get("user") or {}
            client = user.get("id") or (scope.get("client") or ("unknown",))[0]
            allowed, retry = check_rate(scope["method"], scope["path"], str(client))
            if not allowed:
                response = JSONResponse({"detail": f"Too many requests. Try again in {retry} s."}, status_code=429, headers={"Retry-After": str(retry)})
                return await response(scope, receive, send)
        await self.app(scope, receive, send)


# ---- storage -----------------------------------------------------------------------------------------

def storage_status() -> dict:
    """Free disk (MB) and free inode percentage for the volume that holds STORAGE_PATH. Cached for 15 s."""
    now = time.monotonic()
    if _disk_cache["value"] and now - _disk_cache["at"] < 15:
        return _disk_cache["value"]
    path = Path(settings.STORAGE_PATH)
    path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    inode_pct = None
    if hasattr(os, "statvfs"):
        stat = os.statvfs(path)
        inode_pct = round(100 * stat.f_favail / stat.f_files, 1) if stat.f_files else None
    value = {"freeMb": usage.free // (1024 * 1024), "totalMb": usage.total // (1024 * 1024), "freeInodePct": inode_pct}
    _disk_cache.update(at=now, value=value)
    return value


def storage_problem(extra_bytes: int = 0) -> str | None:
    status = storage_status()
    if status["freeMb"] - extra_bytes // (1024 * 1024) < settings.MIN_FREE_DISK_MB:
        return f"Server storage is low ({status['freeMb']} MB free). Delete unused media or videos and try again."
    if status["freeInodePct"] is not None and status["freeInodePct"] < settings.MIN_FREE_INODE_PCT:
        return f"Server file-count capacity is low ({status['freeInodePct']}% inodes free). Delete unused media or videos and try again."
    return None


def ensure_storage(extra_bytes: int = 0):
    problem = storage_problem(extra_bytes)
    if problem:
        raise HTTPException(507, problem)


async def ensure_queue_room(kind_label: str = "jobs"):
    """Cap how much unfinished background work one user can pile up."""
    from pyapp.db import db
    waiting = 0
    for status in ("QUEUED", "RUNNING"):
        waiting += len(await db.query("jobs", {"status": status}))
    if waiting >= settings.MAX_QUEUED_JOBS:
        raise HTTPException(429, f"{waiting} {kind_label} are already waiting or running. Let them finish (or cancel some) before adding more.")


def dir_size(path: Path) -> tuple[int, int]:
    """(bytes, file count) under a directory; used for per-user quotas."""
    total = count = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
                count += 1
            except OSError:
                continue
    return total, count
