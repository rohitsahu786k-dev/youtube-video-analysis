"""Decide whether a discovered channel video is trading education worth analysing.

Uncertain titles are kept (never silently excluded); only clearly unrelated videos are skipped, and the user can force analysis.
"""
import json
import re
from typing import Literal
from pydantic import BaseModel, Field

from pyapp.ai import structured
from pyapp.config import settings

CATEGORIES = ("strategy", "trade_demo", "price_action", "market_analysis", "risk_management", "psychology", "other_trading_education", "not_trading")
TRADING_WORDS = re.compile(r"(trad|forex|fx\b|strateg|scalp|intraday|swing|price action|candle|chart|setup|liquidity|order ?block|fvg|fair value|bos\b|choch|smc|ict\b|support|resistance|breakout|"
                           r"retest|stop ?loss|take ?profit|risk|reward|entry|exit|pips?|lot size|nifty|bank ?nifty|option|futures|crypto|bitcoin|gold|xau|indicator|ema|rsi|macd|"
                           r"market structure|supply|demand|backtest|journal|psychology|discipline|loss|profit|session|london|new york|asia|ट्रेडिंग|स्ट्रैटेजी|स्ट्रेटेजी|फॉरेक्स|शेयर|मार्केट)", re.I)
NOT_TRADING = re.compile(r"(vlog|unboxing|giveaway|podcast|music video|lyrics|trailer|reaction|prank|cooking|travel|birthday|announcement|subscribe to|channel intro|community post)", re.I)


class VideoLabel(BaseModel):
    index: int
    category: Literal["strategy", "trade_demo", "price_action", "market_analysis", "risk_management", "psychology", "other_trading_education", "not_trading"]
    confidence: float = Field(ge=0, le=1)


class VideoLabels(BaseModel):
    labels: list[VideoLabel] = Field(default_factory=list)


def heuristic(video):
    text = f"{video.get('title') or ''} {video.get('description') or ''}"
    if NOT_TRADING.search(text) and not TRADING_WORDS.search(text):
        return {"category": "not_trading", "confidence": 0.6, "source": "heuristic"}
    if TRADING_WORDS.search(text):
        return {"category": "other_trading_education", "confidence": 0.5, "source": "heuristic"}
    return {"category": "other_trading_education", "confidence": 0.3, "source": "heuristic-uncertain"}


async def classify_videos(videos, owner_note=""):
    """Returns {youtubeId: {category, confidence, source}}. Falls back to heuristics when AI is unavailable."""
    result = {v["youtubeId"]: heuristic(v) for v in videos}
    if settings.MOCK_AI_MODE or not settings.GEMINI_API_KEY:
        return result
    for start in range(0, len(videos), 40):
        batch = videos[start:start + 40]
        payload = [{"index": i, "title": v.get("title"), "description": (v.get("description") or "")[:240], "durationSeconds": v.get("duration")} for i, v in enumerate(batch)]
        try:
            labelled = await structured(settings.GEMINI_ANALYSIS_MODEL,
                "Classify each YouTube video as trading education. Titles may be Hindi, English or Hinglish. Categories: strategy (teaches a trading method), trade_demo (shows or reviews specific trades), "
                "price_action, market_analysis, risk_management, psychology, other_trading_education, not_trading (clearly unrelated: vlogs, music, giveaways, non-finance). "
                "When unsure whether a video is trading-related choose other_trading_education, never not_trading. Judge only from the supplied text.",
                VideoLabels, json.dumps(payload))
        except Exception:
            continue
        for item in labelled["labels"]:
            if 0 <= item["index"] < len(batch):
                result[batch[item["index"]]["youtubeId"]] = {"category": item["category"], "confidence": item["confidence"], "source": "ai"}
    return result
