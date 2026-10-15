"""Source-grounded, multi-turn Q&A. Answers are validated against the retrieved evidence before they are shown."""
import asyncio
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pyapp import ai_strategy, budget, retrieval
from pyapp.config import settings
from pyapp.db import db, owner_var
from pyapp.grounding import norm

MARKER = re.compile(r"\[E(\d+)\]")
HINGLISH = {"kya", "kaise", "kaha", "kahan", "kab", "kyun", "kyu", "hai", "hain", "nahi", "nahin", "ka", "ki", "ke", "me", "mein", "se", "ko", "aur", "ye", "yeh", "kitna", "kitne", "kaun", "kis",
            "wala", "wale", "lagate", "lagana", "karte", "karna", "batao", "samjhao", "kaisa", "kon", "tha", "thi", "hota", "hoti", "kar", "kare"}


def needs_rewrite(question: str) -> bool:
    """The rewrite call only helps when the question is not plain English (Hindi script or Hinglish words) or is a follow-up."""
    return any(ord(c) > 127 for c in question) or bool(HINGLISH & set(re.findall(r"[a-z]+", question.lower())))


_last = datetime.min.replace(tzinfo=timezone.utc)
_answers: OrderedDict = OrderedDict()      # identical question over identical evidence is answered from memory (no model cost)
ANSWER_CACHE_SIZE, ANSWER_CACHE_TTL = 200, 3600


def now():
    """Strictly increasing timestamps so message order never depends on the OS clock resolution."""
    global _last
    t = datetime.now(timezone.utc)
    if t <= _last:
        t = _last + timedelta(microseconds=1)
    _last = t
    return t.isoformat()


def evidence_view(items):
    out = []
    for n, item in enumerate(items, start=1):
        out.append({"id": f"E{n}", "kind": item["kind"], "text": item["text"], "quote": item.get("quote"), "video": item.get("video"), "videoId": item.get("videoId"), "time": item.get("time"),
                    "url": item.get("url"), "provenance": item["provenance"], "category": item.get("category"), "strategy": item.get("strategy"), "published": item.get("published"),
                    "frames": [{"timestamp": f["timestamp"], "url": f["url"]} for f in item.get("frames", [])][:3], "visualNote": item.get("visualNote"), "key": item["key"]})
    return out


def validate_answer(out: dict, evidence: list) -> dict:
    """Keep only citations that exist; refuse to present un-cited model text as source-based."""
    valid = {e["id"] for e in evidence}
    notes = []
    citations = [c for c in dict.fromkeys(out.get("citations", [])) if c in valid]
    used = {f"E{m}" for m in MARKER.findall(out.get("sourceBasedAnswer", ""))}
    citations = list(dict.fromkeys(citations + [u for u in used if u in valid]))
    answer = MARKER.sub(lambda m: m.group(0) if f"E{m.group(1)}" in valid else "", out.get("sourceBasedAnswer", ""))
    if used - valid:
        notes.append("Removed references to evidence that was not retrieved.")
    result = {**out, "sourceBasedAnswer": answer, "citations": citations, "validationNotes": notes}
    if answer.strip() and not citations and not out.get("insufficient"):
        result.update(sourceBasedAnswer="", insufficient=True, missing="The generated answer cited no retrieved evidence, so it was withheld.", ungroundedWithheld=True)
        notes.append("Answer withheld: no verifiable citation.")
    if not citations and not result.get("insufficient"):
        result["insufficient"] = True
    return result


def extractive_answer(question, evidence):
    """Used when the language model is unavailable: quote the best-matching passages without synthesising anything."""
    lines = []
    for e in evidence[:5]:
        label = (e["quote"] or e["text"])[:300]
        when = f" ({int(e['time'] // 60)}:{int(e['time'] % 60):02d})" if e.get("time") is not None else ""
        lines.append(f"{label} - {e.get('video') or ''}{when} [{e['id']}]")
    return {"sourceBasedAnswer": "AI answer generation is not configured, so these are the closest passages from your analysed videos:\n" + "\n".join(lines), "steps": [], "citations": [e["id"] for e in evidence[:5]],
            "conflicts": [], "generalEducation": None, "insufficient": False, "missing": None, "followUps": []}


async def ask(conversation: dict, question: str) -> dict:
    prior = sorted(await db.query("messages", {"parentId": conversation["id"]}), key=lambda m: m["createdAt"])
    history = [{"role": m["role"], "content": m["text"]} for m in prior][-10:]
    configured = not settings.MOCK_AI_MODE and bool(settings.GEMINI_API_KEY)
    limit_note = await budget.blocked("qa") if configured else None
    ai_on = configured and not limit_note
    items = await retrieval.build_corpus(conversation.get("strategyId"))
    fingerprint = hash(tuple(sorted((i["key"], retrieval.item_hash(i)) for i in items))) if ai_on and not history else None
    cache_key = (owner_var.get(), conversation.get("strategyId"), norm(question), fingerprint) if fingerprint is not None else None
    cached = _answers.get(cache_key) if cache_key else None
    if cached and datetime.now(timezone.utc).timestamp() - cached[0] < ANSWER_CACHE_TTL:
        return await _finish(conversation, question, {**cached[1], "validationNotes": cached[1].get("validationNotes", []) + ["Repeated question over unchanged evidence: answered from memory, no AI cost."]},
                             cached[2], True)
    queries, vectors = [], None
    if ai_on and not history:
        # First turn: embedding the question and (when useful) rewriting it are independent network calls, so run them together.
        rewrite = asyncio.create_task(ai_strategy.rewrite_query(question, history)) if needs_rewrite(question) else None
        vectors = await retrieval.vector_scores(question, items)
        if rewrite:
            try:
                queries = await rewrite
            except Exception:
                queries = []
    elif ai_on:
        try:
            queries = await ai_strategy.rewrite_query(question, history)       # follow-ups need pronouns resolved before searching
        except Exception:
            queries = []
    picked = await retrieval.retrieve(question, queries, items, vectors=vectors)
    evidence = evidence_view(picked)
    strategy = None
    if conversation.get("strategyId"):
        s = await db.get("lib_strategies", conversation["strategyId"])
        if s:
            strategy = {"name": s["name"], "aliases": s["aliases"], "knownConflicts": len(s["conflicts"]), "notSpecifiedBySources": s["manualMissing"]}
    if not evidence:
        out = {"sourceBasedAnswer": "", "steps": [], "citations": [], "conflicts": [], "generalEducation": None, "insufficient": True,
               "missing": "No analysed video content matches this question. Analyse the relevant videos first, or rephrase using the teacher's terms.", "followUps": []}
    elif ai_on:
        try:
            out = validate_answer(await ai_strategy.answer_question(question, history, [{k: v for k, v in e.items() if k not in ("frames", "key")} for e in evidence], strategy), evidence)
            if cache_key and not out.get("ungroundedWithheld"):
                _answers[cache_key] = (datetime.now(timezone.utc).timestamp(), out, evidence)
                while len(_answers) > ANSWER_CACHE_SIZE:
                    _answers.popitem(last=False)
        except Exception as exc:
            out = {**extractive_answer(question, evidence), "validationNotes": [f"Language model unavailable ({type(exc).__name__}); showing source passages only."]}
    else:
        out = extractive_answer(question, evidence)
        if limit_note:
            out["validationNotes"] = [limit_note]
    return await _finish(conversation, question, out, evidence, ai_on)


async def _finish(conversation: dict, question: str, out: dict, evidence: list, ai_on: bool) -> dict:
    user_msg = {"id": uuid4().hex, "parentId": conversation["id"], "role": "user", "text": question, "createdAt": now()}
    assistant = {"id": uuid4().hex, "parentId": conversation["id"], "role": "assistant", "text": out.get("sourceBasedAnswer") or out.get("missing") or "", "answer": out,
                 "evidence": [e for e in evidence if e["id"] in set(out.get("citations", []))] + [e for e in evidence if e["id"] not in set(out.get("citations", []))][:4],
                 "retrieved": len(evidence), "createdAt": now(), "mode": "ai" if ai_on else "extractive"}
    await db.save("messages", user_msg)
    await db.save("messages", assistant)
    if not conversation.get("title") or conversation["title"] == "New conversation":
        conversation["title"] = question[:70]
    conversation["updatedAt"] = now()
    await db.save("conversations", conversation)
    return assistant
