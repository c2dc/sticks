"""FastAPI app factory and application instance.

Usage (dev):
    uvicorn app.main:app --reload

The factory ``create_app`` wires the root REST router and the WebSocket router.
Feature routers and real-time channels are added in later tasks.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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


def configure_app_logging() -> None:
    """Make ``app.*`` log records visible next to uvicorn's output (idempotent).

    uvicorn only configures its own loggers; without a handler the INFO records of
    the ingestion (start, finish, failure causes) would be dropped.
    """
    app_logger = logging.getLogger("app")
    if app_logger.level == logging.NOTSET or app_logger.level > logging.INFO:
        app_logger.setLevel(logging.INFO)
    if not any(getattr(h, "_sticks_handler", False) for h in app_logger.handlers):
        handler = logging.StreamHandler()
        handler._sticks_handler = True  # type: ignore[attr-defined] -- marker attribute set dynamically on the logging handler to keep setup idempotent
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        app_logger.addHandler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the ingestion orchestrator and start it without blocking (Req. 7.1, 7.4)."""
    configure_app_logging()
    from app.db.session import SessionLocal
    from app.services.ingestion.orchestrator import IngestionOrchestrator
    from app.services.ingestion.runners import build_default_runners

    # Close runs/operations left running by a previous process (Req. 13); never blocks startup.
    try:
        import asyncio

        from app.services.ai.reconciler import reconcile_orphans

        await asyncio.to_thread(reconcile_orphans, SessionLocal)
    except Exception:
        logger.exception("startup reconciliation failed; continuing")

    orchestrator = IngestionOrchestrator(SessionLocal, build_default_runners())
    app.state.ingestion = orchestrator
    if ingest_on_start_enabled():
        try:
            orchestrator.start_background()  # daemon thread; returns immediately
        except Exception:
            logger.exception("could not start the background ingestion")
    if ingest_on_start_enabled():
        # Warm the STIX version-list cache off the startup path (never raises).
        import threading

        from app.services.stix.stix_service import get_version_catalog

        threading.Thread(
            target=get_version_catalog().warm_up, name="stix-version-warmup", daemon=True
        ).start()
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
            "Pipeline UI backend for the sticks project (ATT&CK-in-STIX / contained APT emulation)."
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
