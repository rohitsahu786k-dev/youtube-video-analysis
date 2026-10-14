"""Evidence-derived statistics and retrieval; no invented wins or risk ratios."""
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from statistics import mean

def evidence_hash(trades):
    keys = ("id", "videoId", "symbol", "direction", "setup", "timeframe", "executionMode", "entry", "sl", "tp", "exit", "timestamp", "result", "validation", "fieldEvidence", "evidenceCutoff", "content", "title", "type", "time", "strategyTag")
    records = [{k: t.get(k) for k in keys} for t in sorted(trades, key=lambda t: t["id"])]
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def risk_reward(entry, stop, target, direction):
    if any(v is None or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (entry, stop, target)):
        return None
    if direction == "LONG" and stop < entry < target:
        return round((target - entry) / (entry - stop), 4)
    if direction == "SHORT" and target < entry < stop:
        return round((entry - target) / (stop - entry), 4)
    return None


def build_patterns(trades, knowledge=None):
    groups = defaultdict(list)
    valid = [t for t in trades if t.get("validation") == "VALID" and t.get("setup")]
    for trade in valid:
        key = (re.sub(r"\s+", " ", trade["setup"].strip().lower()), trade.get("symbol"), trade.get("timeframe"), trade.get("direction"), trade.get("executionMode", "UNKNOWN"))
        groups[key].append(trade)
    patterns = []
    for key, members in groups.items():
        completed = [t for t in members if t.get("result") in ("WIN", "LOSS")]
        ratios = [t["rr"] for t in members if t.get("rr") is not None]
        wins = sum(t["result"] == "WIN" for t in completed)
        patterns.append({
            "id": hashlib.sha256(repr(key).encode()).hexdigest()[:16],
            "name": members[0]["setup"], "symbol": key[1], "timeframe": key[2], "direction": key[3],
            "description": members[0].get("reason") or "Review linked evidence before operationalizing this setup.",
            "occurrences": len(members), "videos": len({t["videoId"] for t in members}),
            "wins": wins, "completed": len(completed), "winRate": round(100*wins/len(completed), 1) if completed else None,
            "avgR": round(mean(ratios), 2) if ratios else None,
            "frequency": round(100*len(members)/len(valid), 1),
            "confidence": mean(t.get("confidence", 0) for t in members),
            "status": "RECURRING" if len({t["videoId"] for t in members}) >= 2 else "CANDIDATE",
            "tradeIds": [t["id"] for t in members], "example": members[0]["id"],
            "kind": "EXECUTION", "knowledgeIds": [], "executionMode": key[4],
        })
    lessons = defaultdict(list)
    for chunk in knowledge or []:
        if chunk.get("synthesized") and chunk.get("strategyTag") and chunk.get("type") != "Inference":
            key = (chunk["strategyTag"].lower().strip(), chunk.get("symbol"), chunk.get("timeframe"), chunk.get("direction"))
            lessons[key].append(chunk)
    for key, members in lessons.items():
        videos = len({m["videoId"] for m in members})
        patterns.append({"id": hashlib.sha256(("lesson:"+repr(key)).encode()).hexdigest()[:16],
            "name": key[0], "symbol": key[1], "timeframe": key[2], "direction": key[3], "description": members[0]["content"],
            "occurrences": len(members), "videos": videos, "wins": 0, "completed": 0, "winRate": None, "avgR": None,
            "frequency": 0, "confidence": 0, "status": "RECURRING" if videos >= 2 else "CANDIDATE",
            "tradeIds": [], "knowledgeIds": [m["id"] for m in members], "example": members[0]["id"], "kind": "TEACHING"})
    return sorted(patterns, key=lambda p: p["occurrences"], reverse=True)


def retrieve(question, knowledge, limit=8):
    tokens = set(re.findall(r"\w+", question.lower()))
    scored = []
    for chunk in knowledge:
        if chunk.get("type") == "Trade" and chunk.get("validation") != "VALID":
            continue
        words = Counter(re.findall(r"\w+", (chunk.get("title", "") + " " + chunk.get("content", "")).lower()))
        score = sum(min(words[token], 3) for token in tokens if len(token) > 2)
        if score:
            scored.append((score, chunk))
    return [chunk for _, chunk in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]]


def aggregate_stats(videos, trades):
    completed = [t for t in trades if t.get("validation") == "VALID" and t.get("result") in ("WIN", "LOSS")]
    analyzed = [v for v in videos if v.get("status") == "COMPLETED"]
    return {"totalVideos": len(videos), "analyzedVideos": len(analyzed), "totalTrades": len(trades),
            "winRate": round(100*sum(t["result"] == "WIN" for t in completed)/len(completed), 1) if completed else None,
            "completedTrades": len(completed), "hoursAnalyzed": round(sum(v.get("duration") or 0 for v in analyzed)/3600, 1),
            "activePipelines": sum(v.get("status") not in ("COMPLETED", "FAILED") for v in videos)}
