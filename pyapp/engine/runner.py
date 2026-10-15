"""Orchestrates a full audited backtest: window selection, simulation, validation, lookahead audit,
reproducibility check, analytics and the audit-trail document (SRS 20.5)."""
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from pyapp.engine import ENGINE_VERSION
from pyapp.engine import metrics as M
from pyapp.engine.clock import iso_utc
from pyapp.engine.data import CandleSet, parse_iso, quality_report
from pyapp.engine.simulator import LookaheadError, Simulator
from pyapp.engine.spec import BacktestConfig, SpecError, StrategySpec, TF_SECONDS, digest
from pyapp.engine.validate import causality_audit, validate_run

DISCLAIMER = ("Historical simulation on candle data. Fills inside a candle are approximations; results do not guarantee future performance. "
              "A strategy taught after the tested prices existed is applied retrospectively and is not verified real-time instructor performance.")


def parse_spec(document) -> StrategySpec:
    try:
        return StrategySpec.model_validate(document)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        raise SpecError(f"Invalid strategy rules: {problems}") from exc


def warmup_seconds(spec: StrategySpec) -> int:
    tf = TF_SECONDS[spec.timeframe]
    longest = max([TF_SECONDS[d.tf] for d in spec.series.values() if d.tf] + [tf])
    return max(tf * 2500, longest * 400)


def result_hash(result):
    rows = [[t["direction"], t["entryIdx"], round(t["entry"], 10), t["exitIdx"], round(t["finalExit"], 10), round(t["netPnl"], 8), t["exitReason"]] for t in result["trades"]]
    return digest({"trades": rows, "balanceEnd": round(result["balanceEnd"], 8)})


def execute_backtest(spec_document, cfg: BacktestConfig, candles: CandleSet, fine: CandleSet | None = None, meta: dict | None = None, verify_reproducible: bool = True) -> dict:
    started = time.time()
    meta = meta or {}
    spec = parse_spec(spec_document)
    tf = TF_SECONDS[spec.timeframe]
    start_ts, end_ts = (parse_iso(cfg.start) if cfg.start else None), (parse_iso(cfg.end) if cfg.end else None)
    window = candles.window((start_ts - warmup_seconds(spec)) if start_ts else None, end_ts)
    if len(window) < 100:
        raise SpecError("The selected date range contains fewer than 100 candles.")
    if start_ts and window.t[-1] < start_ts:
        raise SpecError("The dataset ends before the requested start date.")
    fine_window = fine.window(window.t[0], window.t[-1] + tf) if fine else None
    window.meta.update(candles.meta)
    quality = quality_report(window.window(start_ts, None) if start_ts else window, spec.timeframe, cfg.calendar)
    factory = lambda cs, guard=False: Simulator(spec, cs, cfg, fine_window, guard)
    main_sim = factory(window)
    result = main_sim.run()
    checks = validate_run(spec, cfg, window, result, quality)
    audit = causality_audit(factory, window, result, cfg.causality_samples, main_sim)
    guarded_ok, guard_detail = True, "guarded replay read no bar beyond the one being processed"
    try:
        guarded = factory(window, True).run()
        if result_hash(guarded) != result_hash(result):
            guarded_ok, guard_detail = False, "guarded replay produced different results"
    except LookaheadError as exc:
        guarded_ok, guard_detail = False, str(exc)
    audit["guardedReplay"] = {"passed": guarded_ok, "detail": guard_detail}
    audit["passed"] = audit["passed"] and guarded_ok
    checks.append({"name": "Lookahead audit (prefix consistency)", "status": "PASS" if audit["passed"] else "FAIL",
                   "detail": f"{audit['sampled']} of {audit['totalClosedTrades']} trades re-simulated on truncated data (failures: {audit['failures'][:10]}); series audit mismatches: {len(audit['seriesAudit']['mismatches'])}; {guard_detail}"})
    first_hash = result_hash(result)
    reproducible = None
    if verify_reproducible:
        second = factory(window).run()
        reproducible = {"firstRun": first_hash, "secondRun": result_hash(second), "identical": first_hash == result_hash(second)}
        checks.append({"name": "Reproducibility: identical inputs give identical results", "status": "PASS" if reproducible["identical"] else "FAIL", "detail": first_hash[:16]})
    status = "INVALID" if any(c["status"] == "FAIL" for c in checks) else "VALID"

    backtest_id = meta.get("id") or digest([first_hash, meta.get("strategyHash"), cfg.model_dump()])[:24]
    trades = result["trades"]
    for t in trades:
        t["id"] = f"{backtest_id[:8]}-T{t['seq']:04d}"
    first_idx = next((i for i, t in enumerate(window.t) if not start_ts or t >= start_ts), 0)
    equity_times = [window.t[i] + tf for i in range(first_idx, len(window))]
    equity = result["equity"][first_idx:]
    annualization = 365 if cfg.calendar == "24x7" else 252
    headline = M.trade_metrics(trades, cfg.initial_capital, equity)
    headline.update(M.daily_stats(equity_times, equity, cfg.initial_capital, annualization))
    headline["exposurePct"] = M.exposure(trades, len(window) - first_idx)
    is_end = parse_iso(cfg.in_sample_end) if cfg.in_sample_end else None
    pub = parse_iso(cfg.strategy_published_at) if cfg.strategy_published_at else None
    for t in trades:
        t["period"] = None if not is_end else ("in_sample" if t["entryTs"] <= is_end else "out_of_sample")
        t["knowledgeStatus"] = None if not pub else ("retrospective" if t["entryTs"] < pub else "post_publication")
    periods = {}
    if is_end:
        periods["inSample"] = M.trade_metrics([t for t in trades if t["period"] == "in_sample"])
        periods["outOfSample"] = M.trade_metrics([t for t in trades if t["period"] == "out_of_sample"])
    if pub:
        periods["retrospective"] = M.trade_metrics([t for t in trades if t["knowledgeStatus"] == "retrospective"])
        periods["postPublication"] = M.trade_metrics([t for t in trades if t["knowledgeStatus"] == "post_publication"])
    ambiguous = [t for t in trades if t["ambiguous"] and not t.get("openAtEnd")]
    cumulative, running = [], 0.0
    for t in sorted((t for t in trades if not t.get("openAtEnd")), key=lambda t: t["exitTs"]):
        running += t["netPnl"]
        cumulative.append([t["exitTs"], round(running, 2)])
    drawdown_curve, peak = [], None
    for ts, v in zip(equity_times, equity):
        peak = v if peak is None else max(peak, v)
        drawdown_curve.append(round((v - peak) / peak * 100 if peak else 0.0, 4))
    assumptions = [
        "Signals are evaluated at the close of each bar; orders can only execute from the next bar (market orders at its open).",
        f"Candle prices are treated as mid; longs exit on the bid and shorts on the ask using a spread of {cfg.costs.spread} price units (dataset spread column used when present: {cfg.costs.use_dataset_spread and window.spread is not None}).",
        f"Slippage {cfg.costs.slippage} price units on market and stop fills; commission model '{cfg.costs.commission_model}' = {cfg.costs.commission_value}.",
        f"Same-bar stop/target policy: {cfg.same_bar_policy} (conservative = the stop is assumed to be hit first).",
        f"Limit orders fill when price trades {'through (by at least one tick)' if cfg.limit_fill == 'through' else 'to'} the level; stop orders fill at the worse of level or open.",
        "Stop and break-even/trailing changes decided at a bar close take effect from the next bar. Gaps through a stop fill at the (worse) open.",
        f"Position sizing: {spec.risk.model} {spec.risk.value}; quantity is rounded DOWN to the lot step and never exceeds intended risk.",
        f"Account currency {cfg.account_currency}; initial capital {cfg.initial_capital}; margin rate {Simulator(spec, window.prefix(10), cfg).margin_rate if len(window) >= 10 else 'n/a'}.",
        "Higher-timeframe values are used only after that higher-timeframe bar has fully closed.",
    ]
    report = {
        "id": backtest_id, "status": status, "runDate": datetime.now(timezone.utc).isoformat(), "runSeconds": round(time.time() - started, 2),
        "engine": "independent-python", "engineVersion": ENGINE_VERSION, "simulatorVersion": ENGINE_VERSION,
        "strategyId": meta.get("strategyId"), "strategyVersionId": meta.get("strategyVersionId"), "strategyVersion": meta.get("versionLabel"),
        "strategyName": spec.name, "strategyHash": meta.get("strategyHash") or digest(spec.dump()), "configHash": digest(cfg.model_dump(mode="json")),
        "specSnapshot": spec.dump(), "configSnapshot": cfg.model_dump(mode="json"),
        "datasetId": cfg.dataset_id, "datasetSha256": candles.sha256(), "provider": (meta.get("dataset") or {}).get("provider"),
        "dataSource": (meta.get("dataset") or {}).get("source"), "symbol": result["symbol"], "instrument": result["instrument"], "timeframe": spec.timeframe,
        "dataRange": {"requestedStart": cfg.start, "requestedEnd": cfg.end, "firstBar": iso_utc(window.t[first_idx]), "lastBar": iso_utc(window.t[-1]), "bars": len(window) - first_idx, "warmupBars": first_idx},
        "initialCapital": cfg.initial_capital, "accountCurrency": cfg.account_currency, "finalBalance": round(result["balanceEnd"], 2),
        "metrics": headline, "periods": periods, "segments": M.segments(trades, cfg.display_tz),
        "equityCurve": M.downsample(equity_times, [round(v, 2) for v in equity]), "drawdownCurve": M.downsample(equity_times, drawdown_curve),
        "cumulativeNet": cumulative, "monthly": M.monthly_pnl(trades),
        "distributions": {"netPnl": M.histogram([t["netPnl"] for t in trades if not t.get("openAtEnd")]), "rMultiple": M.histogram([t["rMultiple"] for t in trades if t["rMultiple"] is not None and not t.get("openAtEnd")])},
        "ambiguity": {"policy": cfg.same_bar_policy, "trades": len(ambiguous), "netPnlAsAccounted": round(sum(t["netPnl"] for t in ambiguous), 2),
                      "netPnlIfFavourable": round(sum(t.get("ambiguityBestCaseNet", t["netPnl"]) for t in ambiguous), 2)},
        "validation": {"checks": checks, "passed": sum(c["status"] == "PASS" for c in checks), "failed": sum(c["status"] == "FAIL" for c in checks), "warnings": sum(c["status"] == "WARN" for c in checks)},
        "lookaheadAudit": audit, "reproducibility": reproducible, "resultHash": first_hash, "dataQuality": quality,
        "counters": result["counters"], "warnings": result["warnings"], "assumptions": assumptions, "limitations": DISCLAIMER,
        "dataGapDisclosure": "None." if not quality["unexpectedGapCount"] else f"{quality['unexpectedGapCount']} unexpected gaps ({quality['unexpectedMissingBars']} missing bars). A gap is not a period without opportunities; the strategy may have had valid setups there that this run could not see.",
        "tradeCount": len(trades),
        "trades": trades, "events": result["events"][:20000], "eventsTruncated": len(result["events"]) > 20000,
    }
    return report
