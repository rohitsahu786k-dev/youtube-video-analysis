"""Dependency-free SVG candlestick evidence for a trade: entry, stop, targets, exit and the setup window."""
from datetime import datetime, timezone
from html import escape

from pyapp.engine.data import CandleSet


def trade_chart_svg(candles: CandleSet, trade: dict, tf_seconds: int, symbol: str, timeframe: str, before=40, after=12, width=980, height=460) -> str:
    lo_i = max(0, trade["signalIdx"] - before)
    hi_i = min(len(candles) - 1, trade["exitIdx"] + after)
    if hi_i - lo_i > 260:
        lo_i = max(lo_i, hi_i - 260)
    idx = range(lo_i, hi_i + 1)
    levels = [trade["entry"], trade["stop0"]] + [t["price"] for t in trade["targets"]] + [trade["finalExit"]]
    high = max(max(candles.h[i] for i in idx), max(levels))
    low = min(min(candles.l[i] for i in idx), min(levels))
    pad = (high - low) * 0.06 or high * 0.001
    high, low = high + pad, low - pad
    left, right, top, bottom = 70, 150, 34, 38
    plot_w, plot_h = width - left - right, height - top - bottom
    count = len(idx)
    step = plot_w / count
    body_w = max(1.5, step * 0.62)
    y = lambda price: top + (high - price) / (high - low) * plot_h
    x = lambda i: left + (i - lo_i + 0.5) * step
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="Trade {escape(str(trade.get("id", trade["seq"])))} chart evidence" font-family="system-ui,Segoe UI,Arial,sans-serif" font-size="11">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<text x="{left}" y="20" font-size="13" font-weight="600" fill="#0f172a">{escape(symbol)} {escape(timeframe)} - {trade["direction"]} - {escape(trade["setup"])} ({trade["result"]}, {trade["netPnl"]:+.2f})</text>']
    for k in range(6):
        price = low + (high - low) * k / 5
        out.append(f'<line x1="{left}" y1="{y(price):.1f}" x2="{width - right}" y2="{y(price):.1f}" stroke="#e2e8f0"/>')
        out.append(f'<text x="{left - 6}" y="{y(price) + 4:.1f}" text-anchor="end" fill="#64748b">{price:.5g}</text>')
    marks = max(1, count // 6)
    for n, i in enumerate(idx):
        if n % marks == 0:
            label = datetime.fromtimestamp(candles.t[i], timezone.utc).strftime("%m-%d %H:%M")
            out.append(f'<text x="{x(i):.1f}" y="{height - 14}" text-anchor="middle" fill="#64748b">{label}</text>')
    for i in idx:
        up = candles.c[i] >= candles.o[i]
        color = "#059669" if up else "#dc2626"
        top_body, bot_body = y(max(candles.o[i], candles.c[i])), y(min(candles.o[i], candles.c[i]))
        out.append(f'<line x1="{x(i):.1f}" y1="{y(candles.h[i]):.1f}" x2="{x(i):.1f}" y2="{y(candles.l[i]):.1f}" stroke="{color}"/>')
        out.append(f'<rect x="{x(i) - body_w / 2:.1f}" y="{top_body:.1f}" width="{body_w:.1f}" height="{max(1.0, bot_body - top_body):.1f}" fill="{color}"/>')
    def hline(price, color, label, dash=""):
        yy = y(price)
        out.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width - right}" y2="{yy:.1f}" stroke="{color}" stroke-width="1.3" stroke-dasharray="{dash}"/>')
        out.append(f'<text x="{width - right + 6}" y="{yy + 4:.1f}" fill="{color}" font-weight="600">{escape(label)} {price:.6g}</text>')
    hline(trade["entry"], "#2563eb", "Entry")
    hline(trade["stop0"], "#dc2626", "SL", "5 3")
    for t in trade["targets"]:
        hline(t["price"], "#059669", f"TP{t['idx']}", "5 3")
    sig_x, ent_x, exit_x = x(trade["signalIdx"]), x(trade["entryIdx"]), x(trade["exitIdx"])
    out.append(f'<line x1="{sig_x:.1f}" y1="{top}" x2="{sig_x:.1f}" y2="{top + plot_h}" stroke="#7c3aed" stroke-dasharray="2 3"/>')
    out.append(f'<text x="{sig_x:.1f}" y="{top - 4}" text-anchor="middle" fill="#7c3aed">signal</text>')
    out.append(f'<polygon points="{ent_x:.1f},{y(trade["entry"]) - 9:.1f} {ent_x - 6:.1f},{y(trade["entry"]) + 3:.1f} {ent_x + 6:.1f},{y(trade["entry"]) + 3:.1f}" fill="#2563eb"/>')
    out.append(f'<circle cx="{exit_x:.1f}" cy="{y(trade["finalExit"]):.1f}" r="5" fill="none" stroke="#0f172a" stroke-width="2"/>')
    out.append(f'<text x="{exit_x:.1f}" y="{y(trade["finalExit"]) - 9:.1f}" text-anchor="middle" fill="#0f172a">exit {escape(trade["exitReason"])}</text>')
    out.append("</svg>")
    return "".join(out)
