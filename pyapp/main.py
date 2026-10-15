import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pyapp.config import settings
from pyapp.db import db
from pyapp import auth, limits
from pyapp import lab  # noqa: F401  (registers background job handlers)
from pyapp.routes.web import router as web_router
from pyapp.routes.api import router as api_router
from pyapp.routes.research import router as research_router
from pyapp.routes.lab import router as lab_router
from pyapp.routes.knowledge import router as knowledge_router
from pyapp.routes.account import router as account_router
from pyapp.routes.pages import router as pages_router
from pyapp.routes.media import router as media_router
from pyapp.routes.usage import router as usage_router
from pyapp.worker import run_worker

logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pyapp")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Trader Video Intelligence (auth %s)", "required" if settings.auth_required else "OFF - local use only")
    os.makedirs(settings.STORAGE_PATH, exist_ok=True)
    await db.connect()
    await auth.bootstrap_admin()
    worker = asyncio.create_task(run_worker()) if settings.ENABLE_WORKER else None
    try:
        yield
    finally:
        if worker:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await db.disconnect()
    logger.info("Shutdown complete.")

app = FastAPI(
    title="Trader Video Intelligence",
    description="Source-grounded trading strategy research: YouTube analysis, strategy manuals, causal backtesting and auditable journals.",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.APP_ENV.lower() != "production" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if settings.APP_ENV.lower() != "production" else None,
)
app.add_middleware(limits.RateLimitMiddleware)
app.add_middleware(auth.SecurityMiddleware)

# Mount static assets
app.mount("/static", StaticFiles(directory="pyapp/static"), name="static")
os.makedirs(os.path.join(settings.STORAGE_PATH, "evidence"), exist_ok=True)
app.mount("/evidence", StaticFiles(directory=os.path.join(settings.STORAGE_PATH, "evidence")), name="evidence")

# Routes
app.include_router(pages_router)
app.include_router(web_router)
app.include_router(api_router)
app.include_router(research_router)
app.include_router(lab_router)
app.include_router(knowledge_router)
app.include_router(account_router)
app.include_router(media_router)
app.include_router(usage_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("pyapp.main:app", host="0.0.0.0", port=8000, reload=False)
