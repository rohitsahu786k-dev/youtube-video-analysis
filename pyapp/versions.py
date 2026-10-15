"""Versioned, approvable test configurations derived from extracted strategies.

The instructor's extracted rules (lib_strategies.manual) are never edited. Executable interpretations live in strategy_versions:
DRAFT (editable) -> APPROVED (explicit approver + acknowledged assumptions) -> FROZEN (immutable; auto-applied on first backtest).
"""
from datetime import datetime, timezone
from uuid import uuid4

from pyapp.db import db
from pyapp.engine.runner import parse_spec
from pyapp.engine.spec import SpecError, digest

REQUIRED = ("timeframe", "entry", "stop", "targets")
OPTIONAL = ("management", "sessions", "risk", "filters")
STATUSES = ("DRAFT", "APPROVED", "FROZEN")


def now():
    return datetime.now(timezone.utc).isoformat()


def classify_readiness(components: dict, assumptions: list, unsupported: list, spec: dict | None = None, user_authored: bool = False) -> dict:
    """Fully / partially / not reliably backtestable, from provenance of each component (SRS 9)."""
    if user_authored:
        return {"level": "USER_DEFINED", "fidelity": "USER_AUTHORED_RULES", "reasons": ["Rules were written by you, not extracted from an instructor; results say nothing about any instructor's method."]}
    status = lambda c: (components.get(c) or {}).get("status", "missing")
    reasons, level = [], "FULLY_BACKTESTABLE"
    targets_ok = status("targets") != "missing" or any(status(c) != "missing" for c in ("management",)) or bool(spec and (spec.get("exits") or spec.get("management", {}).get("trailing")))
    missing = [c for c in ("timeframe", "entry", "stop") if status(c) == "missing"]
    if not targets_ok:
        missing.append("targets/exit")
    if missing:
        level = "NOT_RELIABLY_BACKTESTABLE"
        reasons.append("Not specified by the instructor and no faithful programmatic representation exists: " + ", ".join(missing) + ".")
    else:
        weak = [c for c in REQUIRED + OPTIONAL if status(c) == "proposed_assumption"]
        if weak:
            level = "PARTIALLY_BACKTESTABLE"
            reasons.append("Contains interpretations that need your approval: " + ", ".join(weak) + ".")
        if assumptions and level == "FULLY_BACKTESTABLE":
            level = "PARTIALLY_BACKTESTABLE"
            reasons.append(f"{len(assumptions)} assumption(s) were introduced to make the idea executable.")
        if unsupported:
            level = "PARTIALLY_BACKTESTABLE"
            reasons.append("Concepts the engine cannot reproduce: " + "; ".join(unsupported[:6]) + ".")
        unconfirmed = [c for c in REQUIRED if status(c) not in ("source_confirmed", "proposed_assumption")]
        if unconfirmed and level == "FULLY_BACKTESTABLE":
            level = "PARTIALLY_BACKTESTABLE"
            reasons.append("Some core components lack source confirmation: " + ", ".join(unconfirmed) + ".")
    if level == "FULLY_BACKTESTABLE":
        reasons.append("Every material condition is source-confirmed, objective and programmable.")
    fidelity = "FAITHFUL_TO_INSTRUCTOR" if level == "FULLY_BACKTESTABLE" else "CONTAINS_APPROVED_ASSUMPTIONS" if level == "PARTIALLY_BACKTESTABLE" else "NOT_A_FAITHFUL_TEST"
    return {"level": level, "reasons": reasons, "fidelity": fidelity}


async def next_number(strategy_id):
    versions = await db.query("strategy_versions", {"strategyId": strategy_id})
    return max([v["number"] for v in versions], default=0) + 1


async def create_version(strategy_id, spec_document, *, author, components=None, assumptions=None, unsupported=None, parent=None, kind="test_config", note="", label=None, evidence_hash=None):
    spec = parse_spec(spec_document)                       # raises SpecError with a readable message
    number = await next_number(strategy_id)
    assumptions = [{"id": uuid4().hex[:8], **a, "approved": False} for a in (assumptions or [])]
    version = {"id": uuid4().hex, "strategyId": strategy_id, "number": number, "label": label or f"v{number}", "kind": kind, "status": "DRAFT",
               "spec": spec.dump(), "specHash": digest(spec.dump()), "components": components or {}, "assumptions": assumptions,
               "unsupported": unsupported or [], "parentVersionId": parent, "author": author, "createdAt": now(), "approvedBy": None, "approvedAt": None, "frozenAt": None,
               "sourceEvidenceHash": evidence_hash, "changeLog": [{"at": now(), "by": author, "change": note or "Version created"}]}
    version["userAuthored"] = strategy_id.startswith("custom-")
    version["readiness"] = classify_readiness(version["components"], assumptions, version["unsupported"], version["spec"], version["userAuthored"])
    await db.save("strategy_versions", version)
    return version


async def update_draft(version_id, spec_document, *, author, note, components=None, assumptions=None):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise SpecError("Version not found.")
    if version["status"] != "DRAFT":
        raise SpecError(f"Version {version['label']} is {version['status']} and cannot be edited; create a new version instead.")
    spec = parse_spec(spec_document)
    changed = digest(spec.dump()) != version["specHash"]
    version.update(spec=spec.dump(), specHash=digest(spec.dump()))
    if components is not None:
        version["components"] = components
    if assumptions is not None:
        known = {a["id"]: a for a in version["assumptions"]}
        version["assumptions"] = [{**{"id": uuid4().hex[:8], "approved": False}, **a, "approved": bool(known.get(a.get("id"), {}).get("approved")) and not changed} for a in assumptions]
    elif changed:
        for a in version["assumptions"]:
            a["approved"] = False                          # an edited rule must be re-approved
    version["readiness"] = classify_readiness(version["components"], version["assumptions"], version["unsupported"], version["spec"], version.get("userAuthored", False))
    version["changeLog"].append({"at": now(), "by": author, "change": note or "Edited"})
    await db.save("strategy_versions", version)
    return version


async def approve(version_id, approver, acknowledged: list[str]):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise SpecError("Version not found.")
    if version["status"] != "DRAFT":
        raise SpecError(f"Version is already {version['status']}.")
    parse_spec(version["spec"])
    pending = [a for a in version["assumptions"] if a["id"] not in set(acknowledged)]
    if pending:
        raise SpecError("Every assumption must be explicitly approved. Missing approval for: " + "; ".join(a["text"] for a in pending[:5]))
    for a in version["assumptions"]:
        a.update(approved=True, approvedBy=approver, approvedAt=now())
    version.update(status="APPROVED", approvedBy=approver, approvedAt=now())
    version["changeLog"].append({"at": now(), "by": approver, "change": f"Approved with {len(version['assumptions'])} acknowledged assumption(s)"})
    await db.save("strategy_versions", version)
    return version


async def freeze(version_id, by):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise SpecError("Version not found.")
    if version["status"] == "DRAFT":
        raise SpecError("Approve the version before freezing it.")
    if version["status"] == "FROZEN":
        return version
    if digest(version["spec"]) != version["specHash"]:
        raise SpecError("Specification hash mismatch; refusing to freeze a modified document.")
    version.update(status="FROZEN", frozenAt=now())
    version["changeLog"].append({"at": now(), "by": by, "change": "Frozen for testing"})
    await db.save("strategy_versions", version)
    return version


async def fork(version_id, author, note="Forked for modification"):
    version = await db.get("strategy_versions", version_id)
    if not version:
        raise SpecError("Version not found.")
    return await create_version(version["strategyId"], version["spec"], author=author, components=version["components"],
                                assumptions=[{k: v for k, v in a.items() if k not in ("id", "approved", "approvedBy", "approvedAt")} for a in version["assumptions"]],
                                unsupported=version["unsupported"], parent=version["id"], note=note, evidence_hash=version.get("sourceEvidenceHash"))


def provenance_from_translation(document: dict, valid_atom_ids: set) -> tuple[dict, list, list]:
    """Validate the model's claim of what is source-confirmed: cited atoms must exist, otherwise the component becomes an assumption."""
    components, assumptions = {}, [dict(a) for a in document.get("assumptions", []) if isinstance(a, dict)]
    for name, comp in (document.get("components") or {}).items():
        if not isinstance(comp, dict):
            continue
        status = comp.get("status", "missing")
        ids = [i for i in comp.get("atomIds", []) if i in valid_atom_ids]
        if status == "source_confirmed" and not ids:
            status = "proposed_assumption"
            assumptions.append({"component": name, "text": f"The model claimed '{name}' is source-confirmed but cited no verifiable evidence.", "reason": "Downgraded automatically."})
        if status not in ("source_confirmed", "proposed_assumption", "missing", "user_setting"):
            status = "proposed_assumption"
        components[name] = {"status": status, "atomIds": ids, "note": comp.get("note")}
    for name in REQUIRED:
        components.setdefault(name, {"status": "missing", "atomIds": [], "note": "Not provided by the translation."})
    return components, assumptions, [str(u) for u in document.get("unsupported", [])]
