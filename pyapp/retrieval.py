"""Hybrid retrieval over rule atoms, transcript passages, manual items and trade examples (BM25 + optional embeddings)."""
import array
import base64
import hashlib
import logging
import math
import re
from collections import Counter

import numpy as np

from pyapp.config import settings
from pyapp.db import db, owner_var
from pyapp.grounding import norm, youtube_link

logger = logging.getLogger(__name__)
TOKEN = re.compile(r"\w+", re.UNICODE)
STOP = {"the", "a", "an", "is", "are", "of", "to", "and", "or", "in", "on", "for", "it", "this", "that", "what", "how", "when", "why", "which", "do", "does", "can", "i", "you", "if", "be", "at", "with",
        "kya", "hai", "ka", "ki", "ke", "se", "me", "mein", "ko", "aur", "ye", "yeh", "to", "kaise", "kab", "kyun"}
_vector_cache: dict = {}


def tokens(text):
    return [t for t in TOKEN.findall(norm(text)) if t not in STOP and len(t) > 1]


def bm25(query_tokens, documents, k1=1.4, b=0.75):
    """Okapi BM25 scores for tokenised documents."""
    if not documents:
        return []
    n = len(documents)
    avg = sum(len(d) for d in documents) / n or 1
    df = Counter()
    for d in documents:
        df.update(set(d))
    scores = []
    for d in documents:
        counts, length, score = Counter(d), len(d), 0.0
        for term in query_tokens:
            if term not in counts:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += idf * counts[term] * (k1 + 1) / (counts[term] + k1 * (1 - b + b * length / avg))
        scores.append(score)
    return scores


# ---- corpus -----------------------------------------------------------------------------------------

def atom_item(a):
    return {"key": a["id"], "kind": "atom", "text": a["text"], "quote": a["quote"], "video": a["videoTitle"], "videoId": a["videoId"], "youtubeId": a.get("youtubeId"), "time": a["startTime"],
            "url": a["sourceUrl"], "provenance": a["provenance"], "category": a["category"], "strategy": a.get("strategyName"), "frames": a.get("frames", []), "visualNote": a.get("visualNote"),
            "published": a.get("published"), "hypothetical": a.get("hypothetical")}


def knowledge_item(k):
    return {"key": k["id"], "kind": "spoken" if k["type"] == "Spoken" else "visual" if k["type"] == "Visual" else k["type"].lower(), "text": k["content"], "quote": k["content"] if k["type"] == "Spoken" else None,
            "video": k["video"], "videoId": k["videoId"], "youtubeId": k["videoId"].removeprefix("video-"), "time": k["time"], "url": k["sourceUrl"], "provenance": "transcript" if k["type"] == "Spoken" else "ai_reading",
            "category": k["type"], "strategy": k.get("strategyTag"), "frames": k.get("frames", []), "title": k.get("title")}


def manual_items(strategy):
    out = []
    manual = strategy.get("manual") or {}
    for section, items in manual.items():
        for n, item in enumerate(items):
            out.append({"key": f"{strategy['id']}:{section}:{n}", "kind": "manual", "text": item["text"], "quote": None, "video": strategy["name"], "videoId": None, "youtubeId": None, "time": None, "url": None,
                        "provenance": "source_confirmed", "category": section, "strategy": strategy["name"], "frames": [], "atomIds": item.get("atomIds", []), "strategyId": strategy["id"]})
    return out


async def build_corpus(strategy_id=None):
    atoms, strategies = await db.list("atoms"), await db.list("lib_strategies")
    items, scope_ids, general_videos = [], None, set()
    if strategy_id:
        strategy = next((s for s in strategies if s["id"] == strategy_id), None)
        if strategy:
            scope_ids = set(strategy["atomIds"]) | set(strategy.get("generalAtomIds", []))
            general_videos = {s["videoId"] for s in strategy["sources"]}
            items += manual_items(strategy)
    for a in atoms:
        if scope_ids is None or a["id"] in scope_ids:
            items.append(atom_item(a))
    knowledge = await db.list("knowledge")
    for k in knowledge:
        if k["type"] in ("Spoken", "Visual", "Spoken", "Inference") and (scope_ids is None or k["videoId"] in general_videos):
            items.append(knowledge_item(k))
    if scope_ids is None:
        for s in strategies:
            items += manual_items(s)
    return items


# ---- embeddings -------------------------------------------------------------------------------------

def _pack(vector):
    return base64.b64encode(array.array("f", vector).tobytes()).decode()


def _unpack(text):
    out = array.array("f")
    out.frombytes(base64.b64decode(text))
    return out


async def embed_texts(texts):
    from google.genai import types
    from pyapp.ai import get_client
    client = get_client()
    try:
        vectors = []
        for start in range(0, len(texts), 64):
            response = await client.aio.models.embed_content(model=settings.GEMINI_EMBEDDING_MODEL, contents=texts[start:start + 64],
                                                              config=types.EmbedContentConfig(output_dimensionality=768, task_type="RETRIEVAL_DOCUMENT"))
            vectors.extend(e.values for e in response.embeddings)
        return vectors
    finally:
        await client.aio.aclose()
        client.close()


async def embed_query(text):
    from google.genai import types
    from pyapp.ai import get_client
    client = get_client()
    try:
        response = await client.aio.models.embed_content(model=settings.GEMINI_EMBEDDING_MODEL, contents=[text], config=types.EmbedContentConfig(output_dimensionality=768, task_type="RETRIEVAL_QUERY"))
        return response.embeddings[0].values
    finally:
        await client.aio.aclose()
        client.close()


def item_hash(item):
    return hashlib.sha1(((item.get("text") or "") + "|" + (item.get("quote") or "")).encode()).hexdigest()[:12]


async def _owner_vectors(owner):
    """Per-owner in-memory index: key -> (content hash, unit vector). The table is read once per process, not once per question."""
    cache = _vector_cache.get(owner)
    if cache is None:
        cache = {}
        for doc in await db.list("embeddings"):
            try:
                vec = np.frombuffer(base64.b64decode(doc["v"]), dtype=np.float32)
                length = float(np.linalg.norm(vec)) or 1.0
                cache[doc["id"]] = (doc.get("hash"), vec / length)
            except (KeyError, ValueError):
                continue
        _vector_cache[owner] = cache
    return cache


async def sync_embeddings(items, limit_new=400):
    """Embed items that are new or whose text changed (one cheap batch call), persist them and refresh the index."""
    owner = owner_var.get()
    cache = await _owner_vectors(owner)
    missing = [i for i in items if cache.get(i["key"], (None,))[0] != item_hash(i)][:limit_new]
    if missing:
        vectors = await embed_texts([(i["text"] + " " + (i.get("quote") or ""))[:1500] for i in missing])
        docs = []
        for item, vec in zip(missing, vectors):
            arr = np.asarray(vec, dtype=np.float32)
            cache[item["key"]] = (item_hash(item), arr / (float(np.linalg.norm(arr)) or 1.0))
            docs.append({"id": item["key"], "v": _pack(vec), "hash": item_hash(item), "dim": len(vec)})
        await db.save_many("embeddings", docs)
    return cache


async def vector_scores(question, items, limit_new=400):
    """Cosine similarity to every item that has an embedding; empty when embeddings are unavailable."""
    if settings.MOCK_AI_MODE or not settings.GEMINI_API_KEY or not items:
        return {}
    try:
        cache = await sync_embeddings(items, limit_new)
        query = np.asarray(await embed_query(question), dtype=np.float32)
        query /= float(np.linalg.norm(query)) or 1.0
        keys = [i["key"] for i in items if cache.get(i["key"], (None,))[0] == item_hash(i)]
        if not keys:
            return {}
        matrix = np.stack([cache[k][1] for k in keys])
        return dict(zip(keys, (matrix @ query).tolist()))
    except Exception:
        logger.exception("Vector retrieval unavailable; using lexical retrieval only")
        return {}


async def warm(items=None):
    """Embed everything searchable now (after an analysis finishes) so the first chat question does not pay for it."""
    if settings.MOCK_AI_MODE or not settings.GEMINI_API_KEY:
        return
    try:
        await sync_embeddings(items if items is not None else await build_corpus(None), limit_new=2000)
    except Exception:
        logger.exception("Embedding warm-up failed; questions will embed lazily")


async def retrieve(question, queries, items, limit=14, vectors=None):
    """Reciprocal-rank fusion of lexical and vector rankings. Unverified atoms are retrievable but flagged."""
    if not items:
        return []
    docs = [tokens(i["text"] + " " + (i.get("quote") or "") + " " + (i.get("strategy") or "")) for i in items]
    qtokens = []
    for q in [question] + list(queries):
        qtokens += tokens(q)
    lexical = bm25(qtokens, docs)
    rank_lex = {items[i]["key"]: r for r, i in enumerate(sorted(range(len(items)), key=lambda i: lexical[i], reverse=True)) if lexical[i] > 0}
    if vectors is None:
        vectors = await vector_scores(" ".join([question] + list(queries))[:800], items)
    rank_vec = {k: r for r, k in enumerate(sorted(vectors, key=vectors.get, reverse=True)[:200])}
    fused = {}
    for key in set(rank_lex) | set(rank_vec):
        fused[key] = (1 / (60 + rank_lex[key]) if key in rank_lex else 0) + (1 / (60 + rank_vec[key]) if key in rank_vec else 0)
    by_key = {i["key"]: i for i in items}
    ranked = [by_key[k] for k in sorted(fused, key=fused.get, reverse=True)]
    # prefer verified evidence, keep a little diversity across videos
    ranked.sort(key=lambda i: (i["provenance"] == "missing_unclear",))
    picked, per_video, seen_text = [], Counter(), set()
    for item in ranked:
        fingerprint = norm(item.get("quote") or item["text"])
        if fingerprint in seen_text:
            continue                       # the same words stored as an atom and as a transcript chunk
        seen_text.add(fingerprint)
        if per_video[item.get("videoId")] >= 6 and len(ranked) > limit * 2:
            continue
        picked.append(item)
        per_video[item.get("videoId")] += 1
        if len(picked) >= limit:
            break
    return picked
