"""REST endpoint for the persisted audit trail (task 11.1).

``GET /api/audit/{operation}`` returns the persisted audit trail of an
Operation — one row per executed command (Req. 6.8) — via
:meth:`AuditLogger.read_trail`, ordered oldest first.

The handler stays thin: it constructs the :class:`AuditLogger` with the
request-scoped session and maps the ORM rows onto a Pydantic response model.

_Requirements: 7.4, 8.4_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.audit.audit_logger import AuditLogger

router = APIRouter(prefix="/api", tags=["audit"])


class AuditEntryResponse(BaseModel):
    """One persisted ``AuditLogEntry`` row (Req. 6.8).

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    operation_id: Optional[int] = None
    ability_id: Optional[str] = None
    command: str
    target_container: str
    result: Optional[str] = None
    recorded_at: dt.datetime


class AuditTrailResponse(BaseModel):
    """``GET /api/audit/{operation}`` payload — the trail of one Operation."""

    operation_id: int
    records: list[AuditEntryResponse] = Field(default_factory=list)


@router.get(
    "/audit/{operation}",
    response_model=AuditTrailResponse,
    summary="Persisted audit trail of the Operation",
)
def get_audit(
    operation: int = Path(..., description="Id of the Operation"),
    db: Session = Depends(get_db),
) -> AuditTrailResponse:
    """Return the persisted audit trail for an Operation, oldest first (Req. 6.8)."""
    entries = AuditLogger(db).read_trail(operation)
    return AuditTrailResponse(
        operation_id=operation,
        records=[AuditEntryResponse.model_validate(e) for e in entries],
    )
