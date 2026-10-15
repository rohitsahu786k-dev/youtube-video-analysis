"""Gemini calls for strategy knowledge: rule atoms, clustering, manuals, conflicts, DSL translation and grounded Q&A.

Every prompt treats transcripts as untrusted data and demands verbatim quotes that grounding.py later verifies.
"""
import json
import re
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

from pyapp import budget
from pyapp.ai import EVIDENCE, NO_THINKING, get_client, is_thinking_error, structured
from pyapp.config import settings
from pyapp.db import db

ATOM_CATEGORIES = ("market_context", "bias", "chart_levels", "setup_detection", "confirmation", "entry", "stop_loss", "take_profit", "trade_management",
                   "risk_management", "session_time", "avoidance", "exception", "psychology", "mistake", "backtesting", "terminology", "example_trade", "other")

Category = Literal["market_context", "bias", "chart_levels", "setup_detection", "confirmation", "entry", "stop_loss", "take_profit", "trade_management",
                   "risk_management", "session_time", "avoidance", "exception", "psychology", "mistake", "backtesting", "terminology", "example_trade", "other"]


class AtomOut(BaseModel):
    category: Category
    strategyName: str | None = None
    text: str
    quote: str
    startTime: float = Field(ge=0)
    endTime: float = Field(ge=0)
    evidenceType: Literal["spoken", "visual", "both"]
    visualNote: str | None = None
    timeframe: str | None = None
    instrument: str | None = None
    direction: Literal["LONG", "SHORT", "BOTH"] | None = None
    objective: bool = False
    hypothetical: bool = False
    confidence: float = Field(ge=0, le=1)


class Atoms(BaseModel):
    atoms: list[AtomOut] = Field(default_factory=list)


class Cluster(BaseModel):
    canonicalName: str
    memberNames: list[str]
    relation: Literal["distinct", "variation"] = "distinct"
    variationOf: str | None = None


class Clusters(BaseModel):
    clusters: list[Cluster] = Field(default_factory=list)


class ManualItem(BaseModel):
    text: str
    atomIds: list[str] = Field(default_factory=list)
    note: str | None = None


class ManualStep(BaseModel):
    step: int
    title: str
    instruction: str
    atomIds: list[str] = Field(default_factory=list)


class Manual(BaseModel):
    objective: list[ManualItem] = Field(default_factory=list)
    instruments_timeframes_sessions: list[ManualItem] = Field(default_factory=list)
    chart_setup_indicators: list[ManualItem] = Field(default_factory=list)
    chart_preparation: list[ManualItem] = Field(default_factory=list)
    htf_analysis: list[ManualItem] = Field(default_factory=list)
    execution_steps: list[ManualStep] = Field(default_factory=list)
    entry_rules: list[ManualItem] = Field(default_factory=list)
    stop_loss_rules: list[ManualItem] = Field(default_factory=list)
    take_profit_rules: list[ManualItem] = Field(default_factory=list)
    trade_management: list[ManualItem] = Field(default_factory=list)
    risk_management: list[ManualItem] = Field(default_factory=list)
    sessions_time: list[ManualItem] = Field(default_factory=list)
    avoidance: list[ManualItem] = Field(default_factory=list)
    supporting_knowledge: list[ManualItem] = Field(default_factory=list)


class Conflict(BaseModel):
    topic: str
    description: str
    category: str
    atomIds: list[str]


class Conflicts(BaseModel):
    conflicts: list[Conflict] = Field(default_factory=list)


class AnswerOut(BaseModel):
    sourceBasedAnswer: str = ""
    steps: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    generalEducation: str | None = None
    insufficient: bool = False
    missing: str | None = None
    followUps: list[str] = Field(default_factory=list)


class SearchRewrite(BaseModel):
    queries: list[str]


# ---- atoms -----------------------------------------------------------------------------------------

async def extract_atoms(segments, observations, video, window, video_id):
    """One window (about 10 minutes) of aligned speech and visual observations -> candidate rule atoms."""
    spoken = [{"start": round(s["start"], 1), "end": round(s["end"], 1), "text": s["text"]} for s in segments]
    seen = [{"time": o["timestamp"], "chart": (o.get("summary") or {}).get("visualDescription"), "symbol": (o.get("summary") or {}).get("symbol"),
             "timeframe": (o.get("summary") or {}).get("timeframe")} for o in observations]
    result = await structured(settings.GEMINI_REASONING_MODEL,
        "You build a trading knowledge base from a teacher's video. Extract ATOMIC rules and teachings, one per atom: strategy names, setup definitions, market context, bias, levels, confirmations, "
        "entries, stop-loss, take-profit, trade management, risk management, session/time guidance, trade avoidance, exceptions, psychology, mistakes, backtesting advice, terminology and concrete trade examples. "
        "RULES: (1) 'quote' MUST be copied VERBATIM from the supplied transcript in its ORIGINAL language and script (Hindi/Hinglish/English) - never translate or paraphrase it; "
        "(2) 'text' is a concise English statement that keeps the teacher's own terms untranslated; (3) use the teacher's own strategy name in 'strategyName' exactly as spoken, null for general teaching; "
        "(4) never add a rule the teacher did not state; if a value (price, timeframe, ratio) is not stated, leave it out; (5) a rule only illustrated visually must have evidenceType 'visual' and describe what the chart shows in 'visualNote'; "
        "(6) hypothetical = the teacher describes a general idea rather than a rule to apply or a trade taken; (7) objective = the rule can be tested mechanically without human judgement; "
        "(8) startTime/endTime are absolute seconds from the start of the video covering the quote; (9) lower confidence when speech is unclear.",
        Atoms, json.dumps({"video": {"title": video.get("title"), "channel": video.get("channel")}, "windowSeconds": window, "transcript": spoken, "chartObservations": seen}, ensure_ascii=False), video_id)
    return result["atoms"]


# ---- clustering, manuals, conflicts -----------------------------------------------------------------

async def cluster_strategy_names(names_with_samples):
    """names_with_samples: [{name, videos, sample}] -> Clusters. Merge only genuinely identical methods."""
    result = await structured(settings.GEMINI_REASONING_MODEL,
        "Group these strategy names taught by traders. Merge names ONLY when the descriptions show the SAME method (different wording, spelling, language or transliteration of the same concept). "
        "A genuinely different set of conditions is a distinct strategy; a method that adds or changes conditions of another is relation 'variation' with variationOf naming the base. "
        "canonicalName must be one of memberNames exactly as supplied (keep the teacher's terminology). Every supplied name must appear in exactly one cluster.",
        Clusters, json.dumps(names_with_samples, ensure_ascii=False))
    return result["clusters"]


async def write_manual(name, atoms):
    payload = [{"id": a["id"], "category": a["category"], "text": a["text"], "quote": a["quote"], "video": a.get("videoTitle"), "time": round(a["startTime"]),
                "provenance": a["provenance"], "visualNote": a.get("visualNote"), "timeframe": a.get("timeframe"), "instrument": a.get("instrument"), "direction": a.get("direction")} for a in atoms]
    result = await structured(settings.GEMINI_REASONING_MODEL,
        f"Write the complete beginner-readable execution manual for the strategy '{name}' STRICTLY from the supplied atoms. Every item must cite the ids of the atoms that support it in atomIds. "
        "Never add a rule, number, indicator or condition that is not in the atoms; if the sources do not specify something, leave that section empty (it will be reported as missing). "
        "Keep the teacher's terminology. execution_steps must be a numbered sequence in the order the teacher teaches it (market context, bias, levels, setup detection, confirmation, entry, stop-loss, take-profit, management, exit/invalidation) "
        "but only include steps the atoms support. Do not resolve contradictions: where atoms disagree, include each statement with its own atom ids. Do not mention video titles in the text.",
        Manual, json.dumps(payload, ensure_ascii=False))
    return result


async def find_conflicts(name, atoms):
    payload = [{"id": a["id"], "category": a["category"], "text": a["text"], "video": a.get("videoTitle"), "published": a.get("published"), "time": round(a["startTime"])} for a in atoms]
    result = await structured(settings.GEMINI_REASONING_MODEL,
        f"Find genuine CONTRADICTIONS among rules for the strategy '{name}': statements that cannot both be followed (different stop placement, conflicting entry confirmation, incompatible timeframes or sessions, different risk percentages for the same situation). "
        "Do NOT report added detail, different examples, wording differences, or rules for different instruments/situations. Each conflict must cite at least two atom ids exactly as supplied.",
        Conflicts, json.dumps(payload, ensure_ascii=False))
    return result["conflicts"]


# ---- translation to the executable rule DSL ----------------------------------------------------------

DSL_GUIDE = """
Executable rule document (JSON) understood by the backtesting engine:
{
 "name": str, "setup_name": str|null, "timeframe": "M1|M3|M5|M15|M30|H1|H2|H4|D",
 "day": {"tz": "America/New_York", "rollover_hour": 17},
 "series": { "<name>": {"type": T, ...} },   // T and params below. Add "tf":"H1" etc. to compute on a HIGHER timeframe (only its last COMPLETED bar is visible).
 "bias": {"bull": COND|null, "bear": COND|null},
 "entry": {"long": COND|null, "short": COND|null, "order": {"type":"market|limit|stop","price":OPERAND,"offset_points":0,"expire_bars":3}, "invalidation": COND|null, "cooldown_bars":0},
 "stop": {"type":"atr|points|percent|level|signal_candle|swing", "mult":.., "series":"<atr series>", "value":.., "ref":OPERAND, "buffer":0},
 "targets": [{"type":"rr|points|atr|level","value":..,"mult":..,"series":..,"ref":OPERAND,"portion":0-1}],
 "management": {"breakeven":{"trigger_r":..,"after_target":n,"offset_points":0}, "trailing":{"type":"points|atr|candle|swing","start_r":0,"value":..,"mult":..,"series":..,"n":..,"buffer":0}, "max_bars":n},
 "exits": [{"cond": COND, "directions":"LONG|SHORT|BOTH"}],
 "sessions": {"windows":[{"name","start":"HH:MM","end":"HH:MM","tz":"Europe/London","days":[0-4]}], "avoid":[same], "weekdays":[0-4], "force_flat":{"time":"HH:MM","tz":".."}},
 "risk": {"model":"percent_equity|percent_initial|fixed_cash|fixed_qty","value":1.0,"max_trades_per_day":n,"max_daily_loss_r":x,"max_consecutive_losses":n,"max_concurrent":1,"min_rr":x,"day_tz":"UTC"}
}
Series types: price{field:open|high|low|close|hl2|hlc3|ohlc4|range|body|upper_wick|lower_wick}, sma/ema/wma/rsi/atr/stdev/roc/er{period,source?}, highest/lowest{period,source?,exclude_current},
 bbands{period,mult,field:upper|middle|lower}, macd{fast,slow,signal,field:line|signal|hist}, swing_high/swing_low{n,field?:level|age} (confirmed n bars late), trend{n}, bos{n,dir:bull|bear}, choch{n,dir},
 sweep{dir,ref:swing|prior_day|range|session_range, n|period|name,start,end,tz, min_penetration?, close_back?}, fvg{dir,field:formed|top|bottom|mid,min_size?,max_age?}, order_block{dir,field,n,max_age?},
 candle{pattern:engulfing|pinbar|inside|doji,dir}, prior_day{field:open|high|low|close}, session_range{name,start,end,tz,field:high|low} (completed window only), clock{field:hour|minute|minute_of_day|weekday,tz}, vwap.
OPERAND: number | "series_or_price_name" | {"s":name,"shift":k>=0} | {"add|sub|mul|div|min|max":[..]} | {"abs":x}.
COND: {"left":OPERAND,"op":">|>=|<|<=|==|!=|crosses_above|crosses_below","right":OPERAND} | {"is":"bool_series","within":n} | {"all":[COND]} | {"any":[COND]} | {"not":COND} |
      {"seq":[COND,...],"within":n|[n..],"invalidate":COND|null}   // ordered stages on strictly later bars
All decisions are made at a bar CLOSE; orders execute from the next bar. You cannot look into the future: shifts are >= 0.
"""


class _Retry(Exception):
    pass


async def generate_json(system, prompt, model=None):
    client = get_client()
    try:
        used_model = model or settings.GEMINI_REASONING_MODEL
        await budget.require_available("strategy")
        response = await client.aio.models.generate_content(model=used_model, contents=prompt,
            config=types.GenerateContentConfig(system_instruction=EVIDENCE + " " + system, response_mime_type="application/json", temperature=0))
        await budget.record("strategy", used_model, response.usage_metadata)
        text = (response.text or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        return json.loads(text)
    finally:
        await client.aio.aclose()
        client.close()


async def translate_to_spec(name, atoms, parse_spec):
    """Ask the model for an executable rule document; validate it ourselves and let it repair at most twice."""
    payload = [{"id": a["id"], "category": a["category"], "text": a["text"], "quote": a["quote"], "provenance": a["provenance"], "timeframe": a.get("timeframe"),
                "instrument": a.get("instrument"), "direction": a.get("direction"), "objective": a.get("objective")} for a in atoms]
    system = ("Translate the teacher's strategy atoms into the executable rule document described below. Use ONLY what the atoms state. "
              "Where the atoms leave a value that the engine REQUIRES (stop distance, target, timeframe, sizing) undefined, you may propose the smallest sensible value but you MUST list it in 'assumptions' "
              "with the reason and set that component's status to 'proposed_assumption'. Components fully supported by atoms are 'source_confirmed' and must cite atom ids; components the teacher never covered and the engine "
              "does not need are 'missing'. Concepts the engine cannot express go to 'unsupported'. Never present a discretionary idea as objective.\n" + DSL_GUIDE +
              '\nReturn JSON: {"spec": {...}, "components": {"timeframe|entry|stop|targets|management|sessions|risk|filters": {"status":"source_confirmed|proposed_assumption|missing","atomIds":[..],"note":".."}}, '
              '"assumptions":[{"component":"..","text":"..","reason":".."}], "unsupported":["..."]}')
    prompt = json.dumps({"strategy": name, "atoms": payload}, ensure_ascii=False)
    last_error = None
    for attempt in range(3):
        body = prompt if last_error is None else prompt + "\nYour previous answer was rejected by the validator: " + last_error + "\nReturn a corrected full JSON document."
        document = await generate_json(system, body)
        try:
            spec = parse_spec(document["spec"])
        except Exception as exc:
            last_error = str(exc)[:800]
            continue
        return spec, document
    raise ValueError("The model could not produce a valid executable rule document: " + (last_error or "unknown error"))


# ---- grounded question answering --------------------------------------------------------------------

async def rewrite_query(question, history):
    result = await structured(settings.GEMINI_CHAT_MODEL,
        "Rewrite the user's trading question (English, Hindi or Hinglish; possibly a follow-up using pronouns) into up to 4 short English search queries for a transcript knowledge base, resolving references using the conversation. "
        "Include likely synonyms and the teacher's possible terms. Do not answer.", SearchRewrite, json.dumps({"question": question, "history": history[-6:]}, ensure_ascii=False),
        kind="qa-rewrite", think=False)
    return result["queries"][:4]


ANSWER_SYSTEM = (EVIDENCE + (
    " You answer questions about a trader's teaching using ONLY the supplied evidence items (each has an id like E1). "
    "sourceBasedAnswer: what the teaching says, step by step where useful, citing evidence ids inline like [E3]; never include anything not in the evidence. "
    "citations: ids actually used. conflicts: describe disagreements between evidence items (different videos/dates) without picking a winner. "
    "If the evidence does not answer the question set insufficient=true and say exactly what is missing. "
    "generalEducation: optional short general trading explanation that is NOT from the sources - keep it clearly separate and never use it to fill gaps in the teacher's rules. "
    "Answer in the language of the question (Hindi/Hinglish/English), keeping the teacher's terms."))


async def answer_question(question, history, evidence, strategy_context):
    """Grounded answer. Hidden reasoning is disabled: the evidence is already retrieved, so it only added ~8 s and extra output cost."""
    client = get_client()
    try:
        use_thinking_config = True
        for _ in range(2):
            try:
                await budget.require_available("qa")
                extra = {"thinking_config": NO_THINKING} if use_thinking_config else {}
                response = await client.aio.models.generate_content(model=settings.GEMINI_CHAT_MODEL,
                    contents=json.dumps({"question": question, "conversation": history[-8:], "strategy": strategy_context, "evidence": evidence}, ensure_ascii=False),
                    config=types.GenerateContentConfig(system_instruction=ANSWER_SYSTEM, response_mime_type="application/json", response_schema=AnswerOut, temperature=0, **extra))
                break
            except Exception as exc:
                if use_thinking_config and is_thinking_error(exc):
                    use_thinking_config = False
                    continue
                raise
        await budget.record("qa", settings.GEMINI_CHAT_MODEL, response.usage_metadata)
        return AnswerOut.model_validate_json(response.text).model_dump()
    finally:
        await client.aio.aclose()
        client.close()
