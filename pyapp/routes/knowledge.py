"""API for sources/coverage, video actions, authorized uploads, the strategy library, Q&A and dashboard statistics."""
import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from pyapp import jobs, limits, qa
from pyapp.config import settings
from pyapp.db import db
from pyapp.grounding import youtube_link
from pyapp.sources import coverage, source_overview, status_group
from pyapp.youtube import parse_source

router = APIRouter(prefix="/api", tags=["Knowledge"])


def now():
    return datetime.now(timezone.utc).isoformat()


from pyapp.routes.api import require_processing  # AI key + FFmpeg present


def require_ai():
    if settings.MOCK_AI_MODE or not settings.GEMINI_API_KEY:
        raise HTTPException(503, "This action needs GEMINI_API_KEY and MOCK_AI_MODE=false.")


# ---- sources ---------------------------------------------------------------------------------------

class SourceRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2048)


@router.post("/sources", status_code=202)
async def add_source(req: SourceRequest):
    try:
        parsed = parse_source(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    require_processing()
    for job in await db.list("ingestions"):
        if job["url"] == parsed["url"] and job["status"] in ("QUEUED", "RESOLVING"):
            return {"status": "queued", "jobId": job["id"], "kind": parsed["kind"], "message": "Already queued"}
    job = {"id": uuid4().hex, "url": parsed["url"], "kind": parsed["kind"], "isPlaylist": parsed["kind"] != "video", "status": "QUEUED", "createdAt": now()}
    await db.save("ingestions", job)
    return {"status": "queued", "jobId": job["id"], "kind": parsed["kind"], "message": f"{parsed['kind'].capitalize()} discovery queued"}


@router.post("/sources/preview")
async def preview_source(req: SourceRequest):
    try:
        parsed = parse_source(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    thumb = f"https://i.ytimg.com/vi/{parsed['id']}/hqdefault.jpg" if parsed["kind"] == "video" else None
    return {**parsed, "thumbnail": thumb, "note": {"video": "A single video will be analysed.", "playlist": "Every video in the playlist will be discovered and queued.",
            "channel": "All accessible public videos (and past live streams) of the channel are discovered; videos not related to trading are skipped but can be forced."}[parsed["kind"]]}


@router.get("/sources")
async def list_sources():
    return await source_overview()


@router.get("/ingestions")
async def list_ingestions():
    return await db.list("ingestions", 40)


@router.post("/sources/{source_id}/sync", status_code=202)
async def sync_source(source_id: str):
    source = await db.get("sources", source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    require_processing()
    job = {"id": uuid4().hex, "url": source["url"], "kind": source["kind"], "isPlaylist": source["kind"] != "video", "sourceId": source_id, "status": "QUEUED", "createdAt": now(), "sync": True}
    await db.save("ingestions", job)
    return {"status": "queued", "jobId": job["id"]}


@router.delete("/sources/{source_id}")
async def delete_source(source_id: str):
    """Remove the source record only; analysed evidence and strategies stay (they are linked to videos)."""
    if not await db.get("sources", source_id):
        raise HTTPException(404, "Source not found")
    await db.delete("sources", source_id)
    return {"deleted": source_id}


@router.get("/sources/{source_id}/videos")
async def source_videos(source_id: str, status: str | None = None):
    videos = [v for v in await db.get_videos() if source_id in v.get("sourceIds", [])]
    return [{k: v for k, v in x.items() if k not in ("transcript", "detectedEvents", "observations")} | {"group": status_group(x["status"])} for x in videos if not status or status_group(x["status"]) == status]


# ---- video actions ------------------------------------------------------------------------------------

@router.post("/videos/{identifier}/analyze")
async def force_analyze(identifier: str):
    """Analyze a skipped / awaiting / failed video (user override)."""
    require_processing()
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    if status_group(video["status"]) in ("analyzing", "queued"):
        raise HTTPException(409, "Already queued or running")
    video.update(status="QUEUED", error=None, progress=0, attempts=0, statusNote=None, notBefore=0, eligible=True)
    await db.save_video(video)
    return {"status": "queued", "videoId": video["id"]}


@router.post("/videos/{identifier}/reprocess")
async def reprocess(identifier: str):
    """Re-run the full analysis (e.g. after the source video changed). Previous evidence is replaced atomically on completion."""
    require_processing()
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    await db.delete("video_details", video["id"])
    video.update(status="QUEUED", error=None, progress=0, attempts=0, statusNote=None, sourceChanged=False, notBefore=0)
    await db.save_video(video)
    return {"status": "queued", "videoId": video["id"]}


def parse_timestamp(text):
    text = text.strip().replace(",", ".")
    parts = text.split(":")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def parse_transcript(content: str, filename: str) -> tuple[list, bool]:
    """SRT / VTT / JSON / plain text -> segments. Returns (segments, timestamps_estimated)."""
    name = filename.lower()
    if name.endswith(".json"):
        import json
        data = json.loads(content)
        data = data.get("segments", data) if isinstance(data, dict) else data
        segments = [{"start": float(x["start"]), "end": float(x.get("end", x["start"] + 3)), "text": str(x["text"]).strip()} for x in data if str(x.get("text", "")).strip()]
        return segments, False
    stamp = re.compile(r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}")
    if stamp.search(content):
        segments, blocks = [], re.split(r"\n\s*\n", content.replace("\r\n", "\n"))
        for block in blocks:
            lines = [l for l in block.split("\n") if l.strip()]
            for i, line in enumerate(lines):
                if stamp.search(line):
                    a, b = [x.strip().split(" ")[0] for x in line.split("-->")]
                    text = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:])).strip()
                    if text:
                        segments.append({"start": parse_timestamp(a), "end": parse_timestamp(b), "text": text})
                    break
        return segments, False
    words = content.split()
    if not words:
        return [], True
    segments, step, t = [], 40, 0.0                                   # no timestamps: ~2.5 words per second, flagged as estimated
    for i in range(0, len(words), step):
        chunk = words[i:i + step]
        duration = max(4.0, len(chunk) / 2.5)
        segments.append({"start": round(t, 1), "end": round(t + duration, 1), "text": " ".join(chunk)})
        t += duration
    return segments, True


@router.post("/videos/{identifier}/transcript", status_code=202)
async def upload_transcript(identifier: str, file: UploadFile = File(...)):
    """Authorized transcript (SRT/VTT/JSON/TXT) for a video that cannot be downloaded. No chart analysis is performed."""
    require_ai()
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    raw = await file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Transcript exceeds 10 MB")
    try:
        segments, estimated = parse_transcript(raw.decode("utf-8-sig"), file.filename or "transcript.txt")
    except Exception as exc:
        raise HTTPException(400, f"Could not read the transcript: {exc}") from exc
    if len(segments) < 3:
        raise HTTPException(400, "The transcript has too little text.")
    details = await db.get("video_details", video["id"]) or {"id": video["id"]}
    details.update(transcript=sorted(segments, key=lambda s: s["start"]), transcriptComplete=True, timestampsEstimated=estimated, events=None)
    details.pop("events", None)
    details.pop("observations", None)
    details.pop("trades", None)
    await db.save("video_details", details)
    video.update(inputMode="uploaded_transcript", status="QUEUED", error=None, progress=0, attempts=0, statusNote="Analysing an uploaded transcript (no chart analysis).", notBefore=0,
                 duration=video.get("duration") or int(max(s["end"] for s in segments)) + 1, timestampsEstimated=estimated)
    await db.save_video(video)
    return {"status": "queued", "segments": len(segments), "timestampsEstimated": estimated}


@router.post("/videos/{identifier}/media", status_code=202)
async def upload_media(identifier: str, file: UploadFile = File(...)):
    """Authorized media file (mp4/mkv/webm/mov/mp3/m4a/wav) for a video that cannot be downloaded."""
    require_processing()
    limits.ensure_storage(settings.MAX_UPLOAD_MB * 1024 * 1024)
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".wav"):
        raise HTTPException(400, "Upload mp4, mkv, webm, mov, mp3, m4a or wav.")
    folder = Path(settings.STORAGE_PATH) / "uploads" / re.sub(r"[^A-Za-z0-9_-]", "", video["id"])
    folder.mkdir(parents=True, exist_ok=True)
    target, size, limit = folder / f"source{suffix}", 0, settings.MAX_UPLOAD_MB * 1024 * 1024
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, f"File exceeds {settings.MAX_UPLOAD_MB} MB")
            handle.write(chunk)
    await db.delete("video_details", video["id"])
    video.update(inputMode="uploaded_media", uploadedMediaPath=str(target), status="QUEUED", error=None, progress=0, attempts=0, notBefore=0, statusNote="Analysing authorized uploaded media.")
    await db.save_video(video)
    return {"status": "queued", "bytes": size}


@router.post("/videos/{identifier}/extract-strategies", status_code=202)
async def reextract(identifier: str):
    require_ai()
    video = await db.get_video(identifier)
    details = await db.get("video_details", video["id"]) if video else None
    if not video or not details or not details.get("transcript"):
        raise HTTPException(404, "The video has no stored transcript to extract from.")
    from pyapp.library import build_atoms
    atoms = await build_atoms(video, details["transcript"], details.get("observations", []), video.get("duration") or 0)
    await db.replace_for_video("atoms", video["id"], atoms)
    video.update(atoms=len(atoms), atomsVerified=sum(1 for a in atoms if a["provenance"] == "source_confirmed"), strategyWarning=None)
    await db.save_video(video)
    await jobs.enqueue("rebuild_library", {}, "Rebuild strategy library")
    return {"atoms": len(atoms), "verified": video["atomsVerified"]}


@router.get("/videos/{identifier}/atoms")
async def video_atoms(identifier: str):
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    return await db.query("atoms", {"videoId": video["id"]})


@router.delete("/videos/{identifier}")
async def delete_video(identifier: str):
    """Remove a video and everything derived from it (transcript, frames, rules, trades). The library is rebuilt afterwards."""
    video = await db.get_video(identifier)
    if not video:
        raise HTTPException(404, "Video not found")
    if status_group(video["status"]) == "analyzing":
        raise HTTPException(409, "The video is being analysed right now; wait for it to finish or fail.")
    vid = video["id"]
    for collection in ("atoms", "trades", "knowledge"):
        for item in await db.query(collection, {"videoId": vid}):
            await db.delete(collection, item["id"])
    await db.delete("video_details", vid)
    await db.delete("videos", vid)
    import shutil
    for base in (Path(settings.STORAGE_PATH) / "evidence" / vid, Path(settings.STORAGE_PATH) / "uploads" / re.sub(r"[^A-Za-z0-9_-]", "", vid)):
        shutil.rmtree(base, ignore_errors=True)
    queued = [j for j in await db.list("jobs") if j["kind"] == "rebuild_library" and j["status"] == "QUEUED"]
    if not queued:
        await jobs.enqueue("rebuild_library", {}, "Rebuild strategy library")
    return {"deleted": vid}


# ---- library ------------------------------------------------------------------------------------------

@router.get("/library/strategies")
async def list_strategies(q: str = "", status: str | None = None, readiness: str | None = None, sort: str = "rank"):
    strategies = await db.list("lib_strategies")
    versions = await db.list("strategy_versions")
    by = {}
    for v in versions:
        by.setdefault(v["strategyId"], []).append(v)
    rows = []
    for s in strategies:
        mine = by.get(s["id"], [])
        best = max((v for v in mine if v["status"] in ("APPROVED", "FROZEN")), key=lambda v: v["number"], default=None) or (max(mine, key=lambda v: v["number"]) if mine else None)
        level = (best or {}).get("readiness", {}).get("level") if best else None
        text = f"{s['name']} {' '.join(s['aliases'])} {s.get('notes', '')}".lower()
        if q and q.lower() not in text:
            continue
        if status and s["status"] != status:
            continue
        if readiness and level != readiness:
            continue
        rows.append({"id": s["id"], "name": s["name"], "aliases": s["aliases"], "relation": s["relation"], "variationOf": s.get("variationOf"), "status": s["status"], "starred": s.get("starred", False),
                     "sourceVideos": len(s["sources"]), "exampleCount": s["exampleCount"], "confirmedCount": s["confirmedCount"], "unverifiedCount": s["unverifiedCount"], "completeness": s["completeness"],
                     "conflicts": len(s["conflicts"]), "rankScore": s["rankScore"], "readiness": level, "versions": len(mine), "approvedVersion": (best or {}).get("label") if best and best["status"] != "DRAFT" else None,
                     "updatedAt": s["updatedAt"], "orphaned": s.get("orphaned", False), "hasManual": bool(s.get("manual"))})
    key = {"rank": lambda r: -r["rankScore"], "name": lambda r: r["name"].lower(), "recent": lambda r: r["updatedAt"], "examples": lambda r: -r["exampleCount"]}.get(sort, lambda r: -r["rankScore"])
    return sorted(rows, key=key, reverse=(sort == "recent"))


@router.get("/library/strategies/{strategy_id}")
async def get_strategy(strategy_id: str):
    s = await db.get("lib_strategies", strategy_id)
    if not s:
        raise HTTPException(404, "Strategy not found")
    atoms = {a["id"]: a for a in await db.list("atoms") if a["id"] in set(s["atomIds"]) | set(s.get("generalAtomIds", []))}
    versions = sorted(await db.query("strategy_versions", {"strategyId": strategy_id}), key=lambda v: v["number"], reverse=True)
    cited = {i for sec in (s.get("manual") or {}).values() for item in sec for i in item.get("atomIds", [])}
    evidence = {i: {k: atoms[i][k] for k in ("id", "text", "quote", "videoTitle", "published", "startTime", "sourceUrl", "provenance", "evidenceType", "visualNote", "frames", "category", "timestampCorrected", "quoteMethod") if k in atoms[i]} for i in cited if i in atoms}
    unverified = [{k: a[k] for k in ("id", "text", "quote", "videoTitle", "startTime", "sourceUrl", "category", "provenance")} for a in atoms.values() if a["provenance"] != "source_confirmed" and a["id"] in set(s["atomIds"])]
    return {**s, "evidence": evidence, "unverifiedAtoms": unverified, "versions": [{k: v for k, v in x.items() if k != "spec"} for x in versions]}


class StrategyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    starred: bool | None = None
    notes: str | None = Field(default=None, max_length=4000)
    status: str | None = Field(default=None, pattern="^(NEEDS_REVIEW|IN_REVIEW|REVIEWED)$")


@router.patch("/library/strategies/{strategy_id}")
async def patch_strategy(strategy_id: str, patch: StrategyPatch):
    s = await db.get("lib_strategies", strategy_id)
    if not s:
        raise HTTPException(404, "Strategy not found")
    changes = patch.model_dump(exclude_none=True)
    if "name" in changes and not s.get("userAuthored"):
        raise HTTPException(409, "Extracted strategies keep the instructor's own name; only your own strategies can be renamed.")
    for field, value in changes.items():
        s[field] = value
    s["rankScore"] = s["rankScore"] + (10 if patch.starred else -10 if patch.starred is False and s.get("rankScore", 0) >= 10 else 0)
    s["updatedAt"] = now()
    await db.save("lib_strategies", s)
    return s


@router.delete("/library/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str):
    """Delete a hand-written strategy and its DRAFT versions. Strategies with approved versions or runs are kept for the audit trail."""
    s = await db.get("lib_strategies", strategy_id)
    if not s:
        raise HTTPException(404, "Strategy not found")
    if not s.get("userAuthored"):
        raise HTTPException(409, "Extracted strategies are derived from your videos and are rebuilt from them; delete the source videos instead.")
    versions = await db.query("strategy_versions", {"strategyId": strategy_id})
    if any(v["status"] != "DRAFT" for v in versions) or any(r.get("strategyId") == strategy_id for r in await db.list("bt_runs")):
        raise HTTPException(409, "This strategy has approved versions or backtest runs. Delete those runs first; approved versions are kept for auditability.")
    for v in versions:
        await db.delete("strategy_versions", v["id"])
    for c in await db.query("conversations", {"strategyId": strategy_id}):
        for m in await db.query("messages", {"parentId": c["id"]}):
            await db.delete("messages", m["id"])
        await db.delete("conversations", c["id"])
    await db.delete("lib_strategies", strategy_id)
    return {"deleted": strategy_id}


@router.post("/library/rebuild", status_code=202)
async def rebuild_library():
    return await jobs.enqueue("rebuild_library", {}, "Rebuild strategy library")


@router.post("/library/strategies/{strategy_id}/translate", status_code=202)
async def translate_strategy(strategy_id: str):
    require_ai()
    if not await db.get("lib_strategies", strategy_id):
        raise HTTPException(404, "Strategy not found")
    return await jobs.enqueue("translate_strategy", {"strategyId": strategy_id}, "Translate rules to an executable draft")


@router.post("/library/custom", status_code=201)
async def create_custom_strategy(payload: dict[str, Any]):
    """A hand-authored strategy (no video evidence). Its versions are labelled as user-authored, never instructor-confirmed."""
    name = str(payload.get("name", "")).strip()
    if not 2 <= len(name) <= 120:
        raise HTTPException(422, "Give the strategy a name.")
    sid = "custom-" + uuid4().hex[:8]
    doc = {"id": sid, "name": name, "aliases": [], "relation": "distinct", "variationOf": None, "atomIds": [], "confirmedCount": 0, "unverifiedCount": 0, "generalAtomIds": [], "sources": [], "exampleCount": 0,
           "examples": [], "manual": None, "manualMissing": ["User-authored strategy: no instructor evidence is attached."], "manualDropped": 0, "conflicts": [], "completeness": {"score": 0, "components": {}, "objectiveShare": 0, "missingCore": []},
           "evidenceHash": None, "status": "NEEDS_REVIEW", "starred": False, "notes": "User-authored", "createdAt": now(), "updatedAt": now(), "rankScore": 0, "userAuthored": True}
    await db.save("lib_strategies", doc)
    return doc


# ---- Q&A ----------------------------------------------------------------------------------------------

class ConversationCreate(BaseModel):
    strategyId: str | None = None


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


@router.post("/qa/conversations", status_code=201)
async def new_conversation(req: ConversationCreate):
    if req.strategyId and not await db.get("lib_strategies", req.strategyId):
        raise HTTPException(404, "Strategy not found")
    doc = {"id": uuid4().hex, "title": "New conversation", "strategyId": req.strategyId, "createdAt": now(), "updatedAt": now()}
    await db.save("conversations", doc)
    return doc


@router.get("/qa/conversations")
async def conversations(strategyId: str | None = None):
    rows = await db.list("conversations")
    return [c for c in rows if not strategyId or c.get("strategyId") == strategyId]


@router.get("/qa/conversations/{conversation_id}")
async def conversation(conversation_id: str):
    c = await db.get("conversations", conversation_id)
    if not c:
        raise HTTPException(404, "Conversation not found")
    messages = sorted(await db.query("messages", {"parentId": conversation_id}), key=lambda m: m["createdAt"])
    return {**c, "messages": messages}


@router.post("/qa/conversations/{conversation_id}/ask")
async def ask(conversation_id: str, req: Question):
    c = await db.get("conversations", conversation_id)
    if not c:
        raise HTTPException(404, "Conversation not found")
    return await qa.ask(c, req.question.strip())


@router.delete("/qa/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    for m in await db.query("messages", {"parentId": conversation_id}):
        await db.delete("messages", m["id"])
    await db.delete("conversations", conversation_id)
    return {"deleted": conversation_id}


@router.get("/evidence/{kind}/{item_id}")
async def evidence(kind: str, item_id: str):
    """Everything needed to open the original video at the right moment: link, quote, frames and surrounding transcript."""
    if kind == "atom":
        a = await db.get("atoms", item_id)
        if not a:
            raise HTTPException(404, "Evidence not found")
        item = {"id": a["id"], "text": a["text"], "quote": a["quote"], "videoId": a["videoId"], "youtubeId": a["youtubeId"], "video": a["videoTitle"], "time": a["startTime"], "url": a["sourceUrl"],
                "provenance": a["provenance"], "frames": a.get("frames", []), "visualNote": a.get("visualNote"), "published": a.get("published")}
    elif kind == "knowledge":
        k = await db.get("knowledge", item_id)
        if not k:
            raise HTTPException(404, "Evidence not found")
        item = {"id": k["id"], "text": k["content"], "quote": k["content"] if k["type"] == "Spoken" else None, "videoId": k["videoId"], "youtubeId": k["videoId"].removeprefix("video-"), "video": k["video"],
                "time": k["time"], "url": k["sourceUrl"], "provenance": "transcript", "frames": k.get("frames", [])}
    else:
        raise HTTPException(400, "kind must be atom or knowledge")
    details = await db.get("video_details", item["videoId"]) or {}
    around = [s for s in details.get("transcript", []) if item["time"] - 30 <= s["start"] <= item["time"] + 45]
    return {**item, "embedUrl": f"https://www.youtube-nocookie.com/embed/{item['youtubeId']}?start={int(item['time'])}&rel=0", "watchUrl": youtube_link(item["youtubeId"], item["time"]), "transcriptAround": around}


# ---- dashboard ----------------------------------------------------------------------------------------

@router.get("/dashboard")
async def dashboard():
    videos, sources = await db.get_videos(), await db.list("sources")
    strategies, versions, runs, jobs_all = await db.list("lib_strategies"), await db.list("strategy_versions"), await db.list("bt_runs"), await db.list("jobs", 200)
    groups = {}
    for v in videos:
        g = status_group(v["status"])
        groups[g] = groups.get(g, 0) + 1
    tested = {v["strategyId"] for v in versions if v["status"] in ("APPROVED", "FROZEN") and v.get("readiness", {}).get("level") != "NOT_RELIABLY_BACKTESTABLE"}
    exports = await db.list("exports", 8)
    return {"channels": sum(1 for s in sources if s["kind"] == "channel"), "sources": len(sources), "videosDiscovered": len(videos), "videosAnalyzed": groups.get("completed", 0),
            "videoStates": groups, "activeTasks": groups.get("analyzing", 0) + groups.get("queued", 0) + sum(1 for j in jobs_all if j["status"] in ("RUNNING", "QUEUED")),
            "failedTasks": groups.get("failed", 0) + sum(1 for j in jobs_all if j["status"] == "FAILED"), "strategiesFound": len(strategies),
            "strategiesAwaitingReview": sum(1 for s in strategies if s["status"] in ("NEEDS_REVIEW", "IN_REVIEW") and s.get("manual")), "strategiesBacktestReady": len({s["id"] for s in strategies} & tested),
            "backtestsCompleted": sum(1 for r in runs if r["status"] == "VALID"), "backtestsInvalid": sum(1 for r in runs if r["status"] != "VALID"),
            "recentRuns": [{k: r.get(k) for k in ("id", "label", "strategyName", "strategyVersion", "symbol", "timeframe", "status", "runDate", "fidelity")} | {"netPnl": r["metrics"]["netPnl"], "trades": r["metrics"]["totalTrades"], "winRate": r["metrics"]["winRate"]} for r in runs[:6]],
            "latestJournals": [{k: e.get(k) for k in ("runId", "kind", "at", "url")} for e in exports],
            "activeJobs": [{k: j.get(k) for k in ("id", "kind", "label", "status", "progress", "message")} for j in jobs_all if j["status"] in ("RUNNING", "QUEUED")][:8]}
