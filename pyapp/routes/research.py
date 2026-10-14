import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from pyapp.backtest import Rule, rule_hash, run_backtest, live_candidate
from pyapp.config import settings
from pyapp.db import db
from pyapp.intelligence import build_patterns, evidence_hash
from pyapp.market import instrument_name, parse_csv, oanda_snapshot

router = APIRouter(prefix="/api", tags=["Research"])
Timeframe = Literal["M1", "M5", "M15", "M30", "H1", "H4", "D"]

class StrategyRequest(BaseModel):
    patternId: str
    symbol: str | None = None
    timeframe: Timeframe
    rule: Rule
    reviewed: bool = False

class BacktestRequest(BaseModel):
    strategyId: str
    datasetId: str
    spread: float = Field(gt=0, le=100, allow_inf_nan=False)
    slippage: float = Field(ge=0, le=100, allow_inf_nan=False)

class MarketRequest(BaseModel):
    symbol: str = Field(min_length=6, max_length=20)
    timeframe: Timeframe
    count: int = Field(default=5000, ge=100, le=5000)

@router.get("/strategies")
async def strategies():
    return await db.list("strategies")

@router.post("/strategies", status_code=201)
async def create_strategy(req: StrategyRequest):
    trades = await db.get_trades()
    knowledge = await db.list("knowledge")
    pattern = next((p for p in build_patterns(trades, knowledge) if p["id"] == req.patternId), None)
    if not pattern or pattern["status"] != "RECURRING":
        raise HTTPException(400, "A recurring validated pattern across at least two videos is required.")
    if pattern["direction"] and req.rule.direction != pattern["direction"]:
        raise HTTPException(400, "Rule direction must match the source pattern.")
    try:
        symbol = instrument_name(pattern["symbol"] or req.symbol or "").replace("_", "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    members = [t for t in trades if t["id"] in pattern["tradeIds"]]
    members.extend(k for k in knowledge if k["id"] in pattern["knowledgeIds"])
    source_tf = (pattern.get("timeframe") or "").upper().replace(" ", "")
    aliases = {"M1": ("1M", "M1", "1MIN"), "M5": ("5M", "M5", "5MIN"), "M15": ("15M", "M15", "15MIN"), "M30": ("30M", "M30", "30MIN"), "H1": ("1H", "H1", "60M"), "H4": ("4H", "H4", "240M"), "D": ("D", "1D", "DAILY")}
    if (source_tf or pattern["kind"] == "EXECUTION") and source_tf not in aliases[req.timeframe]:
        raise HTTPException(400, "Selected timeframe must match the supported source timeframe exactly; ambiguous multi-timeframe rules are not supported.")
    cutoffs = [t.get("evidenceCutoff") for t in members]
    cutoff = max(cutoffs) if cutoffs and all(cutoffs) else None
    document = {"id": uuid4().hex, "name": pattern["name"], "patternId": pattern["id"],
        "symbol": symbol, "timeframe": req.timeframe, "rule": req.rule.model_dump(), "reviewed": req.reviewed,
        "sourceTradeIds": pattern["tradeIds"], "sourceKnowledgeIds": pattern["knowledgeIds"], "sourceEvidenceHash": evidence_hash(members), "evidenceCutoff": cutoff,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "interpretation": "User-defined mechanical interpretation of the video pattern; review all parameters against the source evidence. Backtesting evaluates these exact rules, not the expert's discretionary decisions."}
    await db.save("strategies", document)
    return document

@router.post("/datasets/csv", status_code=201)
async def upload_dataset(file: UploadFile = File(...), symbol: str = Form(...), timeframe: Timeframe = Form(...),
                         provider: str = Form(..., min_length=2, max_length=100), priceBasis: Literal["mid"] = Form("mid")):
    content = await file.read(10*1024*1024+1)
    if len(content) > 10*1024*1024:
        raise HTTPException(413, "CSV exceeds 10 MB")
    try:
        symbol = instrument_name(symbol).replace("_", "")
        candles = await asyncio.to_thread(parse_csv, content, timeframe)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    document = {"id": uuid4().hex, "symbol": symbol, "timeframe": timeframe, "provider": provider.strip(),
        "priceBasis": priceBasis, "candles": candles, "sha256": hashlib.sha256(content).hexdigest(), "source": "csv"}
    await db.save("datasets", document)
    return {key: value for key, value in document.items() if key != "candles"} | {"count": len(candles)}

@router.post("/datasets/oanda", status_code=201)
async def import_market(req: MarketRequest):
    try:
        snapshot = await oanda_snapshot(req.symbol, req.timeframe, req.count, include_quote=False)
    except Exception as exc:
        raise HTTPException(503, "Market feed unavailable. Check OANDA settings, permissions and whether the market is open.") from exc
    if len(snapshot["candles"]) < 100:
        raise HTTPException(400, "Insufficient complete candles from OANDA")
    document = {"id": uuid4().hex, "symbol": snapshot["symbol"], "timeframe": req.timeframe,
        "provider": "OANDA", "environment": snapshot["environment"], "priceBasis": "mid", "candles": snapshot["candles"], "source": "oanda",
        "sha256": hashlib.sha256(str(snapshot["candles"]).encode()).hexdigest()}
    await db.save("datasets", document)
    return {key: value for key, value in document.items() if key != "candles"} | {"count": len(document["candles"])}

@router.get("/datasets")
async def datasets():
    return [{**{k: v for k, v in d.items() if k != "candles"}, "count": len(d["candles"])} for d in await db.list("datasets")]

@router.post("/backtests", status_code=201)
async def backtest(req: BacktestRequest):
    strategy = await db.get("strategies", req.strategyId)
    dataset = await db.get("datasets", req.datasetId)
    if not strategy or not dataset:
        raise HTTPException(404, "Strategy or candle dataset not found")
    if any(strategy[k] != dataset[k] for k in ("symbol", "timeframe")):
        raise HTTPException(400, "Dataset symbol and timeframe must match the strategy")
    report = await asyncio.to_thread(run_backtest, dataset, strategy, req.spread, req.slippage, settings.MIN_HOLDOUT_TRADES)
    report.update(id=uuid4().hex, createdAt=datetime.now(timezone.utc).isoformat())
    await db.save("backtests", report)
    return report

@router.get("/backtests")
async def reports():
    return [{k: v for k, v in r.items() if k != "trades"} for r in await db.list("backtests")]

@router.get("/backtests/{identifier}")
async def report(identifier: str):
    document = await db.get("backtests", identifier)
    if not document:
        raise HTTPException(404, "Backtest not found")
    return document

@router.get("/signals")
async def signals(symbol: str = "EURUSD", timeframe: Timeframe = "M15"):
    try:
        symbol = instrument_name(symbol).replace("_", "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    candidates = [s for s in await db.list("strategies") if s["symbol"] == symbol and s["timeframe"] == timeframe and s["reviewed"]]
    if not candidates:
        return {"status": "WAIT", "reason": "No reviewed video-derived strategy for this symbol/timeframe.", "signals": []}
    reports = await db.list("backtests")
    trades = await db.get_trades()
    knowledge = await db.list("knowledge")
    candidates = [s for s in candidates if s.get("sourceEvidenceHash") == evidence_hash([t for t in trades if t["id"] in s["sourceTradeIds"]] + [k for k in knowledge if k["id"] in s.get("sourceKnowledgeIds", [])])]
    if not candidates:
        return {"status": "WAIT", "reason": "Source evidence changed. Review the strategy and rerun its backtest.", "signals": []}
    matched = [(s, next((r for r in reports if r["strategyId"] == s["id"] and r["strategyHash"] == rule_hash(s) and r["eligibleForLive"]), None)) for s in candidates]
    if not any(r for _, r in matched):
        return {"status": "WAIT", "reason": "No qualifying independent holdout backtest. Use the Backtesting page.", "signals": []}
    try:
        snapshot = await oanda_snapshot(symbol, timeframe)
    except Exception:
        return {"status": "WAIT", "reason": "Live feed unavailable. Configure OANDA or check market hours and permissions.", "signals": []}
    results = [live_candidate(snapshot, s, r, settings.MIN_HOLDOUT_TRADES) for s, r in matched]
    return {"status": "PAPER_SETUP" if any(r["status"] == "PAPER_SETUP" for r in results) else "WAIT",
            "provider": "OANDA", "providerEnvironment": snapshot["environment"], "quoteTime": snapshot["time"], "signals": results}
