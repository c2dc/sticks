"""Root REST router.

Aggregates the REST sub-routers. The campaigns/stages, session-state,
preferences and audit routers are wired here (task 11.1); the emulation
preview/execute endpoints (task 11.2) are wired via the ``emulation`` router. The
WebSocket/SSE channels (task 11.3) are added by their own task.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    audit,
    campaigns,
    emulation,
    health,
    preferences,
    session_state,
    stix,
    topology,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(campaigns.router)
api_router.include_router(emulation.router)
api_router.include_router(session_state.router)
api_router.include_router(preferences.router)
api_router.include_router(audit.router)
api_router.include_router(topology.router)
api_router.include_router(stix.router)
