"""Root REST router.

Aggregates the REST sub-routers. Feature routers (casos, estagios, emulacao,
estado-sessao, preferencias, auditoria) are added in later tasks.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import health

api_router = APIRouter()
api_router.include_router(health.router)
