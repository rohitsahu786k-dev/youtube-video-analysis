"""Programmatic evidence checks: an AI-extracted rule is only 'source-confirmed' if its quote really occurs in the transcript.

This is the main defence against invented rules: the model must copy transcript text verbatim and we verify it ourselves.
"""
import re
import unicodedata
from difflib import SequenceMatcher


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _windows(segments, max_span=4):
    """Normalised text of every run of up to `max_span` consecutive segments with its time range."""
    normalised = [norm(s["text"]) for s in segments]
    for i in range(len(segments)):
        text = ""
        for j in range(i, min(len(segments), i + max_span)):
            text = (text + " " + normalised[j]).strip()
            yield i, j, text


def ground_quote(quote: str, segments: list, claimed_start: float | None = None, fuzzy_threshold: float = 0.82) -> dict:
    """Locate `quote` in the transcript. Returns verification status, method, matched time range and similarity."""
    target = norm(quote)
    if len(target) < 6 or not segments:
        return {"verified": False, "method": None, "ratio": 0.0, "start": None, "end": None, "reason": "quote too short or no transcript"}
    best_exact, best_fuzzy = None, None
    for i, j, text in _windows(segments):
        if target in text:
            distance = abs(segments[i]["start"] - claimed_start) if claimed_start is not None else 0.0
            rank = (j - i, distance)                      # the tightest window containing the quote, then the one nearest the claimed time
            if best_exact is None or rank < best_exact[0]:
                best_exact = (rank, i, j)
            continue
        if len(text) >= len(target) * 0.6 and len(text) <= len(target) * 2.2:
            ratio = SequenceMatcher(None, target, text, autojunk=False).ratio()
            if ratio >= fuzzy_threshold and (best_fuzzy is None or (ratio, -(j - i)) > (best_fuzzy[0], -(best_fuzzy[2] - best_fuzzy[1]))):
                best_fuzzy = (ratio, i, j)
    if best_exact:
        _, i, j = best_exact
        return {"verified": True, "method": "exact", "ratio": 1.0, "start": segments[i]["start"], "end": segments[j]["end"]}
    if best_fuzzy:
        ratio, i, j = best_fuzzy
        return {"verified": True, "method": "fuzzy", "ratio": round(ratio, 3), "start": segments[i]["start"], "end": segments[j]["end"]}
    return {"verified": False, "method": None, "ratio": 0.0, "start": None, "end": None, "reason": "quote not found in the transcript"}


def frames_near(observations: list, start: float, end: float, before: float = 10.0, after: float = 20.0, limit: int = 4) -> list:
    """Frame references from visual observations that overlap the atom's time range (audio-visual alignment)."""
    frames = []
    for obs in observations:
        for frame in obs.get("frames", []):
            if start - before <= frame["timestamp"] <= end + after:
                frames.append({"timestamp": frame["timestamp"], "url": frame["url"], "sha256": frame.get("sha256")})
    frames.sort(key=lambda f: abs(f["timestamp"] - (start + end) / 2))
    seen, picked = set(), []
    for f in frames:
        if f["url"] not in seen:
            seen.add(f["url"])
            picked.append(f)
        if len(picked) >= limit:
            break
    return sorted(picked, key=lambda f: f["timestamp"])


def provenance_of(atom: dict) -> str:
    """source_confirmed requires a verified quote; visual-only claims additionally need a captured frame."""
    if not atom.get("quoteVerified"):
        return "missing_unclear"
    if atom.get("evidenceType") == "visual" and not atom.get("frames"):
        return "missing_unclear"
    if atom.get("confidence", 0) < 0.5:
        return "missing_unclear"
    return "source_confirmed"


def youtube_link(youtube_id: str, seconds: float) -> str:
    return f"https://www.youtube.com/watch?v={youtube_id}&t={int(max(0, seconds or 0))}s"
