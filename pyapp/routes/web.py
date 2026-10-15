from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pyapp.config import settings
from pyapp.db import db
from pyapp.intelligence import aggregate_stats, build_patterns, retrieve

router = APIRouter(tags=["Web Pages"])
templates = Jinja2Templates(directory="pyapp/templates")
templates.env.globals.update(runtime_ai_configured=bool(settings.GEMINI_API_KEY) and not settings.MOCK_AI_MODE)

@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    videos = await db.get_videos()
    trades = await db.get_trades()
    stats = aggregate_stats(videos, trades)
    usage = await db.list("usage")

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active_page": "dashboard",
            "stats": stats,
            "trades": trades,
            "contradictions": [{"a": t.get("setup") or "Trade evidence", "b": c} for t in trades for c in t.get("contradictions", [])],
            "usage": {"calls": len(usage), "tokens": sum(u.get("totalTokens", 0) for u in usage)},
        }
    )

@router.get("/videos", response_class=HTMLResponse)
async def videos_page(request: Request, playlist: str = ""):
    videos = [v for v in await db.get_videos() if not playlist or playlist in v.get("playlistIds", [])]
    return templates.TemplateResponse(
        request=request,
        name="videos.html",
        context={
            "active_page": "videos",
            "videos": videos
        }
    )

@router.get("/videos/{video_id}", response_class=HTMLResponse)
async def video_detail_page(request: Request, video_id: str):
    v = await db.get_video(video_id)
    if not v:
        raise HTTPException(status_code=404, detail="Video not found")
    trades = await db.get_trades(v["id"])
    details = await db.get("video_details", v["id"]) or {}
    return templates.TemplateResponse(
        request=request,
        name="video_detail.html",
        context={
            "active_page": "videos",
            "video": v,
            "trades": trades,
            "events": details.get("events") or v.get("detectedEvents", []),
            "transcript": details.get("transcript") or v.get("transcript", [])
        }
    )

@router.get("/playlists", response_class=HTMLResponse)
async def playlists_page(request: Request):
    playlists = await db.get_playlists()
    return templates.TemplateResponse(
        request=request,
        name="playlists.html",
        context={
            "active_page": "playlists",
            "playlists": playlists
        }
    )

@router.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    trades = await db.get_trades()
    return templates.TemplateResponse(
        request=request,
        name="trades.html",
        context={
            "active_page": "trades",
            "trades": trades
        }
    )

@router.get("/charts", response_class=HTMLResponse)
async def charts_page(
    request: Request,
    symbol: str = Query(default=settings.DEFAULT_CHART_SYMBOL, min_length=1, max_length=64,
                        pattern=r"^[A-Za-z0-9_:.!/-]+$"),
    interval: str = Query(default="15", pattern=r"^(1|5|15|30|60|240|D|W|M)$"),
    theme: str = Query(default="light", pattern=r"^(light|dark)$"),
):
    symbol = symbol.upper()
    if ":" not in symbol:
        symbol = "FX:" + symbol.replace("/", "")
    chart_config = {
        "autosize": True,
        "symbol": symbol,
        "interval": interval,
        "timezone": "Asia/Kolkata",
        "theme": theme,
        "style": "1",
        "locale": "en",
        "allow_symbol_change": True,
        "hide_side_toolbar": False,
        "calendar": False,
        "support_host": "https://www.tradingview.com",
    }
    return templates.TemplateResponse(
        request=request,
        name="charts.html",
        context={
            "active_page": "charts",
            "symbol": symbol,
            "interval": interval,
            "theme": theme,
            "chart_config": chart_config,
        }
    )

@router.get("/playbook", response_class=HTMLResponse)
async def playbook_page(request: Request):
    trades = await db.get_trades()
    patterns = build_patterns(trades, await db.list("knowledge"))
    videos = await db.get_videos()
    rules = [k for k in await db.list("knowledge") if k.get("synthesized") and k.get("type") != "Inference"]
    playbook = {"coreModels": [{"name": p["name"], "trigger": p["description"], "riskReward": p["avgR"] if p["avgR"] is not None else "Unknown", "evidenceLevel": p["status"]} for p in patterns],
        "trader": ", ".join(sorted({v.get("channel") or "Unknown" for v in videos})), "primaryTimeframe": "See evidence per setup",
        "observedMarkets": sorted({t["symbol"] for t in trades if t["validation"] == "VALID"}),
        "riskRules": [k["content"] for k in rules]}
    return templates.TemplateResponse(
        request=request,
        name="playbook.html",
        context={
            "active_page": "playbook",
            "playbook": playbook,
            "patterns": patterns
        }
    )

@router.get("/patterns", response_class=HTMLResponse)
async def patterns_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="patterns.html",
        context={
            "active_page": "patterns",
            "patterns": build_patterns(await db.get_trades(), await db.list("knowledge"))
        }
    )

@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={
            "active_page": "chat"
        }
    )

@router.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, q: str = ""):
    knowledge = await db.list("knowledge")
    return templates.TemplateResponse(
        request=request,
        name="knowledge.html",
        context={
            "active_page": "knowledge",
            "knowledge": retrieve(q, knowledge, 100) if q else knowledge[:500],
            "total_chunks": len(knowledge), "q": q,
        }
    )

@router.get("/processing", response_class=HTMLResponse)
async def processing_page(request: Request):
    jobs = await db.get_jobs()
    return templates.TemplateResponse(
        request=request,
        name="processing.html",
        context={
            "active_page": "processing",
            "jobs": jobs,
            "ingestions": await db.list("ingestions"),
        }
    )

@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    services = {
        "gemini": bool(settings.effective_api_key) and not settings.MOCK_AI_MODE,
        "youtube": bool(settings.YOUTUBE_API_KEY),
        "database": True,
        "market": bool(settings.OANDA_API_TOKEN and settings.OANDA_ACCOUNT_ID)
    }
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active_page": "settings",
            "settings": settings,
            "services": services
        }
    )

@router.get("/backtesting", response_class=HTMLResponse)
async def backtesting_page(request: Request):
    return templates.TemplateResponse(request=request, name="backtesting.html", context={
        "active_page": "backtesting", "patterns": build_patterns(await db.get_trades(), await db.list("knowledge")),
        "strategies": await db.list("strategies"), "datasets": await db.list("datasets"),
        "reports": await db.list("backtests"), "proposals": await db.list("proposals"), "min_holdout": settings.MIN_HOLDOUT_TRADES})
