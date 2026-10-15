"""Historical candle container, strict CSV ingestion, integrity checks and local dataset storage."""
import csv
import gzip
import hashlib
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pyapp.config import settings
from pyapp.engine.spec import TF_SECONDS, SpecError


@dataclass
class CandleSet:
    """Columnar OHLCV. `t` is the bar OPEN time as UTC epoch seconds. Prices are mid unless `spread` is given."""
    t: list
    o: list
    h: list
    l: list
    c: list
    v: list
    spread: list | None = None
    meta: dict = field(default_factory=dict)

    def __len__(self):
        return len(self.t)

    def sha256(self):
        digest = hashlib.sha256()
        for name in ("t", "o", "h", "l", "c", "v"):
            digest.update(repr(getattr(self, name)).encode())
        if self.spread is not None:
            digest.update(repr(self.spread).encode())
        return digest.hexdigest()

    def window(self, start_ts=None, end_ts=None):
        lo = 0 if start_ts is None else next((i for i, t in enumerate(self.t) if t >= start_ts), len(self.t))
        hi = len(self.t) if end_ts is None else next((i for i, t in enumerate(self.t) if t >= end_ts), len(self.t))
        sl = slice(lo, hi)
        return CandleSet(self.t[sl], self.o[sl], self.h[sl], self.l[sl], self.c[sl], self.v[sl],
                         None if self.spread is None else self.spread[sl], dict(self.meta))

    def prefix(self, count):
        sl = slice(0, count)
        return CandleSet(self.t[sl], self.o[sl], self.h[sl], self.l[sl], self.c[sl], self.v[sl],
                         None if self.spread is None else self.spread[sl], dict(self.meta))

    def rows(self):
        for i in range(len(self.t)):
            yield {"time": iso(self.t[i]), "open": self.o[i], "high": self.h[i], "low": self.l[i], "close": self.c[i], "volume": self.v[i]}


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


_TIME_COLUMNS = ("time", "timestamp", "datetime", "date_time", "gmt time", "gmt_time", "local time", "open time", "open_time", "t", "date")
_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%Y.%m.%d %H:%M:%S", "%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y%m%d %H:%M:%S", "%Y%m%d %H%M%S")


def _parse_time(text, zone):
    text = text.strip().strip('"')
    if re.fullmatch(r"\d{9,10}(\.\d+)?", text):
        return int(float(text))
    if re.fullmatch(r"\d{12,13}", text):
        return int(text) // 1000
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return int(parsed.timestamp())
        return int(parsed.replace(tzinfo=zone).timestamp())
    except ValueError:
        pass
    cleaned = re.sub(r"\s*GMT[+-]\d{4}$", "", text)
    for fmt in _FORMATS:
        try:
            return int(datetime.strptime(cleaned, fmt).replace(tzinfo=zone).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Unrecognised timestamp '{text}'.")


def parse_csv(content: bytes, timeframe: str, tz: str = "UTC", repair: bool = True) -> tuple[CandleSet, dict]:
    """Strict-but-practical CSV ingestion. Returns (candles, ingestion_report).

    Naive timestamps are interpreted in `tz`; every adjustment (sorting, duplicate removal) is disclosed.
    """
    if timeframe not in TF_SECONDS:
        raise ValueError(f"Unsupported timeframe {timeframe}.")
    zone = ZoneInfo(tz)
    text = content.decode("utf-8-sig")
    sample = text[:4096]
    delimiter = ";" if sample.count(";") > sample.count(",") else ("\t" if sample.count("\t") > sample.count(",") else ",")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")
    # Vendors annotate headers with units or zones, e.g. "Time (EET)" or "Volume [lots]"; match on the bare column name.
    names = {re.sub(r"\s*[\(\[].*?[\)\]]\s*", "", name).strip().lower(): name for name in reader.fieldnames}
    def pick(*options):
        return next((names[o] for o in options if o in names), None)
    time_col = pick(*_TIME_COLUMNS[:-1]) or pick("date")
    date_col, clock_col = pick("date"), pick("time", "clock")
    open_col, high_col, low_col, close_col = pick("open", "o"), pick("high", "h"), pick("low", "l"), pick("close", "c")
    if not all((open_col, high_col, low_col, close_col)) or not (time_col or date_col):
        raise ValueError("CSV requires time/date, open, high, low and close columns.")
    volume_col = pick("volume", "vol", "tickvol", "tick_volume", "tick volume", "v")
    spread_col = pick("spread")
    split_dt = bool(date_col and clock_col and time_col == clock_col)
    rows, skipped, ohlc_clamped = [], 0, 0
    for line_number, row in enumerate(reader, start=2):
        try:
            stamp = f"{row[date_col]} {row[clock_col]}" if split_dt else row[time_col]
            ts = _parse_time(stamp, zone)
            o, h, l, c = (float(row[col]) for col in (open_col, high_col, low_col, close_col))
            v = float(row[volume_col]) if volume_col and row.get(volume_col) not in (None, "") else 0.0
            sp = float(row[spread_col]) if spread_col and row.get(spread_col) not in (None, "") else None
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"Row {line_number}: {exc}") from exc
        if not all(math.isfinite(x) for x in (o, h, l, c)) or min(o, h, l, c) <= 0:
            raise ValueError(f"Row {line_number}: non-positive or non-finite price.")
        if not l <= min(o, c) <= max(o, c) <= h:
            # Vendors occasionally round the last tick a point outside the bar's high/low. Widen the range for such sub-0.01% breaches and disclose it; anything larger is corrupt data.
            if repair and max(l - min(o, c), max(o, c) - h) <= max(o, c) * 1e-4:
                h, l = max(h, o, c), min(l, o, c)
                ohlc_clamped += 1
            else:
                raise ValueError(f"Row {line_number}: invalid OHLC bounds (low <= open/close <= high).")
        rows.append((ts, o, h, l, c, v, sp))
    if len(rows) < 2:
        raise ValueError("Upload at least two candles.")
    if len(rows) > 3_000_000:
        raise ValueError("Dataset exceeds 3,000,000 candles.")
    report = {"rows": len(rows), "sorted": True, "duplicatesRemoved": 0, "timezoneAssumed": tz, "repairs": []}
    if ohlc_clamped:
        report["repairs"].append(f"Widened high/low on {ohlc_clamped} bar(s) where open/close sat a rounding point outside the range (< 0.01% of price).")
    if any(rows[i][0] <= rows[i-1][0] for i in range(1, len(rows))):
        if not repair:
            raise ValueError("Timestamps are duplicated or not ascending.")
        before = len(rows)
        rows = sorted({r[0]: r for r in rows}.values(), key=lambda r: r[0])
        report.update(sorted=False, duplicatesRemoved=before - len(rows))
        report["repairs"].append("Sorted ascending and dropped duplicate timestamps (last occurrence kept).")
    seconds = TF_SECONDS[timeframe]
    misaligned = sum(1 for r in rows if timeframe != "D" and r[0] % seconds != 0)
    if misaligned > len(rows) * 0.5:
        raise ValueError(f"Most timestamps are not aligned to {timeframe} boundaries. Check the timeframe or the timezone.")
    report["misalignedBars"] = misaligned
    candles = CandleSet([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows],
                        [r[5] for r in rows], [r[6] for r in rows] if all(r[6] is not None for r in rows) else None,
                        {"timeframe": timeframe, "priceBasis": "mid"})
    return candles, report


def _weekend_gap(a_end, b_start):
    a, b = datetime.fromtimestamp(a_end, timezone.utc), datetime.fromtimestamp(b_start, timezone.utc)
    after_close = a.weekday() == 4 and a.hour >= 20 or a.weekday() in (5, 6) and not (a.weekday() == 6 and a.hour >= 21)
    before_open = b.weekday() == 6 and b.hour >= 20 or b.weekday() == 0 and b.hour <= 3
    return after_close and before_open and (b_start - a_end) <= 56 * 3600


def _holiday_gap(a_end, b_start):
    a, b = datetime.fromtimestamp(a_end, timezone.utc), datetime.fromtimestamp(b_start, timezone.utc)
    return (b_start - a_end) <= 32 * 3600 and any((d.month, d.day) in ((12, 25), (1, 1)) for d in (a, b, a + timedelta(hours=12)))


def quality_report(candles: CandleSet, timeframe: str, calendar: str = "forex") -> dict:
    """Integrity checks required before a result may be treated as valid (SRS 10.2)."""
    seconds = TF_SECONDS[timeframe]
    gaps, expected, unexpected_missing = [], 0, 0
    duplicates = unordered = invalid = 0
    for i in range(len(candles)):
        if not candles.l[i] <= min(candles.o[i], candles.c[i]) <= max(candles.o[i], candles.c[i]) <= candles.h[i]:
            invalid += 1
        if i == 0:
            continue
        step = candles.t[i] - candles.t[i-1]
        if step == 0:
            duplicates += 1
            continue
        if step < 0:
            unordered += 1
            continue
        missing = step - seconds
        if missing <= 0:
            continue
        a_end, b_start = candles.t[i-1] + seconds, candles.t[i]
        if calendar == "forex":
            closure = _weekend_gap(a_end, b_start) or _holiday_gap(a_end, b_start)
        elif calendar == "equity":
            closure = missing >= 4 * 3600 and missing <= 5 * 86400
        else:
            closure = False
        missing_bars = missing // seconds
        if closure:
            expected += missing_bars
        else:
            unexpected_missing += missing_bars
        gaps.append({"start": iso(a_end), "end": iso(b_start), "missingBars": int(missing_bars), "expectedClosure": closure})
    total = len(candles) + unexpected_missing
    coverage = 100.0 * len(candles) / total if total else 0.0
    unexpected = [g for g in gaps if not g["expectedClosure"]]
    return {"bars": len(candles), "first": iso(candles.t[0]) if len(candles) else None, "last": iso(candles.t[-1]) if len(candles) else None,
        "duplicates": duplicates, "unordered": unordered, "invalidOhlc": invalid, "calendar": calendar,
        "coveragePct": round(coverage, 4), "unexpectedGapCount": len(unexpected), "unexpectedMissingBars": int(unexpected_missing),
        "expectedClosureBars": int(expected), "gaps": gaps[:500], "gapListTruncated": len(gaps) > 500,
        "assessment": "CLEAN" if not (unexpected or duplicates or unordered or invalid) else "GAPS_OR_DEFECTS",
        "note": "A data gap is not a period without valid opportunities; results over gaps carry reduced confidence."}


def resample(candles: CandleSet, seconds: int) -> CandleSet:
    """Aggregate fixed-duration buckets aligned to the UTC epoch. Only buckets with at least one source bar appear."""
    out = {"t": [], "o": [], "h": [], "l": [], "c": [], "v": []}
    bucket = None
    for i in range(len(candles)):
        start = candles.t[i] - candles.t[i] % seconds
        if start != bucket:
            bucket = start
            out["t"].append(start); out["o"].append(candles.o[i]); out["h"].append(candles.h[i])
            out["l"].append(candles.l[i]); out["c"].append(candles.c[i]); out["v"].append(candles.v[i])
        else:
            out["h"][-1] = max(out["h"][-1], candles.h[i]); out["l"][-1] = min(out["l"][-1], candles.l[i])
            out["c"][-1] = candles.c[i]; out["v"][-1] += candles.v[i]
    return CandleSet(out["t"], out["o"], out["h"], out["l"], out["c"], out["v"], None, dict(candles.meta))


# ---- storage -------------------------------------------------------------------------------------

def dataset_dir():
    path = Path(settings.STORAGE_PATH) / "datasets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_candles(dataset_id: str, candles: CandleSet):
    payload = {"t": candles.t, "o": candles.o, "h": candles.h, "l": candles.l, "c": candles.c, "v": candles.v, "spread": candles.spread, "meta": candles.meta}
    with gzip.open(dataset_dir() / f"{dataset_id}.json.gz", "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle, allow_nan=False, separators=(",", ":"))


def load_candles(dataset_id: str) -> CandleSet:
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", dataset_id):
        raise SpecError("Invalid dataset identifier.")
    path = dataset_dir() / f"{dataset_id}.json.gz"
    if not path.is_file():
        raise SpecError("Dataset file not found; re-import the data.")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    return CandleSet(payload["t"], payload["o"], payload["h"], payload["l"], payload["c"], payload["v"], payload.get("spread"), payload.get("meta") or {})
