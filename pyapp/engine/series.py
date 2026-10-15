"""Causal series builders.

Every value at index i is a function of bars 0..i only ("as of the close of bar i"). Structure and swing
levels become available only after their confirmation bar. Higher-timeframe series expose a value only
once the higher-timeframe bar has fully closed, so an unclosed bar never leaks into a decision.
"""
from collections import deque
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from pyapp.engine.data import CandleSet
from pyapp.engine.spec import SeriesDef, SpecError, TF_SECONDS, DaySpec

REQUIRED = {
    "price": ["field"], "sma": ["period"], "ema": ["period"], "wma": ["period"], "rsi": ["period"], "atr": ["period"],
    "stdev": ["period"], "highest": ["period"], "lowest": ["period"], "bbands": ["period", "mult", "field"],
    "macd": ["fast", "slow", "signal", "field"], "roc": ["period"], "er": ["period"],
    "swing_high": ["n"], "swing_low": ["n"], "trend": ["n"], "bos": ["n", "dir"], "choch": ["n", "dir"],
    "sweep": ["dir", "ref"], "fvg": ["dir", "field"], "order_block": ["dir", "field", "n"], "candle": ["pattern", "dir"],
    "prior_day": ["field"], "session_range": ["name", "start", "end", "field"], "clock": ["field"], "vwap": [],
}
NON_HTF = {"prior_day", "session_range", "clock", "vwap"}
PRICE_FIELDS = {"open", "high", "low", "close", "hl2", "hlc3", "ohlc4", "range", "body", "upper_wick", "lower_wick", "volume", "spread"}


class Bars:
    """Bar container used by builders. `end(i)` is the close time of bar i."""
    def __init__(self, cs: CandleSet, seconds: int):
        self.t, self.o, self.h, self.l, self.c, self.v, self.spread = cs.t, cs.o, cs.h, cs.l, cs.c, cs.v, cs.spread
        self.tf = seconds
        self.n = len(cs.t)
        self.cache = {}

    def end(self, i):
        return self.t[i] + self.tf

    def field(self, name):
        key = ("field", name)
        if key in self.cache:
            return self.cache[key]
        o, h, l, c = self.o, self.h, self.l, self.c
        table = {
            "open": lambda: list(o), "high": lambda: list(h), "low": lambda: list(l), "close": lambda: list(c),
            "hl2": lambda: [(h[i] + l[i]) / 2 for i in range(self.n)],
            "hlc3": lambda: [(h[i] + l[i] + c[i]) / 3 for i in range(self.n)],
            "ohlc4": lambda: [(o[i] + h[i] + l[i] + c[i]) / 4 for i in range(self.n)],
            "range": lambda: [h[i] - l[i] for i in range(self.n)],
            "body": lambda: [abs(c[i] - o[i]) for i in range(self.n)],
            "upper_wick": lambda: [h[i] - max(o[i], c[i]) for i in range(self.n)],
            "lower_wick": lambda: [min(o[i], c[i]) - l[i] for i in range(self.n)],
            "volume": lambda: list(self.v),
        }
        if name == "spread":
            if self.spread is None:
                raise SpecError("The dataset has no spread column.")
            result = list(self.spread)
        elif name in table:
            result = table[name]()
        else:
            raise SpecError(f"Unknown price field '{name}'.")
        self.cache[key] = result
        return result


# ---- zone / calendar helpers ----------------------------------------------------------------------

def _zone(name):
    try:
        return ZoneInfo(name or "UTC")
    except Exception as exc:
        raise SpecError(f"Unknown timezone '{name}'.") from exc


def parse_hhmm(text):
    try:
        hour, minute = text.split(":")
        hour, minute = int(hour), int(minute)
    except Exception as exc:
        raise SpecError(f"Time '{text}' must look like HH:MM.") from exc
    if not (0 <= hour <= 24 and 0 <= minute < 60):
        raise SpecError(f"Time '{text}' is out of range.")
    return hour * 60 + minute


def local_clock(b: Bars, tz):
    """(minute_of_day, weekday) of each bar OPEN in the zone, cached."""
    key = ("clock", tz)
    if key not in b.cache:
        zone = _zone(tz)
        minutes, days = [], []
        for t in b.t:
            local = datetime.fromtimestamp(t, zone)
            minutes.append(local.hour * 60 + local.minute)
            days.append(local.weekday())
        b.cache[key] = (minutes, days)
    return b.cache[key]


def day_keys(b: Bars, day: DaySpec):
    key = ("daykeys", day.tz, day.rollover_hour)
    if key not in b.cache:
        zone = _zone(day.tz)
        shift = timedelta(hours=day.rollover_hour)
        b.cache[key] = [(datetime.fromtimestamp(t, zone) - shift).date().toordinal() for t in b.t]
    return b.cache[key]


def day_end_epoch(ordinal, day: DaySpec):
    zone = _zone(day.tz)
    nxt = datetime.fromordinal(ordinal + 1)
    return int(datetime.combine(nxt.date(), dtime(day.rollover_hour), tzinfo=zone).timestamp())


# ---- moving averages & oscillators ----------------------------------------------------------------

def sma(values, period):
    out, window, total = [None] * len(values), deque(), 0.0
    for i, v in enumerate(values):
        if v is None:
            window.clear(); total = 0.0
            continue
        window.append(v); total += v
        if len(window) > period:
            total -= window.popleft()
        if len(window) == period:
            out[i] = total / period
    return out


def ema(values, period):
    """TradingView-compatible: seeded with the SMA of the first `period` defined values."""
    out, alpha = [None] * len(values), 2.0 / (period + 1)
    prev, seed = None, []
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == period:
                prev = sum(seed) / period
                out[i] = prev
        else:
            prev = alpha * v + (1 - alpha) * prev
            out[i] = prev
    return out


def rma(values, period):
    out, prev, seed = [None] * len(values), None, []
    for i, v in enumerate(values):
        if v is None:
            continue
        if prev is None:
            seed.append(v)
            if len(seed) == period:
                prev = sum(seed) / period
                out[i] = prev
        else:
            prev = (prev * (period - 1) + v) / period
            out[i] = prev
    return out


def wma(values, period):
    out, weights = [None] * len(values), period * (period + 1) / 2
    for i in range(period - 1, len(values)):
        window = values[i - period + 1:i + 1]
        if any(x is None for x in window):
            continue
        out[i] = sum(x * (k + 1) for k, x in enumerate(window)) / weights
    return out


def true_range(b: Bars):
    if "tr" not in b.cache:
        tr = [b.h[0] - b.l[0]] if b.n else []
        for i in range(1, b.n):
            pc = b.c[i - 1]
            tr.append(max(b.h[i] - b.l[i], abs(b.h[i] - pc), abs(b.l[i] - pc)))
        b.cache["tr"] = tr
    return b.cache["tr"]


def rsi(values, period):
    gains, losses = [None] + [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))], [None] + [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    up, down = rma(gains, period), rma(losses, period)
    out = [None] * len(values)
    for i in range(len(values)):
        if up[i] is None or down[i] is None:
            continue
        out[i] = 100.0 if down[i] == 0 else 100.0 - 100.0 / (1 + up[i] / down[i])
    return out


def stdev(values, period):
    out, mean = [None] * len(values), sma(values, period)
    for i in range(period - 1, len(values)):
        if mean[i] is None:
            continue
        window = values[i - period + 1:i + 1]
        out[i] = (sum((x - mean[i]) ** 2 for x in window) / period) ** 0.5
    return out


def rolling_extreme(values, period, maximum, exclude_current):
    """Rolling max/min over `period` bars. With exclude_current the window ends at the PREVIOUS bar."""
    inclusive, dq = [None] * len(values), deque()
    for i, v in enumerate(values):
        while dq and ((values[dq[-1]] <= v) if maximum else (values[dq[-1]] >= v)):
            dq.pop()
        dq.append(i)
        while dq[0] < i - period + 1:
            dq.popleft()
        if i >= period - 1:
            inclusive[i] = values[dq[0]]
    if not exclude_current:
        return inclusive
    return [None] + inclusive[:-1] if inclusive else []


# ---- market structure -----------------------------------------------------------------------------

def swings(b: Bars, n):
    """Confirmed swing levels as of each bar. A pivot at j (n bars each side) is known only at bar j+n."""
    key = ("swings", n)
    if key in b.cache:
        return b.cache[key]
    sh, sl = [None] * b.n, [None] * b.n
    sh_age, sl_age = [None] * b.n, [None] * b.n
    sh_id, sl_id = [None] * b.n, [None] * b.n
    cur_h = cur_l = None
    id_h = id_l = None
    for i in range(b.n):
        j = i - n
        if j >= n:
            if b.h[j] == max(b.h[j - n:i + 1]):
                cur_h, id_h = b.h[j], i
            if b.l[j] == min(b.l[j - n:i + 1]):
                cur_l, id_l = b.l[j], i
        sh[i], sl[i], sh_id[i], sl_id[i] = cur_h, cur_l, id_h, id_l
        sh_age[i] = None if id_h is None else i - id_h
        sl_age[i] = None if id_l is None else i - id_l
    result = {"high": sh, "low": sl, "high_age": sh_age, "low_age": sl_age, "high_id": sh_id, "low_id": sl_id}
    b.cache[key] = result
    return result


def structure(b: Bars, n):
    key = ("structure", n)
    if key in b.cache:
        return b.cache[key]
    sw = swings(b, n)
    bos_bull, bos_bear, choch_bull, choch_bear = ([False] * b.n for _ in range(4))
    trend = [0] * b.n
    state, broken_h, broken_l = 0, False, False
    last_h_id = last_l_id = None
    for i in range(b.n):
        if sw["high_id"][i] != last_h_id:
            last_h_id, broken_h = sw["high_id"][i], False
        if sw["low_id"][i] != last_l_id:
            last_l_id, broken_l = sw["low_id"][i], False
        level_h, level_l = sw["high"][i], sw["low"][i]
        if level_h is not None and not broken_h and b.c[i] > level_h:
            broken_h = True
            (choch_bull if state == -1 else bos_bull)[i] = True
            state = 1
        if level_l is not None and not broken_l and b.c[i] < level_l:
            broken_l = True
            (choch_bear if state == 1 else bos_bear)[i] = True
            state = -1
        trend[i] = state
    result = {"bos_bull": bos_bull, "bos_bear": bos_bear, "choch_bull": choch_bull, "choch_bear": choch_bear, "trend": trend}
    b.cache[key] = result
    return result


def fvg_zones(b: Bars, direction, min_size, max_age):
    """Latest unmitigated fair value gap as of each bar. A close beyond the far edge invalidates it."""
    key = ("fvg", direction, min_size, max_age)
    if key in b.cache:
        return b.cache[key]
    formed, top, bottom = [False] * b.n, [None] * b.n, [None] * b.n
    zone = None
    for i in range(b.n):
        if i >= 2:
            if direction == "bull" and b.l[i] - b.h[i - 2] > min_size and b.l[i] > b.h[i - 2]:
                zone = {"top": b.l[i], "bottom": b.h[i - 2], "born": i}
                formed[i] = True
            elif direction == "bear" and b.l[i - 2] - b.h[i] > min_size and b.l[i - 2] > b.h[i]:
                zone = {"top": b.l[i - 2], "bottom": b.h[i], "born": i}
                formed[i] = True
        if zone and not formed[i]:
            if i - zone["born"] > max_age or (direction == "bull" and b.c[i] < zone["bottom"]) or (direction == "bear" and b.c[i] > zone["top"]):
                zone = None
        if zone:
            top[i], bottom[i] = zone["top"], zone["bottom"]
    result = {"formed": formed, "top": top, "bottom": bottom, "mid": [None if top[i] is None else (top[i] + bottom[i]) / 2 for i in range(b.n)]}
    b.cache[key] = result
    return result


def order_blocks(b: Bars, direction, n, max_age):
    """Last opposite candle before a structure break; active until closed through or aged out."""
    key = ("ob", direction, n, max_age)
    if key in b.cache:
        return b.cache[key]
    st = structure(b, n)
    event = [st["bos_bull"][i] or st["choch_bull"][i] for i in range(b.n)] if direction == "bull" else [st["bos_bear"][i] or st["choch_bear"][i] for i in range(b.n)]
    formed, top, bottom = [False] * b.n, [None] * b.n, [None] * b.n
    zone = None
    for i in range(b.n):
        if event[i]:
            for k in range(i - 1, max(-1, i - 21), -1):
                bearish = b.c[k] < b.o[k]
                if (direction == "bull" and bearish) or (direction == "bear" and b.c[k] > b.o[k]):
                    zone = {"top": b.h[k], "bottom": b.l[k], "born": i}
                    formed[i] = True
                    break
        elif zone and (i - zone["born"] > max_age or (direction == "bull" and b.c[i] < zone["bottom"]) or (direction == "bear" and b.c[i] > zone["top"])):
            zone = None
        if zone:
            top[i], bottom[i] = zone["top"], zone["bottom"]
    result = {"formed": formed, "top": top, "bottom": bottom, "mid": [None if top[i] is None else (top[i] + bottom[i]) / 2 for i in range(b.n)]}
    b.cache[key] = result
    return result


# ---- calendar-based levels ------------------------------------------------------------------------

def prior_day(b: Bars, day: DaySpec, field):
    key = ("prior_day", day.tz, day.rollover_hour, field)
    if key in b.cache:
        return b.cache[key]
    keys = day_keys(b, day)
    out, current, previous = [None] * b.n, None, None
    for i in range(b.n):
        if current is None or keys[i] != current["key"]:
            previous = current
            current = {"key": keys[i], "open": b.o[i], "high": b.h[i], "low": b.l[i], "close": b.c[i]}
        else:
            current["high"] = max(current["high"], b.h[i]); current["low"] = min(current["low"], b.l[i]); current["close"] = b.c[i]
        out[i] = previous[field] if previous else None
    b.cache[key] = out
    return out


def session_range(b: Bars, name, start, end, tz, field):
    """High/low of the most recent COMPLETED window. A window in progress is never exposed."""
    key = ("session_range", start, end, tz, field)
    if key in b.cache:
        return b.cache[key]
    minutes, _ = local_clock(b, tz)
    s, e = parse_hhmm(start), parse_hhmm(end)
    def inside(m):
        return s <= m < e if s < e else (m >= s or m < e)
    out, active, hi, lo, done = [None] * b.n, False, None, None, None
    for i in range(b.n):
        if inside(minutes[i]):
            if not active:
                active, hi, lo = True, b.h[i], b.l[i]
            else:
                hi, lo = max(hi, b.h[i]), min(lo, b.l[i])
        elif active:
            active, done = False, (hi, lo)
        if done is not None:
            out[i] = done[0] if field == "high" else done[1]
    b.cache[key] = out
    return out


def vwap_daily(b: Bars, day: DaySpec):
    if not any(b.v):
        raise SpecError("VWAP needs volume; this dataset has none.")
    keys, out, cum_pv, cum_v, current = day_keys(b, day), [None] * b.n, 0.0, 0.0, None
    for i in range(b.n):
        if keys[i] != current:
            current, cum_pv, cum_v = keys[i], 0.0, 0.0
        typical = (b.h[i] + b.l[i] + b.c[i]) / 3
        cum_pv += typical * b.v[i]; cum_v += b.v[i]
        out[i] = cum_pv / cum_v if cum_v else None
    return out


# ---- candle patterns ------------------------------------------------------------------------------

def candle_pattern(b: Bars, pattern, direction):
    out = [False] * b.n
    for i in range(1, b.n):
        o, h, l, c = b.o[i], b.h[i], b.l[i], b.c[i]
        po, pc, ph, pl = b.o[i - 1], b.c[i - 1], b.h[i - 1], b.l[i - 1]
        body, rng = abs(c - o), h - l
        if pattern == "engulfing":
            out[i] = (c > o and pc < po and c >= po and o <= pc) if direction == "bull" else (c < o and pc > po and c <= po and o >= pc)
        elif pattern == "pinbar" and rng > 0:
            upper, lower = h - max(o, c), min(o, c) - l
            out[i] = (lower >= 2 * body and lower >= 0.6 * rng and upper <= 0.25 * rng) if direction == "bull" else (upper >= 2 * body and upper >= 0.6 * rng and lower <= 0.25 * rng)
        elif pattern == "inside":
            out[i] = h < ph and l > pl
        elif pattern == "doji" and rng > 0:
            out[i] = body <= 0.1 * rng
        elif pattern not in ("engulfing", "pinbar", "inside", "doji"):
            raise SpecError(f"Unknown candle pattern '{pattern}'.")
    return out


# ---- factory --------------------------------------------------------------------------------------

def _need(d: SeriesDef, name):
    for key in REQUIRED.get(d.type, []):
        if getattr(d, key) is None:
            raise SpecError(f"Series '{name}' ({d.type}) requires '{key}'.")


def check_def(name, d: SeriesDef):
    if d.type not in REQUIRED:
        raise SpecError(f"Series '{name}': unsupported type '{d.type}'. Supported: {', '.join(sorted(REQUIRED))}.")
    _need(d, name)
    if d.tf and d.type in NON_HTF:
        raise SpecError(f"Series '{name}': type {d.type} cannot use a higher timeframe.")
    if d.type == "sweep":
        needs = {"swing": ["n"], "prior_day": [], "range": ["period"], "session_range": ["name", "start", "end"]}
        if d.ref not in needs:
            raise SpecError(f"Series '{name}': sweep ref must be one of {sorted(needs)}.")
        for key in needs[d.ref]:
            if getattr(d, key) is None:
                raise SpecError(f"Series '{name}': sweep ref '{d.ref}' requires '{key}'.")
    if d.type == "price" and d.field not in PRICE_FIELDS:
        raise SpecError(f"Series '{name}': unknown price field '{d.field}'.")


def is_event(d: SeriesDef):
    return d.type in ("bos", "choch", "sweep", "candle") or (d.type in ("fvg", "order_block") and d.field == "formed")


class Factory:
    def __init__(self, bars: Bars, day: DaySpec):
        self.bars, self.day = bars, day
        self.htf_cache = {}

    # -- HTF -------------------------------------------------------------------------------------
    def htf(self, tf):
        if tf in self.htf_cache:
            return self.htf_cache[tf]
        seconds = TF_SECONDS[tf]
        b = self.bars
        if tf == "D":
            keys = day_keys(b, self.day)
        else:
            if seconds <= b.tf or seconds % b.tf != 0:
                raise SpecError(f"Higher timeframe {tf} must be a multiple of the execution timeframe.")
            keys = [t - t % seconds for t in b.t]
        if tf == "D" and b.tf >= seconds:
            raise SpecError("Higher timeframe D must be above the execution timeframe.")
        cs = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": [], "end": []}
        last = object()
        for i in range(b.n):
            if keys[i] != last:
                last = keys[i]
                cs["t"].append(b.t[i]); cs["o"].append(b.o[i]); cs["h"].append(b.h[i]); cs["l"].append(b.l[i]); cs["c"].append(b.c[i]); cs["v"].append(b.v[i])
                cs["end"].append(day_end_epoch(keys[i], self.day) if tf == "D" else keys[i] + seconds)
            else:
                cs["h"][-1] = max(cs["h"][-1], b.h[i]); cs["l"][-1] = min(cs["l"][-1], b.l[i]); cs["c"][-1] = b.c[i]; cs["v"][-1] += b.v[i]
        hb = Bars(CandleSet(cs["t"], cs["o"], cs["h"], cs["l"], cs["c"], cs["v"]), seconds)
        # A higher bar is usable at exec bar i only when its bucket has ended by the close of bar i.
        done, pointer = [], 0
        for k in range(hb.n):
            while pointer < b.n and b.t[pointer] + b.tf < cs["end"][k]:
                pointer += 1
            done.append(pointer if pointer < b.n else None)
        self.htf_cache[tf] = (hb, done)
        return self.htf_cache[tf]

    def expand(self, values, done, event):
        out = [False if event else None] * self.bars.n
        if event:
            for k, idx in enumerate(done):
                if idx is not None and values[k]:
                    out[idx] = True
            return out
        k = -1
        ready = {idx: kk for kk, idx in enumerate(done) if idx is not None}
        for i in range(self.bars.n):
            if i in ready:
                k = max(k, ready[i])
            out[i] = values[k] if k >= 0 else None
        return out

    # -- build -----------------------------------------------------------------------------------
    def build(self, name, d: SeriesDef):
        check_def(name, d)
        if d.tf and TF_SECONDS[d.tf] != self.bars.tf:
            hb, done = self.htf(d.tf)
            values = self._compute(hb, d)
            return self.expand(values, done, is_event(d))
        return self._compute(self.bars, d)

    def _compute(self, b: Bars, d: SeriesDef):
        t = d.type
        src = lambda: b.field(d.source or "close")
        if t == "price":
            return b.field(d.field)
        if t == "sma":
            return sma(src(), d.period)
        if t == "ema":
            return ema(src(), d.period)
        if t == "wma":
            return wma(src(), d.period)
        if t == "rsi":
            return rsi(src(), d.period)
        if t == "atr":
            return rma(true_range(b), d.period)
        if t == "stdev":
            return stdev(src(), d.period)
        if t in ("highest", "lowest"):
            field = d.source or ("high" if t == "highest" else "low")
            return rolling_extreme(b.field(field), d.period, t == "highest", bool(d.exclude_current))
        if t == "bbands":
            basis, dev = sma(src(), d.period), stdev(src(), d.period)
            pick = {"upper": lambda i: basis[i] + d.mult * dev[i], "middle": lambda i: basis[i], "lower": lambda i: basis[i] - d.mult * dev[i]}
            if d.field not in pick:
                raise SpecError("bbands field must be upper, middle or lower.")
            return [None if basis[i] is None or dev[i] is None else pick[d.field](i) for i in range(b.n)]
        if t == "macd":
            values = src()
            fast, slow = ema(values, d.fast), ema(values, d.slow)
            line = [None if fast[i] is None or slow[i] is None else fast[i] - slow[i] for i in range(b.n)]
            signal = ema(line, d.signal)
            pick = {"line": line, "signal": signal, "hist": [None if line[i] is None or signal[i] is None else line[i] - signal[i] for i in range(b.n)]}
            if d.field not in pick:
                raise SpecError("macd field must be line, signal or hist.")
            return pick[d.field]
        if t == "roc":
            values = src()
            return [None if i < d.period else (values[i] - values[i - d.period]) / values[i - d.period] * 100 for i in range(b.n)]
        if t == "er":
            values = src()
            out = [None] * b.n
            for i in range(d.period, b.n):
                path = sum(abs(values[k] - values[k - 1]) for k in range(i - d.period + 1, i + 1))
                out[i] = abs(values[i] - values[i - d.period]) / path if path else 0.0
            return out
        if t in ("swing_high", "swing_low"):
            sw = swings(b, d.n)
            kind = "high" if t == "swing_high" else "low"
            return sw[kind + "_age"] if d.field == "age" else sw[kind]
        if t == "trend":
            return structure(b, d.n)["trend"]
        if t in ("bos", "choch"):
            return structure(b, d.n)[f"{t}_{d.dir}"]
        if t == "fvg":
            zones = fvg_zones(b, d.dir, d.min_size or 0.0, d.max_age or 100)
            if d.field not in zones:
                raise SpecError("fvg field must be formed, top, bottom or mid.")
            return zones[d.field]
        if t == "order_block":
            zones = order_blocks(b, d.dir, d.n, d.max_age or 50)
            if d.field not in zones:
                raise SpecError("order_block field must be formed, top, bottom or mid.")
            return zones[d.field]
        if t == "candle":
            return candle_pattern(b, d.pattern, d.dir)
        if t == "prior_day":
            if d.field not in ("open", "high", "low", "close"):
                raise SpecError("prior_day field must be open, high, low or close.")
            return prior_day(b, self.day, d.field)
        if t == "session_range":
            if d.field not in ("high", "low"):
                raise SpecError("session_range field must be high or low.")
            return session_range(b, d.name, d.start, d.end, d.tz or "UTC", d.field)
        if t == "clock":
            minutes, days = local_clock(b, d.tz or "UTC")
            pick = {"hour": [m // 60 for m in minutes], "minute": [m % 60 for m in minutes], "minute_of_day": minutes, "weekday": days}
            if d.field not in pick:
                raise SpecError("clock field must be hour, minute, minute_of_day or weekday.")
            return pick[d.field]
        if t == "vwap":
            return vwap_daily(b, self.day)
        if t == "sweep":
            return self._sweep(b, d)
        raise SpecError(f"Unsupported series type {t}")

    def _sweep(self, b: Bars, d: SeriesDef):
        pen = d.min_penetration or 0.0
        close_back = True if d.close_back is None else d.close_back
        if d.ref == "swing":
            sw = swings(b, d.n)
            ref = sw["low"] if d.dir == "bull" else sw["high"]
        elif d.ref == "prior_day":
            ref = prior_day(b, self.day, "low" if d.dir == "bull" else "high")
        elif d.ref == "range":
            ref = rolling_extreme(b.field("low" if d.dir == "bull" else "high"), d.period, d.dir == "bear", True)
        else:
            ref = session_range(b, d.name, d.start, d.end, d.tz or "UTC", "low" if d.dir == "bull" else "high")
        out = [False] * b.n
        for i in range(b.n):
            level = ref[i]
            if level is None:
                continue
            if d.dir == "bull":
                out[i] = b.l[i] < level - pen and (b.c[i] > level if close_back else True)
            else:
                out[i] = b.h[i] > level + pen and (b.c[i] < level if close_back else True)
        return out
