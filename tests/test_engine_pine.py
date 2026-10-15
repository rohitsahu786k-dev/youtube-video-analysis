"""Pine Script generation, static lookahead audit and TradingView trade-list comparison (SRS 11.3, 12, Test 10)."""
import pytest

from helpers import benchmark_rows, candles, config, run, spec
from pyapp.engine.pine import audit_pine, compare_trades, generate_pine, import_tv_trades
from pyapp.engine.spec import StrategySpec


def rich_spec():
    return StrategySpec.model_validate({
        "name": "Sweep and shift", "timeframe": "M15",
        "series": {"atr14": {"type": "atr", "period": 14}, "ema200h": {"type": "ema", "period": 200, "tf": "H1"},
                   "sweep": {"type": "sweep", "dir": "bull", "ref": "swing", "n": 3}, "bos": {"type": "bos", "dir": "bull", "n": 3},
                   "fvg": {"type": "fvg", "dir": "bull", "field": "mid", "min_size": 0.0002}},
        "entry": {"long": {"all": [{"left": "close", "op": ">", "right": "ema200h"}, {"seq": [{"is": "sweep"}, {"is": "bos"}], "within": 6}]},
                  "order": {"type": "limit", "price": "fvg", "expire_bars": 5}},
        "stop": {"type": "atr", "mult": 1.2, "series": "atr14"},
        "targets": [{"type": "rr", "value": 1.0, "portion": 0.5}, {"type": "rr", "value": 3.0, "portion": 0.5}],
        "management": {"breakeven": {"after_target": 1}, "trailing": {"type": "atr", "mult": 2.0, "series": "atr14", "start_r": 1.5}, "max_bars": 40},
        "sessions": {"windows": [{"start": "07:00", "end": "11:00", "tz": "Europe/London"}], "force_flat": {"time": "20:00", "tz": "UTC"}},
        "risk": {"model": "percent_equity", "value": 1.0, "max_trades_per_day": 2}})


def test_generated_script_is_a_strategy_with_orders_stops_targets_and_risk_controls():
    out = generate_pine(rich_spec(), config())
    text = out["script"]
    assert text.startswith("//@version=6") and 'strategy("Sweep and shift"' in text
    for needle in ("strategy.entry(", "strategy.exit(", "stop=", "limit=", "strategy.equity * riskValue", "tradesToday <", "process_orders_on_close=false", "calc_on_every_tick=false"):
        assert needle in text
    assert "seq1_stage" in text                                         # ordered sequence translated to a state machine
    assert "request.security(" in text and "lookahead=barmerge.lookahead_on" in text and ")[1]" in text
    assert out["audit"]["verdict"] in ("PASS", "WARN")
    assert not [f for f in out["audit"]["findings"] if f["severity"] == "FAIL"]
    assert out["equivalence"] in ("APPROXIMATE", "FULL") and out["warnings"]


def test_generation_is_deterministic():
    assert generate_pine(rich_spec(), config())["script"] == generate_pine(rich_spec(), config())["script"]


def test_unsupported_constructs_are_flagged_not_silently_dropped():
    s = rich_spec().model_dump(mode="json")
    s["series"]["htf_bos"] = {"type": "bos", "dir": "bull", "n": 3, "tf": "H4"}
    s["entry"]["long"] = {"is": "htf_bos"}
    s["risk"]["max_concurrent"] = 3
    out = generate_pine(StrategySpec.model_validate(s), config())
    assert out["equivalence"] == "NOT_EQUIVALENT_UNSUPPORTED_PARTS"
    assert len(out["unsupported"]) == 2 and "// UNSUPPORTED" in out["script"]


@pytest.mark.parametrize("snippet,severity", [
    ('x = request.security(syminfo.tickerid, "D", close, lookahead=barmerge.lookahead_on)', "FAIL"),
    ('x = request.security(syminfo.tickerid, "D", close[1], lookahead=barmerge.lookahead_on)', "PASS"),
    ('x = request.security(syminfo.tickerid, "D", close, lookahead=barmerge.lookahead_off)', "WARN"),
    ('y = close[-1]', "FAIL"),
    ('t = timenow', "FAIL"),
    ('if barstate.isrealtime', "WARN"),
])
def test_pine_audit_catches_future_data_patterns(snippet, severity):
    script = f'//@version=6\nstrategy("t", calc_on_every_tick=false)\n{snippet}\nstrategy.entry("L", strategy.long)\nstrategy.exit("X", "L", stop=1.0)\n'
    assert severity in [f["severity"] for f in audit_pine(script)["findings"]]


def test_pine_audit_rejects_repainting_strategy_settings():
    bad = '//@version=6\nstrategy("t", calc_on_every_tick=true, process_orders_on_close=true)\nstrategy.entry("L", strategy.long)\nstrategy.exit("X", "L", stop=1.0)'
    report = audit_pine(bad)
    assert report["verdict"] == "FAIL"
    assert {"FAIL", "WARN"} <= {f["severity"] for f in report["findings"]}


TV_EXPORT = """Trade #,Type,Signal,Date/Time,Price USD,Position size (qty),Position size (value),Net P&L USD,Net P&L %
1,Exit long,X1,2024-01-01 00:20,1.1026,100000,110260,200.00,0.18
1,Entry long,L,2024-01-01 00:20,1.1006,100000,110060,200.00,0.18
"""


def test_tradingview_export_is_parsed_and_compared_without_overclaiming():
    rows = benchmark_rows((1.1005, 1.1030, 1.1001, 1.1002))
    ours = run(spec(), candles(rows), config())["trades"]
    for t in ours:
        t["id"] = "T-1"
    tv = import_tv_trades(TV_EXPORT.encode(), "UTC")
    assert len(tv) == 1 and tv[0]["direction"] == "LONG" and tv[0]["qty"] == 100000 and tv[0]["netPnl"] == 200.0
    same = compare_trades(ours, tv, 300, 1e-6, 100000)
    assert same["verdict"] == "EQUIVALENT_WITHIN_TOLERANCE" and same["matched"] == 1
    shifted = [{**tv[0], "entry": 1.1008, "netPnl": 150.0}]
    different = compare_trades(ours, shifted, 300, 1e-6, 100000)
    assert different["verdict"] == "NOT_EQUIVALENT"
    assert any("entry price" in r for r in different["pairs"][0]["reasons"])
    missing = compare_trades(ours, [], 300, 1e-6, 100000)
    assert missing["verdict"] == "NOT_EQUIVALENT" and missing["onlyInEngine"] == ["T-1"]


def test_import_rejects_unrelated_files():
    with pytest.raises(ValueError):
        import_tv_trades(b"a,b,c\n1,2,3\n")
