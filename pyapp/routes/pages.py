"""Server-rendered workspace pages. Heavy data is fetched by the page scripts from the JSON API."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pyapp import auth
from pyapp.config import settings
from pyapp.db import db
from pyapp.routes import knowledge as kn
from pyapp.sources import status_group

router = APIRouter(tags=["Pages"])
templates = Jinja2Templates(directory="pyapp/templates")
SECTIONS = ("objective", "instruments_timeframes_sessions", "chart_setup_indicators", "chart_preparation", "htf_analysis", "execution_steps", "entry_rules", "stop_loss_rules", "take_profit_rules",
            "trade_management", "risk_management", "sessions_time", "avoidance", "supporting_knowledge")


def render(request: Request, name: str, active: str, **context):
    user = getattr(request.state, "user", None)
    base = {"active_page": active, "current_user": user if user and user.get("passwordHash") is None else user, "auth_required": settings.auth_required,
            "runtime_ai_configured": bool(settings.GEMINI_API_KEY) and not settings.MOCK_AI_MODE, "settings": settings}
    return templates.TemplateResponse(request=request, name=name, context={**base, **context})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if settings.auth_required and await auth.user_from_request(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"auth_required": settings.auth_required})


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return render(request, "dashboard.html", "dashboard", d=await kn.dashboard(), usage={"calls": len(await db.list("usage"))})


@router.get("/media", response_class=HTMLResponse)
async def media_page(request: Request):
    return render(request, "media.html", "media")


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request):
    return render(request, "sources.html", "sources", ai_ready=bool(settings.GEMINI_API_KEY) and not settings.MOCK_AI_MODE, include_shorts=settings.INCLUDE_SHORTS, max_channel=settings.MAX_CHANNEL_VIDEOS)


@router.get("/videos", response_class=HTMLResponse)
async def videos_page(request: Request, q: str = "", group: str = "", source: str = ""):
    videos = await db.get_videos()
    rows = [v for v in videos if (not q or q.lower() in (v.get("title") or "").lower()) and (not group or status_group(v["status"]) == group) and (not source or source in v.get("sourceIds", []))]
    counts = {}
    for v in videos:
        counts[status_group(v["status"])] = counts.get(status_group(v["status"]), 0) + 1
    return render(request, "videos.html", "videos", videos=rows, counts=counts, q=q, group=group, source=source, total=len(videos), status_group=status_group)


@router.get("/videos/{video_id}", response_class=HTMLResponse)
async def video_page(request: Request, video_id: str):
    video = await db.get_video(video_id)
    if not video:
        raise HTTPException(404, "Video not found")
    details = await db.get("video_details", video["id"]) or {}
    atoms = sorted(await db.query("atoms", {"videoId": video["id"]}), key=lambda a: a["startTime"])
    return render(request, "video_detail.html", "videos", video=video, trades=await db.get_trades(video["id"]), events=details.get("events") or video.get("detectedEvents", []),
                  transcript=details.get("transcript") or video.get("transcript", []), observations=details.get("observations", []), atoms=atoms, status_group=status_group,
                  estimated=bool(details.get("timestampsEstimated")))


@router.get("/library", response_class=HTMLResponse)
async def library_page(request: Request):
    return render(request, "library.html", "library", ai_ready=bool(settings.GEMINI_API_KEY) and not settings.MOCK_AI_MODE)


@router.get("/library/{strategy_id}", response_class=HTMLResponse)
async def strategy_page(request: Request, strategy_id: str):
    strategy = await kn.get_strategy(strategy_id)
    return render(request, "strategy.html", "library", s=strategy, sections=SECTIONS)


@router.get("/qa", response_class=HTMLResponse)
async def qa_page(request: Request, strategy: str = ""):
    strategies = [{"id": s["id"], "name": s["name"]} for s in await db.list("lib_strategies") if not s.get("orphaned")]
    return render(request, "qa.html", "qa", strategies=strategies, scope=strategy)


@router.get("/editor", response_class=HTMLResponse)
async def editor_page(request: Request, strategy: str = "", version: str = ""):
    strategies = [{"id": s["id"], "name": s["name"]} for s in await db.list("lib_strategies")]
    return render(request, "editor.html", "editor", strategies=strategies, strategy_id=strategy, version_id=version, ai_ready=bool(settings.GEMINI_API_KEY) and not settings.MOCK_AI_MODE)


@router.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request, version: str = ""):
    versions = [v for v in await db.list("strategy_versions") if v["status"] in ("APPROVED", "FROZEN")]
    names = {s["id"]: s["name"] for s in await db.list("lib_strategies")}
    return render(request, "backtest.html", "backtest", versions=[{"id": v["id"], "label": v["label"], "strategy": names.get(v["strategyId"], v["strategyId"]), "timeframe": v["spec"]["timeframe"], "status": v["status"],
                                                                   "fidelity": (v.get("readiness") or {}).get("fidelity"), "kind": v["kind"]} for v in versions],
                  datasets=await db.list("market_data"), selected=version, oanda=bool(settings.OANDA_API_TOKEN and settings.OANDA_ACCOUNT_ID), display_tz=settings.DISPLAY_TZ)


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(request: Request, run_id: str, tab: str = ""):
    run = await db.get("bt_runs", run_id)
    if not run:
        raise HTTPException(404, "Backtest run not found")
    return render(request, "run.html", "analytics", run=run, tab=tab)


async def run_chooser(request: Request, active: str, tab: str, title: str):
    runs = await db.list("bt_runs")
    if runs:
        return RedirectResponse(f"/runs/{runs[0]['id']}#{tab}", status_code=303) if request.query_params.get("run") is None else RedirectResponse(f"/runs/{request.query_params['run']}#{tab}", status_code=303)
    return render(request, "runs_empty.html", active, title=title)


@router.get("/journal", response_class=HTMLResponse)
async def journal_page(request: Request):
    return await run_chooser(request, "journal", "journal", "Trade Journal")


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return await run_chooser(request, "analytics", "overview", "Analytics Dashboard")


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    return render(request, "runs.html", "analytics")


@router.get("/compare", response_class=HTMLResponse)
async def compare_page(request: Request):
    return render(request, "compare.html", "compare")


@router.get("/integrations", response_class=HTMLResponse)
async def integrations_page(request: Request):
    return render(request, "integrations.html", "integrations", oanda=bool(settings.OANDA_API_TOKEN and settings.OANDA_ACCOUNT_ID), oanda_env=settings.OANDA_ENVIRONMENT,
                  binance_url=settings.BINANCE_BASE_URL, models={"analysis": settings.GEMINI_ANALYSIS_MODEL, "reasoning": settings.GEMINI_REASONING_MODEL, "answers": settings.GEMINI_FINAL_MODEL,
                                                                  "transcription": settings.GEMINI_TRANSCRIBE_MODEL, "embedding": settings.GEMINI_EMBEDDING_MODEL},
                  display_tz=settings.DISPLAY_TZ, google_configured=bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET))
