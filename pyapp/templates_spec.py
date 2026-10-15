"""Starter rule documents shown in the strategy editor. They are teaching examples, not instructor rules."""

TEMPLATES = {
    "ema_pullback": {
        "name": "EMA trend pullback (example)", "timeframe": "M15",
        "series": {"ema50": {"type": "ema", "period": 50}, "ema200": {"type": "ema", "period": 200}, "atr14": {"type": "atr", "period": 14}},
        "entry": {"long": {"all": [{"left": "ema50", "op": ">", "right": "ema200"}, {"left": {"s": "close", "shift": 1}, "op": "<", "right": {"s": "ema50", "shift": 1}}, {"left": "close", "op": ">", "right": "ema50"}]},
                  "short": {"all": [{"left": "ema50", "op": "<", "right": "ema200"}, {"left": {"s": "close", "shift": 1}, "op": ">", "right": {"s": "ema50", "shift": 1}}, {"left": "close", "op": "<", "right": "ema50"}]},
                  "order": {"type": "market"}},
        "stop": {"type": "atr", "mult": 1.5, "series": "atr14"}, "targets": [{"type": "rr", "value": 2.0}],
        "management": {"max_bars": 80}, "risk": {"model": "percent_equity", "value": 1.0},
    },
    "breakout_retest": {
        "name": "Range breakout and retest (example)", "timeframe": "M15",
        "series": {"hh": {"type": "highest", "period": 20, "exclude_current": True}, "atr14": {"type": "atr", "period": 14}},
        "entry": {"long": {"seq": [{"left": "close", "op": ">", "right": "hh"}, {"all": [{"left": "low", "op": "<=", "right": {"s": "hh", "shift": 1}}, {"left": "close", "op": ">", "right": {"s": "hh", "shift": 1}}]}], "within": 10},
                  "order": {"type": "market"}},
        "stop": {"type": "atr", "mult": 1.2, "series": "atr14"}, "targets": [{"type": "rr", "value": 1.5, "portion": 0.5}, {"type": "rr", "value": 3.0, "portion": 0.5}],
        "management": {"breakeven": {"after_target": 1}, "max_bars": 100}, "risk": {"model": "percent_equity", "value": 1.0, "max_trades_per_day": 2},
    },
    "asia_sweep_fvg": {
        "name": "Asian-range sweep, structure shift, FVG entry (example)", "timeframe": "M5", "day": {"tz": "America/New_York", "rollover_hour": 17},
        "series": {"asia_sweep": {"type": "sweep", "dir": "bull", "ref": "session_range", "name": "asia", "start": "00:00", "end": "06:00", "tz": "UTC"},
                   "bos_up": {"type": "bos", "dir": "bull", "n": 3}, "fvg_mid": {"type": "fvg", "dir": "bull", "field": "mid", "min_size": 0.00005}, "atr14": {"type": "atr", "period": 14}},
        "entry": {"long": {"seq": [{"is": "asia_sweep"}, {"is": "bos_up"}], "within": [12]}, "order": {"type": "limit", "price": "fvg_mid", "expire_bars": 8}},
        "stop": {"type": "atr", "mult": 1.0, "series": "atr14"}, "targets": [{"type": "rr", "value": 2.0}],
        "sessions": {"windows": [{"name": "London", "start": "07:00", "end": "11:00", "tz": "Europe/London"}], "force_flat": {"time": "16:00", "tz": "Europe/London"}},
        "risk": {"model": "percent_equity", "value": 0.5, "max_trades_per_day": 1},
    },
}
