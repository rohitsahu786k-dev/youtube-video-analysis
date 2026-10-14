"""Deterministic causal bar replay shared with live setup evaluation."""
import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from pyapp.market import TIMEFRAMES

class Rule(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    kind: Literal["sweep_rejection", "breakout"]
    direction: Literal["LONG", "SHORT"]
    lookback: int = Field(ge=2, le=200)
    rewardRisk: float = Field(gt=0, le=20)
    stopBuffer: float = Field(ge=0, le=100)
    maxHoldingBars: int = Field(ge=1, le=1000)

def rule_hash(strategy):
    fields = {key: strategy[key] for key in ("rule", "symbol", "timeframe", "sourceTradeIds", "evidenceCutoff")}
    fields["sourceEvidenceHash"] = strategy.get("sourceEvidenceHash")
    fields["sourceKnowledgeIds"] = strategy.get("sourceKnowledgeIds", [])
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()

def setup_at(candles, index, rule):
    if index < rule["lookback"]:
        return None
    history = candles[index-rule["lookback"]:index]
    bar = candles[index]
    high, low = max(c["high"] for c in history), min(c["low"] for c in history)
    long = rule["direction"] == "LONG"
    if rule["kind"] == "sweep_rejection":
        triggered = (bar["low"] < low and bar["close"] > low) if long else (bar["high"] > high and bar["close"] < high)
    else:
        triggered = bar["close"] > high if long else bar["close"] < low
    if not triggered:
        return None
    stop = bar["low"]-rule["stopBuffer"] if long else bar["high"]+rule["stopBuffer"]
    if stop <= 0:
        return None
    return {"direction": rule["direction"], "stop": stop, "signalTime": bar["time"],
            "reason": f"{rule['kind']} versus previous {rule['lookback']} completed bars"}

def replay(candles, rule, spread, slippage, start=0):
    trades = []
    index = max(start, rule["lookback"])
    while index < len(candles)-1:
        setup = setup_at(candles, index, rule)
        if not setup:
            index += 1
            continue
        sign = 1 if rule["direction"] == "LONG" else -1
        # Decisions use only bar i and earlier; execution begins at i+1 open.
        entry_index = index+1
        entry = candles[entry_index]["open"] + sign*(spread/2+slippage)
        stop = setup["stop"]
        risk = sign*(entry-stop)
        if risk <= 0:
            index += 1
            continue
        target = entry+sign*risk*rule["rewardRisk"]
        if target <= 0:
            index += 1
            continue
        last = min(len(candles)-1, entry_index+rule["maxHoldingBars"]-1)
        exit_price, reason, ambiguous = None, "", False
        for cursor in range(entry_index, last+1):
            bar = candles[cursor]
            # Mid OHLC converted to executable bid for longs, ask for shorts.
            open_price, low, high = (bar[key]-sign*spread/2 for key in ("open", "low", "high"))
            stop_hit = low <= stop if sign == 1 else high >= stop
            target_hit = high >= target if sign == 1 else low <= target
            if sign*(open_price-stop) <= 0:
                exit_price, reason = open_price-sign*slippage, "STOP_GAP"
            elif sign*(open_price-target) >= 0:
                exit_price, reason = target-sign*slippage, "TARGET"
            elif stop_hit:
                exit_price, reason, ambiguous = stop-sign*slippage, "STOP", target_hit
            elif target_hit:
                exit_price, reason = target-sign*slippage, "TARGET"
            if exit_price is not None:
                break
        if exit_price is None:
            cursor = last
            exit_price = candles[last]["close"]-sign*(spread/2+slippage)
            reason = "TIME_EXIT" if last-entry_index+1 == rule["maxHoldingBars"] else "END_OF_DATA"
        trades.append({"signalTime": candles[index]["time"], "entryTime": candles[entry_index]["time"],
            "exitTime": candles[cursor]["time"], "entry": entry, "stop": stop, "target": target,
            "exit": exit_price, "r": round(sign*(exit_price-entry)/risk, 6), "reason": reason,
            "ambiguous": ambiguous, "direction": rule["direction"]})
        index = cursor+1
    return trades

def metrics(trades):
    closed = [t for t in trades if t["reason"] != "END_OF_DATA"]
    returns = [t["r"] for t in closed]
    gains = sum(r for r in returns if r > 0)
    losses = -sum(r for r in returns if r < 0)
    balance = peak = drawdown = 0
    for value in returns:
        balance += value
        peak = max(peak, balance)
        drawdown = max(drawdown, peak-balance)
    return {"trades": len(returns), "openAtEnd": len(trades)-len(closed),
        "winRate": round(100*sum(r > 0 for r in returns)/len(returns), 2) if returns else None,
        "expectancyR": round(sum(returns)/len(returns), 4) if returns else None,
        "netR": round(sum(returns), 4), "profitFactor": round(gains/losses, 4) if losses else None,
        "maxDrawdownR": round(drawdown, 4), "ambiguousBars": sum(t["ambiguous"] for t in closed)}

def run_backtest(dataset, strategy, spread, slippage, min_holdout=30):
    candles = dataset["candles"]
    split = int(len(candles)*0.7)
    cutoff = strategy["evidenceCutoff"]
    after_evidence = next((i for i, c in enumerate(candles) if cutoff and datetime.fromisoformat(c["time"]) > datetime.fromisoformat(cutoff)), len(candles))
    holdout_start = max(split, after_evidence)
    train = replay(candles[:split], strategy["rule"], spread, slippage)
    holdout = replay(candles, strategy["rule"], spread, slippage, holdout_start)
    result = metrics(holdout)
    eligible = bool(strategy.get("reviewed") and cutoff and result["trades"] >= min_holdout
                    and (result["expectancyR"] or 0) > 0 and result["netR"] > 0 and spread > 0
                    and dataset.get("provider") == "OANDA" and dataset.get("source") == "oanda" and dataset.get("environment") in ("practice", "live") and dataset.get("priceBasis") == "mid")
    return {"strategyId": strategy["id"], "strategyHash": rule_hash(strategy), "datasetId": dataset["id"],
        "provider": dataset["provider"], "providerEnvironment": dataset.get("environment"), "symbol": dataset["symbol"], "timeframe": dataset["timeframe"],
        "spread": spread, "slippage": slippage, "training": metrics(train), "holdout": result,
        "trainingEnd": candles[split-1]["time"], "holdoutStart": candles[holdout_start]["time"] if holdout_start < len(candles) else None,
        "dataEnd": candles[-1]["time"], "trades": holdout, "eligibleForLive": eligible,
        "method": "Fixed rules, 70/30 chronological split, post-video holdout only, next-bar execution, one position, stop-first ambiguous bars, costs in absolute price units.",
        "limitations": "OHLC replay cannot reconstruct intrabar order, variable spreads or liquidity. This test is an approximation and does not establish future profitability."}

def live_candidate(snapshot, strategy, report, min_holdout, now=None):
    now = now or datetime.now(timezone.utc)
    def wait(reason):
        return {"status": "WAIT", "reason": reason}
    if not strategy.get("reviewed") or not report or not report.get("eligibleForLive"):
        return wait("Review the rules and obtain a qualifying independent holdout backtest first.")
    if report.get("strategyHash") != rule_hash(strategy) or report.get("holdout", {}).get("trades", 0) < min_holdout:
        return wait("Rules changed or holdout sample is insufficient; rerun the backtest.")
    if any(snapshot.get(key) != strategy.get(key) for key in ("symbol", "timeframe")) or report.get("provider") != snapshot.get("provider"):
        return wait("Chart, strategy and backtest market-data source must match.")
    if report.get("providerEnvironment") != snapshot.get("environment"):
        return wait("OANDA environment changed; rerun the backtest on the matching feed.")
    if not 0 <= (now-datetime.fromisoformat(report["dataEnd"])).total_seconds() <= 30*86400:
        return wait("Backtest data is stale or has a future timestamp; refresh it.")
    quote_time = datetime.fromisoformat(snapshot["time"].replace("Z", "+00:00"))
    if not 0 <= (now-quote_time).total_seconds() <= 60:
        return wait("Market quote is stale or has a future timestamp.")
    candles = snapshot["candles"]
    if len(candles) <= strategy["rule"]["lookback"]:
        return wait("Insufficient completed candles.")
    candle_time = datetime.fromisoformat(candles[-1]["time"])
    seconds = TIMEFRAMES[strategy["timeframe"]]
    age = (now-candle_time).total_seconds()
    if not seconds <= age <= seconds+60:
        return wait("The latest completed setup is stale; wait for the next bar close.")
    spread = snapshot["ask"]-snapshot["bid"]
    if spread <= 0 or spread > report["spread"]:
        return wait("Current spread exceeds the tested cost assumption.")
    candidate = setup_at(candles, len(candles)-1, strategy["rule"])
    if not candidate:
        return wait("No matching setup on the latest completed candle.")
    sign = 1 if candidate["direction"] == "LONG" else -1
    entry = snapshot["ask"] if sign == 1 else snapshot["bid"]
    risk = sign*(entry-candidate["stop"])
    if risk <= 0 or abs(entry-candles[-1]["close"]) > risk*0.25:
        return wait("Setup is invalidated or price has moved too far from its trigger.")
    target = entry+sign*risk*strategy["rule"]["rewardRisk"]
    if target <= 0:
        return wait("Invalid target price.")
    return {**candidate, "status": "PAPER_SETUP", "entry": entry, "target": target,
        "quoteTime": snapshot["time"], "provider": snapshot["provider"], "strategyId": strategy["id"],
        "backtestId": report["id"], "expiresAt": quote_time.timestamp()+60,
        "limitations": "Research suggestion only; no broker order is submitted. Chart feed can differ from the executable quote."}
