"""Cost, abuse and storage protections: rate limits, media library rules, AI budget caps, disk guard, job queue caps and DB housekeeping."""
import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from pyapp import budget, jobs, limits, maintenance, qa
from pyapp.config import settings
from pyapp.db import db
from pyapp.main import app

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 200


def upload(client, name="chart.png", data=PNG, note=""):
    return client.post("/api/media", files={"file": (name, data, "application/octet-stream")}, data={"note": note})


# ---- rate limiting -------------------------------------------------------------------------------------

def test_rate_limit_returns_429_with_retry_after_and_recovers_per_bucket():
    ok = [limits.check_rate("POST", "/api/auth/login", "1.2.3.4")[0] for _ in range(10)]
    assert all(ok)
    allowed, retry = limits.check_rate("POST", "/api/auth/login", "1.2.3.4")
    assert not allowed and 1 <= retry <= 61
    assert limits.check_rate("POST", "/api/auth/login", "5.6.7.8")[0]                      # other clients are unaffected
    assert limits.check_rate("GET", "/api/health", "1.2.3.4")[0]                           # health checks are never limited
    assert limits.check_rate("GET", "/static/css/app.css", "1.2.3.4")[0]


def test_expensive_endpoints_are_throttled_through_the_http_stack():
    client = TestClient(app)
    codes = [client.post("/api/qa/conversations/abc/ask", json={"question": "x"}).status_code for _ in range(14)]
    assert codes.count(429) >= 2 and 429 not in codes[:12]
    blocked = client.post("/api/qa/conversations/abc/ask", json={"question": "x"})
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1


# ---- media library -------------------------------------------------------------------------------------

def test_media_upload_list_download_delete_roundtrip(tmp_path):
    client = TestClient(app)
    created = upload(client, note="EURUSD setup")
    assert created.status_code == 201 and created.json()["kind"] == "image" and created.json()["duplicate"] is False
    media_id = created.json()["id"]
    listing = client.get("/api/media").json()
    assert [m["id"] for m in listing["items"]] == [media_id] and listing["usage"]["files"] == 1 and listing["usage"]["usedBytes"] == len(PNG)
    served = client.get(f"/api/media/{media_id}/file")
    assert served.status_code == 200 and served.content == PNG and served.headers["content-type"] == "image/png"
    assert client.delete(f"/api/media/{media_id}").status_code == 200
    assert client.get("/api/media").json()["items"] == [] and client.get(f"/api/media/{media_id}/file").status_code == 404
    assert not any(Path(settings.STORAGE_PATH, "media").rglob("*.png"))                      # the file left the disk, not just the index


def test_media_rejects_wrong_types_fake_content_and_path_tricks():
    client = TestClient(app)
    assert upload(client, "run.exe", b"MZ" + b"0" * 50).status_code == 400
    assert upload(client, "page.html", b"<script>alert(1)</script>").status_code == 400
    assert upload(client, "fake.png", b"this is not a png at all").status_code == 400      # extension and bytes must agree
    assert upload(client, "empty.png", b"").status_code in (400,)
    assert upload(client, "bin.txt", b"abc\x00def").status_code == 400                      # binary data posing as text
    odd = upload(client, "../../etc/passwd.png", PNG)
    assert odd.status_code == 201 and "/" not in odd.json()["name"] and ".." not in odd.json()["name"]
    stored = list(Path(settings.STORAGE_PATH, "media").rglob("*"))
    assert all(Path(settings.STORAGE_PATH, "media") in p.parents for p in stored)           # nothing escaped the media folder
    assert client.get("/api/media/not-a-valid-id/file").status_code == 404


def test_media_enforces_size_quota_file_count_and_deduplicates(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 1)
    assert upload(client, "big.png", PNG + b"0" * (1024 * 1024 + 10)).status_code == 413
    assert not any(Path(settings.STORAGE_PATH, "media").rglob("*.png"))                      # a refused upload leaves no partial file
    monkeypatch.setattr(settings, "MAX_UPLOAD_MB", 200)
    first = upload(client)
    twin = upload(client, "copy-of-chart.png")
    assert twin.json()["duplicate"] is True and twin.json()["id"] == first.json()["id"]
    assert len(client.get("/api/media").json()["items"]) == 1                               # one copy on disk
    monkeypatch.setattr(settings, "MEDIA_QUOTA_MB", 50)
    asyncio.run(db.save("media", {"id": "a" * 32, "name": "x", "ext": ".png", "kind": "image", "size": 60 * 1024 * 1024, "sha256": "z", "path": "media/x", "createdAt": "2026-01-01T00:00:00Z"}))
    assert upload(client, "another.png", PNG + b"1").status_code == 409                    # quota exhausted


def test_media_refuses_uploads_when_server_disk_is_low(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(limits, "storage_status", lambda: {"freeMb": 100, "totalMb": 50000, "freeInodePct": 50.0})
    refused = upload(client)
    assert refused.status_code == 507 and "storage is low" in refused.json()["detail"]
    monkeypatch.setattr(limits, "storage_status", lambda: {"freeMb": 50000, "totalMb": 50000, "freeInodePct": 2.0})
    assert upload(client).status_code == 507 and "file-count" in upload(client).json()["detail"]    # inode exhaustion is guarded as well


# ---- AI budget -------------------------------------------------------------------------------------------

def test_budget_costs_tokens_at_published_prices_and_blocks_at_the_cap(monkeypatch):
    assert round(budget.cost_usd("gemini-3.8-flash", 1_000_000, 1_000_000), 2) == 4.50              # $0.75 in + $3.75 out per 1M
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 0.004)
    monkeypatch.setattr(settings, "AI_DAILY_QUESTIONS", 2)

    async def scenario():
        assert await budget.blocked("video") is None
        usage = SimpleNamespace(prompt_token_count=1800, candidates_token_count=330, thoughts_token_count=500)
        cost = await budget.record("qa", "gemini-3.8-flash", usage)
        assert 0.004 < cost < 0.006                                                                  # thinking tokens are billed as output
        await budget.record("qa", "gemini-3.8-flash", SimpleNamespace(prompt_token_count=10, candidates_token_count=1))
        assert "Daily AI budget" in (await budget.blocked("video"))
        summary = await budget.summary()
        assert summary["periods"]["today"]["calls"] == 2 and summary["byKind"]["qa"]["calls"] == 2
    asyncio.run(scenario())


def test_chat_degrades_to_free_source_passages_when_the_daily_cap_is_reached(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_AI_MODE", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 0.0001)

    async def scenario():
        await budget.record("qa", "gemini-3.8-flash", SimpleNamespace(prompt_token_count=100000, candidates_token_count=1000))
        await db.save("atoms", {"id": "a1", "text": "Stop loss goes above the sweep high", "quote": "stop above the sweep high", "videoTitle": "Demo", "videoId": "video-x", "youtubeId": "x", "startTime": 5,
                                "sourceUrl": "https://youtu.be/x?t=5", "provenance": "source_confirmed", "category": "stop_loss", "strategyName": "Sweep"})
        conversation = {"id": "c1", "title": "New conversation", "strategyId": None}
        await db.save("conversations", conversation)
        answer = await qa.ask(conversation, "where does the stop loss go")
        assert answer["mode"] == "extractive" and "Daily AI budget" in " ".join(answer["answer"]["validationNotes"])
        assert answer["retrieved"] >= 1
    asyncio.run(scenario())


# ---- queues, database and housekeeping ------------------------------------------------------------------

def test_library_rebuilds_do_not_pile_up_and_user_jobs_are_capped(monkeypatch):
    async def scenario():
        first = await jobs.enqueue("rebuild_library", {}, "Rebuild")
        second = await jobs.enqueue("rebuild_library", {}, "Rebuild")
        assert first["id"] == second["id"] and len(await db.query("jobs", {"status": "QUEUED"})) == 1
        monkeypatch.setattr(settings, "MAX_QUEUED_JOBS", 3)
        for n in range(2):
            await jobs.enqueue("backtest", {"n": n})
        try:
            await limits.ensure_queue_room()
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 429
        else:
            raise AssertionError("queue cap did not trigger")
    asyncio.run(scenario())


def test_database_maintenance_prunes_old_finished_jobs_and_keeps_the_rest():
    async def scenario():
        await db.save("jobs", {"id": "old", "kind": "backtest", "status": "COMPLETED", "createdAt": "2020-01-01T00:00:00+00:00", "finishedAt": "2020-01-01T00:05:00+00:00"})
        await db.save("jobs", {"id": "old-queued", "kind": "backtest", "status": "QUEUED", "createdAt": "2020-01-01T00:00:00+00:00"})
        await db.save("jobs", {"id": "fresh", "kind": "backtest", "status": "COMPLETED", "createdAt": jobs.now(), "finishedAt": jobs.now()})
        report = await db.maintain(30)
        remaining = {j["id"] for j in await db.list("jobs")}
        assert report["jobsRemoved"] == 1 and remaining == {"old-queued", "fresh"}                   # unfinished work is never pruned
        assert await db.check() == ["ok"]
    asyncio.run(scenario())


def test_tmp_cleanup_removes_only_stale_files(tmp_path):
    old, new = tmp_path / "a" / "old.bin", tmp_path / "a" / "new.bin"
    old.parent.mkdir()
    old.write_bytes(b"1"), new.write_bytes(b"2")
    stale = time.time() - 3 * 24 * 3600
    import os
    os.utime(old, (stale, stale))
    assert maintenance.purge_old_files(tmp_path, 24 * 3600) == 1 and not old.exists() and new.exists()


# ---- chat latency / cost behaviour ----------------------------------------------------------------------

def test_query_rewrite_is_only_used_for_hindi_hinglish_questions():
    assert not qa.needs_rewrite("What trade did the trader take in the video?")
    assert qa.needs_rewrite("stop loss kaha lagate hain")
    assert qa.needs_rewrite("स्टॉप लॉस कहाँ लगाते हैं")


def test_repeated_question_is_served_from_memory_without_any_model_call(monkeypatch):
    from pyapp import ai_strategy, retrieval
    monkeypatch.setattr(settings, "MOCK_AI_MODE", False)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    calls = {"answer": 0, "rewrite": 0, "embed": 0}

    async def fake_answer(question, history, evidence, strategy):
        calls["answer"] += 1
        return {"sourceBasedAnswer": f"Stop goes above the sweep high [{evidence[0]['id']}]", "steps": [], "citations": [evidence[0]["id"]], "conflicts": [], "generalEducation": None,
                "insufficient": False, "missing": None, "followUps": []}

    async def fake_rewrite(question, history):
        calls["rewrite"] += 1
        return ["stop loss"]

    async def fake_vectors(question, items, limit_new=400):
        calls["embed"] += 1
        return {}
    monkeypatch.setattr(ai_strategy, "answer_question", fake_answer)
    monkeypatch.setattr(ai_strategy, "rewrite_query", fake_rewrite)
    monkeypatch.setattr(retrieval, "vector_scores", fake_vectors)

    async def scenario():
        await db.save("atoms", {"id": "a1", "text": "Stop loss goes above the sweep high", "quote": "stop above the sweep high", "videoTitle": "Demo", "videoId": "video-x", "youtubeId": "x", "startTime": 5,
                                "sourceUrl": "https://youtu.be/x?t=5", "provenance": "source_confirmed", "category": "stop_loss", "strategyName": "Sweep"})
        conversation = {"id": "c1", "title": "New conversation", "strategyId": None}
        await db.save("conversations", conversation)
        first = await qa.ask(conversation, "where does the stop loss go")
        assert calls == {"answer": 1, "rewrite": 0, "embed": 1} and first["mode"] == "ai"          # plain English: no rewrite call
        conversation2 = {"id": "c2", "title": "New conversation", "strategyId": None}
        await db.save("conversations", conversation2)
        second = await qa.ask(conversation2, "Where does the stop loss go?")
        assert calls == {"answer": 1, "rewrite": 0, "embed": 1}                                     # nothing was called again
        assert "no AI cost" in " ".join(second["answer"]["validationNotes"]) and second["text"] == first["text"]
        await qa.ask(conversation2, "aur target kya hai")                                           # follow-up with history: not cached, rewrite is used
        assert calls["answer"] == 2 and calls["rewrite"] == 1
    asyncio.run(scenario())
