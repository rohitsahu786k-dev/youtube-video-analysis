from datetime import datetime, timedelta, timezone
import pytest
from pyapp.backtest import setup_at, replay, metrics, run_backtest, live_candidate, rule_hash
from pyapp.market import validate_candles, parse_csv

RULE = {"kind": "sweep_rejection", "direction": "LONG", "lookback": 2, "rewardRisk": 2, "stopBuffer": 0, "maxHoldingBars": 5}

def bars(values):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return [dict(time=(start+timedelta(minutes=i)).isoformat(), open=o, high=h, low=l, close=c) for i, (o,h,l,c) in enumerate(values)]

def test_next_bar_entry_and_conservative_ambiguous_fill():
    candles = bars([(10,11,9,10), (10,11,9,10), (10,11,8,10), (10,20,7,10), (10,11,9,10)])
    trades = replay(candles, RULE, .1, .01)
    assert len(trades) == 1
    assert trades[0]["entryTime"] == candles[3]["time"]
    assert trades[0]["reason"] == "STOP"
    assert trades[0]["ambiguous"]
    assert trades[0]["r"] < -1

def test_gap_through_stop_gets_worse_fill():
    candles = bars([(10,11,9,10), (10,11,9,10), (10,11,8,10), (10,11,9,10), (6,7,5,6)])
    trade = replay(candles, RULE, .1, .01)[0]
    assert trade["reason"] == "STOP_GAP"
    assert trade["exit"] < trade["stop"]

def test_signals_do_not_see_future_bars():
    candles = bars([(10,11,9,10), (10,11,9,10), (10,11,8,10), (10,20,7,10)])
    assert setup_at(candles, 2, RULE) == setup_at(candles[:3], 2, RULE)
    candles[3]["high"] = 500
    assert setup_at(candles, 2, RULE) == setup_at(candles[:3], 2, RULE)

def test_unfinished_trades_excluded_from_win_rate():
    result = metrics([{"r": 2, "reason": "TARGET", "ambiguous": False}, {"r": 4, "reason": "END_OF_DATA", "ambiguous": False}])
    assert result["trades"] == 1
    assert result["openAtEnd"] == 1
    assert result["netR"] == 2

def test_post_video_holdout_and_untrusted_csv_live_gate():
    candles = bars([(10,11,9,10), (10,11,9,10), (10,11,8,10), (10,20,9,10)] * 30)
    strategy = dict(id="s", rule=RULE, symbol="EURUSD", timeframe="M1", sourceTradeIds=["a","b"], evidenceCutoff="2026-12-01T00:00:00+00:00", reviewed=True)
    dataset = dict(id="d", candles=candles, provider="OANDA", source="csv", priceBasis="mid", symbol="EURUSD", timeframe="M1")
    report = run_backtest(dataset, strategy, .1, .01, 10)
    assert report["holdout"]["trades"] == 0
    assert report["holdoutStart"] is None
    assert not report["eligibleForLive"]
    strategy["evidenceCutoff"] = "2020-01-01T00:00:00+00:00"
    assert not run_backtest(dataset, strategy, .1, .01, 10)["eligibleForLive"]

@pytest.mark.parametrize("change", ["stale_quote", "wrong_provider", "changed_rule", "spread", "stale_candle", "no_review", "old_backtest"])
def test_live_abstains_when_evidence_or_data_fails(change):
    now = datetime(2026,10,14,12,0,10,tzinfo=timezone.utc)
    candles = bars([(10,11,9,10), (10,11,9,10), (10,11,8,10)])
    for i, candle in enumerate(candles): candle["time"] = (now-timedelta(seconds=70+(2-i)*60)).isoformat()
    strategy = dict(id="s", rule=dict(RULE), symbol="EURUSD", timeframe="M1", sourceTradeIds=["a","b"], evidenceCutoff="2025-01-01T00:00:00+00:00", reviewed=True)
    report = dict(id="r", eligibleForLive=True, strategyHash=rule_hash(strategy), holdout={"trades":30}, provider="OANDA", spread=.2, dataEnd=now.isoformat())
    snapshot = dict(candles=candles, symbol="EURUSD", timeframe="M1", provider="OANDA", bid=9.95, ask=10.05, time=now.isoformat())
    if change == "stale_quote": snapshot["time"] = (now-timedelta(minutes=5)).isoformat()
    if change == "wrong_provider": snapshot["provider"] = "other"
    if change == "changed_rule": strategy["rule"]["rewardRisk"] = 3
    if change == "spread": snapshot["ask"] = 11
    if change == "stale_candle": candles[-1]["time"] = (now-timedelta(minutes=10)).isoformat()
    if change == "no_review": strategy["reviewed"] = False
    if change == "old_backtest": report["dataEnd"] = (now-timedelta(days=40)).isoformat()
    assert live_candidate(snapshot, strategy, report, 30, now)["status"] == "WAIT"

def test_ohlc_validation_rejects_bad_bounds_and_timestamps():
    with pytest.raises(ValueError): validate_candles(bars([(10,9,8,10)]), "M1")
    candles = bars([(10,11,9,10), (10,11,9,10)])
    candles[1]["time"] = candles[0]["time"]
    with pytest.raises(ValueError): validate_candles(candles, "M1")
    with pytest.raises(ValueError): validate_candles([{**candles[0], "time":"2026-01-01T00:00:00"}], "M1")
    with pytest.raises(ValueError): validate_candles([{**candles[0], "high":float('inf')}], "M1")

def test_fresh_eligible_live_setup_has_entry_stop_target_and_expiry():
    now = datetime(2026,10,14,12,0,10,tzinfo=timezone.utc)
    candles = bars([(10,11,9,10), (10,11,9,10), (10,11,8,10)])
    for i,c in enumerate(candles): c["time"]=(now-timedelta(seconds=70+(2-i)*60)).isoformat()
    strategy = dict(id="s",rule=dict(RULE),symbol="EURUSD",timeframe="M1",sourceTradeIds=["a","b"],evidenceCutoff="2020-01-01T00:00:00+00:00",reviewed=True)
    report = dict(id="r",eligibleForLive=True,strategyHash=rule_hash(strategy),holdout={"trades":30},provider="OANDA",spread=.2,dataEnd=now.isoformat())
    snapshot = dict(candles=candles,symbol="EURUSD",timeframe="M1",provider="OANDA",bid=9.95,ask=10.05,time=now.isoformat())
    result=live_candidate(snapshot,strategy,report,30,now)
    assert result["status"]=="PAPER_SETUP"
    assert result["stop"] < result["entry"] < result["target"]
    assert result["expiresAt"]==now.timestamp()+60
