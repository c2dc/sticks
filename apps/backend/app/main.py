"""FastAPI app factory and application instance.

Usage (dev):
    uvicorn app.main:app --reload

The factory ``create_app`` wires the root REST router and the WebSocket router.
Feature routers and real-time channels are added in later tasks.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import api_router
from app.core.config import Settings, get_settings
from app.ws import ws_router


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
            "Backend da Pipeline_UI para o projeto sticks "
            "(ATT&CK-in-STIX / emulacao de APT contida)."
        ),
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
