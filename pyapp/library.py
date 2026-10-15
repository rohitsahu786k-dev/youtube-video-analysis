"""Strategy intelligence: grounded rule atoms -> clustered strategies -> evidence-cited manuals, conflicts, examples and ranking.

No rule is created here from model knowledge: every manual item must cite atoms whose quotes were verified against the transcript.
"""
import asyncio
import hashlib
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone

from pyapp import ai_strategy
from pyapp.config import settings
from pyapp.db import db
from pyapp.grounding import frames_near, ground_quote, norm, provenance_of, youtube_link

logger = logging.getLogger(__name__)

RULE_SECTIONS = ("chart_preparation", "htf_analysis", "execution_steps", "entry_rules", "stop_loss_rules", "take_profit_rules", "trade_management")
REQUIRED_SECTIONS = {"entry_rules": "Entry rules", "stop_loss_rules": "Stop-loss rules", "take_profit_rules": "Take-profit rules", "execution_steps": "Execution sequence"}
OPTIONAL_SECTIONS = {"instruments_timeframes_sessions": "Instruments / timeframes", "risk_management": "Risk and money management", "sessions_time": "Sessions and time restrictions",
                     "avoidance": "Trade-avoidance rules", "htf_analysis": "Higher-timeframe analysis", "trade_management": "Trade management"}
SECTION_TITLES = {"objective": "Strategy objective and rationale", "instruments_timeframes_sessions": "Instruments, timeframes and sessions", "chart_setup_indicators": "Chart setup and indicators",
                  "chart_preparation": "Chart preparation", "htf_analysis": "Higher-timeframe analysis", "execution_steps": "Step-by-step execution sequence", "entry_rules": "Entry rules",
                  "stop_loss_rules": "Stop-loss rules", "take_profit_rules": "Take-profit and risk-to-reward", "trade_management": "Trade management", "risk_management": "Risk and money management",
                  "sessions_time": "Trading sessions and time restrictions", "avoidance": "Trade-avoidance checklist", "supporting_knowledge": "Supporting educational knowledge"}


def now():
    return datetime.now(timezone.utc).isoformat()


def strategy_key(name):
    return hashlib.sha256(norm(name).encode()).hexdigest()[:10]


# ---- atoms from a video -----------------------------------------------------------------------------

async def build_atoms(video, segments, observations, duration, extractor=None):
    """Extract rule atoms for a whole video, ground every quote against the transcript and align with captured frames."""
    extractor = extractor or ai_strategy.extract_atoms
    atoms, number = [], 0
    window = 600
    for start in range(0, max(1, int(duration)), window):
        chunk = [s for s in segments if start - 30 <= s["start"] < start + window]
        seen = [o for o in observations if start <= o["timestamp"] < start + window]
        if not chunk and not seen:
            continue
        for raw in await extractor(chunk, seen, video, [start, min(duration, start + window)], video["id"]):
            claimed = max(0.0, min(raw["startTime"], duration))
            grounding = ground_quote(raw["quote"], segments, claimed)
            number += 1
            begin = grounding["start"] if grounding["verified"] else claimed
            end = grounding["end"] if grounding["verified"] else min(duration, max(raw["endTime"], claimed))
            atom = {"id": f"atom-{video['id']}-{number}", "videoId": video["id"], "youtubeId": video.get("youtubeId"), "videoTitle": video.get("title"), "channel": video.get("channel"),
                    "published": video.get("published"), "category": raw["category"], "strategyName": (raw.get("strategyName") or "").strip() or None, "text": raw["text"].strip(),
                    "quote": raw["quote"].strip(), "startTime": float(begin), "endTime": float(max(end, begin)), "evidenceType": raw["evidenceType"], "visualNote": raw.get("visualNote"),
                    "timeframe": raw.get("timeframe"), "instrument": raw.get("instrument"), "direction": raw.get("direction"), "objective": bool(raw.get("objective")),
                    "hypothetical": bool(raw.get("hypothetical")), "confidence": float(raw["confidence"]), "quoteVerified": grounding["verified"], "quoteMethod": grounding["method"],
                    "quoteRatio": grounding["ratio"], "timestampCorrected": bool(grounding["verified"] and abs(begin - claimed) > 15),
                    "frames": frames_near(observations, begin, max(end, begin)), "evidenceCutoff": video.get("evidenceCutoff")}
            atom["provenance"] = provenance_of(atom)
            atom["sourceUrl"] = youtube_link(video.get("youtubeId") or video["id"].removeprefix("video-"), begin)
            atoms.append(atom)
    return atoms


# ---- consolidation -----------------------------------------------------------------------------------

def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / len(a | b) if a | b else 0.0


def evidence_hash(atoms):
    digest = hashlib.sha256()
    for a in sorted(atoms, key=lambda a: a["id"]):
        digest.update(f"{a['id']}|{a['text']}|{a['provenance']}".encode())
    return digest.hexdigest()[:20]


async def make_clusters(atoms, use_ai=True):
    """Return [{canonical, members:[names], relation, variationOf}] covering every named strategy."""
    names = defaultdict(list)
    for a in atoms:
        if a["strategyName"]:
            names[norm(a["strategyName"])].append(a)
    if not names:
        return []
    display = {key: max((a["strategyName"] for a in items), key=lambda n: sum(1 for x in items if x["strategyName"] == n)) for key, items in names.items()}
    if not use_ai or len(names) == 1 or settings.MOCK_AI_MODE or not settings.GEMINI_API_KEY:
        return [{"canonical": display[k], "members": [display[k]], "memberKeys": [k], "relation": "distinct", "variationOf": None} for k in names]
    samples = [{"name": display[k], "videos": len({a["videoId"] for a in items}), "sample": " | ".join(a["text"] for a in items[:3])[:400]} for k, items in names.items()]
    clusters = []
    try:
        for start in range(0, len(samples), 80):
            clusters.extend(await ai_strategy.cluster_strategy_names(samples[start:start + 80]))
    except Exception:
        logger.exception("Strategy clustering failed; falling back to exact names")
        return [{"canonical": display[k], "members": [display[k]], "memberKeys": [k], "relation": "distinct", "variationOf": None} for k in names]
    by_display = {display[k]: k for k in names}
    placed, out = set(), []
    for c in clusters:
        members = [m for m in c["memberNames"] if m in by_display and by_display[m] not in placed]
        if not members:
            continue
        canonical = c["canonicalName"] if c["canonicalName"] in members else members[0]
        keys = [by_display[m] for m in members]
        placed.update(keys)
        out.append({"canonical": canonical, "members": members, "memberKeys": keys, "relation": c["relation"], "variationOf": c.get("variationOf")})
    for k in names:
        if k not in placed:
            out.append({"canonical": display[k], "members": [display[k]], "memberKeys": [k], "relation": "distinct", "variationOf": None})
    return out


def sanitize_manual(manual, valid, general_ids):
    """Drop uncited/invalid items, never keep general guidance inside strategy rule sections, and report what is missing."""
    dropped, cleaned = 0, {}
    for section in SECTION_TITLES:
        items = manual.get(section) or []
        kept = []
        for item in items:
            ids = [i for i in item.get("atomIds", []) if i in valid]
            text = (item.get("text") if section != "execution_steps" else item.get("instruction")) or ""
            if not ids or not text.strip():
                dropped += 1
                continue
            row = {"text": text.strip(), "atomIds": ids, "note": item.get("note")}
            if section == "execution_steps":
                row.update(step=item.get("step"), title=item.get("title") or "")
            only_general = all(i in general_ids for i in ids)
            if only_general and section != "supporting_knowledge":
                row["note"] = ((row["note"] or "") + " General guidance from the same videos; the teacher did not tie it to this strategy.").strip()
                cleaned.setdefault("supporting_knowledge", []).append({k: v for k, v in row.items() if k not in ("step", "title")})
                continue
            kept.append(row)
        if section == "execution_steps":
            kept.sort(key=lambda r: (r.get("step") is None, r.get("step") or 0))
            for n, row in enumerate(kept, start=1):
                row["step"] = n
        cleaned.setdefault(section, [])
        cleaned[section] = kept + cleaned[section] if section == "supporting_knowledge" else kept
    cleaned["supporting_knowledge"] = cleaned.get("supporting_knowledge", [])
    missing = [f"{title}: not stated in the analysed sources." for section, title in REQUIRED_SECTIONS.items() if not cleaned.get(section)]
    missing += [f"{title}: not stated in the analysed sources (nothing is assumed)." for section, title in OPTIONAL_SECTIONS.items() if not cleaned.get(section)]
    return cleaned, missing, dropped


def completeness(atoms):
    """Deterministic completeness / objectivity estimate used for ranking before any rule translation."""
    confirmed = [a for a in atoms if a["provenance"] == "source_confirmed"]
    have = {c: [a for a in confirmed if a["category"] == c] for c in ("entry", "confirmation", "setup_detection", "stop_loss", "take_profit", "trade_management", "risk_management", "session_time", "market_context", "bias")}
    parts = {"entry": bool(have["entry"] or have["confirmation"]), "setup": bool(have["setup_detection"] or have["confirmation"]), "stop": bool(have["stop_loss"]), "target": bool(have["take_profit"]),
             "management": bool(have["trade_management"]), "risk": bool(have["risk_management"]), "session": bool(have["session_time"]), "context": bool(have["market_context"] or have["bias"])}
    weights = {"entry": 25, "setup": 10, "stop": 20, "target": 20, "management": 8, "risk": 7, "session": 5, "context": 5}
    score = sum(weights[k] for k, v in parts.items() if v)
    rule_atoms = [a for c in ("entry", "confirmation", "setup_detection", "stop_loss", "take_profit") for a in have[c]]
    objective = sum(1 for a in rule_atoms if a["objective"]) / len(rule_atoms) if rule_atoms else 0.0
    missing = [k for k in ("entry", "stop", "target") if not parts[k]]
    return {"score": score, "components": parts, "objectiveShare": round(objective, 3), "missingCore": missing}


def rank_score(comp, atoms, examples, starred):
    confirmed = [a for a in atoms if a["provenance"] == "source_confirmed"]
    with_frames = sum(1 for a in confirmed if a.get("frames"))
    quality = (len(confirmed) / len(atoms) * 0.6 + (with_frames / len(confirmed) if confirmed else 0) * 0.4) if atoms else 0
    backtestable = 1.0 if not comp["missingCore"] and comp["objectiveShare"] >= 0.6 else 0.5 if not comp["missingCore"] else 0.0
    return round(0.30 * comp["score"] + 25 * comp["objectiveShare"] + 20 * quality + 15 * min(len(examples), 5) / 5 + 10 * backtestable + (10 if starred else 0), 2)


async def rebuild_library(progress=None, writer=None, conflicts_fn=None, clusters_fn=None):
    """Re-derive the whole strategy library from the current atoms. User state (stars, notes, approvals) is preserved."""
    writer = writer or ai_strategy.write_manual
    conflicts_fn = conflicts_fn or ai_strategy.find_conflicts
    atoms = await db.list("atoms")
    trades = await db.get_trades()
    existing = {s["id"]: s for s in await db.list("lib_strategies")}
    ai_on = not settings.MOCK_AI_MODE and bool(settings.GEMINI_API_KEY)
    clusters = await (clusters_fn(atoms) if clusters_fn else make_clusters(atoms, ai_on))
    used, results, general = set(), [], [a for a in atoms if not a["strategyName"]]
    for index, cluster in enumerate(clusters):
        if progress:
            await progress(100 * index / max(1, len(clusters)), f"Building manual {index + 1}/{len(clusters)}: {cluster['canonical']}")
        keys = set(cluster["memberKeys"])
        members = [a for a in atoms if a["strategyName"] and norm(a["strategyName"]) in keys]
        confirmed = [a for a in members if a["provenance"] == "source_confirmed"]
        videos = {a["videoId"]: a for a in members}
        general_ids = {a["id"] for a in general if a["videoId"] in videos and a["provenance"] == "source_confirmed"}
        context_atoms = confirmed + [a for a in general if a["id"] in general_ids]
        digest = evidence_hash(context_atoms)
        best_id, best = None, 0.0
        for sid, old in existing.items():
            overlap = jaccard(old.get("atomIds", []), [a["id"] for a in members])
            if sid not in used and overlap > best:
                best_id, best = sid, overlap
        sid = best_id if best >= 0.3 else "strat-" + strategy_key(cluster["canonical"])
        while sid in used:
            sid += "x"
        used.add(sid)
        old = existing.get(sid, {})
        manual = old.get("manual") if old.get("evidenceHash") == digest and old.get("manual") else None
        conflicts = old.get("conflicts") if manual else None
        missing, dropped = (old.get("manualMissing", []), old.get("manualDropped", 0)) if manual else ([], 0)
        if manual is None:
            if confirmed and ai_on:
                try:
                    raw = await writer(cluster["canonical"], [{**a, "general": a["id"] in general_ids} for a in context_atoms])
                    raw = raw if isinstance(raw, dict) else raw.model_dump()
                    manual, missing, dropped = sanitize_manual(raw, {a["id"] for a in context_atoms}, general_ids)
                except Exception:
                    logger.exception("Manual generation failed for %s", cluster["canonical"])
                    manual, missing, dropped = None, ["Manual generation failed; retry the library rebuild."], 0
            elif confirmed:
                manual, missing, dropped = None, ["AI is not configured; manual could not be generated."], 0
            else:
                manual, missing, dropped = None, ["No source-confirmed evidence yet: every extracted rule failed quote verification."], 0
            decision = [a for a in confirmed if a["category"] in ("entry", "confirmation", "setup_detection", "stop_loss", "take_profit", "trade_management", "risk_management", "session_time", "bias", "market_context")]
            conflicts = []
            if ai_on and len(decision) >= 3:
                try:
                    valid = {a["id"]: a for a in decision}
                    for c in await conflicts_fn(cluster["canonical"], decision):
                        c = c if isinstance(c, dict) else c.model_dump()
                        ids = [i for i in c["atomIds"] if i in valid]
                        if len(set(ids)) >= 2:
                            conflicts.append({"topic": c["topic"], "description": c["description"], "category": c["category"], "atomIds": ids,
                                              "statements": [{"atomId": i, "text": valid[i]["text"], "video": valid[i]["videoTitle"], "published": valid[i].get("published"), "url": valid[i]["sourceUrl"]} for i in ids]})
                except Exception:
                    logger.exception("Conflict detection failed for %s", cluster["canonical"])
        names = {norm(n) for n in cluster["members"]} | {norm(cluster["canonical"])}
        examples = [{"kind": "trade", "tradeId": t["id"], "videoId": t["videoId"], "symbol": t.get("symbol"), "direction": t.get("direction"), "validation": t.get("validation"),
                     "executionMode": t.get("executionMode"), "outcome": t.get("result"), "timestamp": t.get("timestamp"), "sourceUrl": t.get("sourceUrl")}
                    for t in trades if t.get("setup") and norm(t["setup"]) in names]
        examples += [{"kind": "atom", "atomId": a["id"], "videoId": a["videoId"], "text": a["text"], "sourceUrl": a["sourceUrl"], "provenance": a["provenance"]} for a in members if a["category"] == "example_trade"]
        comp = completeness(members)
        doc = {"id": sid, "name": cluster["canonical"], "aliases": [m for m in cluster["members"] if m != cluster["canonical"]], "relation": cluster["relation"], "variationOf": cluster.get("variationOf"),
               "atomIds": [a["id"] for a in members], "confirmedCount": len(confirmed), "unverifiedCount": len(members) - len(confirmed), "generalAtomIds": sorted(general_ids),
               "sources": [{"videoId": vid, "title": a["videoTitle"], "youtubeId": a["youtubeId"], "published": a.get("published"), "atoms": sum(1 for m in members if m["videoId"] == vid),
                            "firstTime": min(m["startTime"] for m in members if m["videoId"] == vid), "url": min((m for m in members if m["videoId"] == vid), key=lambda m: m["startTime"])["sourceUrl"]} for vid, a in videos.items()],
               "exampleCount": len(examples), "examples": examples, "manual": manual, "manualMissing": missing, "manualDropped": dropped, "conflicts": conflicts or [], "completeness": comp,
               "evidenceHash": digest, "status": old.get("status", "NEEDS_REVIEW"), "starred": old.get("starred", False), "notes": old.get("notes", ""), "createdAt": old.get("createdAt", now()), "updatedAt": now(),
               "manualGeneratedAt": old.get("manualGeneratedAt") if manual and old.get("evidenceHash") == digest else now(),
               "extractedOriginal": old.get("extractedOriginal") or (manual and {"manual": manual, "capturedAt": now(), "note": "Original source-derived manual; edits are made in versioned test configurations and never overwrite this."})}
        doc["rankScore"] = rank_score(comp, members, examples, doc["starred"])
        results.append(doc)
    versions = await db.list("strategy_versions")
    for sid, old in existing.items():
        if sid in used:
            continue
        if old.get("starred") or any(v["strategyId"] == sid for v in versions):
            results.append({**old, "orphaned": True, "updatedAt": now()})   # keep: approved versions reference it
        else:
            await db.delete("lib_strategies", sid)
    await db.save_many("lib_strategies", results)
    if progress:
        await progress(100, f"Library rebuilt: {len(results)} strategies")
    return {"strategies": len(results), "atoms": len(atoms), "confirmedAtoms": sum(1 for a in atoms if a["provenance"] == "source_confirmed")}
