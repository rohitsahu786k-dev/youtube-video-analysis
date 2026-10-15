"""Historical market-data providers. Each returns a CandleSet of COMPLETE bars plus provenance metadata.

Providers are swappable: add a function returning a CandleSet and register it in `PROVIDERS`.
"""
import asyncio
import time
from datetime import datetime, timezone

import httpx

from pyapp.config import settings
from pyapp.engine.data import CandleSet, iso, parse_iso
from pyapp.engine.instruments import normalize_symbol
from pyapp.engine.spec import TF_SECONDS, SpecError

OANDA_GRANULARITY = {"M1": "M1", "M5": "M5", "M15": "M15", "M30": "M30", "H1": "H1", "H2": "H2", "H4": "H4", "D": "D"}
BINANCE_INTERVAL = {"M1": "1m", "M3": "3m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "1h", "H2": "2h", "H4": "4h", "D": "1d"}
MAX_BARS = 1_500_000


def _oanda_host():
    return "https://api-fxtrade.oanda.com" if settings.OANDA_ENVIRONMENT == "live" else "https://api-fxpractice.oanda.com"


def oanda_instrument(symbol):
    key = normalize_symbol(symbol)
    if len(key) != 6:
        raise SpecError("OANDA symbols must be six letters such as EURUSD or XAUUSD.")
    return key[:3] + "_" + key[3:]


async def fetch_oanda(symbol: str, timeframe: str, start: str, end: str, with_spread: bool = True, client: httpx.AsyncClient | None = None) -> CandleSet:
    if not settings.OANDA_API_TOKEN or not settings.OANDA_ACCOUNT_ID:
        raise SpecError("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID to download OANDA history.")
    if timeframe not in OANDA_GRANULARITY:
        raise SpecError(f"OANDA does not provide {timeframe} candles; use one of {sorted(OANDA_GRANULARITY)}.")
    seconds = TF_SECONDS[timeframe]
    start_ts, end_ts = parse_iso(start), min(parse_iso(end), int(time.time()))
    if end_ts <= start_ts:
        raise SpecError("The end date must be after the start date.")
    instrument = oanda_instrument(symbol)
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    rows, cursor = [], start_ts
    try:
        while cursor < end_ts and len(rows) < MAX_BARS:
            response = await client.get(f"{_oanda_host()}/v3/accounts/{settings.OANDA_ACCOUNT_ID}/instruments/{instrument}/candles",
                headers={"Authorization": f"Bearer {settings.OANDA_API_TOKEN}"},
                params={"granularity": OANDA_GRANULARITY[timeframe], "price": "MBA" if with_spread else "M", "from": str(cursor), "count": 5000, "includeFirst": "true"})
            if response.status_code != 200:
                raise SpecError(f"OANDA rejected the request ({response.status_code}). Check token, account, environment and instrument.")
            candles = [c for c in response.json().get("candles", []) if c.get("complete")]
            if not candles:
                break
            progressed = False
            for c in candles:
                ts = int(datetime.fromisoformat(c["time"].replace("Z", "+00:00")).timestamp())
                if ts >= end_ts:
                    cursor = end_ts
                    break
                if rows and ts <= rows[-1][0]:
                    continue
                spread = None
                if with_spread and "bid" in c and "ask" in c:
                    spread = ((float(c["ask"]["o"]) - float(c["bid"]["o"])) + (float(c["ask"]["c"]) - float(c["bid"]["c"]))) / 2
                m = c["mid"]
                rows.append((ts, float(m["o"]), float(m["h"]), float(m["l"]), float(m["c"]), float(c.get("volume", 0)), spread))
                progressed = True
                cursor = ts + seconds
            if not progressed:
                break
            await asyncio.sleep(0.15)
    finally:
        if own:
            await client.aclose()
    if len(rows) < 100:
        raise SpecError("OANDA returned fewer than 100 complete candles for this range.")
    spreads = [r[6] for r in rows]
    return CandleSet([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows],
                     spreads if all(s is not None and s >= 0 for s in spreads) else None,
                     {"provider": "OANDA", "source": "oanda", "environment": settings.OANDA_ENVIRONMENT, "symbol": normalize_symbol(symbol), "timeframe": timeframe,
                      "priceBasis": "mid", "fetchedAt": datetime.now(timezone.utc).isoformat(), "licence": "OANDA API terms; do not redistribute."})


async def fetch_binance(symbol: str, timeframe: str, start: str, end: str, client: httpx.AsyncClient | None = None) -> CandleSet:
    if timeframe not in BINANCE_INTERVAL:
        raise SpecError(f"Unsupported timeframe {timeframe}.")
    seconds = TF_SECONDS[timeframe]
    start_ts, end_ts = parse_iso(start), min(parse_iso(end), int(time.time()))
    if end_ts <= start_ts:
        raise SpecError("The end date must be after the start date.")
    pair = normalize_symbol(symbol)
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    rows, cursor = [], start_ts * 1000
    try:
        while cursor < end_ts * 1000 and len(rows) < MAX_BARS:
            response = await client.get(f"{settings.BINANCE_BASE_URL}/api/v3/klines", params={"symbol": pair, "interval": BINANCE_INTERVAL[timeframe], "startTime": cursor, "endTime": end_ts * 1000 - 1, "limit": 1000})
            if response.status_code != 200:
                raise SpecError(f"Binance rejected the request ({response.status_code}): {response.text[:120]}")
            batch = response.json()
            if not batch:
                break
            for k in batch:
                ts = k[0] // 1000
                if k[6] // 1000 + 1 > int(time.time()):
                    continue
                if rows and ts <= rows[-1][0]:
                    continue
                rows.append((ts, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
            cursor = batch[-1][0] + seconds * 1000
            await asyncio.sleep(0.1)
    finally:
        if own:
            await client.aclose()
    if len(rows) < 100:
        raise SpecError("Binance returned fewer than 100 candles for this range.")
    return CandleSet([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows], None,
                     {"provider": "Binance", "source": "binance", "symbol": pair, "timeframe": timeframe, "priceBasis": "last", "fetchedAt": datetime.now(timezone.utc).isoformat(),
                      "licence": "Binance public market data terms."})


PROVIDERS = {"oanda": fetch_oanda, "binance": fetch_binance}
