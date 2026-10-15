"""API for market data, strategy versions, backtest runs, journals, exports, Pine/TradingView, comparison and optimisation."""
import asyncio
import copy
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from pyapp import exporters, jobs, lab, limits, sheets, versions
from pyapp.config import settings
from pyapp.db import db
from pyapp.engine.chart import trade_chart_svg
from pyapp.engine.data import CandleSet, load_candles, parse_csv
from pyapp.engine.journal import JOURNAL_COLUMNS, explain_trade, journal_rows
from pyapp.engine.pine import compare_trades, generate_pine, import_tv_trades
from pyapp.engine.runner import parse_spec
from pyapp.engine.simulator import Simulator
from pyapp.engine.spec import BacktestConfig, SpecError, TF_SECONDS
from pyapp.templates_spec import TEMPLATES

router = APIRouter(prefix="/api", tags=["Lab"])
Timeframe = Literal["M1", "M3", "M5", "M15", "M30", "H1", "H2", "H4", "D"]


def user_name(request: Request) -> str:
    user = getattr(request.state, "user", None) or {"username": "local"}
    return user["username"]


def bad(exc: Exception, code=400):
    return HTTPException(code, str(exc))


# ---- market data -------------------------------------------------------------------------------------

class FetchRequest(BaseModel):
    provider: Literal["oanda", "binance"]
    symbol: str = Field(min_length=3, max_length=24)
    timeframe: Timeframe
    start: str
    end: str


@router.get("/market-data")
async def list_market_data():
    return await db.list("market_data")


@router.post("/market-data/csv", status_code=201)
async def upload_market_data(file: UploadFile = File(...), symbol: str = Form(...), timeframe: Timeframe = Form(...), provider: str = Form(..., min_length=2, max_length=100),
                             tz: str = Form("UTC"), exchange: str = Form(""), calendar: Literal["forex", "24x7", "equity"] = Form("forex"), asset_class: str = Form("forex"),
                             licence: str = Form("")):
    limits.ensure_storage(settings.MAX_UPLOAD_MB * 1024 * 1024)
    content = await file.read(settings.MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(content) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {settings.MAX_UPLOAD_MB} MB")
    try:
        candles, report = await asyncio.to_thread(parse_csv, content, timeframe, tz)
        doc = await lab.store_dataset(candles, {"symbol": re.sub(r"[^A-Z0-9]", "", symbol.upper().split(":")[-1]), "timeframe": timeframe, "provider": provider.strip(), "source": "csv", "exchange": exchange or None,
                                                "timezoneAssumed": report["timezoneAssumed"], "repairs": report["repairs"], "calendar": calendar, "assetClass": asset_class, "licence": licence or None,
                                                "priceBasis": "mid"})
    except (ValueError, SpecError) as exc:
        raise bad(exc) from exc
    return doc


@router.post("/market-data/fetch", status_code=202)
async def fetch_market_data(req: FetchRequest):
    await limits.ensure_queue_room()
    return await jobs.enqueue("fetch_data", req.model_dump(), f"Download {req.symbol} {req.timeframe} ({req.provider})")


@router.get("/market-data/{dataset_id}")
async def market_data_detail(dataset_id: str):
    doc = await db.get("market_data", dataset_id)
    if not doc:
        raise HTTPException(404, "Dataset not found")
    return doc


@router.get("/market-data/{dataset_id}/quality")
async def market_data_quality(dataset_id: str):
    from pyapp.engine.data import quality_report
    doc = await db.get("market_data", dataset_id)
    if not doc:
        raise HTTPException(404, "Dataset not found")
    candles = await asyncio.to_thread(load_candles, dataset_id)
    return quality_report(candles, doc["timeframe"], doc.get("calendar", "forex"))


@router.delete("/market-data/{dataset_id}")
async def delete_market_data(dataset_id: str):
    used = [r for r in await db.list("bt_runs") if r.get("datasetId") == dataset_id]
    if used:
        raise HTTPException(409, f"{len(used)} backtest run(s) reference this dataset; delete them first to keep audit trails intact.")
    await db.delete("market_data", dataset_id)
    return {"deleted": dataset_id}


# ---- specification helpers ---------------------------------------------------------------------------

def dummy_candles(timeframe: str, bars=600) -> CandleSet:
    import math
    seconds, t0 = TF_SECONDS[timeframe], 1704067200
    rows = [(1.1 + math.sin(i / 20) * 0.01, 1.1 + math.sin(i / 20) * 0.01 + 0.002, 1.1 + math.sin(i / 20) * 0.01 - 0.002, 1.1 + math.sin(i / 20 + 0.1) * 0.01) for i in range(bars)]
    return CandleSet([t0 + i * seconds for i in range(bars)], [r[0] for r in rows], [max(r[1], r[0], r[3]) for r in rows], [min(r[2], r[0], r[3]) for r in rows], [r[3] for r in rows], [100.0] * bars, None, {"timeframe": timeframe})


def semantic_check(spec_document: dict) -> dict:
    """Parse and compile the rules against throw-away candles so unknown series, bad references and unsupported combinations surface now."""
    try:
        spec = parse_spec(spec_document)
        cfg = BacktestConfig(dataset_id="validation-only", symbol="EURUSD")
        Simulator(spec, dummy_candles(spec.timeframe), cfg)
    except SpecError as exc:
        return {"valid": False, "errors": [str(exc)]}
    except Exception as exc:
        return {"valid": False, "errors": [f"{type(exc).__name__}: {exc}"]}
    return {"valid": True, "errors": [], "spec": spec.dump()}


class SpecRequest(BaseModel):
    spec: dict[str, Any]


@router.post("/spec/validate")
async def validate_spec(req: SpecRequest):
    return await asyncio.to_thread(semantic_check, req.spec)


@router.get("/spec/templates")
async def spec_templates():
    return TEMPLATES


# ---- versions ----------------------------------------------------------------------------------------

class VersionCreate(BaseModel):
    strategyId: str
    spec: dict[str, Any]
    note: str = ""
    components: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[dict[str, Any]] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)


class VersionUpdate(BaseModel):
    spec: dict[str, Any]
    note: str = ""
    components: dict[str, Any] | None = None
    assumptions: list[dict[str, Any]] | None = None


class Approval(BaseModel):
    acknowledged: list[str] = Field(default_factory=list)


@router.get("/versions")
async def list_versions(strategyId: str | None = None):
    found = await (db.query("strategy_versions", {"strategyId": strategyId}) if strategyId else db.list("strategy_versions"))
    return [{k: v for k, v in x.items() if k != "spec"} | {"timeframe": x["spec"]["timeframe"]} for x in found]


@router.post("/versions", status_code=201)
async def create_version(req: VersionCreate, request: Request):
    strategy = await db.get("lib_strategies", req.strategyId)
    if not strategy and not req.strategyId.startswith("custom-"):
        raise HTTPException(404, "Strategy not found. Hand-written strategies must use an id starting with 'custom-'.")
    checked = await asyncio.to_thread(semantic_check, req.spec)
    if not checked["valid"]:
        raise HTTPException(422, checked["errors"])
    try:
        return await versions.create_version(req.strategyId, req.spec, author=user_name(request), components=req.components, assumptions=req.assumptions, unsupported=req.unsupported,
                                             note=req.note, evidence_hash=(strategy or {}).get("evidenceHash"))
    except SpecError as exc:
        raise bad(exc, 422) from exc


@router.get("/versions/{version_id}")
async def get_version(version_id: str):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise HTTPException(404, "Version not found")
    return version


@router.put("/versions/{version_id}")
async def update_version(version_id: str, req: VersionUpdate, request: Request):
    checked = await asyncio.to_thread(semantic_check, req.spec)
    if not checked["valid"]:
        raise HTTPException(422, checked["errors"])
    try:
        return await versions.update_draft(version_id, req.spec, author=user_name(request), note=req.note, components=req.components, assumptions=req.assumptions)
    except SpecError as exc:
        raise bad(exc, 409) from exc


@router.delete("/versions/{version_id}")
async def delete_version(version_id: str):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise HTTPException(404, "Version not found")
    if version["status"] != "DRAFT":
        raise HTTPException(409, "Only drafts can be deleted. Approved and frozen versions are kept: backtests reference them.")
    await db.delete("strategy_versions", version_id)
    return {"deleted": version_id}


@router.post("/versions/{version_id}/approve")
async def approve_version(version_id: str, req: Approval, request: Request):
    try:
        return await versions.approve(version_id, user_name(request), req.acknowledged)
    except SpecError as exc:
        raise bad(exc, 409) from exc


@router.post("/versions/{version_id}/freeze")
async def freeze_version(version_id: str, request: Request):
    try:
        return await versions.freeze(version_id, user_name(request))
    except SpecError as exc:
        raise bad(exc, 409) from exc


@router.post("/versions/{version_id}/fork", status_code=201)
async def fork_version(version_id: str, request: Request):
    try:
        return await versions.fork(version_id, user_name(request))
    except SpecError as exc:
        raise bad(exc, 404) from exc


class PineRequest(BaseModel):
    config: dict[str, Any] | None = None


@router.post("/versions/{version_id}/pine")
async def version_pine(version_id: str, req: PineRequest):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise HTTPException(404, "Version not found")
    if version["status"] == "DRAFT":
        raise HTTPException(409, "Generate Pine Script from an approved version so TradingView tests exactly what was approved.")
    try:
        cfg = BacktestConfig.model_validate({"dataset_id": "pine-export", "symbol": "EURUSD", **(req.config or {})})
        return {**generate_pine(parse_spec(version["spec"]), cfg), "versionId": version_id, "versionLabel": version["label"]}
    except SpecError as exc:
        raise bad(exc, 422) from exc


# ---- runs --------------------------------------------------------------------------------------------

class RunRequest(BaseModel):
    versionId: str
    config: dict[str, Any]
    label: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


LIST_FIELDS = ("id", "label", "strategyName", "strategyVersion", "strategyVersionId", "symbol", "timeframe", "status", "runDate", "fidelity", "fidelityNote", "tradeCount", "dataRange", "provider", "initialCapital", "finalBalance")


@router.post("/runs", status_code=202)
async def start_run(req: RunRequest):
    version = await db.get("strategy_versions", req.versionId)
    if not version:
        raise HTTPException(404, "Version not found")
    if version["status"] == "DRAFT":
        raise HTTPException(409, "Approve the strategy version before backtesting; runs must reference an exact approved version.")
    try:
        cfg = BacktestConfig.model_validate(req.config)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    if not await db.get("market_data", cfg.dataset_id):
        raise HTTPException(404, "Market data set not found")
    if cfg.in_sample_end and not cfg.start:
        raise HTTPException(422, "Set a start date when marking an in-sample period.")
    await limits.ensure_queue_room()
    return await jobs.enqueue("backtest", {"versionId": req.versionId, "config": cfg.model_dump(mode="json"), "label": req.label, "note": req.note},
                              f"Backtest {version['label']} on {cfg.symbol or cfg.dataset_id[:6]}")


@router.get("/runs")
async def list_runs(strategyId: str | None = None):
    runs = await db.list("bt_runs")
    rows = []
    for r in runs:
        if strategyId and r.get("strategyId") != strategyId:
            continue
        m = r["metrics"]
        rows.append({k: r.get(k) for k in LIST_FIELDS} | {"metrics": {k: m.get(k) for k in ("totalTrades", "winRate", "netPnl", "profitFactor", "expectancyR", "maxDrawdownPct", "totalReturnPct", "sharpe")}})
    return rows


async def load_run(run_id: str):
    run = await db.get("bt_runs", run_id)
    if not run:
        raise HTTPException(404, "Backtest run not found")
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    return await load_run(run_id)


class RunPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=1000)


@router.patch("/runs/{run_id}")
async def edit_run(run_id: str, patch: RunPatch):
    """Label and note are the only editable fields; results are immutable."""
    run = await load_run(run_id)
    for key, value in patch.model_dump(exclude_none=True).items():
        run["label" if key == "label" else "ownerNote"] = value.strip()
    await db.save("bt_runs", run)
    return {"id": run_id, "label": run.get("label"), "note": run.get("ownerNote")}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str):
    await load_run(run_id)
    for t in await db.query("bt_trades", {"runId": run_id}):
        await db.delete("bt_trades", t["id"])
    await db.delete("bt_events", run_id)
    await db.delete("bt_runs", run_id)
    return {"deleted": run_id}


@router.get("/runs/{run_id}/trades")
async def run_trades(run_id: str, direction: str | None = None, result: str | None = None, session: str | None = None, symbol: str | None = None,
                     start: str | None = None, end: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=2000), sort: str = "seq"):
    await load_run(run_id)
    trades = sorted(await db.query("bt_trades", {"runId": run_id}), key=lambda t: t["seq"])
    def keep(t):
        return ((not direction or t["direction"] == direction.upper()) and (not result or t["result"] == result.upper()) and (not session or t["session"] == session)
                and (not symbol or t["symbol"] == symbol.upper()) and (not start or t["entryTime"] >= start) and (not end or t["entryTime"] <= end))
    rows = [t for t in trades if keep(t)]
    if sort.lstrip("-") in ("netPnl", "rMultiple", "entryTs", "holdSeconds", "seq"):
        rows.sort(key=lambda t: (t[sort.lstrip("-")] is None, t[sort.lstrip("-")]), reverse=sort.startswith("-"))
    light = [{k: v for k, v in t.items() if k not in ("events", "legs")} for t in rows[offset:offset + limit]]
    return {"total": len(rows), "trades": light}


@router.get("/runs/{run_id}/trades/{trade_id}")
async def run_trade(run_id: str, trade_id: str):
    run = await load_run(run_id)
    trade = await db.get("bt_trades", trade_id)
    if not trade or trade["runId"] != run_id:
        raise HTTPException(404, "Trade not found")
    spec = parse_spec(run["specSnapshot"])
    cfg = BacktestConfig.model_validate(run["configSnapshot"])
    return {**trade, "explanation": explain_trade(trade, spec, cfg, run["instrument"]), "chart": f"/api/runs/{run_id}/trades/{trade_id}/chart.svg"}


@lru_cache(maxsize=3)
def _cached_candles(dataset_id: str):
    return load_candles(dataset_id)


@router.get("/runs/{run_id}/trades/{trade_id}/chart.svg")
async def trade_chart(run_id: str, trade_id: str):
    run = await load_run(run_id)
    trade = await db.get("bt_trades", trade_id)
    if not trade or trade["runId"] != run_id:
        raise HTTPException(404, "Trade not found")
    candles = await asyncio.to_thread(_cached_candles, run["datasetId"])
    window_start = run["dataRange"]["firstBar"]
    from pyapp.engine.data import parse_iso
    window = candles.window(parse_iso(window_start) - TF_SECONDS[run["timeframe"]] * run["dataRange"].get("warmupBars", 0), None)
    svg = trade_chart_svg(window, trade, TF_SECONDS[run["timeframe"]], run["symbol"], run["timeframe"])
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=600"})


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str):
    await load_run(run_id)
    return await db.get("bt_events", run_id) or {"events": []}


async def journal_for(run: dict, base_url=""):
    trades = sorted(await db.query("bt_trades", {"runId": run["id"]}), key=lambda t: t["seq"])
    spec = parse_spec(run["specSnapshot"])
    cfg = BacktestConfig.model_validate(run["configSnapshot"])
    version = await db.get("strategy_versions", run.get("strategyVersionId")) if run.get("strategyVersionId") else None
    rows = journal_rows(run, trades, spec, cfg, run.get("strategyVersion") or "", base_url)
    return trades, rows, version


def require_publishable(run: dict):
    if run["status"] != "VALID":
        failed = [c["name"] for c in run["validation"]["checks"] if c["status"] == "FAIL"]
        raise HTTPException(409, "This run failed validation and cannot be published as a valid result: " + "; ".join(failed[:5]))


@router.get("/runs/{run_id}/journal")
async def run_journal(run_id: str, request: Request):
    run = await load_run(run_id)
    _, rows, _ = await journal_for(run, str(request.base_url).rstrip("/"))
    return {"columns": [{"key": k, "label": label} for k, label in JOURNAL_COLUMNS], "rows": rows, "status": run["status"]}


@router.get("/runs/{run_id}/export.csv")
async def export_csv(run_id: str, request: Request):
    run = await load_run(run_id)
    require_publishable(run)
    _, rows, _ = await journal_for(run, str(request.base_url).rstrip("/"))
    await db.save("exports", {"id": f"{run_id}-csv-{datetime.now(timezone.utc).timestamp()}", "runId": run_id, "kind": "csv", "rows": len(rows), "at": datetime.now(timezone.utc).isoformat()})
    return Response(exporters.trades_csv(rows), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="journal-{run_id[:8]}.csv"'})


@router.get("/runs/{run_id}/export.xlsx")
async def export_xlsx(run_id: str, request: Request):
    run = await load_run(run_id)
    require_publishable(run)
    _, rows, version = await journal_for(run, str(request.base_url).rstrip("/"))
    data = await asyncio.to_thread(exporters.build_workbook, run, rows, version)
    await db.save("exports", {"id": f"{run_id}-xlsx-{datetime.now(timezone.utc).timestamp()}", "runId": run_id, "kind": "xlsx", "rows": len(rows), "at": datetime.now(timezone.utc).isoformat()})
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="journal-{run_id[:8]}.xlsx"'})


class SheetsExport(BaseModel):
    spreadsheet: str | None = None
    title: str | None = Field(default=None, max_length=120)


@router.post("/runs/{run_id}/export/sheets")
async def export_sheets(run_id: str, req: SheetsExport, request: Request):
    run = await load_run(run_id)
    require_publishable(run)
    _, rows, version = await journal_for(run, str(request.base_url).rstrip("/"))
    try:
        result = await sheets.export_run(run, rows, version, sheet=req.spreadsheet, title=req.title)
    except sheets.SheetsError as exc:
        raise bad(exc, 502) from exc
    await db.save("exports", {"id": f"{run_id}-sheets-{datetime.now(timezone.utc).timestamp()}", "runId": run_id, "kind": "sheets", "url": result["url"], "at": datetime.now(timezone.utc).isoformat(), **result})
    return result


@router.post("/runs/{run_id}/pine")
async def run_pine(run_id: str):
    run = await load_run(run_id)
    cfg = BacktestConfig.model_validate(run["configSnapshot"])
    return {**generate_pine(parse_spec(run["specSnapshot"]), cfg), "runId": run_id}


@router.post("/runs/{run_id}/tradingview/compare")
async def tradingview_compare(run_id: str, file: UploadFile = File(...), tz: str = Form("UTC"), time_tolerance_bars: int = Form(1)):
    run = await load_run(run_id)
    try:
        tv = import_tv_trades(await file.read(20 * 1024 * 1024), tz)
    except (ValueError, KeyError) as exc:
        raise bad(exc) from exc
    trades = sorted(await db.query("bt_trades", {"runId": run_id}), key=lambda t: t["seq"])
    spec = parse_spec(run["specSnapshot"])
    instrument = run["instrument"]
    price_tol = instrument["tick_size"] * 5
    result = compare_trades(trades, tv, TF_SECONDS[spec.timeframe], price_tol, instrument["contract_size"], time_tolerance_bars)
    doc = {"id": f"{run_id}-tv-{int(datetime.now(timezone.utc).timestamp())}", "runId": run_id, "at": datetime.now(timezone.utc).isoformat(), "tvTrades": len(tv), **result}
    await db.save("tv_comparisons", doc)
    return doc


# ---- comparison, optimisation, jobs ------------------------------------------------------------------

class CompareRequest(BaseModel):
    runIds: list[str] = Field(min_length=2, max_length=12)


@router.post("/compare")
async def compare_runs(req: CompareRequest):
    rows = []
    for run_id in req.runIds:
        run = await load_run(run_id)
        m = run["metrics"]
        trades = [t for t in await db.query("bt_trades", {"runId": run_id}) if not t.get("openAtEnd")]
        monthly = [x["netPnl"] for x in run["monthly"]]
        stability = round(100 * sum(1 for x in monthly if x > 0) / len(monthly), 1) if monthly else None
        rows.append({"runId": run_id, "label": run.get("label") or f"{run['strategyName']} {run.get('strategyVersion')}", "strategy": run["strategyName"], "version": run.get("strategyVersion"), "symbol": run["symbol"],
                     "timeframe": run["timeframe"], "from": run["dataRange"]["firstBar"], "to": run["dataRange"]["lastBar"], "status": run["status"], "fidelity": run.get("fidelity"),
                     "trades": m["totalTrades"], "winRate": m["winRate"], "netPnl": m["netPnl"], "profitFactor": m["profitFactor"], "expectancy": m["expectancy"], "expectancyR": m["expectancyR"],
                     "maxDrawdownPct": m.get("maxDrawdownPct"), "sharpe": m.get("sharpe"), "sortino": m.get("sortino"), "recoveryFactor": m.get("recoveryFactor"), "sqn": m.get("sqn"),
                     "profitableMonthsPct": stability, "totalCosts": m["totalCosts"], "equityCurve": run["equityCurve"], "periods": run.get("periods", {}),
                     "sessionBreakdown": run["segments"]["session"], "instrumentBreakdown": run["segments"]["instrument"]})
    warnings = []
    if len({r["symbol"] for r in rows}) > 1 or len({(r["from"], r["to"]) for r in rows}) > 1:
        warnings.append("Runs cover different instruments or periods; differences in results may reflect market conditions rather than the strategies.")
    if any(r["status"] != "VALID" for r in rows):
        warnings.append("At least one run failed validation and must not be relied upon.")
    return {"runs": rows, "warnings": warnings, "note": "Historical comparison only; it does not predict future results."}


class OptimizeRequest(BaseModel):
    versionId: str
    config: dict[str, Any]
    grid: dict[str, list[Any]]
    mode: Literal["grid", "walk_forward"] = "grid"
    objective: Literal["expectancyR", "netPnl", "profitFactor", "sqn", "recoveryFactor"] = "expectancyR"
    oosFraction: float = Field(default=0.3, ge=0.1, le=0.5)
    folds: int = Field(default=4, ge=2, le=10)
    minTrades: int = Field(default=20, ge=1)
    topK: int = Field(default=3, ge=1, le=10)


@router.post("/optimize", status_code=202)
async def start_optimization(req: OptimizeRequest):
    version = await db.get("strategy_versions", req.versionId)
    if not version:
        raise HTTPException(404, "Version not found")
    if version["status"] == "DRAFT":
        raise HTTPException(409, "Optimise from an approved version.")
    try:
        lab.grid_combos(req.grid)
        BacktestConfig.model_validate(req.config)
    except (SpecError, Exception) as exc:
        raise HTTPException(422, str(exc)) from exc
    await limits.ensure_queue_room()
    return await jobs.enqueue("optimize", req.model_dump(), f"{req.mode} optimisation of {version['label']}")


@router.get("/optimizations")
async def list_optimizations():
    return [{k: v for k, v in o.items() if k not in ("trials", "folds", "top")} for o in await db.list("optimizations")]


@router.get("/optimizations/{identifier}")
async def get_optimization(identifier: str):
    doc = await db.get("optimizations", identifier)
    if not doc:
        raise HTTPException(404, "Optimisation not found")
    return doc


@router.get("/jobs")
async def list_jobs(status: str | None = None, limit: int = Query(60, ge=1, le=300)):
    found = await db.list("jobs", 300)
    return [{k: v for k, v in j.items() if k not in ("trace",)} for j in found if not status or j["status"] == status][:limit]


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await db.get("jobs", job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    job = await db.get("jobs", job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] != "QUEUED":
        raise HTTPException(409, "Only queued jobs can be cancelled.")
    job.update(status="CANCELLED", message="Cancelled by user")
    await db.save("jobs", job)
    return job


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = await db.get("jobs", job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("FAILED", "CANCELLED"):
        raise HTTPException(409, "Only failed or cancelled jobs can be retried.")
    job.update(status="QUEUED", error=None, attempts=0, message="Queued for retry")
    await db.save("jobs", job)
    return job
