"""Root REST router.

Aggregates the REST sub-routers (campaigns, flows, session-state, preferences,
audit, topology, stix, ai, threat-intel, operationalization, ingestion and llm).
The WebSocket channels live in ``app.ws``.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    ai,
    audit,
    campaigns,
    flows,
    health,
    ingestion,
    llm,
    operationalization,
    preferences,
    session_state,
    stix,
    threat_intel,
    topology,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(campaigns.router)
api_router.include_router(flows.router)
api_router.include_router(session_state.router)
api_router.include_router(preferences.router)
api_router.include_router(audit.router)
api_router.include_router(topology.router)
api_router.include_router(stix.router)
api_router.include_router(ai.router)
api_router.include_router(threat_intel.router)
api_router.include_router(operationalization.router)
api_router.include_router(ingestion.router)
api_router.include_router(llm.router)
