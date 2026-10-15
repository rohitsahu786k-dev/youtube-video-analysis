"""Per-video analysis: transcript, aligned visual evidence, trade validation, grounded strategy atoms.

Stages checkpoint into `video_details` so an interrupted video resumes instead of starting over.
"""
import asyncio
import hashlib
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pyapp.config import settings
from pyapp.db import db
from pyapp.media import media_processor, blocking_call, classify_download_error
from pyapp.ai import semantic_events, transcribe, scan_video, analyze_frames, extract_trades, validate_trade, extract_lessons
from pyapp.intelligence import risk_reward

logger = logging.getLogger(__name__)
PIPELINE_VERSION = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def frame_times(timestamp, duration, scenes=()):
    start = max(0, timestamp-settings.FRAME_EVENT_BEFORE_SECONDS)
    end = min(max(0, duration-0.1), timestamp+settings.FRAME_EVENT_AFTER_SECONDS)
    count = max(1, settings.MAX_FRAMES_PER_EVENT)
    values = {round(timestamp, 3), round(start, 3), round(end, 3)}
    step = max(1, settings.FRAME_INTERVAL_SECONDS)
    values.update(round(start+i*step, 3) for i in range(int((end-start)/step)+1))
    values.update(round(s, 3) for s in scenes if start <= s <= end)   # chart redraws / new zones
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


def segments_within(segments, duration, offset, chunk_seconds):
    out = []
    for segment in segments:
        start, end = segment["start"]+offset, segment["end"]+offset
        if 0 <= start <= end <= min(duration, offset+chunk_seconds)+0.5:
            out.append({"start": start, "end": min(end, duration), "text": segment["text"]})
    return out


async def process_video_pipeline(video_id, youtube_url):
    video = await db.get_video(video_id)
    if not video:
        raise ValueError("Queued video does not exist")
    details = await db.get("video_details", video_id) or {"id": video_id}
    mode = video.get("inputMode", "download")
    final = False

    async def stage(name, progress):
        video.update(status=name, progress=progress, updatedAt=now())
        await db.save_video(video)

    async def checkpoint():
        await db.save("video_details", details)

    try:
        video["error"] = None
        video["statusNote"] = None
        await stage("DOWNLOADING", 5)
        try:
            info = await blocking_call(media_processor.get_info, youtube_url)
        except Exception:
            if mode == "download":
                raise
            info = {}                                    # an authorized upload can proceed without public metadata
        video.update({k: info.get(k) for k in ("title", "channel", "thumbnail", "description") if info.get(k)})
        if info.get("liveStatus") in ("is_live", "is_upcoming"):
            raise ValueError("Livestreams are not supported; analyse the recording after the stream ends.")
        upload_date = info.get("uploadDate")
        if upload_date:
            video["published"] = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        video["evidenceCutoff"] = (datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)+timedelta(days=1)).isoformat() if upload_date else video.get("evidenceCutoff")
        duration = info.get("duration") or video.get("duration") or 0
        source, visual_ok = None, True
        if mode == "download":
            source = await blocking_call(media_processor.download_video, youtube_url, video_id)
        elif mode == "uploaded_media":
            source = video.get("uploadedMediaPath")
            if not source or not Path(source).is_file():
                raise ValueError("The uploaded media file is missing; upload it again.")
            visual_ok = await blocking_call(media_processor.has_video_stream, source)
            duration = duration or await blocking_call(media_processor.media_duration, source)
        else:
            visual_ok = False
            transcript = details.get("transcript") or []
            duration = duration or (math.ceil(max((s["end"] for s in transcript), default=0)))
        if duration <= 0:
            raise ValueError("A completed video with known duration is required; livestreams are not supported.")
        video["duration"] = duration
        video["inputMode"] = mode
        video["visualAnalysis"] = bool(visual_ok)
        if source and Path(source).is_file() and visual_ok:
            video["mediaQuality"] = await blocking_call(media_processor.dimensions, source)
        video["sourceFingerprint"] = hashlib.sha256(f"{video.get('title')}|{duration}|{(video.get('description') or '')[:2000]}".encode()).hexdigest()[:16]
        await db.save_video(video)

        # ---- transcript (checkpointed per audio chunk) ---------------------------------------
        await stage("TRANSCRIBING", 15)
        if mode == "uploaded_transcript":
            segments = sorted(details.get("transcript") or [], key=lambda s: s["start"])
        else:
            done = {c["offset"]: c["segments"] for c in details.get("transcriptChunks", [])}
            chunks = await blocking_call(media_processor.audio_chunks, source, video_id, duration)
            for chunk in chunks:
                if chunk["offset"] in done:
                    continue
                result = await transcribe(chunk["path"], video_id)
                done[chunk["offset"]] = segments_within(result["segments"], duration, chunk["offset"], settings.AUDIO_CHUNK_SECONDS)
                details["transcriptChunks"] = [{"offset": k, "segments": v} for k, v in sorted(done.items())]
                await checkpoint()
            segments = sorted((s for seg in done.values() for s in seg), key=lambda s: s["start"])
        details["transcript"] = segments
        details["language"] = details.get("language")
        await checkpoint()

        # ---- events -----------------------------------------------------------------------------
        await stage("DETECTING_EVENTS", 30)
        scenes = []
        if visual_ok and source and settings.SCENE_DETECTION:
            try:
                scenes = details.get("scenes") or await blocking_call(media_processor.scene_changes, source)
            except Exception:
                logger.warning("Scene detection failed for %s; fixed-interval sampling only", video_id)
        details["scenes"] = scenes
        if "events" not in details:
            if "spokenEvents" not in details:
                details["spokenEvents"] = await semantic_events(segments, video_id) if segments else []
                await checkpoint()
            spoken = details["spokenEvents"]
            visual = await scan_video(source, duration, video_id,
                completed=details.setdefault("scanChunks", {}), checkpoint=checkpoint) if visual_ok and source else []
            events = sorted([e for e in spoken+visual if 0 <= e["timestamp"] < duration], key=lambda e: e["timestamp"])
            unique = []
            for event in events:
                if not any(abs(e["timestamp"]-event["timestamp"]) < 3 and e["eventType"].lower() == event["eventType"].lower() for e in unique):
                    unique.append(event)
            details["events"] = unique
            await checkpoint()
        unique = details["events"]
        video.update(events=len(unique))

        # ---- aligned visual evidence (checkpointed per event) -----------------------------------
        await stage("ANALYZING_VISUALS", 45)
        selected = unique[:settings.MAX_CANDIDATE_EVENTS_PER_VIDEO] if visual_ok else []
        video["coverage"] = {"scanFps": 1 if visual_ok else 0, "detectedEvents": len(unique), "analyzedEvents": len(selected), "sceneChanges": len(scenes),
            "truncated": len(selected) < len(unique) and visual_ok, "visual": bool(visual_ok), "mode": mode,
            "limitations": ("One-frame-per-second discovery can miss brief actions. AI readings require evidence review." if visual_ok else
                            "No video frames were available (transcript or audio only): chart evidence was NOT analysed and no trade execution can be verified.")}
        observations, trades = list(details.get("observations", [])), list(details.get("trades", []))
        for index in range(len(observations), len(selected)):
            event = selected[index]
            timestamp = event["timestamp"]
            frames = []
            for time_point in frame_times(timestamp, duration, scenes):
                path = await blocking_call(media_processor.extract_frame, source, video_id, time_point)
                if not Path(path).is_file() or Path(path).stat().st_size == 0:
                    raise RuntimeError(f"No frame extracted at {time_point} seconds")
                frames.append({"timestamp": time_point, "path": path, "url": f"/evidence/{video_id}/frames/{Path(path).name}",
                    "sha256": await asyncio.to_thread(media_processor.sha256_hash, path)})
            pending = [t for t in trades if t.get("result") == "UNKNOWN" and t.get("timestamp") is not None][-3:]
            context_start = min([max(0, timestamp-30)] + [t["timestamp"] for t in pending])
            context = [s for s in segments if s["end"] >= context_start and s["start"] <= timestamp+90]
            summary = await analyze_frames(frames, {"event": event, "transcript": context}, video_id)
            observation = {"timestamp": timestamp, "summary": summary, "frames": frames, "transcript": [s for s in segments if timestamp-8 <= s["start"] <= timestamp+20][:12]}
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
            details["observations"], details["trades"] = observations, trades
            await checkpoint()
            await stage("ANALYZING_VISUALS", 45+int(35*(index+1)/max(1, len(selected))))
        details["observations"], details["trades"] = observations, trades

        # ---- knowledge chunks ------------------------------------------------------------------
        await stage("BUILDING_KNOWLEDGE", 82)
        knowledge = []
        def add(kind, title, content, time_point, **extra):
            knowledge.append({"id": f"kb-{video_id}-{len(knowledge)+1}", "videoId": video_id, "video": video["title"],
                "type": kind, "title": title, "content": content, "time": time_point,
                "sourceUrl": f"https://www.youtube.com/watch?v={video['youtubeId']}&t={int(time_point)}s", "evidenceCutoff": video["evidenceCutoff"], **extra})
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
        video.update(trades=len(trades), error=None)

        # ---- grounded strategy atoms -------------------------------------------------------------
        await stage("EXTRACTING_STRATEGIES", 90)
        video.pop("strategyWarning", None)
        try:
            from pyapp.library import build_atoms
            atoms = await build_atoms({**video}, segments, observations, duration)
            await db.replace_for_video("atoms", video_id, atoms)
            video["atoms"] = len(atoms)
            video["atomsVerified"] = sum(1 for a in atoms if a["provenance"] == "source_confirmed")
        except Exception:
            logger.exception("Strategy extraction failed for %s; evidence remains available", video_id)
            video["strategyWarning"] = "Strategy extraction failed. Evidence was saved; use 'Re-extract strategies' on this video."
        try:
            from pyapp.proposals import discover_proposals
            await stage("DRAFTING_RULES", 97)
            await discover_proposals()
        except Exception:
            logger.exception("Rule proposals unavailable; video evidence remains available")
            video["proposalWarning"] = "Rule drafting failed. Evidence was saved; review the pattern manually."
        video.update(pipelineVersion=PIPELINE_VERSION, processedAt=now(), attempts=0)
        await stage("COMPLETED", 100)
        final = True
        try:
            from pyapp import jobs, retrieval
            await jobs.enqueue("rebuild_library", {}, "Rebuild strategy library")      # de-duplicates against an already queued rebuild
            await retrieval.warm()                                                    # embed the new evidence now so the first chat question is fast
        except Exception:
            logger.exception("Could not queue the library rebuild")
    except asyncio.CancelledError:
        video.update(status="QUEUED", updatedAt=now())
        await db.save_video(video)
        raise
    except Exception as exc:
        logger.exception("Video processing failed for %s", video_id)
        kind, message = classify_download_error(exc)
        message = message.replace(settings.GEMINI_API_KEY or "__none__", "[redacted]")
        attempts = video.get("attempts", 0)
        if kind in ("AWAITING_PERMISSION", "UNAVAILABLE"):
            video.update(status=kind, statusNote=message, error=None, updatedAt=now())
            final = True
        elif kind == "TRANSIENT" and attempts + 1 < settings.MAX_VIDEO_ATTEMPTS:
            video.update(status="QUEUED", attempts=attempts + 1, notBefore=time.time() + 60 * 2 ** attempts, statusNote=f"Retrying after a temporary error: {message}", error=None, updatedAt=now())
        else:
            video.update(status="FAILED", error=message, attempts=attempts + 1, updatedAt=now())
            final = True
        await db.save_video(video)
    finally:
        if final and mode == "download":
            await asyncio.to_thread(media_processor.cleanup, video_id)
