"""REST endpoint for threat-intel report uploads (task 15.1).

A single multipart endpoint over
:class:`~app.services.threat_intel_ingest_service.ThreatIntelIngestService`:

* ``POST /api/threat-intel`` — upload a Threat_Intel_Report (PDF / TXT / Markdown
  free text, or STIX / JSON structured) that enriches the Command_Base. The
  handler reads the ``UploadFile``, builds a
  :class:`~app.services.threat_intel_ingest_service.ThreatIntelReport` from its
  filename, content-type and raw bytes, and delegates to
  :meth:`ThreatIntelIngestService.ingest`, which extracts technique→commands
  (via the LLM for free text — Req. 13.1 — or from the fields for structured
  input — Req. 13.2), persists them into ``technique_commands`` (Req. 13.3) and
  the command↔tool affinities into ``experience_memory`` (Req. 13.4), idempotently
  by ``(technique_id, command_hash, source="threat_intel")`` (Req. 13.6).

On success the handler commits the request-scoped unit of work and returns the
ingest summary. When the report cannot be interpreted in any supported format the
service raises
:class:`~app.services.threat_intel_ingest_service.ThreatIntelFormatError`; the
handler rolls the session back (so the Command_Base is preserved unchanged — the
service only ``flush``es, so the rollback discards any partial write) and returns
a 422 with the descriptive error body (Req. 13.7).

Handlers stay thin and follow the existing router conventions (prefix ``/api``,
pydantic response models with ``Field`` descriptions, injected ``Session`` via
``Depends(get_db)``).

_Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.threat_intel_ingest_service import (
    ThreatIntelFormatError,
    ThreatIntelIngestService,
    ThreatIntelReport,
)

router = APIRouter(prefix="/api", tags=["threat-intel"])


class ThreatIntelIngestResponse(BaseModel):
    """The summary of a ``POST /api/threat-intel`` ingest (Req. 13.1–13.6).

    Mirrors
    :class:`~app.services.threat_intel_ingest_service.ThreatIntelIngestSummary`:
    the detected format and the counts of techniques/commands extracted and the
    new rows persisted (a re-ingest of the same report reports 0 inserts,
    reflecting idempotency — Req. 13.6).
    """

    report_format: str = Field(
        ...,
        description="Detected report format: 'structured' (STIX/JSON) or 'free_text'.",
    )
    techniques: int = Field(
        ..., description="Number of distinct ATT&CK techniques extracted."
    )
    commands_parsed: int = Field(
        ..., description="Number of concrete commands extracted from the report."
    )
    commands_inserted: int = Field(
        ...,
        description=(
            "New technique_commands rows created; 0 for commands already present "
            "(idempotency by technique_id + command_hash + source — Req. 13.6)."
        ),
    )
    affinities_inserted: int = Field(
        ...,
        description="New command↔tool affinity rows created in experience_memory.",
    )


@router.post(
    "/threat-intel",
    response_model=ThreatIntelIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a threat-intel report to enrich the Command_Base",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": (
                "The report could not be interpreted in any supported format "
                "(STIX/JSON, PDF, plain text or Markdown); the Command_Base is "
                "preserved unchanged (Req. 13.7)."
            )
        },
    },
)
async def ingest_threat_intel(
    file: UploadFile = File(
        ..., description="The threat-intel report (PDF/TXT/Markdown or STIX/JSON)."
    ),
    db: Session = Depends(get_db),
) -> ThreatIntelIngestResponse:
    """Ingest an uploaded threat-intel report into the Command_Base (Req. 13.1–13.7).

    Reads the ``UploadFile``, builds a
    :class:`~app.services.threat_intel_ingest_service.ThreatIntelReport` and
    delegates to :meth:`ThreatIntelIngestService.ingest`. Free text is extracted
    via the LLM (Req. 13.1); structured STIX/JSON from its fields (Req. 13.2). The
    technique→commands are persisted into ``technique_commands`` (Req. 13.3) and
    the affinities into ``experience_memory`` (Req. 13.4), idempotently by
    ``(technique_id, command_hash, source="threat_intel")`` (Req. 13.6), so a
    re-upload of the same report adds nothing.

    On success the request-scoped unit of work is committed and the ingest summary
    is returned. If the report cannot be interpreted in any supported format the
    service raises
    :class:`~app.services.threat_intel_ingest_service.ThreatIntelFormatError`; the
    handler rolls the session back (preserving the Command_Base unchanged — the
    service only ``flush``es, so the rollback discards any partial write) and
    returns a 422 carrying the descriptive error (Req. 13.7).
    """
    content = await file.read()
    report = ThreatIntelReport(
        filename=file.filename or "report",
        content_type=file.content_type,
        content=content,
    )

    from app.services.ai.memory import ExperienceMemory

    memory = ExperienceMemory(db)
    service = ThreatIntelIngestService(db, memory=memory)

    try:
        summary = service.ingest(report)
    except ThreatIntelFormatError as exc:
        # Unsupported/undecipherable format: roll back so the Command_Base is
        # preserved unchanged and surface the descriptive error (Req. 13.7).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "unsupported_format",
                "message": str(exc),
                "filename": exc.filename,
                "content_type": exc.content_type,
            },
        ) from exc

    # Commit the extracted technique→commands + affinities; the service only
    # flushed them within the request-scoped session.
    db.commit()
    return ThreatIntelIngestResponse(
        report_format=summary.report_format,
        techniques=summary.techniques,
        commands_parsed=summary.commands_parsed,
        commands_inserted=summary.commands_inserted,
        affinities_inserted=summary.affinities_inserted,
    )
