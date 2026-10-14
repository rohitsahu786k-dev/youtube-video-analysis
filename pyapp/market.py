"""Strict OHLC ingestion and read-only OANDA pricing access."""
import csv
import io
import re
import math
from datetime import datetime, timezone
from typing import Literal
import httpx
from pydantic import BaseModel, ConfigDict, Field
from pyapp.config import settings

TIMEFRAMES = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D": 86400}

def instrument_name(symbol):
    symbol = symbol.upper().split(":")[-1].replace("/", "").replace("_", "")
    if not re.fullmatch(r"[A-Z]{6}", symbol):
        raise ValueError("Use a six-letter forex/metal symbol such as EURUSD or XAUUSD.")
    return symbol[:3] + "_" + symbol[3:]

class Candle(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    time: datetime
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)

def validate_candles(rows, timeframe):
    seconds = TIMEFRAMES[timeframe]
    result = []
    for index, row in enumerate(rows):
        candle = Candle.model_validate(row)
        if candle.time.tzinfo is None:
            raise ValueError(f"Row {index+1}: timestamps must include UTC offset or Z.")
        candle.time = candle.time.astimezone(timezone.utc)
        if not candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high:
            raise ValueError(f"Row {index+1}: invalid OHLC bounds.")
        if result:
            gap = (candle.time - datetime.fromisoformat(result[-1]["time"])).total_seconds()
            daily_gap = timeframe == "D" and gap >= 23*3600 and gap % 3600 == 0
            if not daily_gap and (gap < seconds or gap % seconds != 0):
                raise ValueError(f"Row {index+1}: duplicate, unordered or wrong-timeframe candle.")
        result.append(candle.model_dump(mode="json"))
    return result

def parse_csv(content, timeframe):
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig")))
    required = {"time", "open", "high", "low", "close"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise ValueError("CSV requires time,open,high,low,close columns. Use ISO timestamps with timezone.")
    rows = list(reader)
    if not 100 <= len(rows) <= 100000:
        raise ValueError("Upload between 100 and 100,000 candles.")
    return validate_candles(rows, timeframe)

async def oanda_snapshot(symbol, timeframe, count=500, include_quote=True):
    if not settings.OANDA_API_TOKEN or not settings.OANDA_ACCOUNT_ID:
        raise ValueError("Set OANDA_API_TOKEN and OANDA_ACCOUNT_ID in .env to enable live market analysis.")
    host = "https://api-fxtrade.oanda.com" if settings.OANDA_ENVIRONMENT == "live" else "https://api-fxpractice.oanda.com"
    instrument = instrument_name(symbol)
    async with httpx.AsyncClient(timeout=30) as client:
        headers = {"Authorization": f"Bearer {settings.OANDA_API_TOKEN}"}
        candle_response = await client.get(f"{host}/v3/accounts/{settings.OANDA_ACCOUNT_ID}/instruments/{instrument}/candles",
            headers=headers, params={"granularity": timeframe, "count": min(count, 5000), "price": "M"})
        quote_response = await client.get(f"{host}/v3/accounts/{settings.OANDA_ACCOUNT_ID}/pricing", headers=headers, params={"instruments": instrument}) if include_quote else None
    if candle_response.status_code != 200 or (quote_response is not None and quote_response.status_code != 200):
        raise ValueError("OANDA rejected market-data access. Check token, account, environment and instrument permissions.")
    rows = [{"time": c["time"], "open": c["mid"]["o"], "high": c["mid"]["h"], "low": c["mid"]["l"], "close": c["mid"]["c"]}
            for c in candle_response.json()["candles"] if c.get("complete")]
    result = {"candles": validate_candles(rows, timeframe), "provider": "OANDA", "environment": settings.OANDA_ENVIRONMENT, "symbol": instrument.replace("_", ""), "timeframe": timeframe}
    if not include_quote:
        return result
    prices = quote_response.json().get("prices", [])
    if not prices or prices[0].get("status") != "tradeable":
        raise ValueError("Market is closed or no tradeable quote is available.")
    price = prices[0]
    bid, ask = float(price["closeoutBid"]), float(price["closeoutAsk"])
    if not math.isfinite(bid) or not math.isfinite(ask) or not 0 < bid <= ask:
        raise ValueError("Market feed returned an invalid executable quote.")
    quote_time = datetime.fromisoformat(price["time"].replace("Z", "+00:00"))
    if quote_time.tzinfo is None:
        raise ValueError("Market feed returned a quote without timezone.")
    return {**result, "bid": bid, "ask": ask, "time": price["time"]}
