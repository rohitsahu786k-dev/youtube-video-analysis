"""Per-event visual capture, validation, and atomic evidence publication."""
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pyapp.config import settings
from pyapp.db import db
from pyapp.media import media_processor, blocking_call
from pyapp.ai import semantic_events, transcribe, scan_video, analyze_frames, extract_trades, validate_trade, extract_lessons
from pyapp.intelligence import risk_reward

logger = logging.getLogger(__name__)

def now():
    return datetime.now(timezone.utc).isoformat()

def frame_times(timestamp, duration):
    start = max(0, timestamp-settings.FRAME_EVENT_BEFORE_SECONDS)
    end = min(max(0, duration-0.1), timestamp+settings.FRAME_EVENT_AFTER_SECONDS)
    count = max(1, settings.MAX_FRAMES_PER_EVENT)
    values = {round(timestamp, 3), round(start, 3), round(end, 3)}
    step = max(1, settings.FRAME_INTERVAL_SECONDS)
    values.update(round(start+i*step, 3) for i in range(int((end-start)/step)+1))
    ordered = sorted(t for t in values if 0 <= t < duration)
    if len(ordered) > count:
        indexes = {round(i*(len(ordered)-1)/(count-1)) for i in range(count)} if count > 1 else {min(range(len(ordered)), key=lambda i: abs(ordered[i]-timestamp))}
        ordered = [ordered[i] for i in sorted(indexes)]
    return ordered

def normalized_trade(extracted, validation, video_id, number, frames, duration):
    trade = dict(extracted)
    unsupported = set(validation.get("unsupportedFields", []))
    for field in unsupported:
        if field in trade and field not in ("actuallyTaken", "overallConfidence"):
            trade[field] = None
    proof = trade.get("fieldEvidence") or {}
    for field in ("entryPrice", "stopLoss", "takeProfit", "exitPrice"):
        value = trade.get(field)
        if not proof.get(field) or value is None or not math.isfinite(value) or value <= 0:
            trade[field] = None
    status = validation["status"]
    critical = {"actuallyTaken", "symbol", "direction", "entryTimestamp", "executionEvidence"}
    if critical.intersection(unsupported) or min(trade.get("overallConfidence", 0), validation.get("confidence", 0)) < settings.MIN_EXTRACTION_CONFIDENCE:
        status = "UNCERTAIN" if status != "INVALID" else status
    timestamp = trade.get("entryTimestamp")
    if (not trade.get("executionEvidence") or not trade.get("symbol") or trade.get("direction") not in ("LONG", "SHORT")
        or timestamp is None or not 0 <= timestamp < duration or not frames or trade.get("contradictions")):
        status = "UNCERTAIN" if status != "INVALID" else status
    outcome = trade.get("outcome") or "UNKNOWN"
    if not proof.get("outcome") or trade.get("exitTimestamp") is None or not 0 <= trade["exitTimestamp"] < duration:
        outcome = "UNKNOWN"
    ratio = risk_reward(trade.get("entryPrice"), trade.get("stopLoss"), trade.get("takeProfit"), trade.get("direction"))
    if all(trade.get(f) is not None for f in ("entryPrice", "stopLoss", "takeProfit")) and ratio is None:
        status = "UNCERTAIN"
    if trade.get("exitTimestamp") is not None and timestamp is not None and trade["exitTimestamp"] < timestamp:
        status, outcome = "UNCERTAIN", "UNKNOWN"
    return {**trade, "id": f"trade-{video_id}-{number}", "videoId": video_id, "number": number,
        "executionMode": trade.get("executionMode") or "UNKNOWN",
        "symbol": (trade.get("symbol") or "UNKNOWN").upper().replace("/", ""),
        "entry": trade.get("entryPrice"), "sl": trade.get("stopLoss"), "tp": trade.get("takeProfit"),
        "exit": trade.get("exitPrice"), "timestamp": timestamp, "result": outcome, "rr": ratio,
        "confidence": min(trade.get("overallConfidence", 0), validation.get("confidence", 0)),
        "validation": status, "validationReason": validation["reason"], "unsupportedFields": sorted(unsupported),
        "reason": trade.get("entryReason"), "frames": frames,
        "sourceUrl": f"https://www.youtube.com/watch?v={video_id.removeprefix('video-')}&t={int(timestamp or 0)}s"}

async def process_video_pipeline(video_id, youtube_url):
    video = await db.get_video(video_id)
    if not video:
        raise ValueError("Queued video does not exist")
    async def stage(name, progress):
        video.update(status=name, progress=progress, updatedAt=now())
        await db.save_video(video)
    try:
        video["error"] = None
        await stage("DOWNLOADING", 5)
        info = await blocking_call(media_processor.get_info, youtube_url)
        video.update({k: info.get(k) for k in ("title", "channel", "thumbnail", "description")})
        duration = info.get("duration") or 0
        if duration <= 0:
            raise ValueError("A completed video with known duration is required; livestreams are not supported.")
        video["duration"] = duration
        upload_date = info.get("uploadDate")
        video["evidenceCutoff"] = (datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)+timedelta(days=1)).isoformat() if upload_date else None
        source = await blocking_call(media_processor.download_video, youtube_url, video_id)
        if Path(source).is_file():
            video["mediaQuality"] = await blocking_call(media_processor.dimensions, source)
        await stage("TRANSCRIBING", 15)
        chunks = await blocking_call(media_processor.audio_chunks, source, video_id, duration)
        segments = []
        for chunk in chunks:
            result = await transcribe(chunk["path"], video_id)
            for segment in result["segments"]:
                start, end = segment["start"]+chunk["offset"], segment["end"]+chunk["offset"]
                if 0 <= start <= end <= min(duration, chunk["offset"]+settings.AUDIO_CHUNK_SECONDS)+0.5:
                    segments.append({"start": start, "end": min(end, duration), "text": segment["text"]})
        video["transcript"] = sorted(segments, key=lambda s: s["start"])
        await stage("DETECTING_EVENTS", 30)
        spoken = await semantic_events(segments, video_id) if segments else []
        visual = await scan_video(source, duration, video_id)
        events = sorted([e for e in spoken+visual if 0 <= e["timestamp"] < duration], key=lambda e: e["timestamp"])
        unique = []
        for event in events:
            if not any(abs(e["timestamp"]-event["timestamp"]) < 3 and e["eventType"].lower() == event["eventType"].lower() for e in unique):
                unique.append(event)
        video.update(events=len(unique), detectedEvents=unique)
        await stage("ANALYZING_VISUALS", 45)
        observations, trades = [], []
        selected = unique[:settings.MAX_CANDIDATE_EVENTS_PER_VIDEO]
        video["coverage"] = {"scanFps": 1, "detectedEvents": len(unique), "analyzedEvents": len(selected),
            "truncated": len(selected) < len(unique), "limitations": "One-frame-per-second discovery can miss brief actions. AI readings require evidence review."}
        for index, event in enumerate(selected):
            timestamp = event["timestamp"]
            frames = []
            for time in frame_times(timestamp, duration):
                path = await blocking_call(media_processor.extract_frame, source, video_id, time)
                if not Path(path).is_file() or Path(path).stat().st_size == 0:
                    raise RuntimeError(f"No frame extracted at {time} seconds")
                frames.append({"timestamp": time, "path": path,
                    "url": f"/evidence/{video_id}/frames/{Path(path).name}",
                    "sha256": await asyncio.to_thread(media_processor.sha256_hash, path)})
            pending = [t for t in trades if t.get("result") == "UNKNOWN" and t.get("timestamp") is not None][-3:]
            context_start = min([max(0, timestamp-30)] + [t["timestamp"] for t in pending])
            context = [s for s in segments if s["end"] >= context_start and s["start"] <= timestamp+90]
            summary = await analyze_frames(frames, {"event": event, "transcript": context}, video_id)
            observation = {"timestamp": timestamp, "summary": summary, "frames": frames}
            observations.append(observation)
            related_frames = {f["url"]: f for t in pending for f in sorted(t.get("frames", []), key=lambda frame: abs(frame["timestamp"]-t["timestamp"]))[:4]}
            related_frames.update({f["url"]: f for f in frames})
            extraction_evidence = {**observation, "frames": sorted(related_frames.values(), key=lambda f: f["timestamp"]), "priorTrades": [{k: v for k, v in t.items() if k != "frames"} for t in pending]}
            trade_event = any(word in event["eventType"].upper() for word in ("ENTRY", "EXIT", "EXECUT", "POSITION", "TRADE", "ORDER"))
            extracted = await extract_trades(video_id, context, extraction_evidence) if summary.get("executionVisible") or pending or trade_event else []
            for candidate in extracted:
                if candidate.get("actuallyTaken") is not True:
                    continue
                entry_time = candidate.get("entryTimestamp")
                existing = next((t for t in trades if t.get("symbol") == (candidate.get("symbol") or "UNKNOWN").upper().replace("/", "") and t.get("direction") == candidate.get("direction")
                       and entry_time is not None and t.get("timestamp") is not None and abs(t["timestamp"]-entry_time) < 5), None)
                if existing:
                    proof = {**(existing.get("fieldEvidence") or {}), **(candidate.get("fieldEvidence") or {})}
                    candidate = {**existing, **{k: v for k, v in candidate.items() if v is not None}, "fieldEvidence": proof}
                validation = await validate_trade(candidate, context, extraction_evidence, video_id)
                number = existing["number"] if existing else len(trades)+1
                trade = normalized_trade(candidate, validation, video_id, number, extraction_evidence["frames"], duration)
                if summary.get("executionVisible") is not True and not existing:
                    trade["validation"] = "UNCERTAIN" if trade["validation"] != "INVALID" else "INVALID"
                    trade["validationReason"] += " No visible filled order or actual position was identified."
                visible_symbol = (summary.get("symbol") or "").upper().split(":")[-1].replace("/", "").replace("_", "")
                extracted_symbol = trade["symbol"].split(":")[-1].replace("_", "")
                if visible_symbol and visible_symbol != extracted_symbol:
                    trade["validation"] = "UNCERTAIN" if trade["validation"] != "INVALID" else "INVALID"
                    trade["validationReason"] += " Visible chart symbol conflicts with extracted trade symbol."
                trade["evidenceCutoff"] = video["evidenceCutoff"]
                if existing:
                    trades[trades.index(existing)] = trade
                else:
                    trades.append(trade)
            await stage("ANALYZING_VISUALS", 45+int(40*(index+1)/max(1, len(selected))))
        await stage("BUILDING_KNOWLEDGE", 90)
        knowledge = []
        def add(kind, title, content, time, **extra):
            knowledge.append({"id": f"kb-{video_id}-{len(knowledge)+1}", "videoId": video_id, "video": video["title"],
                "type": kind, "title": title, "content": content, "time": time,
                "sourceUrl": f"https://www.youtube.com/watch?v={video['youtubeId']}&t={int(time)}s", "evidenceCutoff": video["evidenceCutoff"], **extra})
        for segment in segments:
            add("Spoken", "Transcript evidence", segment["text"], segment["start"])
        for observation in observations:
            add("Visual", "Chart observation (AI reading)", observation["summary"]["visualDescription"], observation["timestamp"], frames=observation["frames"])
        for start in range(0, max(1, int(duration)), 600):
            chunk_segments = [s for s in segments if start <= s["start"] < start+600]
            chunk_visual = [o for o in observations if start <= o["timestamp"] < start+600]
            if not chunk_segments and not chunk_visual:
                continue
            lessons = await extract_lessons(chunk_segments, chunk_visual, video_id)
            for lesson in lessons:
                if start <= lesson["timestamp"] < min(duration, start+600):
                    add(lesson["type"], lesson["title"], lesson["content"], lesson["timestamp"], synthesized=True,
                        strategyTag=lesson.get("strategyTag"), symbol=lesson.get("symbol"), timeframe=lesson.get("timeframe"), direction=lesson.get("direction"))
        for trade in trades:
            add("Trade", trade.get("setup") or "Executed trade candidate", str(trade.get("reason") or "Unknown entry reason"), trade.get("timestamp") or 0,
                tradeId=trade["id"], validation=trade["validation"], fieldEvidence=trade.get("fieldEvidence", {}))
        await db.replace_evidence(video_id, trades, knowledge)
        video.update(trades=len(trades), observations=observations, error=None)
        try:
            from pyapp.proposals import discover_proposals
            await stage("DRAFTING_RULES", 98)
            await discover_proposals()
        except Exception:
            logger.exception("Rule proposals unavailable; video evidence remains available")
            video["proposalWarning"] = "Rule drafting failed. Evidence was saved; review the pattern manually."
        await stage("COMPLETED", 100)
    except asyncio.CancelledError:
        video.update(status="QUEUED", updatedAt=now())
        await db.save_video(video)
        raise
    except Exception as exc:
        logger.exception("Video processing failed for %s", video_id)
        video.update(status="FAILED", error=str(exc).replace(settings.GEMINI_API_KEY or "__none__", "[redacted]"), updatedAt=now())
        await db.save_video(video)
    finally:
        await asyncio.to_thread(media_processor.cleanup, video_id)
