"""Grounding, strategy library, versions, readiness, Q&A validation, sources and coverage (SRS 4-9, 2.2, 7, 8)."""
import asyncio

import pytest

from pyapp import library, qa, retrieval, versions
from pyapp.config import settings
from pyapp.db import db
from pyapp.grounding import frames_near, ground_quote, norm, provenance_of
from pyapp.sources import coverage, expand_ingestion, status_group
from pyapp.youtube import parse_source

SEGMENTS = [{"start": 10.0, "end": 14.0, "text": "Jab liquidity sweep ho jaye tab market structure shift ka wait karo"},
            {"start": 14.0, "end": 19.0, "text": "Entry us zone se lenge jo humne mark kiya hai"},
            {"start": 40.0, "end": 46.0, "text": "Stop loss swing low ke neeche rakhna hai"},
            {"start": 60.0, "end": 66.0, "text": "Target minimum two R rakhna hai, usse kam nahi"}]


def run(coro):
    return asyncio.run(coro)


# ---- grounding -------------------------------------------------------------------------------------

def test_verbatim_quote_is_verified_and_wrong_timestamp_is_corrected():
    hit = ground_quote("Stop loss swing low ke neeche rakhna hai", SEGMENTS, claimed_start=300.0)
    assert hit["verified"] and hit["method"] == "exact" and hit["start"] == 40.0       # the real position replaces the model's claimed time


def test_invented_or_translated_quote_is_not_verified():
    assert not ground_quote("Place the stop loss below the swing low", SEGMENTS)["verified"]      # an English paraphrase of Hinglish speech
    assert not ground_quote("Always risk exactly one percent of the account", SEGMENTS)["verified"]


def test_fuzzy_match_tolerates_small_transcription_differences():
    hit = ground_quote("entry us zone se lenge jo humne mark kiya", SEGMENTS)
    assert hit["verified"] and hit["start"] == 14.0
    near = ground_quote("Entry us zone se lenge jo humne maark kiya hai", SEGMENTS)
    assert near["verified"] and near["method"] == "fuzzy"


def test_normalisation_handles_unicode_and_punctuation():
    assert norm("  Stop-Loss, at: SWING!  ") == "stop loss at swing"
    assert ground_quote("स्टॉप लॉस स्विंग लो के नीचे", [{"start": 1, "end": 4, "text": "स्टॉप लॉस, स्विंग लो के नीचे।"}])["verified"]


def test_provenance_requires_verified_quote_and_frames_for_visual_only_claims():
    base = {"quoteVerified": True, "evidenceType": "spoken", "confidence": 0.9, "frames": []}
    assert provenance_of(base) == "source_confirmed"
    assert provenance_of({**base, "quoteVerified": False}) == "missing_unclear"
    assert provenance_of({**base, "evidenceType": "visual"}) == "missing_unclear"
    assert provenance_of({**base, "evidenceType": "visual", "frames": [{"timestamp": 1}]}) == "source_confirmed"
    assert provenance_of({**base, "confidence": 0.3}) == "missing_unclear"


def test_frames_are_aligned_to_the_time_of_the_statement():
    observations = [{"timestamp": 12, "frames": [{"timestamp": 12, "url": "/e/a.webp"}, {"timestamp": 200, "url": "/e/z.webp"}]}, {"timestamp": 300, "frames": [{"timestamp": 300, "url": "/e/far.webp"}]}]
    picked = frames_near(observations, 10, 18)
    assert [f["url"] for f in picked] == ["/e/a.webp"]


# ---- atoms, library and versions -----------------------------------------------------------------------

VIDEO = {"id": "video-aaaaaaaaaaa", "youtubeId": "aaaaaaaaaaa", "title": "Sweep entry", "channel": "Teacher", "published": "2026-01-02", "evidenceCutoff": "2026-01-03T00:00:00+00:00"}


async def fake_extractor(segments, observations, video, window, video_id):
    return [
        {"category": "setup_detection", "strategyName": "Liquidity Sweep Entry", "text": "Wait for a liquidity sweep then a market structure shift", "quote": "Jab liquidity sweep ho jaye tab market structure shift ka wait karo",
         "startTime": 500.0, "endTime": 505.0, "evidenceType": "spoken", "objective": True, "confidence": 0.9},
        {"category": "stop_loss", "strategyName": "Liquidity Sweep Entry", "text": "Stop below the swing low", "quote": "Stop loss swing low ke neeche rakhna hai",
         "startTime": 40.0, "endTime": 46.0, "evidenceType": "spoken", "objective": True, "confidence": 0.9},
        {"category": "take_profit", "strategyName": "Liquidity Sweep Entry", "text": "Always target 3R", "quote": "Target hamesha teen R rakhna hai",
         "startTime": 60.0, "endTime": 66.0, "evidenceType": "spoken", "objective": True, "confidence": 0.9},          # invented number: quote is not in the transcript
        {"category": "psychology", "strategyName": None, "text": "Be patient", "quote": "Entry us zone se lenge jo humne mark kiya hai", "startTime": 14.0, "endTime": 19.0, "evidenceType": "spoken", "confidence": 0.8},
    ]


def test_atoms_are_grounded_and_invented_rules_are_not_source_confirmed():
    atoms = run(library.build_atoms(VIDEO, SEGMENTS, [], 120, fake_extractor))
    by_cat = {a["category"]: a for a in atoms}
    assert by_cat["setup_detection"]["provenance"] == "source_confirmed" and by_cat["setup_detection"]["startTime"] == 10.0
    assert by_cat["setup_detection"]["timestampCorrected"]
    assert by_cat["stop_loss"]["sourceUrl"].endswith("&t=40s")
    assert by_cat["take_profit"]["provenance"] == "missing_unclear" and not by_cat["take_profit"]["quoteVerified"]


async def fake_writer(name, atoms):
    stop = next(a for a in atoms if a["category"] == "stop_loss")
    setup = next(a for a in atoms if a["category"] == "setup_detection")
    return {"entry_rules": [{"text": "Wait for the sweep and structure shift", "atomIds": [setup["id"]]}], "stop_loss_rules": [{"text": "Stop below swing low", "atomIds": [stop["id"]]},
            {"text": "Invented 2 pip buffer", "atomIds": ["atom-does-not-exist"]}], "take_profit_rules": [{"text": "Target 3R", "atomIds": []}],
            "execution_steps": [{"step": 2, "title": "Stop", "instruction": "Place the stop", "atomIds": [stop["id"]]}, {"step": 1, "title": "Setup", "instruction": "Wait for the sweep", "atomIds": [setup["id"]]}],
            "supporting_knowledge": [], "risk_management": [{"text": "General patience", "atomIds": [a["id"] for a in atoms if a["category"] == "psychology"]}]}


async def no_conflicts(name, atoms):
    return []


def test_library_manual_only_contains_cited_source_confirmed_rules():
    atoms = run(library.build_atoms(VIDEO, SEGMENTS, [], 120, fake_extractor))
    async def go():
        await db.save_video(VIDEO)
        await db.replace_for_video("atoms", VIDEO["id"], atoms)
        result = await library.rebuild_library(writer=fake_writer, conflicts_fn=no_conflicts, clusters_fn=lambda a: library.make_clusters(a, False))
        return result, await db.list("lib_strategies")
    settings.GEMINI_API_KEY, settings.MOCK_AI_MODE = "test-key", False
    try:
        result, strategies = run(go())
    finally:
        settings.GEMINI_API_KEY, settings.MOCK_AI_MODE = None, True
    assert result["strategies"] == 1
    s = strategies[0]
    manual = s["manual"]
    assert [i["text"] for i in manual["entry_rules"]] == ["Wait for the sweep and structure shift"]
    assert [i["text"] for i in manual["stop_loss_rules"]] == ["Stop below swing low"]            # the item citing a non-existent atom was dropped
    assert manual["take_profit_rules"] == []                                                      # the uncited item was dropped
    assert [x["step"] for x in manual["execution_steps"]] == [1, 2] and manual["execution_steps"][0]["text"] == "Wait for the sweep"
    assert any("Take-profit rules: not stated" in m for m in s["manualMissing"])                  # absent rule is reported, never invented
    assert s["manualDropped"] == 2 and s["unverifiedCount"] == 1 and s["name"] == "Liquidity Sweep Entry"
    assert manual["risk_management"] == []                                                        # general guidance is never filed as this strategy's own rule
    assert any("General guidance" in (i.get("note") or "") for i in manual["supporting_knowledge"])
    assert s["extractedOriginal"]["manual"] == manual


def test_readiness_levels_follow_provenance():
    full = {c: {"status": "source_confirmed", "atomIds": ["a"]} for c in ("timeframe", "entry", "stop", "targets")}
    assert versions.classify_readiness(full, [], [])["level"] == "FULLY_BACKTESTABLE"
    assumed = {**full, "stop": {"status": "proposed_assumption", "atomIds": []}}
    result = versions.classify_readiness(assumed, [{"component": "stop", "text": "x"}], [])
    assert result["level"] == "PARTIALLY_BACKTESTABLE" and result["fidelity"] == "CONTAINS_APPROVED_ASSUMPTIONS"
    assert versions.classify_readiness({**full, "entry": {"status": "missing"}}, [], [])["level"] == "NOT_RELIABLY_BACKTESTABLE"
    assert versions.classify_readiness(full, [], ["order block imbalance"])["level"] == "PARTIALLY_BACKTESTABLE"


def test_translation_provenance_downgrades_unverifiable_claims():
    components, assumptions, unsupported = versions.provenance_from_translation(
        {"components": {"entry": {"status": "source_confirmed", "atomIds": ["a1"]}, "stop": {"status": "source_confirmed", "atomIds": ["ghost"]}}, "assumptions": [], "unsupported": ["FVG imbalance"]}, {"a1"})
    assert components["entry"]["status"] == "source_confirmed"
    assert components["stop"]["status"] == "proposed_assumption" and assumptions and "downgraded" in assumptions[0]["reason"].lower()
    assert components["targets"]["status"] == "missing" and unsupported == ["FVG imbalance"]


def test_version_lifecycle_freezes_and_never_edits_the_original():
    spec = {"name": "t", "timeframe": "M15", "entry": {"long": {"left": "close", "op": ">", "right": 1}}, "stop": {"type": "points", "value": 0.001}}
    async def go():
        v = await versions.create_version("custom-x", spec, author="me")
        with pytest.raises(Exception):
            await versions.freeze(v["id"], "me")                       # must be approved first
        approved = await versions.approve(v["id"], "me", [])
        frozen = await versions.freeze(v["id"], "me")
        with pytest.raises(Exception):
            await versions.update_draft(v["id"], {**spec, "name": "changed"}, author="me", note="x")
        forked = await versions.fork(v["id"], "me")
        return approved, frozen, forked
    approved, frozen, forked = run(go())
    assert approved["approvedBy"] == "me" and frozen["status"] == "FROZEN"
    assert forked["status"] == "DRAFT" and forked["parentVersionId"] == frozen["id"] and forked["number"] == 2 and len(forked["changeLog"]) == 1


# ---- sources and coverage -----------------------------------------------------------------------------------

def test_source_urls_are_classified_and_unsafe_hosts_rejected():
    assert parse_source("https://www.youtube.com/@TradeGuru/videos")["kind"] == "channel"
    assert parse_source("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv")["kind"] == "channel"
    assert parse_source("https://youtu.be/abcdefghijk")["kind"] == "video"
    assert parse_source("https://www.youtube.com/playlist?list=PL12345678901")["kind"] == "playlist"
    for bad in ("http://www.youtube.com/@x", "https://evil.com/@TradeGuru", "https://youtube.com.evil.com/watch?v=abcdefghijk", "https://user:pw@www.youtube.com/@abc"):
        with pytest.raises(ValueError):
            parse_source(bad)


def video(status, **extra):
    return {"id": f"v-{status}-{len(extra)}", "status": status, **extra}


def test_full_coverage_is_claimed_only_when_every_eligible_video_is_analysed():
    done = [video("COMPLETED") for _ in range(3)]
    assert coverage({}, done)["fullCoverage"]
    partial = coverage({}, done + [video("QUEUED")])
    assert not partial["fullCoverage"] and "still queued" in partial["statement"]
    blocked = coverage({}, done + [video("AWAITING_PERMISSION"), video("UNAVAILABLE"), video("FAILED")])
    assert not blocked["fullCoverage"] and blocked["awaiting_permission"] == 1 and blocked["unavailable"] == 1 and blocked["failed"] == 1
    assert not coverage({"truncated": True}, done)["fullCoverage"] and "NOT discovered" in coverage({"truncated": True}, done)["statement"]
    assert coverage({}, done + [video("SKIPPED")])["fullCoverage"] and coverage({}, done + [video("SKIPPED")])["skipped"] == 1
    assert coverage({}, done + [video("COMPLETED", visualAnalysis=False)])["transcriptOnly"] == 1
    assert {status_group(s) for s in ("DOWNLOADING", "BUILDING_KNOWLEDGE")} == {"analyzing"}


def test_channel_discovery_dedupes_classifies_and_syncs(monkeypatch):
    from pyapp import sources
    from pyapp.media import media_processor
    listing = {"v1": ("Liquidity sweep strategy explained", "visible"), "v2": ("My birthday vlog", "visible"), "v3": ("[Private video]", "private"), "v4": ("Risk management for beginners", "needs_auth")}
    def fake_info(url, playlist=False, limit=None):
        entries = [{"youtubeId": k.ljust(11, "x"), "title": t, "duration": 600, "availability": a} for k, (t, a) in listing.items()]
        return {"isPlaylist": True, "playlistId": "UCchan", "title": "Trade Guru", "channel": "Trade Guru", "channelId": "UCchan", "videoCount": len(entries), "videos": entries if url.endswith("/videos") else [], "truncated": False}
    monkeypatch.setattr(media_processor, "get_info", fake_info)
    async def go():
        job = {"id": "j1", "url": "https://www.youtube.com/@TradeGuru", "kind": "channel", "isPlaylist": True, "status": "QUEUED"}
        await expand_ingestion(job)
        first = {v["title"]: v for v in await db.get_videos()}
        again = {"id": "j2", "url": job["url"], "kind": "channel", "isPlaylist": True, "status": "QUEUED"}
        await expand_ingestion(again)
        return job, again, first, await db.get_videos(), await db.list("sources")
    job, again, first, videos, sources_list = run(go())
    assert job["status"] == "COMPLETED" and job["videoCount"] == 4 and again["queuedCount"] == 0 and len(videos) == 4        # no duplicates on sync
    assert first["[Private video]"]["status"] == "UNAVAILABLE"
    assert first["Risk management for beginners"]["status"] == "AWAITING_PERMISSION"
    assert first["Liquidity sweep strategy explained"]["status"] == "QUEUED"
    assert first["My birthday vlog"]["status"] == "SKIPPED" and not first["My birthday vlog"]["eligible"]
    assert len(sources_list) == 1 and sources_list[0]["kind"] == "channel"


def test_transcript_upload_parsing_flags_estimated_timestamps():
    from pyapp.routes.knowledge import parse_transcript
    srt = "1\n00:00:01,000 --> 00:00:04,000\nHello traders\n\n2\n00:00:05,000 --> 00:00:08,500\nToday liquidity sweep\n"
    segments, estimated = parse_transcript(srt, "a.srt")
    assert not estimated and segments[1]["start"] == 5.0 and segments[1]["end"] == 8.5
    vtt = "WEBVTT\n\n00:01.000 --> 00:03.000\nfirst line\n\n01:00:00.000 --> 01:00:02.000\n<c>later</c> line\n"
    parsed, est = parse_transcript(vtt, "a.vtt")
    assert not est and parsed[1]["start"] == 3600.0 and parsed[1]["text"] == "later line"
    plain, est = parse_transcript("word " * 200, "a.txt")
    assert est and len(plain) == 5


# ---- Q&A -------------------------------------------------------------------------------------------------------

EVIDENCE = [{"id": "E1", "text": "Stop below swing low", "quote": "Stop loss swing low ke neeche rakhna hai", "video": "V", "time": 40.0}, {"id": "E2", "text": "Target 2R", "quote": None, "video": "V", "time": 60.0}]


def test_answers_keep_only_real_citations_and_withhold_uncited_claims():
    good = qa.validate_answer({"sourceBasedAnswer": "Place it under the swing low [E1] and [E9].", "citations": ["E1", "E7"], "insufficient": False}, EVIDENCE)
    assert good["citations"] == ["E1"] and "[E9]" not in good["sourceBasedAnswer"] and good["validationNotes"]
    fabricated = qa.validate_answer({"sourceBasedAnswer": "Use a 12 pip stop.", "citations": [], "insufficient": False}, EVIDENCE)
    assert fabricated["insufficient"] and fabricated["sourceBasedAnswer"] == "" and fabricated["ungroundedWithheld"]
    refusal = qa.validate_answer({"sourceBasedAnswer": "", "citations": [], "insufficient": True, "missing": "The teacher never states a risk percentage."}, EVIDENCE)
    assert refusal["insufficient"] and refusal["missing"]


def test_hybrid_retrieval_finds_hinglish_and_english_terms():
    items = [{"key": "a", "kind": "atom", "text": "Stop below swing low", "quote": "Stop loss swing low ke neeche rakhna hai", "provenance": "source_confirmed", "videoId": "v1"},
             {"key": "b", "kind": "atom", "text": "Risk one percent per trade", "quote": "Ek percent risk lena hai", "provenance": "source_confirmed", "videoId": "v1"},
             {"key": "c", "kind": "atom", "text": "Avoid news", "quote": "News ke time trade nahi", "provenance": "missing_unclear", "videoId": "v2"}]
    picked = run(retrieval.retrieve("stop loss kahan rakhna hai", ["stop loss placement"], items))
    assert picked[0]["key"] == "a"
    assert run(retrieval.retrieve("quantum chromodynamics", [], items)) == []


def test_conversation_flow_is_multi_turn_scoped_and_never_invents_without_evidence():
    from fastapi.testclient import TestClient
    from pyapp.main import app
    client = TestClient(app)
    async def seed():
        atoms = await library.build_atoms(VIDEO, SEGMENTS, [], 120, fake_extractor)
        await db.save_video(VIDEO)
        await db.replace_for_video("atoms", VIDEO["id"], atoms)
    run(seed())
    conversation = client.post("/api/qa/conversations", json={}).json()
    first = client.post(f"/api/qa/conversations/{conversation['id']}/ask", json={"question": "stop loss kahan rakhna hai?"}).json()
    assert first["mode"] == "extractive" and first["evidence"] and "swing low" in first["text"].lower()
    none = client.post(f"/api/qa/conversations/{conversation['id']}/ask", json={"question": "what is the dividend yield of apple"}).json()
    assert none["answer"]["insufficient"] and not none["evidence"]
    thread = client.get(f"/api/qa/conversations/{conversation['id']}").json()
    assert [m["role"] for m in thread["messages"]] == ["user", "assistant", "user", "assistant"] and thread["title"].startswith("stop loss")
