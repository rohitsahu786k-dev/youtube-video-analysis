"""Trade journal rows and plain-language explanations generated only from simulation records."""
from pyapp.engine.clock import fmt
from pyapp.engine.spec import StrategySpec, BacktestConfig

JOURNAL_COLUMNS = [
    ("tradeId", "Trade ID"), ("backtestId", "Backtest ID"), ("strategy", "Strategy"), ("strategyVersion", "Strategy Version"),
    ("symbol", "Symbol"), ("exchange", "Exchange / Data Source"), ("direction", "Direction"), ("timeframe", "Timeframe"),
    ("entryTimeUtc", "Entry Time (UTC)"), ("entryTimeLocal", "Entry Time (Display TZ)"), ("exitTimeUtc", "Exit Time (UTC)"), ("exitTimeLocal", "Exit Time (Display TZ)"),
    ("timezone", "Timezone"), ("session", "Session"), ("htfBias", "Higher-Timeframe Bias"), ("setup", "Setup Name"), ("confirmation", "Confirmation (conditions met)"),
    ("triggerLogic", "Trigger Logic"), ("orderType", "Order Type"), ("plannedEntry", "Planned Entry"), ("actualEntry", "Actual Entry"),
    ("initialSl", "Initial SL"), ("slDistance", "SL Distance"), ("positionSize", "Position Size"), ("amountRisked", "Amount Risked"), ("actualAmountRisked", "Actual Risk at Fill"),
    ("plannedTp", "Planned TP"), ("tpDistance", "TP Distance"), ("plannedRR", "Planned R:R"),
    ("slChanges", "SL Changes"), ("breakeven", "Break-even"), ("trailing", "Trailing"), ("partialExits", "Partial Exits"), ("scaling", "Scaling"),
    ("finalExit", "Final Exit Price"), ("avgExit", "Average Exit Price"), ("exitReason", "Exit Reason"),
    ("grossPnl", "Gross P&L"), ("fees", "Fees"), ("netPnl", "Net P&L"), ("rMultiple", "Realized R"),
    ("result", "Result"), ("holdingTime", "Holding Time"), ("ambiguous", "Ambiguous Same-Bar"),
    ("spreadCost", "Spread Cost"), ("slippageCost", "Slippage Cost"), ("regime", "Market Regime"), ("chart", "Chart Evidence"), ("eventLog", "Event Log Reference"),
    ("reasonForTrade", "Reason for Trade"), ("slTpLogic", "SL/TP Logic"), ("exceptions", "Exceptions"), ("observations", "Observations"),
]


def describe_stop(spec: StrategySpec):
    s = spec.stop
    if s.type == "atr":
        return f"{s.mult} x {s.series} below/above the entry reference"
    if s.type == "points":
        return f"fixed {s.value} price units from the entry reference"
    if s.type == "percent":
        return f"{s.value}% from the entry reference"
    if s.type == "signal_candle":
        return f"beyond the signal candle extreme (buffer {s.buffer})"
    return f"at the level of {s.ref!s} (buffer {s.buffer})"


def describe_targets(spec: StrategySpec):
    parts = []
    for t in spec.targets:
        base = f"{t.value}R" if t.type == "rr" else f"{t.value} price units" if t.type == "points" else f"{t.mult} x {t.series}" if t.type == "atr" else f"level {t.ref!s}"
        parts.append(f"{int(t.portion * 100)}% at {base}")
    return "; ".join(parts) or "no fixed target (exit by stop, trailing, time or exit rule)"


def hold_text(seconds):
    hours = seconds / 3600
    return f"{hours:.2f} h" if hours < 48 else f"{hours / 24:.2f} d"


def explain_trade(trade, spec: StrategySpec, cfg: BacktestConfig, instrument) -> list[str]:
    """Narrative built strictly from the strategy conditions and simulation events of this trade."""
    tz = cfg.display_tz
    sign = 1 if trade["direction"] == "LONG" else -1
    unit = "pips" if instrument.get("pip_size") else "price units"
    pip = instrument.get("pip_size") or 1.0
    lines = []
    lines.append(f"Context: {trade['direction']} {trade['symbol']} on {spec.timeframe}. Higher-timeframe bias: {trade['bias']}. Regime at signal: {trade['regime']['trend']}, {trade['regime']['volatility']}. Session: {trade['session']}.")
    cond = trade["conditions"] or ["the entry condition was satisfied"]
    lines.append("Preceded by / rules satisfied at the signal bar close (" + trade["events"][0]["time"] + "): " + "; ".join(cond) + ".")
    entry_kind = {"open": "at the open of the next bar", "intrabar_limit": "when price traded to the limit level inside the bar", "intrabar_stop": "when price traded through the stop-entry level inside the bar"}[trade["entryKind"]]
    lines.append(f"Entry: a {trade['orderType']} order was placed with planned price {trade['plannedEntry']:.6g} and filled {entry_kind} at {trade['entry']:.6g} ({fmt(trade['entryTs'], tz)}); fill includes the configured spread/slippage.")
    lines.append(f"Stop-loss: {trade['stop0']:.6g}, {describe_stop(spec)}; distance {trade['stopDistance']:.6g} ({trade['stopDistance'] / pip:.1f} {unit}).")
    lines.append(f"Position size: {trade['qty']:g} units/lots using risk model '{spec.risk.model}' ({spec.risk.value}): planned risk {trade['riskAmount']:.2f} {cfg.account_currency}, risk at the actual fill {trade['actualRiskAmount']:.2f}.")
    if trade["targets"]:
        tps = ", ".join(f"TP{t['idx']} {t['price']:.6g} ({t['rr']:.2f}R, qty {t['qty']:g}{', filled' if t['filled'] else ''})" for t in trade["targets"])
        lines.append(f"Take-profit plan: {describe_targets(spec)} -> {tps}. Weighted planned R:R {trade['plannedRR']:.2f}.")
    else:
        lines.append("Take-profit plan: none fixed; the position was managed by stop, trailing, time or exit rules.")
    managed = [e for e in trade["events"] if e["type"] in ("SL_MOVED", "PARTIAL_EXIT", "AMBIGUOUS_BAR")]
    if managed:
        lines.append("Management: " + "; ".join(f"{e['time']} {e['detail']}" + (f" @ {e['price']:.6g}" if e.get("price") else "") for e in managed) + ".")
    else:
        lines.append("Management: none; the initial stop and target stayed unchanged.")
    verdict = {"WIN": "a win", "LOSS": "a loss", "BREAKEVEN": "break-even"}[trade["result"]]
    lines.append(f"Exit: {trade['exitReason']} at {trade['finalExit']:.6g} on {fmt(trade['exitTs'], tz)}. Result: {verdict}; gross {trade['grossPnl']:.2f}, fees {trade['fees']:.2f}, net {trade['netPnl']:.2f} {cfg.account_currency} = {trade['rMultiple']:.2f}R" if trade["rMultiple"] is not None
                 else f"Exit: {trade['exitReason']} at {trade['finalExit']:.6g}. Net {trade['netPnl']:.2f}.")
    lines[-1] += f". Held {hold_text(trade['holdSeconds'])}. Costs vs zero-cost fills: spread {trade['spreadCost']:.2f}, slippage {trade['slippageCost']:.2f}."
    if trade["ambiguous"]:
        lines.append("Caution: a single candle contained both the stop and a target; the conservative stop-first convention was applied, so this outcome is a lower bound.")
    if trade.get("openAtEnd"):
        lines.append("Caution: the data ended while this position was open; it is excluded from closed-trade statistics.")
    return lines


def journal_rows(report: dict, trades: list, spec: StrategySpec, cfg: BacktestConfig, version_label: str, base_url: str = ""):
    inst = report["instrument"]
    tz = cfg.display_tz
    rows = []
    for t in trades:
        text = explain_trade(t, spec, cfg, inst)
        rows.append({
            "tradeId": t["id"], "backtestId": report["id"], "strategy": spec.name, "strategyVersion": version_label,
            "symbol": t["symbol"], "exchange": report.get("provider") or "", "direction": t["direction"], "timeframe": spec.timeframe,
            "entryTimeUtc": t["entryTime"], "entryTimeLocal": fmt(t["entryTs"], tz), "exitTimeUtc": t["exitTime"], "exitTimeLocal": fmt(t["exitTs"], tz),
            "timezone": tz, "session": t["session"], "htfBias": t["bias"], "setup": t["setup"], "confirmation": " | ".join(t["conditions"]),
            "triggerLogic": text[1], "orderType": t["orderType"], "plannedEntry": t["plannedEntry"], "actualEntry": t["entry"],
            "initialSl": t["stop0"], "slDistance": t["stopDistance"], "positionSize": t["qty"], "amountRisked": t["riskAmount"], "actualAmountRisked": t["actualRiskAmount"],
            "plannedTp": ", ".join(f"TP{x['idx']}={x['price']:.6g}" for x in t["targets"]) or "none", "tpDistance": t["tpDistance"], "plannedRR": t["plannedRR"],
            "slChanges": " | ".join(f"{c['time']} {c['detail']}" for c in t["slChanges"]) or "none", "breakeven": "yes" if t["breakeven"] else "no",
            "trailing": "yes" if t["trailing"] else "no", "partialExits": " | ".join(f"{p['time']} {p['qty']:g}@{p['price']:.6g} ({p['reason']})" for p in t["partialExits"]) or "none",
            "scaling": "not used (each entry is a separate trade)", "finalExit": t["finalExit"], "avgExit": t["exit"], "exitReason": t["exitReason"],
            "grossPnl": round(t["grossPnl"], 2), "fees": round(t["fees"], 2), "netPnl": round(t["netPnl"], 2), "rMultiple": None if t["rMultiple"] is None else round(t["rMultiple"], 3),
            "result": t["result"] + (" (open at end)" if t.get("openAtEnd") else ""), "holdingTime": hold_text(t["holdSeconds"]), "ambiguous": "yes" if t["ambiguous"] else "no",
            "spreadCost": round(t["spreadCost"], 2), "slippageCost": round(t["slippageCost"], 2), "regime": f"{t['regime']['trend']} / {t['regime']['volatility']}",
            "chart": f"{base_url}/api/backtests/{report['id']}/trades/{t['id']}/chart.svg", "eventLog": f"{base_url}/api/backtests/{report['id']}/trades/{t['id']}",
            "reasonForTrade": text[1], "slTpLogic": text[3] + " " + text[5], "exceptions": text[-1] if (t["ambiguous"] or t.get("openAtEnd")) else "none",
            "observations": " ".join(text),
        })
    return rows
