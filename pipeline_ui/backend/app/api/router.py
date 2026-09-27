"""Root REST router.

Aggregates the REST sub-routers. The casos/estágios, estado-sessão,
preferências and auditoria routers are wired here (task 11.1); the emulation
preview/execute endpoints (task 11.2) are wired via the ``emulacao`` router. The
WebSocket/SSE channels (task 11.3) are added by their own task.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import auditoria, casos, emulacao, estado_sessao, health, preferencias

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(casos.router)
api_router.include_router(emulacao.router)
api_router.include_router(estado_sessao.router)
api_router.include_router(preferencias.router)
api_router.include_router(auditoria.router)
