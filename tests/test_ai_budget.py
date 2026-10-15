import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pyapp import ai, budget
from pyapp.config import settings
from pyapp.db import db


def test_scan_retry_reuses_completed_chunks(monkeypatch):
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 3.0)
    uploaded = SimpleNamespace(name="test-upload", uri="test-uri", mime_type="video/mp4", state=SimpleNamespace(name="ACTIVE"))
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(aclose=AsyncMock()), close=Mock(),
    )
    monkeypatch.setattr(ai, "get_client", lambda: client)
    first = {"timestamp": 10}
    second = {"timestamp": 610}
    infer = AsyncMock(side_effect=[{"events": [first]}, RuntimeError("interrupted"), {"events": [second]}])
    monkeypatch.setattr(ai, "structured", infer)
    checkpoint = AsyncMock()

    async def scenario():
        completed = {}
        with pytest.raises(RuntimeError, match="interrupted"):
            await ai.scan_video("video.mp4", 1200, "video-id", completed, checkpoint)
        assert completed == {"0": [first]}
        assert checkpoint.await_count == 1
        assert await ai.scan_video("video.mp4", 1200, "video-id", completed, checkpoint) == [first, second]
        assert infer.await_count == 3
        assert checkpoint.await_count == 2
        monkeypatch.setattr(ai, "get_client", Mock(side_effect=AssertionError("cached scan must not call Gemini")))
        assert await ai.scan_video("video.mp4", 1200, "video-id", completed, checkpoint) == [first, second]

    asyncio.run(scenario())


def fake_client(monkeypatch, response):
    generate = AsyncMock(return_value=response)
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate), aclose=AsyncMock()),
        close=Mock(),
    )
    monkeypatch.setattr(ai, "get_client", lambda: client)
    return generate


def test_running_video_stops_before_next_call_after_budget_reached(monkeypatch):
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 0.001)
    generate = fake_client(monkeypatch, SimpleNamespace(
        text='{"terms": ["entry"]}',
        usage_metadata=SimpleNamespace(prompt_token_count=2000, candidates_token_count=10),
    ))

    async def scenario():
        await ai.structured("gemini-3.8-flash", "find terms", ai.SearchTerms, "evidence")
        with pytest.raises(budget.BudgetExceeded):
            await ai.structured("gemini-3.8-flash", "find terms", ai.SearchTerms, "more evidence")
        assert generate.await_count == 1
        assert len(await db.list("usage")) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("response_text", [None, "invalid json"])
def test_unusable_response_still_records_billable_usage(monkeypatch, response_text):
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 3.0)
    fake_client(monkeypatch, SimpleNamespace(
        text=response_text,
        usage_metadata=SimpleNamespace(prompt_token_count=2000, candidates_token_count=10),
    ))

    async def scenario():
        with pytest.raises((RuntimeError, ValueError)):
            await ai.structured("gemini-3.8-flash", "find terms", ai.SearchTerms, "evidence")
        rows = await db.list("usage")
        assert len(rows) == 1
        assert rows[0]["promptTokens"] == 2000
        assert rows[0]["costUsd"] > 0

    asyncio.run(scenario())


def fake_embed_client(monkeypatch, billable_characters=4000):
    embed = AsyncMock(return_value=SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3])],
        metadata=SimpleNamespace(billable_character_count=billable_characters),
    ))
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed), aclose=AsyncMock()),
        close=Mock(),
    )
    monkeypatch.setattr(ai, "get_client", lambda: client)
    return embed


def test_embedding_spend_is_recorded(monkeypatch):
    from pyapp import retrieval
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 3.0)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    fake_embed_client(monkeypatch)

    async def scenario():
        await retrieval.embed_query("what is the entry rule")
        rows = await db.list("usage")
        assert [r["kind"] for r in rows] == ["embed"]
        assert rows[0]["promptTokens"] == 1000          # 4000 billable characters
        assert rows[0]["costUsd"] > 0

    asyncio.run(scenario())


def test_embedding_refused_once_budget_is_spent(monkeypatch):
    from pyapp import retrieval
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 0.0000001)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    embed = fake_embed_client(monkeypatch)

    async def scenario():
        await retrieval.embed_query("first question")
        with pytest.raises(budget.BudgetExceeded):
            await retrieval.embed_texts(["a chunk of knowledge"])
        assert embed.await_count == 1

    asyncio.run(scenario())


def test_embedding_halts_everything_when_credits_are_gone(monkeypatch):
    from pyapp import retrieval
    monkeypatch.setattr(settings, "AI_DAILY_BUDGET_USD", 3.0)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "test-key")
    depleted = RuntimeError("402 RESOURCE_EXHAUSTED prepayment credits are depleted")
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(embed_content=AsyncMock(side_effect=depleted)), aclose=AsyncMock()),
        close=Mock(),
    )
    monkeypatch.setattr(ai, "get_client", lambda: client)

    async def scenario():
        with pytest.raises(RuntimeError):
            await retrieval.embed_query("question")
        assert await budget.provider_halt() is not None
        assert "exhausted" in (await budget.blocked("video"))
        await budget.clear_provider_halt()

    asyncio.run(scenario())
