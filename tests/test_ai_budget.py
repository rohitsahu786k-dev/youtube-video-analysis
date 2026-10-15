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
