"""Independent validation of a finished simulation (SRS 11.6) and the prefix-consistency lookahead audit.

A material FAIL blocks the run from being published as valid.
"""
import math

from pyapp.engine.clock import in_window
from pyapp.engine.data import CandleSet, parse_iso
from pyapp.engine.instruments import conversion_factor, round_down_qty
from pyapp.engine.spec import BacktestConfig, StrategySpec, TF_SECONDS

TOL = 1e-6


def check(name, ok, detail="", level="FAIL"):
    return {"name": name, "status": "PASS" if ok else level, "detail": detail}


def validate_run(spec: StrategySpec, cfg: BacktestConfig, cs: CandleSet, result: dict, quality: dict) -> list:
    tf = TF_SECONDS[spec.timeframe]
    inst_data = result["instrument"]
    from pyapp.engine.spec import InstrumentSpec
    inst = InstrumentSpec(**inst_data)
    trades = result["trades"]
    checks = []
    conv = lambda p: conversion_factor(inst, cfg.account_currency, p, cfg.conversion_rate)
    spread_at = lambda i: cs.spread[i] if (cfg.costs.use_dataset_spread and cs.spread is not None) else cfg.costs.spread

    bad = [t["seq"] for t in trades if not (t["signalIdx"] + 1 <= t["entryIdx"] <= t["exitIdx"])]
    checks.append(check("Entries occur strictly after the signal bar closes", not bad, f"violations: {bad[:10]}"))
    ordered = all(trades[k]["entryTs"] <= trades[k + 1]["entryTs"] for k in range(len(trades) - 1)) if trades else True
    checks.append(check("Trade log is chronological", ordered))
    bad = [t["seq"] for t in trades if t["exitTs"] < t["entryTs"]]
    checks.append(check("Exits never precede entries", not bad, f"violations: {bad[:10]}"))

    slip = cfg.costs.slippage
    bad = []
    for t in trades:
        i = t["entryIdx"]
        half = spread_at(i) / 2
        lo, hi = cs.l[i] - half - slip - 1e-9, cs.h[i] + half + slip + 1e-9
        if not lo <= t["entry"] <= hi:
            bad.append(t["seq"])
    checks.append(check("Entry prices lie inside the entry bar's (spread/slippage-adjusted) range", not bad, f"violations: {bad[:10]}"))
    bad = []
    for t in trades:
        i = t["exitIdx"]
        half = spread_at(i) / 2
        last = t["legs"][-1]["price"]
        if t["exitReason"] != "END_OF_DATA" and not (cs.l[i] - half - slip - 1e-9 <= last <= cs.h[i] + half + slip + 1e-9):
            bad.append(t["seq"])
    checks.append(check("Final exit prices lie inside the exit bar's range", not bad, f"violations: {bad[:10]}"))

    bad, rr_bad = [], []
    for t in trades:
        sign = 1 if t["direction"] == "LONG" else -1
        if not sign * (t["entry"] - t["stop0"]) > 0:
            bad.append(t["seq"])
        for tp in t["targets"]:
            if not sign * (tp["price"] - t["entry"]) > 0:
                bad.append(t["seq"])
            expected = sign * (tp["price"] - t["entry"]) / (sign * (t["entry"] - t["stop0"]))
            if abs(expected - tp["rr"]) > 1e-6:
                rr_bad.append(t["seq"])
    checks.append(check("Stop-loss and take-profit sit on the correct side of entry", not bad, f"violations: {sorted(set(bad))[:10]}"))
    checks.append(check("Planned R:R recomputes from entry/stop/target", not rr_bad, f"violations: {sorted(set(rr_bad))[:10]}"))

    bad, over = [], []
    for t in trades:
        steps = t["qty"] / inst.qty_step
        if abs(steps - round(steps)) > 1e-6 or not inst.min_qty - 1e-12 <= t["qty"] <= inst.max_qty + 1e-12:
            bad.append(t["seq"])
        if spec.risk.model != "fixed_qty":
            intended = {"percent_equity": None, "percent_initial": cfg.initial_capital * spec.risk.value / 100.0, "fixed_cash": spec.risk.value}[spec.risk.model]
            if intended is not None and t["riskAmount"] > intended * (1 + 1e-9):
                over.append(t["seq"])
        planned = t["qty"] * (t["plannedEntry"] - t["stop0"]) * (1 if t["direction"] == "LONG" else -1) * inst.contract_size * conv(t["plannedEntry"])
        if abs(planned - t["riskAmount"]) > max(1e-6, 1e-6 * abs(planned)):
            over.append(t["seq"])
    checks.append(check("Quantities respect lot step, minimum and maximum", not bad, f"violations: {bad[:10]}"))
    checks.append(check("Sized risk never exceeds the configured risk and recomputes from the stop distance", not over, f"violations: {sorted(set(over))[:10]}"))

    bad = []
    for t in trades:
        sign = 1 if t["direction"] == "LONG" else -1
        gross = sum((leg["price"] - t["entry"]) * sign * leg["qty"] * inst.contract_size * conv(leg["price"]) for leg in t["legs"])
        qty_sum = sum(leg["qty"] for leg in t["legs"])
        if abs(gross - t["grossPnl"]) > max(1e-6, 1e-8 * abs(gross)) or abs(qty_sum - t["qty"]) > 1e-9:
            bad.append(t["seq"])
        if abs(t["grossPnl"] - t["fees"] - t["netPnl"]) > 1e-6:
            bad.append(t["seq"])
    checks.append(check("Realised P&L recomputes independently from fills, quantities and fees", not bad, f"violations: {sorted(set(bad))[:10]}"))

    total_net = sum(t["netPnl"] for t in trades)
    drift = abs(cfg.initial_capital + total_net - result["balanceEnd"])
    checks.append(check("Ending balance reconciles with the sum of trade-level net P&L", drift < 1e-6 * max(1.0, abs(result["balanceEnd"])) + 1e-6, f"difference {drift:.8f}"))

    bad = []
    for t in trades:
        fees = 0.0
        qty = t["qty"]
        for price, q in [(t["entry"], qty)] + [(leg["price"], leg["qty"]) for leg in t["legs"]]:
            if cfg.costs.commission_model == "per_lot_side":
                fees += cfg.costs.commission_value * q
            elif cfg.costs.commission_model == "percent_notional_side":
                fees += cfg.costs.commission_value / 100.0 * q * inst.contract_size * price * conv(price)
        if cfg.costs.commission_model == "fixed_per_trade":
            fees = cfg.costs.commission_value
        if abs(fees - t["fees"]) > 1e-6 * max(1.0, fees):
            bad.append(t["seq"])
    checks.append(check("Commissions recompute from the cost model", not bad, f"violations: {bad[:10]}"))
    checks.append(check("Costs are non-negative and applied", all(t["spreadCost"] >= -1e-9 and t["slippageCost"] >= -1e-9 for t in trades)))

    bad = []
    for t in trades:
        decision = cs.t[t["signalIdx"]] + tf
        if spec.sessions.windows and not any(in_window(w, decision) for w in spec.sessions.windows):
            bad.append(t["seq"])
        if any(in_window(w, decision) for w in spec.sessions.avoid):
            bad.append(t["seq"])
        for b in cfg.news_blackouts:
            if parse_iso(b.start) <= decision < parse_iso(b.end):
                bad.append(t["seq"])
    checks.append(check("Every signal respects the session, avoid-window and news-blackout filters", not bad, f"violations: {sorted(set(bad))[:10]}"))

    events = [(t["entryIdx"], 1) for t in trades] + [(t["exitIdx"] + 1, -1) for t in trades]
    live = peak = 0
    for _, delta in sorted(events):
        live += delta
        peak = max(peak, live)
    checks.append(check("Concurrent positions never exceed the configured maximum", peak <= spec.risk.max_concurrent, f"peak {peak}"))

    equity = result["equity"]
    checks.append(check("Equity curve is finite and final equity equals the closing balance", all(math.isfinite(v) for v in equity) and abs(equity[-1] - result["balanceEnd"]) < 1e-6 * max(1.0, abs(result["balanceEnd"])) + 1e-6))

    checks.append(check("Candle series is free of duplicate, unordered or invalid bars", not (quality["duplicates"] or quality["unordered"] or quality["invalidOhlc"]),
                        f"duplicates={quality['duplicates']} unordered={quality['unordered']} invalid={quality['invalidOhlc']}"))
    if quality["unexpectedGapCount"]:
        checks.append(check("No unexpected data gaps", False, f"{quality['unexpectedGapCount']} gaps / {quality['unexpectedMissingBars']} missing bars; results over gaps carry reduced confidence.", level="WARN"))
    else:
        checks.append(check("No unexpected data gaps", True))
    if quality["coveragePct"] < 99:
        checks.append(check("Data coverage at least 99%", False, f"coverage {quality['coveragePct']}%", level="WARN"))
    if result["warnings"]:
        checks.append(check("Simulation raised no warnings", False, "; ".join(result["warnings"]), level="WARN"))
    amb = sum(1 for t in trades if t["ambiguous"])
    if amb:
        checks.append(check("Ambiguous same-bar stop/target outcomes", False, f"{amb} trades had stop and target inside one bar; conservative convention applied (policy: {cfg.same_bar_policy}).", level="WARN"))
    return checks


_KEYS = ("direction", "entryIdx", "entry", "stop0", "qty", "exitIdx", "finalExit", "exitReason", "netPnl", "grossPnl", "fees")


def _same(a, b):
    for key in _KEYS:
        x, y = a[key], b[key]
        if isinstance(x, float) or isinstance(y, float):
            if abs(x - y) > 1e-9 * max(1.0, abs(x)):
                return False
        elif x != y:
            return False
    return True


def sample_indices(count, samples):
    if samples <= 0 or count == 0:
        return []
    if count <= samples:
        return list(range(count))
    return sorted({round(k * (count - 1) / (samples - 1)) for k in range(samples)}) if samples > 1 else [count // 2]


def series_audit(sim, candles: CandleSet, cuts) -> dict:
    """Rebuild every series on truncated data; each value must equal the full-data value at the same bar."""
    from pyapp.engine.series import Bars, Factory
    from pyapp.engine.spec import SeriesDef
    definitions = dict(sim.spec.series)
    definitions.update({"__er20": SeriesDef(type="er", period=20), "__atr14": SeriesDef(type="atr", period=14)})
    seconds = sim.tf
    mismatches = []
    for cut in cuts:
        prefix = candles.prefix(cut)
        factory = Factory(Bars(prefix, seconds), sim.spec.day)
        for name, definition in definitions.items():
            rebuilt = factory.build(name, definition)
            full = sim.ctx.series[name]
            for i in range(cut):
                if rebuilt[i] != full[i]:
                    mismatches.append({"series": name, "bar": i, "cut": cut})
                    break
    return {"cuts": list(cuts), "series": len(definitions), "mismatches": mismatches, "passed": not mismatches}


def causality_audit(make_simulator, candles: CandleSet, result: dict, samples: int, sim=None) -> dict:
    """Independent lookahead checks.

    1. Every series is rebuilt on truncated data and must match the full-data value at each bar.
    2. Sampled trades are re-simulated on data truncated at the signal bar and at the exit bar; identical results prove no later bar was consulted.
    3. A guarded replay makes any read of a not-yet-closed bar raise (performed by the caller).
    """
    closed = [t for t in result["trades"] if not t.get("openAtEnd")]
    picked = sample_indices(len(closed), samples)
    failures, rows = [], []
    series = {"passed": True, "mismatches": [], "cuts": [], "series": 0}
    if sim is not None and samples > 0:
        n = len(candles)
        cuts = sorted({max(30, int(n * f)) for f in (0.35, 0.6, 0.85)})
        series = series_audit(sim, candles, [c for c in cuts if c < n])
    for k in picked:
        trade = closed[k]
        early = make_simulator(candles.prefix(trade["signalIdx"] + 1)).run()
        order = next((o for o in early["orders"] if o["createdIdx"] == trade["signalIdx"] and o["direction"] == trade["direction"]), None)
        decision_ok = bool(order and abs(order["stop"] - trade["stop0"]) < 1e-12 and abs(order["qty"] - trade["qty"]) < 1e-12)
        late = make_simulator(candles.prefix(trade["exitIdx"] + 1)).run()
        match = next((t for t in late["trades"] if t["signalIdx"] == trade["signalIdx"] and t["direction"] == trade["direction"]), None)
        exit_ok = bool(match and not match.get("openAtEnd") and _same(match, trade))
        rows.append({"trade": trade["seq"], "decisionReproducedFromPastOnly": decision_ok, "tradeReproducedWithoutFutureBars": exit_ok})
        if not (decision_ok and exit_ok):
            failures.append(trade["seq"])
    return {"method": "Series rebuilt on truncated data must match the full-data value at every bar; sampled trades are re-simulated on data truncated at the signal and exit bars; a guarded replay raises on any read of a future bar.",
            "sampled": len(picked), "totalClosedTrades": len(closed), "failures": failures, "rows": rows, "seriesAudit": series,
            "passed": not failures and series["passed"]}
