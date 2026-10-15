"""Source manager: videos, playlists and whole channels with explicit coverage accounting."""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from pyapp.classify import classify_videos
from pyapp.config import settings
from pyapp.db import db
from pyapp.media import blocking_call, media_processor

logger = logging.getLogger(__name__)

ANALYZING = {"DOWNLOADING", "TRANSCRIBING", "DETECTING_EVENTS", "ANALYZING_VISUALS", "BUILDING_KNOWLEDGE", "EXTRACTING_STRATEGIES", "DRAFTING_RULES", "RESOLVING"}


def now():
    return datetime.now(timezone.utc).isoformat()


def status_group(status):
    """Map internal pipeline states onto the seven explicit states of the specification."""
    if status in ("DISCOVERED",):
        return "discovered"
    if status == "QUEUED":
        return "queued"
    if status in ANALYZING:
        return "analyzing"
    return {"COMPLETED": "completed", "FAILED": "failed", "UNAVAILABLE": "unavailable", "AWAITING_PERMISSION": "awaiting_permission", "SKIPPED": "skipped"}.get(status, "analyzing")


def source_id(kind, key):
    return "src-" + hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:14]


def coverage(source, videos):
    """Counts and an honest statement; full coverage is claimed only when nothing eligible is unprocessed."""
    counts = {k: 0 for k in ("discovered", "queued", "analyzing", "completed", "failed", "unavailable", "awaiting_permission", "skipped")}
    for v in videos:
        counts[status_group(v.get("status"))] += 1
    total = len(videos)
    eligible = total - counts["skipped"]
    pending = counts["discovered"] + counts["queued"] + counts["analyzing"]
    blockers = counts["failed"] + counts["unavailable"] + counts["awaiting_permission"]
    truncated = bool(source.get("truncated"))
    transcript_only = sum(1 for v in videos if v.get("status") == "COMPLETED" and v.get("visualAnalysis") is False)
    full = bool(eligible) and not truncated and not pending and not blockers and counts["completed"] == eligible
    parts = [f"{total} discovered", f"{counts['completed']} of {eligible} eligible analyzed"]
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped (not trading-related; can be forced)")
    if pending:
        parts.append(f"{pending} still queued or in progress")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    if counts["unavailable"]:
        parts.append(f"{counts['unavailable']} unavailable")
    if counts["awaiting_permission"]:
        parts.append(f"{counts['awaiting_permission']} awaiting permission (upload authorized media or a transcript)")
    if truncated:
        parts.append("discovery hit the configured limit so some videos were NOT discovered")
    if transcript_only:
        parts.append(f"{transcript_only} analysed from a transcript only (no chart analysis)")
    statement = ("Full coverage: every eligible discovered video was analysed. " if full else "Partial coverage: ") + ", ".join(parts) + "."
    return {**counts, "total": total, "eligible": eligible, "fullCoverage": full, "truncated": truncated, "transcriptOnly": transcript_only, "statement": statement}


def blocked_status(entry):
    """Initial status for a flat-listed video whose availability is already known to be restricted."""
    title = (entry.get("title") or "").lower()
    availability = (entry.get("availability") or "").lower()
    if title in ("[private video]", "[deleted video]") or availability in ("private",):
        return "UNAVAILABLE", "Private or deleted video."
    if availability in ("needs_auth", "subscriber_only", "premium_only"):
        return "AWAITING_PERMISSION", "Requires sign-in or membership. Provide authorized cookies, media or a transcript."
    return None, None


async def expand_ingestion(job):
    """Discover the videos of a video/playlist/channel source and queue new ones; existing records are never duplicated."""
    kind = job.get("kind") or ("playlist" if job.get("isPlaylist") else "video")
    job.update(status="RESOLVING", startedAt=now())
    await db.save("ingestions", job)
    try:
        playlist_like = kind in ("playlist", "channel")
        if kind == "channel":
            urls = [job["url"].rstrip("/") + "/videos", job["url"].rstrip("/") + "/streams"] + ([job["url"].rstrip("/") + "/shorts"] if settings.INCLUDE_SHORTS else [])
            info, truncated = None, False
            videos, seen = [], set()
            for position, url in enumerate(urls):
                try:
                    part = await blocking_call(media_processor.get_info, url, True, settings.MAX_CHANNEL_VIDEOS)
                except Exception as exc:
                    if position == 0:
                        raise
                    logger.info("Optional channel tab %s unavailable: %s", url, exc)
                    continue
                info = info or part
                truncated = truncated or bool(part.get("truncated"))
                for item in part["videos"]:
                    if item["youtubeId"] not in seen:
                        seen.add(item["youtubeId"])
                        videos.append(item)
            info = {**info, "videos": videos, "videoCount": len(videos), "truncated": truncated, "isPlaylist": True}
        else:
            info = await blocking_call(media_processor.get_info, job["url"], playlist_like)
        playlist_id = None
        if info.get("isPlaylist"):
            if kind == "playlist":
                playlist_id = info["playlistId"]
                await db.save("playlists", {"id": playlist_id, "title": info["title"], "channel": info.get("channel") or "Unknown", "videoCount": info["videoCount"], "url": job["url"]})
            videos = info["videos"]
        else:
            videos = [info]
        key = info.get("channelId") or info.get("playlistId") or job["url"] if kind != "video" else (info.get("youtubeId") or job["url"])
        sid = job.get("sourceId") or source_id(kind, key)
        source = (await db.get("sources", sid)) or {"id": sid, "kind": kind, "url": job["url"], "createdAt": now()}
        source.update(title=info.get("title") or source.get("title") or job["url"], channel=info.get("channel") or "Unknown", channelId=info.get("channelId"),
                      truncated=bool(info.get("truncated")), lastSyncedAt=now(), discoveredCount=len(videos), includeShorts=settings.INCLUDE_SHORTS)
        await db.save("sources", source)
        fresh = [v for v in videos if v.get("youtubeId") and not await db.get("videos", "video-" + v["youtubeId"])]
        labels = {}
        if kind == "channel" and fresh:
            labels = await classify_videos(fresh)
        count = skipped = 0
        changed = 0
        for item in videos:
            youtube_id = item.get("youtubeId")
            if not youtube_id:
                continue
            identifier = "video-" + youtube_id
            existing = await db.get_video(identifier)
            if existing:
                dirty = False
                if sid not in existing.setdefault("sourceIds", []):
                    existing["sourceIds"].append(sid)
                    dirty = True
                if playlist_id and playlist_id not in existing.get("playlistIds", []):
                    existing.setdefault("playlistIds", []).append(playlist_id)
                    dirty = True
                fingerprint = existing.get("sourceFingerprint")
                if fingerprint and item.get("title") and item.get("duration"):
                    current = hashlib.sha256(f"{item['title']}|{item['duration']}".encode()).hexdigest()[:16]
                    if existing.get("listFingerprint") and existing["listFingerprint"] != current:
                        existing["sourceChanged"] = True
                        changed += 1
                        dirty = True
                if dirty:
                    await db.save_video(existing)
                continue
            status, note = blocked_status(item)
            label = labels.get(youtube_id)
            eligible = True
            if status is None and label and label["category"] == "not_trading" and label["confidence"] >= 0.6:
                status, note, eligible = "SKIPPED", "Classified as not trading-related; use 'Analyze anyway' to override.", False
                skipped += 1
            await db.save_video({"id": identifier, "youtubeId": youtube_id, "title": item.get("title") or youtube_id,
                "duration": item.get("duration") or 0, "channel": info.get("channel") or "Unknown", "status": status or "QUEUED",
                "statusNote": note, "progress": 0, "trades": 0, "events": 0, "attempts": 0,
                "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{youtube_id}/hqdefault.jpg",
                "playlistIds": [playlist_id] if playlist_id else [], "sourceIds": [sid], "sourceUrl": f"https://www.youtube.com/watch?v={youtube_id}",
                "published": item.get("uploadDate") or "Unknown", "updatedAt": now(), "category": (label or {}).get("category"),
                "classificationSource": (label or {}).get("source"), "eligible": eligible,
                "listFingerprint": hashlib.sha256(f"{item.get('title')}|{item.get('duration') or 0}".encode()).hexdigest()[:16] if item.get("title") and item.get("duration") else None})
            count += 1
        job.update(status="COMPLETED", videoCount=len(videos), queuedCount=count, skippedCount=skipped, sourceChangedCount=changed, sourceId=sid, truncated=bool(info.get("truncated")), error=None, finishedAt=now())
    except asyncio.CancelledError:
        job["status"] = "QUEUED"
        raise
    except Exception as exc:
        logger.exception("Source discovery failed")
        job.update(status="FAILED", error=str(exc), finishedAt=now())
    finally:
        await db.save("ingestions", job)


async def source_overview():
    videos = await db.get_videos()
    rows = []
    for source in await db.list("sources"):
        members = [v for v in videos if source["id"] in v.get("sourceIds", [])]
        rows.append({**source, "coverage": coverage(source, members)})
    return rows
