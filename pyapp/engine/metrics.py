"""Performance analytics computed only from the trade ledger and the per-bar mark-to-market equity curve."""
import math
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CLOSED = lambda t: not t.get("openAtEnd")


def _mean(values):
    return sum(values) / len(values) if values else None


def _std(values, sample=True):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - (1 if sample else 0)))


def _round(value, digits=4):
    return None if value is None or (isinstance(value, float) and not math.isfinite(value)) else round(value, digits)


def streaks(results):
    best_win = best_loss = cur_win = cur_loss = 0
    for r in results:
        if r == "WIN":
            cur_win, cur_loss = cur_win + 1, 0
        elif r == "LOSS":
            cur_loss, cur_win = cur_loss + 1, 0
        else:
            cur_win = cur_loss = 0
        best_win, best_loss = max(best_win, cur_win), max(best_loss, cur_loss)
    return best_win, best_loss


def drawdown(curve):
    """(max drawdown amount, max drawdown percent of peak, longest underwater bars)."""
    peak, worst, worst_pct, run, longest = None, 0.0, 0.0, 0, 0
    for v in curve:
        if peak is None or v >= peak:
            peak, run = v, 0
        else:
            run += 1
            longest = max(longest, run)
            worst = max(worst, peak - v)
            worst_pct = max(worst_pct, (peak - v) / peak * 100 if peak > 0 else 0.0)
    return worst, worst_pct, longest


def trade_metrics(trades, initial_capital=None, equity=None, bars_per_trade_year=None):
    """Core metrics (SRS 14.1). Trades still open at the end of data are excluded from every closed-trade figure."""
    closed = [t for t in trades if CLOSED(t)]
    wins = [t for t in closed if t["result"] == "WIN"]
    losses = [t for t in closed if t["result"] == "LOSS"]
    even = [t for t in closed if t["result"] == "BREAKEVEN"]
    gross_profit = sum(t["netPnl"] for t in wins)
    gross_loss = -sum(t["netPnl"] for t in losses)
    net = sum(t["netPnl"] for t in closed)
    r_values = [t["rMultiple"] for t in closed if t["rMultiple"] is not None]
    n = len(closed)
    best_win, best_loss = streaks([t["result"] for t in sorted(closed, key=lambda t: t["exitTs"])])
    balance, peak, dd_closed, dd_closed_pct = (initial_capital or 0.0), (initial_capital or 0.0), 0.0, 0.0
    for t in sorted(closed, key=lambda t: t["exitTs"]):
        balance += t["netPnl"]
        peak = max(peak, balance)
        dd_closed = max(dd_closed, peak - balance)
        dd_closed_pct = max(dd_closed_pct, (peak - balance) / peak * 100 if peak > 0 else 0.0)
    out = {
        "totalTrades": n, "wins": len(wins), "losses": len(losses), "breakeven": len(even), "openAtEnd": len(trades) - n,
        "winRate": _round(100 * len(wins) / n, 2) if n else None, "lossRate": _round(100 * len(losses) / n, 2) if n else None,
        "grossProfit": _round(gross_profit, 2), "grossLoss": _round(gross_loss, 2), "netPnl": _round(net, 2),
        "profitFactor": _round(gross_profit / gross_loss) if gross_loss > 0 else None,
        "expectancy": _round(net / n, 2) if n else None, "expectancyR": _round(_mean(r_values)),
        "avgWin": _round(gross_profit / len(wins), 2) if wins else None, "avgLoss": _round(-gross_loss / len(losses), 2) if losses else None,
        "payoffRatio": _round((gross_profit / len(wins)) / (gross_loss / len(losses))) if wins and losses and gross_loss > 0 else None,
        "avgPlannedRR": _round(_mean([t["plannedRR"] for t in closed if t["plannedRR"] is not None])),
        "avgRealizedR": _round(_mean(r_values)), "totalR": _round(sum(r_values)),
        "sqn": _round(math.sqrt(len(r_values)) * _mean(r_values) / _std(r_values)) if len(r_values) > 2 and _std(r_values) else None,
        "maxConsecutiveWins": best_win, "maxConsecutiveLosses": best_loss,
        "largestWin": _round(max((t["netPnl"] for t in closed), default=None), 2) if wins else None,
        "largestLoss": _round(min((t["netPnl"] for t in closed), default=None), 2) if losses else None,
        "avgHoldingHours": _round(_mean([t["holdSeconds"] for t in closed]) / 3600) if closed else None,
        "totalCommission": _round(sum(t["fees"] for t in closed), 2), "totalSpreadCost": _round(sum(t["spreadCost"] for t in closed), 2),
        "totalSlippageCost": _round(sum(t["slippageCost"] for t in closed), 2),
        "totalCosts": _round(sum(t["fees"] + t["spreadCost"] + t["slippageCost"] for t in closed), 2),
        "idealNetPnlBeforeCosts": _round(sum(t["idealPnl"] for t in closed), 2),
        "ambiguousTrades": sum(1 for t in closed if t["ambiguous"]),
        "maxDrawdownClosedTrades": _round(dd_closed, 2), "maxDrawdownClosedTradesPct": _round(dd_closed_pct, 2),
    }
    if initial_capital:
        out["totalReturnPct"] = _round(100 * net / initial_capital, 2)
    if equity:
        dd, dd_pct, underwater = drawdown(equity)
        out.update(maxDrawdown=_round(dd, 2), maxDrawdownPct=_round(dd_pct, 2), longestUnderwaterBars=underwater,
                   recoveryFactor=_round(net / dd) if dd > 0 else None)
    return out


def daily_stats(times, equity, initial_capital, annualization=252):
    """Sharpe/Sortino/Calmar on end-of-day marked equity (UTC days). Reported only with enough days."""
    by_day = {}
    for ts, eq in zip(times, equity):
        by_day[ts // 86400] = eq
    values = [initial_capital] + [by_day[k] for k in sorted(by_day)]
    returns = [(values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values)) if values[i - 1] > 0]
    out = {"days": len(returns), "sharpe": None, "sortino": None, "calmar": None, "cagrPct": None, "annualizedVolPct": None}
    if len(returns) < 30:
        out["note"] = "Fewer than 30 daily observations; risk-adjusted ratios are not meaningful."
        return out
    mean, sd = _mean(returns), _std(returns)
    downside = math.sqrt(sum(min(r, 0) ** 2 for r in returns) / len(returns))
    years = len(returns) / annualization
    total = values[-1] / values[0]
    cagr = (total ** (1 / years) - 1) if years > 0 and total > 0 else None
    dd = drawdown(values)[1]
    out.update(sharpe=_round(mean / sd * math.sqrt(annualization)) if sd else None,
               sortino=_round(mean / downside * math.sqrt(annualization)) if downside else None,
               cagrPct=_round(100 * cagr, 2) if cagr is not None else None,
               calmar=_round(100 * cagr / dd) if cagr is not None and dd > 0 else None,
               annualizedVolPct=_round(100 * sd * math.sqrt(annualization), 2) if sd else None)
    return out


def _group(trades, keyfn):
    groups = defaultdict(list)
    for t in trades:
        groups[keyfn(t)].append(t)
    rows = []
    for key, items in groups.items():
        items = [t for t in items if CLOSED(t)]
        if not items:
            continue
        m = trade_metrics(items)
        rows.append({"key": key, "trades": m["totalTrades"], "winRate": m["winRate"], "netPnl": m["netPnl"], "profitFactor": m["profitFactor"],
                     "expectancyR": m["expectancyR"], "avgRealizedR": m["avgRealizedR"], "avgWin": m["avgWin"], "avgLoss": m["avgLoss"],
                     "totalCosts": m["totalCosts"]})
    return sorted(rows, key=lambda r: str(r["key"]))


def rr_bucket(rr):
    if rr is None:
        return "no target"
    return "<1R" if rr < 1 else "1-2R" if rr < 2 else "2-3R" if rr < 3 else "3R+"


def segments(trades, display_tz="UTC"):
    """Segmented analysis (SRS 14.2)."""
    zone = ZoneInfo(display_tz)
    local = lambda ts: datetime.fromtimestamp(ts, zone)
    return {
        "instrument": _group(trades, lambda t: t["symbol"]),
        "direction": _group(trades, lambda t: t["direction"]),
        "session": _group(trades, lambda t: t["session"]),
        "hour": _group(trades, lambda t: f"{local(t['entryTs']).hour:02d}:00"),
        "weekday": _group(trades, lambda t: ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][local(t["entryTs"]).weekday()]),
        "month": _group(trades, lambda t: local(t["entryTs"]).strftime("%Y-%m")),
        "setup": _group(trades, lambda t: t["setup"]),
        "regime": _group(trades, lambda t: f"{t['regime']['trend']} / {t['regime']['volatility']}"),
        "rrCategory": _group(trades, lambda t: rr_bucket(t["plannedRR"])),
        "exitReason": _group(trades, lambda t: t["exitReason"]),
        "displayTimezone": display_tz,
    }


def downsample(times, values, limit=1500):
    """Min/max decimation keeps every drawdown trough and peak visible."""
    n = len(values)
    if n <= limit:
        return [[times[i], values[i]] for i in range(n)]
    step = n / (limit // 2)
    out, i = [], 0.0
    while int(i) < n:
        lo, hi = int(i), min(n, int(i + step))
        window = range(lo, max(hi, lo + 1))
        a = min(window, key=lambda k: values[k])
        b = max(window, key=lambda k: values[k])
        for k in sorted({a, b}):
            out.append([times[k], values[k]])
        i += step
    if out[-1][0] != times[-1]:
        out.append([times[-1], values[-1]])
    return out


def histogram(values, bins=20):
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [{"from": lo, "to": hi, "count": len(values)}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    return [{"from": _round(lo + k * width, 4), "to": _round(lo + (k + 1) * width, 4), "count": c} for k, c in enumerate(counts)]


def monthly_pnl(trades):
    rows = defaultdict(float)
    for t in trades:
        if CLOSED(t):
            rows[datetime.fromtimestamp(t["exitTs"], timezone.utc).strftime("%Y-%m")] += t["netPnl"]
    return [{"month": k, "netPnl": _round(v, 2)} for k, v in sorted(rows.items())]


def exposure(trades, total_bars):
    held = sum(t["holdBars"] for t in trades if CLOSED(t))
    return _round(100 * held / total_bars, 2) if total_bars else None
