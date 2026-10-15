"""Backtest lab services and background job handlers: market data, runs, optimisation, translation and library rebuilds."""
import asyncio
import copy
import hashlib
import itertools
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from pyapp import jobs, library, versions
from pyapp.config import settings
from pyapp.db import db
from pyapp.engine import metrics as M
from pyapp.engine.data import CandleSet, load_candles, parse_iso, quality_report, save_candles, iso
from pyapp.engine.feeds import PROVIDERS
from pyapp.engine.runner import execute_backtest, parse_spec
from pyapp.engine.spec import BacktestConfig, SpecError, TF_SECONDS, digest

logger = logging.getLogger(__name__)
_pool: ProcessPoolExecutor | None = None


def now():
    return datetime.now(timezone.utc).isoformat()


def _cpu_pool():
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=max(1, min(4, (os.cpu_count() or 2) - 1)))
    return _pool


async def run_cpu(fn, *args):
    """Run CPU-bound work outside the event loop (separate process when enabled) so the web UI stays responsive."""
    if settings.BACKTEST_PROCESSES:
        return await asyncio.get_running_loop().run_in_executor(_cpu_pool(), fn, *args)
    return await asyncio.to_thread(fn, *args)


# ---- market data -------------------------------------------------------------------------------------

async def store_dataset(candles: CandleSet, meta: dict) -> dict:
    dataset_id = uuid4().hex
    quality = quality_report(candles, meta["timeframe"], meta.get("calendar") or ("24x7" if meta.get("assetClass") == "crypto" else "forex"))
    candles.meta = {**candles.meta, **meta}
    await asyncio.to_thread(save_candles, dataset_id, candles)
    doc = {"id": dataset_id, "symbol": meta["symbol"], "timeframe": meta["timeframe"], "provider": meta.get("provider") or "unspecified", "source": meta.get("source") or "csv",
           "environment": meta.get("environment"), "priceBasis": meta.get("priceBasis", "mid"), "exchange": meta.get("exchange"), "bars": len(candles), "first": iso(candles.t[0]), "last": iso(candles.t[-1]),
           "sha256": candles.sha256(), "hasSpread": candles.spread is not None, "hasVolume": any(candles.v), "timezoneAssumed": meta.get("timezoneAssumed"), "repairs": meta.get("repairs", []),
           "calendar": meta.get("calendar") or ("24x7" if meta.get("assetClass") == "crypto" else "forex"), "licence": meta.get("licence") or "User-supplied; confirm your licence permits this use.",
           "quality": {k: quality[k] for k in ("coveragePct", "unexpectedGapCount", "unexpectedMissingBars", "duplicates", "unordered", "invalidOhlc", "assessment", "calendar")},
           "createdAt": now()}
    await db.save("market_data", doc)
    return doc


async def get_dataset(dataset_id: str):
    doc = await db.get("market_data", dataset_id)
    if not doc:
        raise SpecError("Market data set not found.")
    candles = await asyncio.to_thread(load_candles, dataset_id)
    return doc, candles


@jobs.handler("fetch_data")
async def fetch_data_job(job):
    p = job["params"]
    await jobs.update(job, 5, f"Downloading {p['symbol']} {p['timeframe']} from {p['provider']}")
    fetch = PROVIDERS.get(p["provider"])
    if not fetch:
        raise SpecError("Unknown market-data provider.")
    candles = await fetch(p["symbol"], p["timeframe"], p["start"], p["end"])
    await jobs.update(job, 80, "Validating and storing")
    doc = await store_dataset(candles, {**candles.meta, "symbol": candles.meta["symbol"], "timeframe": p["timeframe"], "assetClass": "crypto" if p["provider"] == "binance" else "forex"})
    return {"datasetId": doc["id"], "bars": doc["bars"]}


# ---- backtests ---------------------------------------------------------------------------------------

def cpu_backtest(spec_document, cfg, candles, fine, meta, verify):
    return execute_backtest(spec_document, cfg, candles, fine, meta, verify)


async def persist_report(report: dict, extra: dict | None = None) -> dict:
    trades, events = report.pop("trades"), report.pop("events")
    report.update(extra or {})
    run_id = report["id"]
    await db.delete("bt_runs", run_id)
    for old in await db.query("bt_trades", {"runId": run_id}):
        await db.delete("bt_trades", old["id"])
    await db.save("bt_runs", report)
    await db.save_many("bt_trades", [{**t, "runId": run_id} for t in trades])
    await db.save("bt_events", {"id": run_id, "events": events})
    return report


@jobs.handler("backtest")
async def backtest_job(job):
    p = job["params"]
    version = await db.get("strategy_versions", p["versionId"])
    if not version:
        raise SpecError("Strategy version not found.")
    if version["status"] == "DRAFT":
        raise SpecError("Approve the strategy version first: backtests must reference an exact approved version.")
    cfg = BacktestConfig.model_validate(p["config"])
    await jobs.update(job, 10, "Loading market data")
    dataset, candles = await get_dataset(cfg.dataset_id)
    fine = None
    if cfg.fine_dataset_id:
        _, fine = await get_dataset(cfg.fine_dataset_id)
    if version["status"] == "APPROVED":
        await versions.freeze(version["id"], "system (first backtest)")
        version = await db.get("strategy_versions", version["id"])
    spec_hash = version["specHash"]
    meta = {"strategyId": version["strategyId"], "strategyVersionId": version["id"], "versionLabel": version["label"], "strategyHash": spec_hash, "dataset": dataset}
    await jobs.update(job, 25, "Running causal simulation and audits")
    report = await run_cpu(cpu_backtest, version["spec"], cfg, candles, fine, meta, True)
    assumptions = version["assumptions"]
    extra = {"readiness": version.get("readiness"), "approvedAssumptions": assumptions, "approvedBy": version.get("approvedBy"), "approvedAt": version.get("approvedAt"),
             "fidelity": (version.get("readiness") or {}).get("fidelity", "UNKNOWN"), "unsupportedConcepts": version.get("unsupported", []),
             "datasetMeta": {k: dataset.get(k) for k in ("id", "symbol", "timeframe", "provider", "source", "environment", "priceBasis", "sha256", "licence", "first", "last", "bars", "timezoneAssumed", "repairs")},
             "label": p.get("label"), "ownerNote": p.get("note")}
    if assumptions:
        extra["fidelityNote"] = f"This run contains {len(assumptions)} approved assumption(s); it is NOT a faithful reproduction of the instructor's method."
    elif extra["fidelity"] == "USER_AUTHORED_RULES":
        extra["fidelityNote"] = "Rules were written by you; results say nothing about any instructor's method."
    elif extra["fidelity"] != "FAITHFUL_TO_INSTRUCTOR":
        extra["fidelityNote"] = "This run is not a verified instructor-method backtest."
    await persist_report(report, extra)
    return {"runId": report["id"], "status": report["status"], "trades": report["tradeCount"]}


# ---- optimisation / walk-forward ---------------------------------------------------------------------

def set_path(document: dict, path: str, value):
    node = document
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    last = parts[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value


OBJECTIVES = {"expectancyR": lambda m: m.get("expectancyR"), "netPnl": lambda m: m.get("netPnl"), "profitFactor": lambda m: m.get("profitFactor"),
              "sqn": lambda m: m.get("sqn"), "recoveryFactor": lambda m: m.get("recoveryFactor")}


def trial(spec_document, cfg_dict, candles, fine, start, end):
    """One lightweight evaluation (no audits). Still a full causal run with validation checks."""
    cfg = BacktestConfig.model_validate({**cfg_dict, "start": start, "end": end, "causality_samples": 0})
    report = execute_backtest(spec_document, cfg, candles, fine, {"strategyHash": digest(spec_document)}, False)
    return {"metrics": report["metrics"], "status": report["status"], "tradeCount": report["tradeCount"], "failed": [c["name"] for c in report["validation"]["checks"] if c["status"] == "FAIL"]}


def grid_combos(grid: dict, limit=200):
    keys = list(grid)
    combos = [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]
    if len(combos) > limit:
        raise SpecError(f"{len(combos)} combinations exceed the limit of {limit}. Narrow the grid; testing many variants of one idea invites over-fitting.")
    return combos


def score(metrics, objective, min_trades):
    if not metrics or (metrics.get("totalTrades") or 0) < min_trades:
        return None
    value = OBJECTIVES[objective](metrics)
    return None if value is None else float(value)


@jobs.handler("optimize")
async def optimize_job(job):
    p = job["params"]
    version = await db.get("strategy_versions", p["versionId"])
    if not version or version["status"] == "DRAFT":
        raise SpecError("Optimise from an approved strategy version.")
    cfg = BacktestConfig.model_validate(p["config"])
    objective, min_trades = p.get("objective", "expectancyR"), int(p.get("minTrades", 20))
    if objective not in OBJECTIVES:
        raise SpecError(f"Objective must be one of {sorted(OBJECTIVES)}.")
    dataset, candles = await get_dataset(cfg.dataset_id)
    fine = (await get_dataset(cfg.fine_dataset_id))[1] if cfg.fine_dataset_id else None
    first = max(candles.t[0], parse_iso(cfg.start)) if cfg.start else candles.t[0]
    last = min(candles.t[-1] + TF_SECONDS[version["spec"]["timeframe"]], parse_iso(cfg.end)) if cfg.end else candles.t[-1] + TF_SECONDS[version["spec"]["timeframe"]]
    combos = grid_combos(p["grid"])
    for combo in combos[:1]:
        probe = copy.deepcopy(version["spec"])
        for path, value in combo.items():
            set_path(probe, path, value)
        parse_spec(probe)                                    # fail early on a bad path
    mode = p.get("mode", "grid")
    cfg_dict = cfg.model_dump(mode="json")
    def variant(combo):
        spec_document = copy.deepcopy(version["spec"])
        for path, value in combo.items():
            set_path(spec_document, path, value)
        return spec_document
    total = len(combos)
    rows = []

    async def evaluate(combo, start, end):
        return await run_cpu(trial, variant(combo), cfg_dict, candles, fine, iso(start), iso(end))

    if mode == "walk_forward":
        folds = max(2, min(10, int(p.get("folds", 4))))
        segment = (last - first) / (folds + 1)
        results = []
        for k in range(folds):
            train = (first, first + segment * (k + 1))
            test = (train[1], train[1] + segment)
            await jobs.update(job, 100 * k / folds, f"Walk-forward fold {k + 1}/{folds}: optimising on the training window")
            trial_rows = []
            for combo in combos:
                result = await evaluate(combo, int(train[0]), int(train[1]))
                trial_rows.append({"params": combo, "train": result["metrics"], "score": score(result["metrics"], objective, min_trades)})
            valid = [r for r in trial_rows if r["score"] is not None]
            if not valid:
                results.append({"fold": k + 1, "train": [iso(int(train[0])), iso(int(train[1]))], "test": [iso(int(test[0])), iso(int(test[1]))], "chosen": None, "note": "No parameter set met the minimum trade count on the training window."})
                continue
            best = max(valid, key=lambda r: r["score"])
            out_of_sample = await evaluate(best["params"], int(test[0]), int(test[1]))
            results.append({"fold": k + 1, "train": [iso(int(train[0])), iso(int(train[1]))], "test": [iso(int(test[0])), iso(int(test[1]))], "chosen": best["params"],
                            "trainMetrics": best["train"], "testMetrics": out_of_sample["metrics"], "trainScore": best["score"], "testScore": score(out_of_sample["metrics"], objective, 1)})
        tested = [r for r in results if r.get("testMetrics")]
        profitable = sum(1 for r in tested if (r["testMetrics"].get("netPnl") or 0) > 0)
        doc = {"id": job["id"], "kind": "walk_forward", "versionId": version["id"], "strategyId": version["strategyId"], "objective": objective, "folds": results, "trialsPerFold": total,
               "stability": {"foldsTested": len(tested), "profitableFolds": profitable, "paramChanges": len({repr(r["chosen"]) for r in results if r.get("chosen")})},
               "warning": "Out-of-sample windows are only those after each fold's training window. Parameters were re-chosen per fold, which is the honest estimate of what optimisation delivers.",
               "createdAt": now(), "grid": p["grid"], "config": p["config"]}
        await db.save("optimizations", doc)
        return {"optimizationId": doc["id"], "mode": mode}

    oos_fraction = float(p.get("oosFraction", 0.3))
    if not 0.1 <= oos_fraction <= 0.5:
        raise SpecError("Out-of-sample fraction must be between 0.1 and 0.5.")
    split = int(first + (last - first) * (1 - oos_fraction))
    for n, combo in enumerate(combos):
        await jobs.update(job, 85 * n / total, f"In-sample trial {n + 1}/{total}")
        result = await evaluate(combo, int(first), split)
        rows.append({"params": combo, "inSample": result["metrics"], "score": score(result["metrics"], objective, min_trades), "validation": result["status"]})
    ranked = sorted((r for r in rows if r["score"] is not None and r["validation"] == "VALID"), key=lambda r: r["score"], reverse=True)
    top = ranked[: int(p.get("topK", 3))]
    created = []
    for place, row in enumerate(top, start=1):
        await jobs.update(job, 85 + 10 * place / max(1, len(top)), f"Out-of-sample check {place}/{len(top)}")
        oos = await evaluate(row["params"], split, int(last))
        row["outOfSample"] = oos["metrics"]
        row["oosScore"] = score(oos["metrics"], objective, 1)
        row["degradation"] = None if row["oosScore"] is None or not row["score"] else round(row["oosScore"] / row["score"], 3)
        row["overfitWarning"] = bool(row["oosScore"] is None or row["oosScore"] <= 0 or (row["degradation"] is not None and row["degradation"] < 0.5))
        new_spec = variant(row["params"])
        label = "opt-" + hashlib.sha1(repr(sorted(row["params"].items())).encode()).hexdigest()[:6]
        v = await versions.create_version(version["strategyId"], new_spec, author="optimizer", components=version["components"], assumptions=[], unsupported=version["unsupported"], parent=version["id"],
                                          kind="optimized_variant", label=label, note=f"Optimised variant of {version['label']}: {row['params']}. In-sample {objective}={row['score']}, out-of-sample={row['oosScore']}. Original instructor rules untouched.",
                                          evidence_hash=version.get("sourceEvidenceHash"))
        row["versionId"], row["versionLabel"] = v["id"], v["label"]
        created.append(v["id"])
    doc = {"id": job["id"], "kind": "grid", "versionId": version["id"], "strategyId": version["strategyId"], "objective": objective, "trials": rows, "top": top, "split": iso(split), "combinations": total,
           "warning": (f"{total} parameter combinations were tried on the in-sample window; the best of many trials is biased upwards. Only the out-of-sample column is an unbiased estimate, and even it is one sample of history."),
           "createdVersions": created, "createdAt": now(), "grid": p["grid"], "config": p["config"]}
    await db.save("optimizations", doc)
    return {"optimizationId": doc["id"], "mode": mode, "versions": created}


# ---- library / translation ---------------------------------------------------------------------------

@jobs.handler("rebuild_library")
async def rebuild_library_job(job):
    async def progress(percent, message):
        await jobs.update(job, percent, message)
    return await library.rebuild_library(progress)


@jobs.handler("translate_strategy")
async def translate_job(job):
    from pyapp import ai_strategy
    p = job["params"]
    strategy = await db.get("lib_strategies", p["strategyId"])
    if not strategy:
        raise SpecError("Strategy not found.")
    atoms = [a for a in await db.list("atoms") if a["id"] in set(strategy["atomIds"]) | set(strategy.get("generalAtomIds", [])) and a["provenance"] == "source_confirmed"]
    if not atoms:
        raise SpecError("There is no source-confirmed evidence to translate.")
    await jobs.update(job, 20, "Translating the source rules into an executable draft")
    spec, document = await ai_strategy.translate_to_spec(strategy["name"], atoms, parse_spec)
    components, assumptions, unsupported = versions.provenance_from_translation(document, {a["id"] for a in atoms})
    v = await versions.create_version(strategy["id"], spec.dump(), author="ai-translation", components=components, assumptions=assumptions, unsupported=unsupported,
                                      note="AI draft translated from source-confirmed atoms. Review every assumption before approving.", evidence_hash=strategy.get("evidenceHash"))
    return {"versionId": v["id"], "readiness": v["readiness"]}
