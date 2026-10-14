import asyncio
from unittest.mock import AsyncMock
from google import genai
from google.genai import _transformers
import pytest
from fastapi.testclient import TestClient
from pyapp.main import app
from pyapp.config import settings
import pyapp.ai as ai
import pyapp.pipeline as pipeline
import pyapp.worker as worker

def test_gemini_structured_schemas_match_installed_sdk():
    client = genai.Client(api_key="schema-test-only")
    try:
        for schema in (ai.Events, ai.Transcript, ai.Visual, ai.Trades, ai.Validation, ai.Lessons, ai.RuleProposal, ai.SearchTerms):
            assert _transformers.t_schema(client._api_client, schema) is not None
    finally:
        client.close()

def test_playlist_expansion_is_idempotent_and_persists_membership(isolated_database, monkeypatch):
    info = {"isPlaylist": True, "playlistId": "playlist-test", "title": "Test playlist", "channel": "Test",
            "videoCount": 2, "videos": [{"youtubeId": "abcdefghijk", "title": "One", "duration": 100}, {"youtubeId": "12345678901", "title": "Two", "duration": 200}]}
    monkeypatch.setattr(worker.media_processor, "get_info", lambda *args: info)
    async def run():
        job = {"id": "job", "url": "https://www.youtube.com/playlist?list=PL12345678901", "isPlaylist": True, "status": "QUEUED"}
        await worker.expand_ingestion(job)
        assert job["status"] == "COMPLETED"
        assert job["queuedCount"] == 2
        await worker.expand_ingestion(job)
        assert job["queuedCount"] == 0
        assert len(await isolated_database.get_videos()) == 2
        assert (await isolated_database.get_playlists())[0]["analyzedCount"] == 0
        assert (await isolated_database.get_videos())[0]["playlistIds"] == ["playlist-test"]
    asyncio.run(run())

def test_pipeline_publishes_real_evidence_and_updates_exit(isolated_database, monkeypatch, tmp_path):
    image = tmp_path / "frame.webp"
    image.write_bytes(b"test-only-frame")
    processor = pipeline.media_processor
    monkeypatch.setattr(processor, "get_info", lambda *_: {"title": "Test", "duration": 120, "channel": "Test", "uploadDate": "20260101"})
    monkeypatch.setattr(processor, "download_video", lambda *_: "test-source.mp4")
    monkeypatch.setattr(processor, "audio_chunks", lambda *_: [{"path": "test-audio.mp3", "offset": 0}])
    monkeypatch.setattr(processor, "extract_frame", lambda *_: str(image))
    monkeypatch.setattr(processor, "cleanup", lambda *_: None)
    monkeypatch.setattr(pipeline, "transcribe", AsyncMock(return_value={"segments": [{"start":10,"end":11,"text":"Entry"},{"start":90,"end":91,"text":"Exit"}]}))
    events = [dict(timestamp=t, eventType=e, transcriptEvidence=e, confidence=.9, detectionSource="both") for t,e in ((10,"ENTRY"),(90,"EXIT"))]
    monkeypatch.setattr(pipeline, "semantic_events", AsyncMock(return_value=events))
    monkeypatch.setattr(pipeline, "scan_video", AsyncMock(return_value=[]))
    monkeypatch.setattr(pipeline, "analyze_frames", AsyncMock(return_value={"visualDescription":"Test-only position evidence", "executionVisible": True}))
    entry = dict(actuallyTaken=True, symbol="EURUSD", direction="LONG", entryPrice=100., stopLoss=95., takeProfit=110., entryTimestamp=10., outcome="UNKNOWN", exitTimestamp=None,
                 executionEvidence="Position filled at 10s", setup="Sweep", timeframe="M1", overallConfidence=.9,
                 fieldEvidence={"entryPrice":"10s", "stopLoss":"10s", "takeProfit":"10s"})
    exit_trade = {**entry, "exitPrice":110., "exitTimestamp":90., "outcome":"WIN", "fieldEvidence":{**entry["fieldEvidence"], "exitPrice":"90s", "outcome":"90s"}}
    monkeypatch.setattr(pipeline, "extract_trades", AsyncMock(side_effect=[[entry],[exit_trade]]))
    monkeypatch.setattr(pipeline, "validate_trade", AsyncMock(return_value={"status":"VALID", "reason":"Test support", "confidence":.9, "unsupportedFields":[]}))
    monkeypatch.setattr(pipeline, "extract_lessons", AsyncMock(return_value=[{"timestamp":10,"type":"Spoken","title":"Test rule","content":"Test lesson"}]))
    async def run():
        await isolated_database.save_video({"id":"video-abcdefghijk", "youtubeId":"abcdefghijk", "title":"Test", "status":"QUEUED"})
        await pipeline.process_video_pipeline("video-abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk")
        video = await isolated_database.get_video("video-abcdefghijk")
        assert video["status"] == "COMPLETED"
        trades = await isolated_database.get_trades()
        assert len(trades) == 1
        assert trades[0]["result"] == "WIN"
        assert trades[0]["rr"] == 2
        assert trades[0]["frames"][0]["sha256"]
        assert trades[0]["evidenceCutoff"].startswith("2026-01-02")
        assert await isolated_database.list("knowledge")
        response = TestClient(app).get("/videos/video-abcdefghijk")
        assert response.status_code == 200
        assert "Captured chart evidence" in response.text
        assert TestClient(app).get("/trades").status_code == 200
    asyncio.run(run())

def test_failure_is_visible_and_not_success(isolated_database, monkeypatch):
    def fail(*_): raise RuntimeError("test metadata failure")
    monkeypatch.setattr(pipeline.media_processor, "get_info", fail)
    monkeypatch.setattr(pipeline.media_processor, "cleanup", lambda *_: None)
    async def run():
        await isolated_database.save_video({"id":"v", "title":"Test", "status":"QUEUED"})
        await pipeline.process_video_pipeline("v", "https://www.youtube.com/watch?v=abcdefghijk")
        assert (await isolated_database.get_video("v"))["status"] == "FAILED"
        assert await isolated_database.get_trades() == []
    asyncio.run(run())

def test_ingest_validates_url_and_writes_durable_queue(monkeypatch, isolated_database):
    monkeypatch.setattr(settings, "MOCK_AI_MODE", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-only")
    monkeypatch.setattr(pipeline.media_processor, "ffmpeg", lambda: "test-ffmpeg")
    client = TestClient(app)
    assert client.post("/api/ingest", json={"url":"https://evil.com/watch?v=abcdefghijk"}).status_code == 400
    response = client.post("/api/ingest", json={"url":"https://youtu.be/abcdefghijk"})
    assert response.status_code == 202
    repeat = client.post("/api/ingest", json={"url":"https://youtu.be/abcdefghijk"})
    assert repeat.json()["jobId"] == response.json()["jobId"]
    assert len(asyncio.run(isolated_database.list("ingestions"))) == 1
