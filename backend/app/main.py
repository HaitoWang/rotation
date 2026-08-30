from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.database import Database
from app.infrastructure.redis import RedisManager

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    database = Database(settings)
    redis = RedisManager(settings)
    application.state.database = database
    application.state.redis = redis
    application.state.settings = settings
    if settings.auto_create_schema:
        await database.create_schema()
    await redis.start()
    logger.info("async API started env=%s", settings.app_env)
    try:
        yield
    finally:
        await redis.close()
        await database.close()
        logger.info("async API stopped")


app = FastAPI(title="Regert Register API", version="0.2.0", lifespan=lifespan)
app.include_router(router)


def _frontend_dist() -> Path:
    configured = Path(os.getenv("FRONTEND_DIST_DIR", "frontend/dist"))
    return configured if configured.is_absolute() else Path(__file__).resolve().parents[2] / configured


if (_frontend_dist() / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(_frontend_dist() / "assets")), name="assets")


@app.get("/", include_in_schema=False)
async def root():
    # Serve the built Vue UI from the same PostgreSQL-backed API process.
    index = _frontend_dist() / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"service": settings.app_name, "docs": "/docs", "api": "/api/v1"}


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=settings.app_host, port=settings.app_port)


if __name__ == "__main__":
    run()
