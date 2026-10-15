"""Shared builders for engine tests. Timestamps start Monday 2024-01-01 00:00 UTC."""
import random
from copy import deepcopy

from pyapp.engine.data import CandleSet
from pyapp.engine.simulator import Simulator
from pyapp.engine.spec import BacktestConfig, StrategySpec, TF_SECONDS

T0 = 1704067200


def candles(rows, tf="M5", symbol="EURUSD", t0=T0, spread=None):
    seconds = TF_SECONDS[tf]
    return CandleSet([t0 + i * seconds for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
                     [r[3] for r in rows], [100.0] * len(rows), spread, {"timeframe": tf, "symbol": symbol})


def config(**kw):
    base = {"dataset_id": "dataset-test", "symbol": "EURUSD", "initial_capital": 10000, "costs": {"spread": 0.0002, "slippage": 0.0}, "causality_samples": 0}
    for key, value in kw.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **value}
        else:
            base[key] = value
    return BacktestConfig.model_validate(base)


def spec(**kw):
    base = {"name": "test", "timeframe": "M5", "entry": {"long": {"left": "close", "op": ">", "right": 1.1004}},
            "stop": {"type": "points", "value": 0.0010}, "targets": [{"type": "rr", "value": 2.0}], "risk": {"model": "percent_equity", "value": 1.0}}
    base.update(kw)
    return StrategySpec.model_validate(base)


def run(spec_obj, cs, cfg, fine=None):
    return Simulator(spec_obj, cs, cfg, fine).run()


FLAT = (1.1002, 1.1004, 1.1000, 1.1002)
LOW = (1.0990, 1.0995, 1.0985, 1.0990)


def benchmark_rows(bar4):
    """Bars 0-2 quiet, bar 3 signals (close 1.1005 > 1.1004), bar 4 is the scenario bar, then flat bars."""
    return [LOW, LOW, LOW, (1.0990, 1.1010, 1.0988, 1.1005), bar4] + [FLAT] * 12


def random_market(n=900, tf="M5", seed=7, start=1.1000):
    rng = random.Random(seed)
    rows, price = [], start
    import math
    for i in range(n):
        op = price
        cl = op + math.sin(i / 35) * 0.0004 + rng.gauss(0, 0.00035)
        hi = max(op, cl) + abs(rng.gauss(0, 0.0002))
        lo = min(op, cl) - abs(rng.gauss(0, 0.0002))
        rows.append((round(op, 5), round(hi, 5), round(lo, 5), round(cl, 5)))
        price = cl
    return candles(rows, tf)


def with_future_garbage(cs, after, seed=99):
    """Copy of `cs` whose bars after index `after` are replaced with unrelated valid data."""
    rng = random.Random(seed)
    clone = deepcopy(cs)
    price = 1.2
    for i in range(after + 1, len(cs)):
        op = price
        cl = op + rng.gauss(0, 0.002)
        clone.o[i], clone.c[i] = round(op, 5), round(cl, 5)
        clone.h[i] = round(max(op, cl) + abs(rng.gauss(0, 0.001)), 5)
        clone.l[i] = round(min(op, cl) - abs(rng.gauss(0, 0.001)), 5)
        price = cl
    return clone
