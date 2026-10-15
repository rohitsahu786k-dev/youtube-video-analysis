"""AI spend and server headroom, so cost and capacity are visible instead of discovered on an invoice or an outage."""
from fastapi import APIRouter, Request

from pyapp import budget, limits
from pyapp.routes.account import require_admin

router = APIRouter(prefix="/api/usage", tags=["Usage"])


@router.get("/summary")
async def usage_summary():
    return {**await budget.summary(), "providerHalt": await budget.provider_halt(),
            "storage": limits.storage_status(), "storageProblem": limits.storage_problem()}


@router.post("/resume")
async def resume_provider(request: Request):
    """Clear the circuit breaker tripped when Gemini reported its billing/quota exhausted. Call this only
    after confirming the Gemini project has been topped up, or new calls will immediately fail again."""
    require_admin(request)
    await budget.clear_provider_halt()
    return {"ok": True}
