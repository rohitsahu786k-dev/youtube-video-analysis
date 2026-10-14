import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pyapp.config import settings
from pyapp.db import db
from pyapp.routes.web import router as web_router
from pyapp.routes.api import router as api_router
from pyapp.worker import run_worker
from pyapp.routes.research import router as research_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pyapp")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Forex Trader Video Intelligence (Python FastAPI & Gemini)...")
    os.makedirs(settings.STORAGE_PATH, exist_ok=True)
    await db.connect()
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
    title="Forex Trader Video Intelligence",
    description="Multimodal Forex YouTube Video & Playbook Intelligence System with Google Gemini AI",
    version="2.0.0",
    lifespan=lifespan
)

# Mount static assets
app.mount("/static", StaticFiles(directory="pyapp/static"), name="static")
os.makedirs(os.path.join(settings.STORAGE_PATH, "evidence"), exist_ok=True)
app.mount("/evidence", StaticFiles(directory=os.path.join(settings.STORAGE_PATH, "evidence")), name="evidence")

# Include routes
app.include_router(web_router)
app.include_router(api_router)
app.include_router(research_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("pyapp.main:app", host="0.0.0.0", port=8000, reload=True)
