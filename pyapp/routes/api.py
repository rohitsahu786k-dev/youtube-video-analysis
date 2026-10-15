import asyncio
import shutil
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from pyapp.config import settings
from pyapp.db import db
from pyapp.ai import answer_rag, get_client, search_terms
from pyapp.intelligence import aggregate_stats, build_patterns, retrieve
from pyapp.media import media_processor
from pyapp.youtube import parse_source

router = APIRouter(prefix="/api", tags=["API"])

class ChatRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)

class IngestRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)

def require_processing():
    if settings.MOCK_AI_MODE or not settings.GEMINI_API_KEY:
        raise HTTPException(503, "Real analysis requires GEMINI_API_KEY and MOCK_AI_MODE=false.")
    try:
        media_processor.ffmpeg()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

@router.get("/health")
async def health():
    try:
        media_processor.ffmpeg()
        ffmpeg = True
    except RuntimeError:
        ffmpeg = False
    await db.connect()
    return {"status": "ok", "mode": "disabled" if settings.MOCK_AI_MODE else "real",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "services": {"database": True, "databaseEngine": "sqlite", "gemini": bool(settings.GEMINI_API_KEY),
            "ffmpeg": ffmpeg, "storage": True, "workerEnabled": settings.ENABLE_WORKER, "liveMarketData": bool(settings.OANDA_API_TOKEN and settings.OANDA_ACCOUNT_ID)}}

@router.get("/health/gemini")
async def health_gemini():
    if settings.MOCK_AI_MODE:
        return {"status": "disabled"}
    if not settings.GEMINI_API_KEY:
        return {"status": "not_configured"}
    client = get_client()
    try:
        models = {}
        for model_name in {settings.GEMINI_ANALYSIS_MODEL, settings.GEMINI_REASONING_MODEL, settings.GEMINI_TRANSCRIBE_MODEL, settings.GEMINI_FINAL_MODEL}:
            model = await asyncio.to_thread(client.models.get, model=model_name)
            models[model_name] = model.name
        return {"status": "ok", "provider": "gemini", "models": models}
    except Exception as exc:
        raise HTTPException(503, "Gemini model access failed. Check the configured API key, models and service quota.") from exc
    finally:
        await client.aio.aclose()
        client.close()

@router.get("/stats")
async def stats():
    return aggregate_stats(await db.get_videos(), await db.get_trades())

@router.get("/videos")
async def list_videos(playlist: Optional[str] = None):
    return [v for v in await db.get_videos() if not playlist or playlist in v.get("playlistIds", [])]

@router.get("/videos/{video_id}")
async def get_video(video_id: str):
    video = await db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    details = await db.get("video_details", video["id"]) or {}
    return {"video": video, "trades": await db.get_trades(video["id"]), "events": details.get("events") or video.get("detectedEvents", []), "transcript": details.get("transcript") or video.get("transcript", [])}

@router.post("/ingest", status_code=202)
async def ingest_video(req: IngestRequest):
    """Accepts a video, playlist or channel URL (kept for the header button; /api/sources is the richer entry point)."""
    try:
        parsed = parse_source(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    require_processing()
    url = parsed["url"]
    for job in await db.list("ingestions"):
        if job["url"] == url and job["status"] in ("QUEUED", "RESOLVING"):
            return {"status": "queued", "jobId": job["id"], "kind": parsed["kind"], "message": "Already queued"}
    job = {"id": uuid4().hex, "url": url, "kind": parsed["kind"], "isPlaylist": parsed["kind"] != "video", "status": "QUEUED", "createdAt": datetime.now(timezone.utc).isoformat()}
    await db.save("ingestions", job)
    return {"status": "queued", "jobId": job["id"], "kind": parsed["kind"], "message": {"video": "Video discovery queued", "playlist": "Playlist discovery queued", "channel": "Channel discovery queued"}[parsed["kind"]]}

@router.get("/pipeline")
async def pipeline_status():
    """Discovery queue plus per-video pipeline state (the generic job list is /api/jobs)."""
    return {"ingestions": await db.list("ingestions"), "videos": await db.get_jobs()}

@router.post("/ingestions/{identifier}/retry")
async def retry_ingestion(identifier: str):
    require_processing()
    job = await db.get("ingestions", identifier)
    if not job:
        raise HTTPException(404, "Ingestion not found")
    if job["status"] != "FAILED":
        raise HTTPException(409, "Only failed ingestions can be retried")
    job.update(status="QUEUED", error=None)
    await db.save("ingestions", job)
    return job

@router.post("/videos/{identifier}/retry")
async def retry_video(identifier: str):
    require_processing()
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    if video["status"] != "FAILED":
        raise HTTPException(409, "Only failed videos can be retried")
    video.update(status="QUEUED", error=None, progress=0)
    await db.save_video(video)
    return {"status": "queued", "videoId": video["id"]}

@router.get("/playlists")
async def playlists():
    return await db.get_playlists()

@router.get("/knowledge")
async def knowledge(q: str = ""):
    chunks = await db.list("knowledge")
    return retrieve(q, chunks, 50) if q else chunks

@router.get("/patterns")
async def patterns():
    return build_patterns(await db.get_trades(), await db.list("knowledge"))

@router.get("/trades")
async def list_trades(symbol: Optional[str] = None, direction: Optional[str] = None, outcome: Optional[str] = None):
    return [t for t in await db.get_trades() if (not symbol or t.get("symbol") == symbol.upper())
        and (not direction or t.get("direction") == direction.upper()) and (not outcome or t.get("result") == outcome.upper())]

@router.get("/trades/{trade_id}")
async def get_trade(trade_id: str):
    trade = await db.get_trade(trade_id)
    if not trade:
        raise HTTPException(404, "Trade not found")
    return trade

@router.post("/chat")
async def chat(req: ChatRequest):
    knowledge = await db.list("knowledge")
    evidence = retrieve(req.question, knowledge)
    try:
        if not evidence and knowledge and settings.GEMINI_API_KEY and not settings.MOCK_AI_MODE:
            evidence = retrieve(await search_terms(req.question), knowledge)
        text = await answer_rag(req.question.strip(), evidence)
    except Exception as exc:
        raise HTTPException(503, "Gemini answer generation failed; check model access and quota.") from exc
    return {"answer": text, "sources": [{"id": k["id"], "title": k["video"], "timestamp": k["time"], "type": k["type"], "url": k["sourceUrl"]} for k in evidence]}
