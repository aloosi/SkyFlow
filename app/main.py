"""FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import database
from app.api.v1 import api_router
from app.config import get_settings
from app.services import background_tasks

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await database.init_db()
    yield
    # Let in-flight recalculations finish before the engine goes away.
    await background_tasks.wait_for_pending(timeout=settings.shutdown_grace_seconds)
    await database.engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Event-driven flight catering broker: ingests disruptions, recalculates "
        "meal requirements in the background and surfaces live kitchen priorities."
    ),
    lifespan=lifespan,
)
app.include_router(api_router)


@app.get("/health", tags=["system"], summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": settings.app_version}
