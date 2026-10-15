"""AI spend accounting. Every Gemini call is recorded with its token counts and estimated cost; a per-user daily budget and question cap
stop runaway spend (long playlists, chatty sessions, scripts hammering the API)."""
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from pyapp.config import settings
from pyapp.db import db, owner_var

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output). Output includes thinking tokens. Source: ai.google.dev/gemini-api/docs/pricing, Oct 2026; override with GEMINI_PRICING_JSON.
PRICES = {"gemini-3.8-flash": (0.75, 3.75), "gemini-3.6-flash": (0.75, 3.75), "gemini-3.5-flash-lite": (0.30, 2.50), "gemini-3.1-flash-lite": (0.25, 1.50),
          "gemini-2.5-flash": (0.30, 2.50), "gemini-2.5-flash-lite": (0.10, 0.40), "gemini-embedding-001": (0.15, 0.0)}
DEFAULT_PRICE = (0.75, 3.75)
_day_cache: dict = {}          # owner -> {"day": str, "cost": float, "questions": int, "loaded": monotonic}


class BudgetExceeded(RuntimeError):
    pass


HALT_ID = "provider-halt"


async def mark_provider_exhausted(detail: str) -> None:
    """Trip an app-wide circuit breaker. Called when Gemini itself reports the account's real
    money (prepaid credits / billing) is gone — a signal our own cost estimate cannot be trusted
    to have caught in time. Every further call is refused immediately until an admin clears this,
    instead of each queued video/question silently retrying against a dead key."""
    await db.save("budget_state", {"id": HALT_ID, "haltedAt": datetime.now(timezone.utc).isoformat(), "reason": str(detail)[:500]})
    logger.error("Gemini billing/quota exhausted; halting all further AI calls until cleared: %s", detail)


async def provider_halt() -> dict | None:
    return await db.get("budget_state", HALT_ID)


async def clear_provider_halt() -> None:
    await db.delete("budget_state", HALT_ID)


def is_exhaustion_error(exc) -> bool:
    code = getattr(exc, "code", None)
    text = str(exc).lower()
    return code == 402 or "resource_exhausted" in text or "prepayment credits are depleted" in text or "quota" in text and "exceeded" in text


async def video_cost_usd(video_id: str) -> float:
    rows = await db.query("usage", {"videoId": video_id})
    return sum(r.get("costUsd", 0) for r in rows)


CHARS_PER_TOKEN = 4


class EmbedUsage:
    """Embedding responses report billable characters instead of token counts; this adapts them to the
    same accounting path as generative calls so their spend is capped and visible rather than invisible."""

    def __init__(self, tokens: int):
        self.prompt_token_count = tokens
        self.candidates_token_count = 0
        self.thoughts_token_count = 0


def embed_usage(response, texts) -> EmbedUsage:
    chars = int(getattr(getattr(response, "metadata", None), "billable_character_count", 0) or 0)
    return EmbedUsage(max(1, (chars or sum(len(t) for t in texts)) // CHARS_PER_TOKEN))


def price_for(model: str) -> tuple[float, float]:
    if settings.GEMINI_PRICING_JSON:
        try:
            custom = json.loads(settings.GEMINI_PRICING_JSON).get(model)
            if custom:
                return float(custom["input"]), float(custom["output"])
        except (ValueError, KeyError, TypeError):
            logger.warning("GEMINI_PRICING_JSON is invalid; using the built-in price table")
    return PRICES.get(model, DEFAULT_PRICE)


def cost_usd(model: str, prompt_tokens: int, output_tokens: int) -> float:
    price_in, price_out = price_for(model)
    return (prompt_tokens * price_in + output_tokens * price_out) / 1_000_000


def today() -> str:
    return datetime.now(ZoneInfo(settings.DISPLAY_TZ)).date().isoformat()


async def _state() -> dict:
    owner, day = owner_var.get(), today()
    state = _day_cache.get(owner)
    if state and state["day"] == day:
        return state
    rows = await db.query("usage", {"day": day})
    state = {"day": day, "cost": sum(r.get("costUsd", 0) for r in rows), "questions": sum(1 for r in rows if r.get("kind") == "qa")}
    _day_cache[owner] = state
    return state


async def record(kind: str, model: str, usage, video_id: str | None = None) -> float:
    """Persist one model call. `usage` is the SDK's usage_metadata (or None when the provider reported nothing)."""
    prompt = int(getattr(usage, "prompt_token_count", 0) or 0)
    output = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts = int(getattr(usage, "thoughts_token_count", 0) or 0)
    if usage is None or (prompt == 0 and output == 0 and thoughts == 0):
        logger.warning("Gemini call (kind=%s, model=%s, video=%s) returned no usage_metadata; its real cost is NOT reflected in the tracked budget.", kind, model, video_id)
    cost = cost_usd(model, prompt, output + thoughts)
    state = await _state()
    state["cost"] += cost
    state["questions"] += 1 if kind == "qa" else 0
    if settings.GEMINI_COST_TRACKING:
        await db.save("usage", {"id": uuid4().hex, "videoId": video_id, "model": model, "kind": kind, "promptTokens": prompt, "outputTokens": output, "thoughtTokens": thoughts,
                                "totalTokens": prompt + output + thoughts, "costUsd": round(cost, 6), "day": state["day"], "createdAt": datetime.now(timezone.utc).isoformat()})
    return cost


async def blocked(kind: str = "video", video_id: str | None = None) -> str | None:
    """Reason new AI work must wait, or None. Chat additionally has its own daily question cap."""
    halt = await provider_halt()
    if halt:
        return f"Gemini reported its billing/quota is exhausted at {halt['haltedAt']}: {halt['reason']}. Top up the Gemini project, then clear the halt in Usage settings."
    state = await _state()
    if settings.AI_DAILY_BUDGET_USD and state["cost"] >= settings.AI_DAILY_BUDGET_USD:
        return f"Daily AI budget of ${settings.AI_DAILY_BUDGET_USD:.2f} reached (spent ${state['cost']:.2f}). It resets at midnight {settings.DISPLAY_TZ}."
    if kind == "qa" and settings.AI_DAILY_QUESTIONS and state["questions"] >= settings.AI_DAILY_QUESTIONS:
        return f"Daily limit of {settings.AI_DAILY_QUESTIONS} AI answers reached. Source passages are still shown; the limit resets at midnight {settings.DISPLAY_TZ}."
    if video_id and settings.AI_MAX_COST_PER_VIDEO_USD:
        spent = await video_cost_usd(video_id)
        if spent >= settings.AI_MAX_COST_PER_VIDEO_USD:
            return f"This video alone has used ${spent:.2f} of Gemini calls, over the ${settings.AI_MAX_COST_PER_VIDEO_USD:.2f} per-video ceiling. It likely has unusual length or content; stop and review it manually rather than spend more."
    return None


async def require_available(kind: str = "video", video_id: str | None = None) -> None:
    reason = await blocked("qa" if kind in ("qa", "qa-rewrite") else kind, video_id)
    if reason:
        raise BudgetExceeded(reason)


async def summary() -> dict:
    rows = await db.list("usage")
    now = datetime.now(ZoneInfo(settings.DISPLAY_TZ)).date()
    periods = {"today": 0, "7d": 7, "30d": 30}
    out = {"periods": {}, "byKind": {}, "budget": {"dailyUsd": settings.AI_DAILY_BUDGET_USD, "dailyQuestions": settings.AI_DAILY_QUESTIONS}}
    for name, days in periods.items():
        cutoff = (now - timedelta(days=days)).isoformat() if days else now.isoformat()
        picked = [r for r in rows if (r.get("day") or "") >= cutoff]
        out["periods"][name] = {"calls": len(picked), "costUsd": round(sum(r.get("costUsd", 0) for r in picked), 4), "tokens": sum(r.get("totalTokens", 0) for r in picked)}
    for r in rows:
        if (r.get("day") or "") >= (now - timedelta(days=30)).isoformat():
            k = out["byKind"].setdefault(r.get("kind") or "other", {"calls": 0, "costUsd": 0.0})
            k["calls"] += 1
            k["costUsd"] = round(k["costUsd"] + r.get("costUsd", 0), 4)
    state = await _state()
    out["today"] = {"costUsd": round(state["cost"], 4), "questions": state["questions"], "blocked": await blocked("qa")}
    out["untrackedLegacyCalls"] = sum(1 for r in rows if "day" not in r)
    return out
