"""Restart-safe local queue. Run one web process with configured worker concurrency."""
import asyncio
import logging
from datetime import datetime, timezone
from pyapp.config import settings
from pyapp.db import db
from pyapp.media import media_processor, blocking_call
from pyapp.pipeline import process_video_pipeline

logger = logging.getLogger(__name__)

async def expand_ingestion(job):
    job.update(status="RESOLVING")
    await db.save("ingestions", job)
    try:
        info = await blocking_call(media_processor.get_info, job["url"], job["isPlaylist"])
        playlist_id = None
        if info.get("isPlaylist"):
            playlist_id = info["playlistId"]
            await db.save("playlists", {"id": playlist_id, "title": info["title"], "channel": info.get("channel") or "Unknown", "videoCount": info["videoCount"], "url": job["url"]})
            videos = info["videos"]
        else:
            videos = [info]
        count = 0
        for item in videos:
            youtube_id = item.get("youtubeId")
            if not youtube_id:
                continue
            identifier = "video-" + youtube_id
            existing = await db.get_video(identifier)
            if existing:
                if playlist_id and playlist_id not in existing.get("playlistIds", []):
                    existing.setdefault("playlistIds", []).append(playlist_id)
                    await db.save_video(existing)
                continue
            await db.save_video({"id": identifier, "youtubeId": youtube_id, "title": item.get("title") or youtube_id,
                "duration": item.get("duration") or 0, "channel": info.get("channel") or "Unknown", "status": "QUEUED",
                "progress": 0, "trades": 0, "events": 0, "thumbnail": item.get("thumbnail") or f"https://i.ytimg.com/vi/{youtube_id}/hqdefault.jpg",
                "playlistIds": [playlist_id] if playlist_id else [], "sourceUrl": f"https://www.youtube.com/watch?v={youtube_id}",
                "published": "Unknown", "updatedAt": datetime.now(timezone.utc).isoformat()})
            count += 1
        job.update(status="COMPLETED", videoCount=len(videos), queuedCount=count, error=None)
    except asyncio.CancelledError:
        job["status"] = "QUEUED"
        raise
    except Exception as exc:
        logger.exception("Playlist expansion failed")
        job.update(status="FAILED", error=str(exc))
    finally:
        await db.save("ingestions", job)

async def run_worker():
    tasks = {}
    for video in await db.get_videos():
        if video["status"] not in ("COMPLETED", "FAILED", "QUEUED"):
            video["status"] = "QUEUED"
            await db.save_video(video)
    for job in await db.list("ingestions"):
        if job["status"] == "RESOLVING":
            job["status"] = "QUEUED"
            await db.save("ingestions", job)
    try:
        while True:
            for key, task in list(tasks.items()):
                if task.done():
                    if not task.cancelled() and task.exception():
                        logger.error("Worker task failed: %s", task.exception())
                    del tasks[key]
            for job in await db.list("ingestions"):
                if job["status"] == "QUEUED":
                    await expand_ingestion(job)
                    break
            for video in reversed(await db.get_videos()):
                if len(tasks) >= settings.VIDEO_PROCESS_CONCURRENCY:
                    break
                if video["status"] == "QUEUED" and video["id"] not in tasks:
                    tasks[video["id"]] = asyncio.create_task(process_video_pipeline(video["id"], video["sourceUrl"]))
            await asyncio.sleep(1)
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
