import asyncio
import pytest
from pyapp.db import Database
from pyapp.intelligence import aggregate_stats, build_patterns, risk_reward, retrieve
from pyapp.pipeline import normalized_trade, frame_times
from pyapp.youtube import canonical_url

def test_empty_stats_never_claim_wins():
    assert aggregate_stats([], [])["winRate"] is None
    assert aggregate_stats([], [])["hoursAnalyzed"] == 0

def test_rr_uses_prices_not_confidence():
    assert risk_reward(100, 95, 110, "LONG") == 2
    assert risk_reward(100, 105, 90, "SHORT") == 2
    assert risk_reward(100, 105, 110, "LONG") is None
    assert risk_reward(None, 95, 110, "LONG") is None

def test_pattern_denominator_and_independent_videos():
    trades = [dict(id=str(i), videoId=str(i // 2), setup="Sweep", symbol="EURUSD", timeframe="M5", direction="LONG", validation="VALID", result=result, rr=None, confidence=.7)
              for i, result in enumerate(("WIN", "UNKNOWN", "LOSS"))]
    patterns = build_patterns(trades + [{**trades[0], "validation": "UNCERTAIN"}])
    assert patterns[0]["occurrences"] == 3
    assert patterns[0]["winRate"] == 50
    assert patterns[0]["status"] == "RECURRING"
    assert patterns[0]["avgR"] is None

def test_unsupported_prices_and_execution_stay_uncertain():
    candidate = dict(actuallyTaken=True, symbol="EURUSD", direction="LONG", entryPrice=100, stopLoss=95, takeProfit=110,
                     entryTimestamp=10, outcome="WIN", exitTimestamp=20, overallConfidence=.9, fieldEvidence={}, executionEvidence=None)
    trade = normalized_trade(candidate, {"status": "VALID", "reason": "test", "confidence": .8, "unsupportedFields": []}, "video-abcdefghijk", 1, [{}], 100)
    assert trade["entry"] is None
    assert trade["rr"] is None
    assert trade["result"] == "UNKNOWN"
    assert trade["validation"] == "UNCERTAIN"

def test_frame_windows_stay_inside_duration():
    assert all(0 <= t < 100 for t in frame_times(99.9, 100))
    assert all(0 <= t < 100 for t in frame_times(0, 100))
    assert len(frame_times(50, 100)) <= 8

def test_durable_store_and_atomic_retry(tmp_path):
    async def run():
        path = tmp_path / "db.sqlite3"
        first = Database(path)
        await first.save_video({"id": "v", "youtubeId": "y", "status": "COMPLETED"})
        second = Database(path)
        assert (await second.get_video("v"))["status"] == "COMPLETED"
        await first.replace_evidence("v", [{"id": "t", "videoId": "v"}], [{"id": "k", "videoId": "v"}])
        await first.replace_evidence("v", [{"id": "t", "videoId": "v"}], [])
        assert len(await second.get_trades()) == 1
        assert await second.list("knowledge") == []
        with pytest.raises(Exception):
            await first.replace_evidence("v", [{"id": "t", "videoId": "v"}, {"id": "t", "videoId": "v"}], [])
        assert len(await second.get_trades()) == 1
    asyncio.run(run())

@pytest.mark.parametrize("url", ["https://example.com/?v=abcdefghijk", "file:///tmp/a", "http://youtu.be/abcdefghijk", "https://youtube.com.evil.com/?v=abcdefghijk", "https://www.youtube.com/watch?v=../x"])
def test_youtube_urls_reject_external_sources(url):
    with pytest.raises(ValueError): canonical_url(url)

def test_playlist_is_not_a_video():
    url, playlist = canonical_url("https://www.youtube.com/watch?v=abcdefghijk&list=PL12345678901")
    assert playlist
    assert url == "https://www.youtube.com/playlist?list=PL12345678901"

def test_retrieval_never_returns_unrelated_demo_evidence():
    assert retrieve("breakeven", [{"title": "Entry", "content": "liquidity sweep"}]) == []
