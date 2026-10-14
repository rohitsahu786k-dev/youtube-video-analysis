"""Automatically draft explicit supported rules without inventing missing parameters."""
from pyapp.ai import propose_rule
from pyapp.db import db
from pyapp.intelligence import build_patterns, evidence_hash

async def discover_proposals():
    trades, knowledge = await db.get_trades(), await db.list("knowledge")
    existing = await db.list("proposals")
    for pattern in build_patterns(trades, knowledge):
        if pattern["status"] != "RECURRING":
            continue
        sources = [t for t in trades if t["id"] in pattern["tradeIds"]] + [k for k in knowledge if k["id"] in pattern["knowledgeIds"]]
        fingerprint = evidence_hash(sources)
        identifier = pattern["id"] + "-" + fingerprint[:16]
        if any(p["id"] == identifier for p in existing):
            continue
        # Balanced representatives avoid a single prolific video dominating the prompt.
        representatives = {}
        for source in sources:
            representatives.setdefault(source["videoId"], source)
        proposal = await propose_rule(pattern, list(representatives.values())[:20])
        await db.save("proposals", {**proposal, "id": identifier, "patternId": pattern["id"], "name": pattern["name"],
            "sourceEvidenceHash": fingerprint, "reviewed": False})
