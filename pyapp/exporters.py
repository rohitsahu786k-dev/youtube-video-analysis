"""CSV and Excel journal exports. One row per simulated trade; aggregates are formulas so they reconcile live."""
import csv
import io
import json
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from pyapp.engine.journal import JOURNAL_COLUMNS

HEADER_FILL = PatternFill("solid", fgColor="1E293B")
WIN_FILL, LOSS_FILL, WARN_FILL = PatternFill("solid", fgColor="DCFCE7"), PatternFill("solid", fgColor="FEE2E2"), PatternFill("solid", fgColor="FEF3C7")
THIN = Side(style="thin", color="CBD5E1")


def safe(value):
    """Neutralise spreadsheet formula injection in text that originated from videos or AI output."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r") and not _is_number(value):
        return "'" + value
    return value


def _is_number(text):
    try:
        float(text)
        return True
    except ValueError:
        return False


def trades_csv(rows: list[dict]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _, label in JOURNAL_COLUMNS])
    for row in rows:
        writer.writerow([safe(row.get(key, "")) if row.get(key) is not None else "" for key, _ in JOURNAL_COLUMNS])
    return ("﻿" + out.getvalue()).encode("utf-8")


def summary_pairs(report: dict) -> list[tuple[str, object]]:
    m = report["metrics"]
    keys = [("Total trades", "totalTrades"), ("Wins", "wins"), ("Losses", "losses"), ("Break-even", "breakeven"), ("Open at end (excluded)", "openAtEnd"), ("Win rate %", "winRate"), ("Loss rate %", "lossRate"),
            ("Gross profit", "grossProfit"), ("Gross loss", "grossLoss"), ("Net P&L", "netPnl"), ("Total return %", "totalReturnPct"), ("Profit factor", "profitFactor"), ("Expectancy per trade", "expectancy"),
            ("Expectancy (R)", "expectancyR"), ("Average winner", "avgWin"), ("Average loser", "avgLoss"), ("Payoff ratio", "payoffRatio"), ("Average planned R:R", "avgPlannedRR"),
            ("Average realised R", "avgRealizedR"), ("Total R", "totalR"), ("SQN", "sqn"), ("Max drawdown (mark-to-market)", "maxDrawdown"), ("Max drawdown % (mark-to-market)", "maxDrawdownPct"),
            ("Max drawdown (closed trades)", "maxDrawdownClosedTrades"), ("Max consecutive wins", "maxConsecutiveWins"), ("Max consecutive losses", "maxConsecutiveLosses"),
            ("Largest win", "largestWin"), ("Largest loss", "largestLoss"), ("Average holding (hours)", "avgHoldingHours"), ("Total commission", "totalCommission"), ("Total spread cost", "totalSpreadCost"),
            ("Total slippage cost", "totalSlippageCost"), ("Total costs", "totalCosts"), ("Net P&L before costs", "idealNetPnlBeforeCosts"), ("Recovery factor", "recoveryFactor"),
            ("Sharpe (daily)", "sharpe"), ("Sortino (daily)", "sortino"), ("Calmar", "calmar"), ("CAGR %", "cagrPct"), ("Exposure %", "exposurePct"), ("Ambiguous same-bar trades", "ambiguousTrades")]
    return [(label, m.get(key)) for label, key in keys]


def describe_spec(spec: dict) -> list[tuple[str, str]]:
    rows = [("Name", spec["name"]), ("Setup name", spec.get("setup_name") or ""), ("Execution timeframe", spec["timeframe"]), ("Entry (long)", json.dumps(spec["entry"].get("long"), ensure_ascii=False)),
            ("Entry (short)", json.dumps(spec["entry"].get("short"), ensure_ascii=False)), ("Order", json.dumps(spec["entry"]["order"])), ("Invalidation", json.dumps(spec["entry"].get("invalidation"))),
            ("Stop-loss", json.dumps(spec["stop"])), ("Targets", json.dumps(spec["targets"])), ("Management", json.dumps(spec["management"])), ("Exit rules", json.dumps(spec["exits"], ensure_ascii=False)),
            ("Sessions", json.dumps(spec["sessions"])), ("Risk", json.dumps(spec["risk"])), ("Series definitions", json.dumps(spec["series"]))]
    return rows


def style_header(sheet, row=1):
    for cell in sheet[row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def build_workbook(report: dict, rows: list[dict], version: dict | None) -> bytes:
    wb = Workbook()
    journal = wb.active
    journal.title = "Trade Journal"
    columns = JOURNAL_COLUMNS
    journal.append([label for _, label in columns])
    style_header(journal)
    numeric = {"plannedEntry", "actualEntry", "initialSl", "slDistance", "positionSize", "amountRisked", "actualAmountRisked", "tpDistance", "plannedRR", "finalExit", "avgExit", "grossPnl", "fees", "netPnl", "rMultiple", "spreadCost", "slippageCost"}
    for row in rows:
        journal.append([safe(row.get(key)) if isinstance(row.get(key), str) else row.get(key) for key, _ in columns])
    index = {key: n + 1 for n, (key, _) in enumerate(columns)}
    for r in range(2, len(rows) + 2):
        result = str(journal.cell(r, index["result"]).value or "")
        fill = WIN_FILL if result.startswith("WIN") else LOSS_FILL if result.startswith("LOSS") else None
        if fill:
            journal.cell(r, index["result"]).fill = fill
            journal.cell(r, index["netPnl"]).fill = fill
        if journal.cell(r, index["ambiguous"]).value == "yes":
            journal.cell(r, index["ambiguous"]).fill = WARN_FILL
        for key in numeric:
            journal.cell(r, index[key]).number_format = "#,##0.00" if key in ("grossPnl", "fees", "netPnl", "amountRisked", "actualAmountRisked", "spreadCost", "slippageCost") else "0.00000"
        link = journal.cell(r, index["chart"])
        if isinstance(link.value, str) and link.value.startswith("http"):
            link.hyperlink = link.value
            link.font = Font(color="2563EB", underline="single")
    journal.freeze_panes = "B2"
    if rows:
        journal.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    for n, (key, label) in enumerate(columns, start=1):
        width = 60 if key in ("observations", "reasonForTrade", "slTpLogic", "confirmation", "triggerLogic", "exceptions") else 22 if key in ("entryTimeLocal", "exitTimeLocal", "chart", "eventLog", "slChanges", "partialExits") else 14
        journal.column_dimensions[get_column_letter(n)].width = max(width, min(30, len(label) + 2))
    journal.row_dimensions[1].height = 32

    summary = wb.create_sheet("Performance Summary")
    summary.append(["Metric", "Value", "Note"])
    style_header(summary)
    for label, value in summary_pairs(report):
        summary.append([label, value, ""])
    last = len(rows) + 1
    net_col, count_col = get_column_letter(index["netPnl"]), get_column_letter(index["tradeId"])
    summary.append([])
    summary.append(["Reconciliation (live formulas over the Trade Journal)", "", ""])
    summary.append(["Journal rows", f"=COUNTA('Trade Journal'!{count_col}2:{count_col}{max(last, 2)})", "Every simulated trade appears exactly once"])
    summary.append(["Journal net P&L (sum of rows)", f"=SUM('Trade Journal'!{net_col}2:{net_col}{max(last, 2)})", "Includes trades still open at the end of data"])
    summary.append(["Final balance - initial capital (engine)", round(report["finalBalance"] - report["initialCapital"], 2), "Should equal the line above within rounding"])
    summary.append([])
    summary.append(["Segmented analysis", "", ""])
    for name, label in (("instrument", "By instrument"), ("direction", "By direction"), ("session", "By session"), ("hour", "By hour (display timezone)"), ("weekday", "By weekday"),
                        ("month", "By month"), ("setup", "By setup"), ("regime", "By market regime"), ("rrCategory", "By planned R:R"), ("exitReason", "By exit reason")):
        summary.append([label, "Trades", "Win rate %", "Net P&L", "Profit factor", "Expectancy R", "Costs"])
        for seg in report["segments"].get(name, []):
            summary.append([seg["key"], seg["trades"], seg["winRate"], seg["netPnl"], seg["profitFactor"], seg["expectancyR"], seg["totalCosts"]])
        summary.append([])
    summary.column_dimensions["A"].width = 46
    for col in "BCDEFG":
        summary.column_dimensions[col].width = 18
    disclaimer = summary.cell(summary.max_row + 2, 1, report["limitations"])
    disclaimer.alignment = Alignment(wrap_text=True)

    rules = wb.create_sheet("Strategy Rules")
    rules.append(["Item", "Value"])
    style_header(rules)
    rules.append(["Strategy version", report.get("strategyVersion")])
    rules.append(["Specification hash", report["strategyHash"]])
    if version:
        rules.append(["Status", version["status"]])
        rules.append(["Approved by", version.get("approvedBy") or ""])
        rules.append(["Approved at", version.get("approvedAt") or ""])
        rules.append(["Backtestability", (version.get("readiness") or {}).get("level", "")])
        rules.append(["Fidelity", (version.get("readiness") or {}).get("fidelity", "")])
    rules.append(["Fidelity note", report.get("fidelityNote") or "No approved assumptions; every material rule is source-confirmed."])
    for label, text in describe_spec(report["specSnapshot"]):
        rules.append([label, safe(text)])
    rules.append([])
    rules.append(["Approved assumptions", ""])
    for a in report.get("approvedAssumptions", []):
        rules.append([safe(a.get("component", "")), safe(a.get("text", ""))])
    rules.append(["Concepts the engine could not reproduce", "; ".join(report.get("unsupportedConcepts", [])) or "none"])
    rules.column_dimensions["A"].width = 34
    rules.column_dimensions["B"].width = 120
    for row in rules.iter_rows(min_row=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    config = wb.create_sheet("Backtest Configuration")
    config.append(["Setting", "Value"])
    style_header(config)
    cfg = report["configSnapshot"]
    pairs = [("Backtest ID", report["id"]), ("Run date (UTC)", report["runDate"]), ("Engine", f"{report['engine']} {report['engineVersion']}"), ("Symbol", report["symbol"]), ("Timeframe", report["timeframe"]),
             ("Requested start", cfg.get("start")), ("Requested end", cfg.get("end")), ("First bar", report["dataRange"]["firstBar"]), ("Last bar", report["dataRange"]["lastBar"]),
             ("Bars tested", report["dataRange"]["bars"]), ("Initial capital", cfg["initial_capital"]), ("Account currency", cfg["account_currency"]), ("Risk model", report["specSnapshot"]["risk"]["model"]),
             ("Risk value", report["specSnapshot"]["risk"]["value"]), ("Spread (price units)", cfg["costs"]["spread"]), ("Slippage (price units)", cfg["costs"]["slippage"]),
             ("Commission", f"{cfg['costs']['commission_model']} {cfg['costs']['commission_value']}"), ("Same-bar policy", cfg["same_bar_policy"]), ("Limit fill rule", cfg["limit_fill"]),
             ("Leverage", cfg.get("leverage")), ("Display timezone", cfg["display_tz"]), ("Data provider", report.get("provider")), ("Data source", report.get("dataSource")),
             ("Dataset ID", report["datasetId"]), ("Dataset SHA-256", report["datasetSha256"]), ("Config hash", report["configHash"]), ("Result hash", report["resultHash"]),
             ("In-sample end", cfg.get("in_sample_end")), ("Strategy published", cfg.get("strategy_published_at"))]
    for item in pairs:
        config.append([item[0], safe(item[1]) if isinstance(item[1], str) else item[1]])
    config.append([])
    config.append(["Execution assumptions", ""])
    for text in report["assumptions"]:
        config.append(["", text])
    config.column_dimensions["A"].width = 30
    config.column_dimensions["B"].width = 110

    validation = wb.create_sheet("Validation & Exceptions")
    validation.append(["Check", "Status", "Detail"])
    style_header(validation)
    for c in report["validation"]["checks"]:
        validation.append([c["name"], c["status"], safe(c.get("detail", ""))])
        fill = WIN_FILL if c["status"] == "PASS" else WARN_FILL if c["status"] == "WARN" else LOSS_FILL
        validation.cell(validation.max_row, 2).fill = fill
    validation.append([])
    validation.append(["Run status", report["status"], "INVALID runs must not be relied upon" if report["status"] != "VALID" else ""])
    validation.append(["Data coverage %", report["dataQuality"]["coveragePct"], report["dataGapDisclosure"]])
    for gap in report["dataQuality"]["gaps"][:200]:
        if not gap["expectedClosure"]:
            validation.append(["Data gap", gap["start"], f"to {gap['end']} ({gap['missingBars']} bars missing)"])
    for w in report.get("warnings", []):
        validation.append(["Warning", "", w])
    ambiguity = report["ambiguity"]
    validation.append(["Ambiguous stop/target bars", ambiguity["trades"], f"Accounted {ambiguity['netPnlAsAccounted']}; if resolved favourably {ambiguity['netPnlIfFavourable']}"])
    validation.append(["Limitations", "", report["limitations"]])
    validation.column_dimensions["A"].width = 56
    validation.column_dimensions["B"].width = 26
    validation.column_dimensions["C"].width = 110
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
