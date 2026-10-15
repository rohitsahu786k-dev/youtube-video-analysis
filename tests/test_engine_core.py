"""Engine acceptance tests: numeric accuracy, lookahead prevention, ambiguous same-bar fills, reproducibility."""
import pytest

from helpers import FLAT, LOW, T0, benchmark_rows, candles, config, random_market, run, spec, with_future_garbage
from pyapp.engine.conditions import Ctx, compile_cond
from pyapp.engine.data import CandleSet
from pyapp.engine.instruments import conversion_factor, resolve_instrument, size_position
from pyapp.engine.runner import execute_backtest
from pyapp.engine.series import Bars, Factory, swings
from pyapp.engine.spec import DaySpec, SeriesDef, SpecError


# ---- Test 7: numerical accuracy against a hand-computed benchmark ------------------------------------

def test_long_benchmark_matches_hand_calculation():
    # signal close 1.1005, spread 0.0002 -> ref ask 1.1006; stop 1.0996 (10 pips); risk 1% of 10,000 = 100 -> 1.00 lot
    rows = benchmark_rows((1.1005, 1.1030, 1.1001, 1.1002))
    result = run(spec(), candles(rows), config())
    assert len(result["trades"]) == 1
    t = result["trades"][0]
    assert t["entry"] == pytest.approx(1.1006)
    assert t["stop0"] == pytest.approx(1.0996)
    assert t["qty"] == pytest.approx(1.0)
    assert t["riskAmount"] == pytest.approx(100.0)
    assert t["targets"][0]["price"] == pytest.approx(1.1026)          # entry + 2 x 10 pips
    assert t["exitReason"] == "TARGET_1"
    assert t["finalExit"] == pytest.approx(1.1026)
    assert t["grossPnl"] == pytest.approx(200.0)                       # 20 pips x 1 lot x 100,000 x 0.0001
    assert t["netPnl"] == pytest.approx(200.0)
    assert t["rMultiple"] == pytest.approx(2.0)
    assert t["idealPnl"] == pytest.approx(220.0)                       # mid entry 1.1005, mid exit 1.1027
    assert t["spreadCost"] == pytest.approx(20.0)
    assert t["slippageCost"] == pytest.approx(0.0, abs=1e-9)
    assert result["balanceEnd"] == pytest.approx(10200.0)


def test_commission_and_slippage_are_applied_exactly():
    rows = benchmark_rows((1.1005, 1.1030, 1.1001, 1.1002))
    cfg = config(costs={"spread": 0.0002, "slippage": 0.0001, "commission_model": "per_lot_side", "commission_value": 7})
    t = run(spec(), candles(rows), cfg)["trades"][0]
    assert t["entry"] == pytest.approx(1.1007)                         # open 1.1005 + half spread 0.0001 + slippage 0.0001
    assert t["fees"] == pytest.approx(14.0)                            # 7 per lot per side
    assert t["qty"] == pytest.approx(1.0)
    # target = fill + 2 x (fill - stop) with the stop anchored at the reference 1.0996 -> 1.1007 + 2 x 0.0011 = 1.1029
    assert t["targets"][0]["price"] == pytest.approx(1.1029)
    assert t["grossPnl"] == pytest.approx((1.1029 - 1.1007) * 100000)
    assert t["netPnl"] == pytest.approx((1.1029 - 1.1007) * 100000 - 14.0)
    assert t["slippageCost"] == pytest.approx(10.0, abs=1e-6)          # 1 pip on entry only (limit target has no slippage)


def test_short_benchmark_mirrors_long():
    rows = [(1.1020, 1.1022, 1.1018, 1.1020)] * 3 + [(1.1020, 1.1021, 1.0990, 1.0995), (1.0995, 1.0997, 1.0960, 1.0990)] + [(1.0990, 1.0992, 1.0988, 1.0990)] * 12
    s = spec(entry={"short": {"left": "close", "op": "<", "right": 1.0996}})
    t = run(s, candles(rows), config())["trades"][0]
    assert t["direction"] == "SHORT"
    assert t["entry"] == pytest.approx(1.0994)                         # bid: open 1.0995 - half spread
    assert t["stop0"] == pytest.approx(1.1004)                         # reference bid 1.0994 + 10 pips
    assert t["targets"][0]["price"] == pytest.approx(1.0974)
    assert t["exitReason"] == "TARGET_1"
    assert t["netPnl"] == pytest.approx(200.0)


def test_jpy_pair_conversion_and_sizing_never_round_up():
    inst = resolve_instrument("USDJPY")
    assert inst.pip_size == 0.01 and inst.quote_currency == "JPY"
    conv = conversion_factor(inst, "USD", 150.0, None)
    assert conv == pytest.approx(1 / 150.0)
    assert size_position(100.0, 0.50, inst, conv) == pytest.approx(0.30)
    assert size_position(100.0, 0.53, inst, conv) == pytest.approx(0.28)   # 0.2829 rounds DOWN, never up
    assert size_position(1.0, 0.50, inst, conv) == 0.0                    # below the minimum lot -> no trade
    with pytest.raises(SpecError):
        conversion_factor(resolve_instrument("EURGBP"), "USD", 0.85, None)


# ---- Test 6: ambiguous same-bar stop and target --------------------------------------------------------

def test_same_bar_stop_and_target_never_defaults_to_the_profit():
    rows = benchmark_rows((1.1005, 1.1030, 1.0990, 1.1000))
    t = run(spec(), candles(rows), config())["trades"][0]
    assert t["ambiguous"]
    assert t["exitReason"] == "STOP_AMBIGUOUS_BAR"
    assert t["netPnl"] == pytest.approx(-100.0) and t["rMultiple"] == pytest.approx(-1.0)
    assert t["ambiguityBestCaseNet"] > 0                               # disclosed as an upper bound only


def _fine_for(path):
    return candles(path, "M1", t0=T0 + 4 * 300)


def test_lower_timeframe_resolves_which_level_was_hit_first():
    target_first = [(1.1005, 1.1030, 1.1004, 1.1025), (1.1025, 1.1026, 1.0990, 1.0992), (1.0992, 1.0995, 1.0990, 1.0993), (1.0993, 1.0996, 1.0991, 1.0995), (1.0995, 1.1000, 1.0994, 1.1000)]
    stop_first = [(1.1005, 1.1006, 1.0990, 1.0992), (1.0992, 1.1030, 1.0991, 1.1025), (1.1025, 1.1028, 1.1020, 1.1022), (1.1022, 1.1026, 1.1018, 1.1020), (1.1020, 1.1024, 1.1000, 1.1000)]
    cfg = config(same_bar_policy="lower_tf")
    for path, expected, ambiguous in ((target_first, "TARGET_1", False), (stop_first, "STOP", False)):
        exec_bar = (path[0][0], max(p[1] for p in path), min(p[2] for p in path), path[-1][3])
        rows = benchmark_rows(exec_bar)
        t = run(spec(), candles(rows), cfg, _fine_for(path))["trades"][0]
        assert t["exitReason"] == expected and t["ambiguous"] is ambiguous


def test_gap_through_stop_fills_at_the_worse_open():
    rows = benchmark_rows((1.1005, 1.1006, 1.1002, 1.1003))
    rows[5] = (1.0970, 1.0975, 1.0960, 1.0965)
    t = run(spec(), candles(rows), config())["trades"][0]
    assert t["exitReason"] == "STOP_GAP"
    assert t["finalExit"] == pytest.approx(1.0970 - 0.0001)            # bid at the gapped open, not the stop price
    assert t["netPnl"] < -100


# ---- Test 5: lookahead prevention ---------------------------------------------------------------------

def test_swing_levels_exist_only_after_their_confirmation_bar():
    rows = [(10 + k * 0.01, 10.2 + k * 0.01, 9.8 + k * 0.01, 10.1 + k * 0.01) for k in range(8)]
    rows[5] = (10.0, 10.1, 5.0, 10.0)                                 # a deep pivot low at bar 5
    cs = candles(rows)
    levels = swings(Bars(cs, 300), 3)["low"]
    assert all(v != 5.0 for v in levels[:8])                           # bar 5 needs bars 6,7,8 -> known only at bar 8
    more = candles(rows + [(10.0, 10.2, 9.9, 10.1)])
    assert swings(Bars(more, 300), 3)["low"][8] == 5.0


def test_higher_timeframe_value_waits_for_the_bar_to_close():
    rows = [(1.0 + i * 0.001, 1.0 + i * 0.001 + 0.0005, 1.0 + i * 0.001 - 0.0005, 1.0 + i * 0.001 + 0.0002) for i in range(36)]
    cs = candles(rows)                                                 # three H1 bars of twelve M5 bars each
    series = Factory(Bars(cs, 300), DaySpec()).build("h1", SeriesDef(type="price", field="close", tf="H1"))
    assert series[10] is None                                          # first hour is still forming
    assert series[11] == pytest.approx(rows[11][3])                    # visible exactly at the hour's last bar close
    assert series[12] == pytest.approx(rows[11][3]) and series[22] == pytest.approx(rows[11][3])
    assert series[23] == pytest.approx(rows[23][3])
    altered = [list(r) for r in rows]
    altered[20] = (9.0, 9.5, 8.5, 9.0)                                 # mutate a later bar inside the second hour
    series2 = Factory(Bars(candles([tuple(r) for r in altered]), 300), DaySpec()).build("h1", SeriesDef(type="price", field="close", tf="H1"))
    assert series2[:23] == series[:23]


def test_conditions_cannot_look_forward():
    cs = candles([FLAT] * 5)
    ctx = Ctx(Bars(cs, 300), {})
    with pytest.raises(SpecError):
        compile_cond({"left": {"s": "close", "shift": -1}, "op": ">", "right": "open"}, ctx)


def test_results_do_not_change_when_future_bars_change():
    cs = random_market(700)
    s = spec(series={"ema": {"type": "ema", "period": 20}, "atr": {"type": "atr", "period": 14}},
             entry={"long": {"all": [{"left": "close", "op": ">", "right": "ema"}, {"left": {"s": "close", "shift": 1}, "op": "<=", "right": {"s": "ema", "shift": 1}}]},
                    "short": {"all": [{"left": "close", "op": "<", "right": "ema"}, {"left": {"s": "close", "shift": 1}, "op": ">=", "right": {"s": "ema", "shift": 1}}]}},
             stop={"type": "atr", "mult": 1.5, "series": "atr"}, targets=[{"type": "rr", "value": 1.5}])
    cfg = config(costs={"spread": 0.0001, "slippage": 0.00002})
    full = run(s, cs, cfg)
    cut = 400
    altered = run(s, with_future_garbage(cs, cut), cfg)
    before = [t for t in full["trades"] if t["exitIdx"] <= cut]
    before2 = [t for t in altered["trades"] if t["exitIdx"] <= cut]
    assert len(before) > 3
    keys = ("direction", "entryIdx", "entry", "stop0", "qty", "exitIdx", "finalExit", "exitReason", "netPnl")
    assert [{k: t[k] for k in keys} for t in before] == [{k: t[k] for k in keys} for t in before2]


def _ema_cross_spec():
    return spec(series={"ema": {"type": "ema", "period": 20}, "atr": {"type": "atr", "period": 14}},
                entry={"long": {"all": [{"left": "close", "op": ">", "right": "ema"}, {"left": {"s": "close", "shift": 1}, "op": "<=", "right": {"s": "ema", "shift": 1}}]}},
                stop={"type": "atr", "mult": 1.5, "series": "atr"}, targets=[{"type": "rr", "value": 1.5}])


def test_honest_engine_passes_every_lookahead_audit():
    cfg = config(causality_samples=5, costs={"spread": 0.0001, "slippage": 0.00001})
    report = execute_backtest(_ema_cross_spec().model_dump(mode="json"), cfg, random_market(900))
    audit = report["lookaheadAudit"]
    assert audit["passed"] and audit["sampled"] >= 3
    assert audit["seriesAudit"]["passed"] and audit["seriesAudit"]["series"] >= 3
    assert audit["guardedReplay"]["passed"]
    assert report["status"] == "VALID"


def test_guarded_replay_catches_a_simulator_that_peeks_at_the_next_bar():
    from pyapp.engine import simulator as sim_module
    original = sim_module.Simulator._signals
    def cheating(self, i, t_close):
        if i + 1 < self.n and self.cs.c[i + 1] < self.cs.c[i]:
            return None          # keep only entries whose next bar rises: the classic lookahead trick
        return original(self, i, t_close)
    sim_module.Simulator._signals = cheating
    try:
        cheat = execute_backtest(_ema_cross_spec().model_dump(mode="json"), config(causality_samples=3), random_market(900))
    finally:
        sim_module.Simulator._signals = original
    assert not cheat["lookaheadAudit"]["guardedReplay"]["passed"]
    assert cheat["status"] == "INVALID"


def test_series_audit_catches_an_indicator_that_reads_the_future():
    from pyapp.engine import series as series_module
    original = series_module.ema
    def cheating_ema(values, period):
        real = original(values, period)
        return real[1:] + [real[-1]]          # value at i is really the EMA of bar i+1
    series_module.ema = cheating_ema
    try:
        cheat = execute_backtest(_ema_cross_spec().model_dump(mode="json"), config(causality_samples=3), random_market(900))
    finally:
        series_module.ema = original
    assert not cheat["lookaheadAudit"]["seriesAudit"]["passed"]
    assert cheat["status"] == "INVALID"


# ---- Test 9: reproducibility and Test 8 style reconciliation -----------------------------------------------

def test_identical_inputs_give_identical_results_and_reconcile():
    cs = random_market(900)
    s = spec(series={"ema": {"type": "ema", "period": 20}, "atr": {"type": "atr", "period": 14}},
             entry={"long": {"all": [{"left": "close", "op": ">", "right": "ema"}, {"left": {"s": "close", "shift": 1}, "op": "<=", "right": {"s": "ema", "shift": 1}}]}},
             stop={"type": "atr", "mult": 1.5, "series": "atr"}, targets=[{"type": "rr", "value": 1.5}])
    cfg = config(costs={"spread": 0.0001, "slippage": 0.00001, "commission_model": "per_lot_side", "commission_value": 3.5})
    a = execute_backtest(s.model_dump(mode="json"), cfg, cs)
    b = execute_backtest(s.model_dump(mode="json"), cfg, cs)
    assert a["resultHash"] == b["resultHash"] and a["reproducibility"]["identical"]
    assert a["status"] == "VALID", [c for c in a["validation"]["checks"] if c["status"] == "FAIL"]
    closed = [t for t in a["trades"] if not t["openAtEnd"]]
    assert sum(t["netPnl"] for t in a["trades"]) == pytest.approx(a["finalBalance"] - 10000, abs=0.01)
    assert a["metrics"]["netPnl"] == pytest.approx(sum(t["netPnl"] for t in closed), abs=0.01)
    assert len({t["id"] for t in a["trades"]}) == len(a["trades"])


def test_validation_blocks_a_tampered_ledger():
    from pyapp.engine.data import quality_report
    from pyapp.engine.validate import validate_run
    cs = candles(benchmark_rows((1.1005, 1.1030, 1.1001, 1.1002)))
    s, cfg = spec(), config()
    result = run(s, cs, cfg)
    quality = quality_report(cs, "M5", "forex")
    assert not [c for c in validate_run(s, cfg, cs, result, quality) if c["status"] == "FAIL"]
    result["trades"][0]["netPnl"] += 5.0
    failed = [c["name"] for c in validate_run(s, cfg, cs, result, quality) if c["status"] == "FAIL"]
    assert any("P&L" in name or "balance" in name for name in failed)


# ---- pending orders, management and risk controls ------------------------------------------------------

def test_limit_order_requires_trade_through_and_expires():
    limit = {"long": {"left": "close", "op": ">", "right": 1.1004}, "order": {"type": "limit", "price": {"sub": ["close", 0.0010]}, "expire_bars": 2}}
    stop = {"type": "points", "value": 0.0020}
    through = benchmark_rows((1.1005, 1.1006, 1.0993, 1.1003))        # ask low 1.0994 trades through the 1.0995 limit
    t = run(spec(entry=limit, stop=stop), candles(through), config())["trades"][0]
    assert t["orderType"] == "limit" and t["entryKind"] == "intrabar_limit"
    assert t["entry"] == pytest.approx(1.0995)                         # reference limit = close 1.1005 - 0.0010
    touch_only = benchmark_rows((1.1005, 1.1006, 1.0994, 1.1003))     # ask low equals the level exactly
    assert not run(spec(entry=limit, stop=stop), candles(touch_only), config())["trades"]
    assert run(spec(entry=limit, stop=stop), candles(touch_only), config(limit_fill="touch"))["trades"]
    late = benchmark_rows(FLAT) + [(1.1002, 1.1004, 1.0990, 1.1002)]   # the dip comes after the 2-bar expiry
    result = run(spec(entry=limit, stop=stop), candles(late), config())
    assert not result["trades"] and result["counters"]["expired"] == 1


def test_breakeven_after_first_target_and_trailing_changes_apply_next_bar():
    rows = benchmark_rows((1.1005, 1.1018, 1.1004, 1.1015)) + [(1.1015, 1.1017, 1.1006, 1.1016), (1.1016, 1.1017, 1.1004, 1.1005)]
    s = spec(targets=[{"type": "rr", "value": 1.0, "portion": 0.5}, {"type": "rr", "value": 4.0, "portion": 0.5}], management={"breakeven": {"after_target": 1}})
    t = run(s, candles(rows), config())["trades"][0]
    assert t["partialExits"] and t["partialExits"][0]["reason"] == "TARGET_1"
    assert t["breakeven"] and any("BREAKEVEN" in c["detail"] for c in t["slChanges"])
    assert t["slChanges"][0]["time"] > t["partialExits"][0]["time"]    # never effective in the same bar


def test_daily_trade_limit_session_filter_and_news_blackout():
    base = [LOW, LOW, LOW] + [(1.0990, 1.1010, 1.0988, 1.1005), (1.1005, 1.1006, 1.1002, 1.1003)] * 6
    cs = candles(base)
    limited = run(spec(risk={"max_trades_per_day": 1}), cs, config())
    assert len(limited["trades"]) == 1 and limited["counters"]["rejected"].get("MAX_TRADES_PER_DAY")
    outside = run(spec(sessions={"windows": [{"start": "12:00", "end": "13:00", "tz": "UTC"}]}), cs, config())
    assert not outside["trades"] and outside["counters"]["rejected"].get("OUTSIDE_SESSION")
    news = run(spec(), cs, config(news_blackouts=[{"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T06:00:00Z"}]))
    assert not news["trades"]


def test_sequence_condition_requires_ordered_stages_within_the_window():
    series = {"hi": {"type": "highest", "period": 3, "exclude_current": True}}
    entry = {"long": {"seq": [{"left": "close", "op": "<", "right": 1.0990}, {"left": "close", "op": ">", "right": 1.1000}], "within": 3}}
    ok = [(1.0995, 1.0996, 1.0985, 1.0988), (1.0988, 1.1002, 1.0987, 1.1001)] + [FLAT] * 10
    rows_ok = [LOW] * 3 + ok
    assert run(spec(series=series, entry=entry), candles(rows_ok), config())["counters"]["signals"] == 1
    late = [(1.0995, 1.0996, 1.0985, 1.0988)] + [(1.0990, 1.0994, 1.0988, 1.0992)] * 4 + [(1.0992, 1.1003, 1.0990, 1.1002)] + [FLAT] * 6
    assert run(spec(series=series, entry=entry), candles([LOW] * 3 + late), config())["counters"]["signals"] == 0


def test_csv_accepts_vendor_annotated_headers_and_discloses_rounding_repairs():
    import pytest
    from pyapp.engine.data import parse_csv
    header = "Time (EET),Open,High,Low,Close,Volume \n"
    good = "2024.10.10 22:00:00,0.92565,0.92589,0.92527,0.92580,100\n2024.10.10 23:00:00,0.92574,0.92650,0.92470,0.92469,100\n"
    candles_, report = parse_csv((header + good).encode(), "H1", "Europe/Athens")
    assert len(candles_) == 2 and candles_.l[1] == 0.92469                      # close sat 1 point below low: range widened
    assert candles_.t[0] == 1728586800                                           # 22:00 EEST = 19:00 UTC
    assert any("Widened high/low on 1 bar" in note for note in report["repairs"])
    with pytest.raises(ValueError, match="invalid OHLC"):                        # a real inconsistency is still rejected
        parse_csv((header + good + "2024.10.11 00:00:00,0.92,0.93,0.95,0.92,1\n").encode(), "H1", "Europe/Athens")
    with pytest.raises(ValueError, match="invalid OHLC"):                        # and strict mode refuses any repair
        parse_csv((header + good).encode(), "H1", "Europe/Athens", repair=False)
