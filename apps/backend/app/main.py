"""FastAPI app factory and application instance.

Usage (dev):
    uvicorn app.main:app --reload

The factory ``create_app`` wires the root REST router and the WebSocket router.
Feature routers and real-time channels are added in later tasks.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import Settings, get_settings
from app.ws import ws_router

logger = logging.getLogger(__name__)

_FALSE_VALUES = {"0", "false", "no", "off", ""}


def ingest_on_start_enabled() -> bool:
    """``STICKS_INGEST_ON_START`` (default on); tests turn it off in conftest."""
    return os.environ.get("STICKS_INGEST_ON_START", "1").strip().lower() not in _FALSE_VALUES


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the ingestion orchestrator and start it without blocking (Req. 7.1, 7.4)."""
    from app.db.session import SessionLocal
    from app.services.ingestion.orchestrator import IngestionOrchestrator
    from app.services.ingestion.runners import build_default_runners

    orchestrator = IngestionOrchestrator(SessionLocal, build_default_runners())
    app.state.ingestion = orchestrator
    if ingest_on_start_enabled():
        try:
            orchestrator.start_background()  # daemon thread; returns immediately
        except Exception:  # noqa: BLE001 - never block the API from coming up
            logger.exception("could not start the background ingestion")
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory.

    Args:
        settings: optional Settings override (useful for tests). Defaults to the
            cached settings from ``get_settings``.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=(
            "Pipeline UI backend for the sticks project "
            "(ATT&CK-in-STIX / contained APT emulation)."
        ),
        lifespan=lifespan,
    )

    # In dev the frontend (Vite) runs on a different origin; allow it locally.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.environment == "development" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    app.include_router(ws_router)

    return app


app = create_app()
