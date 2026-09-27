"""Health-check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health", summary="Health check")
async def health() -> dict[str, str]:
    """Simple liveness probe used to verify the app is up."""
    return {"status": "ok"}
