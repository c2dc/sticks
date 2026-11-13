"""REST endpoints for the knowledge-source ingestion (Req. 7.1, 7.4, 7.8, 7.10).

* ``GET  /api/ingestion/status``: persisted state of every source.
* ``POST /api/ingestion/verify``: recompute the plans (database only), start in
  the background whatever is missing and return the same body as ``status``.
* ``POST /api/ingestion/{source}/start``: ``202`` with the state, or ``409``
  when the source is already ``running``.

The orchestrator lives in ``app.state.ingestion`` (created by the app
``lifespan``). It is created lazily here if a caller did not run the lifespan.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.models.enums import IngestSource, IngestStatus
from app.services.ingestion.orchestrator import (
    SOURCE_ORDER,
    IngestionOrchestrator,
    SourceStatus,
    StartResult,
)

router = APIRouter(prefix="/api/ingestion", tags=["ingestion"])


class IngestionSourceStatus(BaseModel):
    """State of one source (design: ``GET /api/ingestion/status``)."""

    source: IngestSource
    status: IngestStatus
    loaded_count: int | None = None
    embedded_count: int | None = None
    expected_count: int | None = None
    error: str | None = None
    skipped_count: int = 0
    updated_at: dt.datetime | None = None


def _to_model(item: SourceStatus) -> IngestionSourceStatus:
    return IngestionSourceStatus(
        source=item.source,
        status=item.status,
        loaded_count=item.loaded_count,
        embedded_count=item.embedded_count,
        expected_count=item.expected_count,
        error=item.error,
        skipped_count=item.skipped_count,
        updated_at=item.updated_at,
    )


def get_orchestrator(request: Request) -> IngestionOrchestrator:
    """Return ``app.state.ingestion``, creating it (without starting) if absent."""
    orchestrator = getattr(request.app.state, "ingestion", None)
    if orchestrator is None:
        from app.db.session import SessionLocal
        from app.services.ingestion.runners import build_default_runners

        orchestrator = IngestionOrchestrator(SessionLocal, build_default_runners())
        request.app.state.ingestion = orchestrator
    return orchestrator


@router.get(
    "/status",
    response_model=list[IngestionSourceStatus],
    summary="Persisted ingestion state of every source",
)
def get_status(request: Request) -> list[IngestionSourceStatus]:
    return [_to_model(item) for item in get_orchestrator(request).status()]


@router.post(
    "/verify",
    response_model=list[IngestionSourceStatus],
    summary="Recompute the plans and start in the background what is missing",
)
def verify(request: Request) -> list[IngestionSourceStatus]:
    statuses = get_orchestrator(request).verify_and_start()
    return [_to_model(statuses[source]) for source in SOURCE_ORDER if source in statuses]


@router.post(
    "/{source}/start",
    response_model=IngestionSourceStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start the ingestion of one source (409 when already running)",
)
def start_source(source: str, request: Request) -> IngestionSourceStatus:
    try:
        parsed = IngestSource(source)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown ingestion source: {source}",
        ) from exc
    orchestrator = get_orchestrator(request)
    try:
        result = orchestrator.start(parsed)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if result is StartResult.ALREADY_RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"ingestion of {parsed.value} is already running",
        )
    current = next((s for s in orchestrator.status() if s.source == parsed), None)
    return _to_model(current or SourceStatus(source=parsed, status=IngestStatus.RUNNING))
