"""REST endpoints for campaigns (agentic-only product).

The curated step-by-step pipeline endpoints (``/dag``, ``/dags*``, ``/reset``)
was removed. The campaign flow now runs through ``/ai``, ``/flows``, ``/stix``,
``/ingestion`` and the operationalization endpoints. This module keeps the
campaign read endpoints that do not belong to the pipeline.

Endpoints implemented here:

* ``GET  /api/campaigns`` - the curated campaigns (data flagged ``curated``).
* ``GET  /api/campaigns/{campaign}`` - Campaign detail (Abilities, Adversary, ordering).
* ``GET  /api/campaigns/{campaign}/report`` - the execution report.
* ``GET  /api/campaigns/{campaign}/operation`` - per-Ability result + aggregate.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.domain import Case as CaseRow
from app.models.domain import ExecutionReport as ExecutionReportRow
from app.models.enums import OperationState, TranslationSource
from app.services.curated_case_models import AbilityData, AdversaryData
from app.services.curated_case_reader import build_case_from_db
from app.services.emulation.views import OperationAbilityResult
from app.services.environment.measurement import (
    AbilityMeasurementResult,
    MeasurementService,
)
from app.services.environment.report import DeprovisioningView, ExecutionReport
from app.services.session.session_state_service import (
    OperationResultView,
    SessionStateService,
)
from app.services.stix.stix_service import CURATED_VERSION

router = APIRouter(prefix="/api", tags=["campaigns"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class CaseListItem(BaseModel):
    """One curated case in ``GET /api/campaigns``."""

    id: str
    name: str
    translation_source: TranslationSource | None = None


class CampaignsResponse(BaseModel):
    """``GET /api/campaigns`` payload - the curated campaigns."""

    campaigns: list[CaseListItem] = Field(default_factory=list)


class CaseExecutorView(BaseModel):
    """A concrete executor (command) of a curated Ability (case detail)."""

    name: str | None = None
    platform: str | None = None
    command: str | None = None


class CaseAbilityView(BaseModel):
    """One curated Ability in the case-detail response (Req. 3.2)."""

    ability_id: str
    name: str | None = None
    tactic: str | None = None
    technique_name: str | None = None
    technique_id: str | None = None
    description: str | None = None
    executors: list[CaseExecutorView] = Field(default_factory=list)


class CaseAdversaryView(BaseModel):
    """The curated Adversary in the case-detail response (Req. 3.5)."""

    id: str
    name: str | None = None
    description: str | None = None
    atomic_ordering: list[str] = Field(default_factory=list)


class CaseDetailResponse(BaseModel):
    """``GET /api/campaigns/{campaign}`` payload — case detail.

    Abilities, Adversary, ordering and translation source. ``atomic_ordering``
    mirrors the adversary's ordering (empty when there is no Adversary).
    """

    id: str
    name: str
    translation_source: TranslationSource
    abilities: list[CaseAbilityView] = Field(default_factory=list)
    adversary: CaseAdversaryView | None = None
    atomic_ordering: list[str] = Field(default_factory=list)


class OperationResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/operation`` payload (Req. 4.1, 4.5).

    Per-Ability result + aggregate for every Operation of the case.
    """

    campaign_id: str
    operations: list[OperationResultView] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _known_campaign_or_404(campaign_key: str, session: Session, version: str) -> None:
    """Raise 404 when ``campaign_key`` is not persisted in ``stix_campaigns``.

    Validates the campaign against the 52 persisted STIX campaigns of ``version``
    (``stix_campaigns``) instead of only the curated slugs, so any persisted campaign — curated or not — is
    accepted, while a truly-absent id still gets a 404 (Req. 2.2, 2.3, 4.4). The
    match uses the same ``Campaign_Key`` rule the listing exposes
    (``curated_slug`` when present, else ``stix_id`` — Req. 4.5), so a curated
    campaign keeps passing exactly as before (its row is present in
    ``stix_campaigns`` with ``curated=True``).

    A version with no persisted package yields the same 404: there is no
    persisted campaign to accept, so the request is refused rather than leaking a
    different error (keeps the guard's contract uniform — Req. 2.3, 4.4).
    """
    from app.services.stix.stix_package_read_service import (
        StixPackageNotPersistedError,
        StixPackageReadService,
    )

    read_service = StixPackageReadService(session)
    try:
        campaigns = read_service.list_campaigns(version)
    except StixPackageNotPersistedError:
        campaigns = []
    known_keys = {summary.campaign_key for summary in campaigns}
    if campaign_key not in known_keys:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown campaign: {campaign_key!r}.",
        )


def _derive_total_time_s(
    operations: list[OperationResultView],
) -> float | None:
    """Derive the real total execution time from the Operation instants (Req. 4.1, 4.3).

    Pure, DB-free derivation shared by ``get_report`` and its property test: for
    each **finished** Operation that carries **both** ``started_at`` and
    ``finished_at``, the duration is ``(finished_at - started_at)`` in seconds,
    accumulated across the finished Operations that provide a measurable duration
    (a curated Campaign runs a single Operation; defensively we consolidate all).

    It stays ``None`` while no finished Operation provides a measurable duration,
    so an absent ``started_at`` or ``finished_at`` (on the only/every finished
    Operation) yields ``None`` (Req. 4.3) — never ``0.0``.
    """
    total_time_s: float | None = None
    for operation in operations:
        if (
            operation.state is OperationState.FINISHED
            and operation.started_at is not None
            and operation.finished_at is not None
        ):
            duration = (operation.finished_at - operation.started_at).total_seconds()
            total_time_s = (total_time_s or 0.0) + duration
    return total_time_s


def _ability_view(a: AbilityData) -> CaseAbilityView:
    return CaseAbilityView(
        ability_id=a.ability_id,
        name=a.name,
        tactic=a.tactic,
        technique_name=a.technique_name,
        technique_id=a.technique_id,
        description=a.description,
        executors=[
            CaseExecutorView(name=e.name, platform=e.platform, command=e.command)
            for e in a.executors
        ],
    )


def _adversary_view(adv: AdversaryData) -> CaseAdversaryView:
    return CaseAdversaryView(
        id=adv.id,
        name=adv.name,
        description=adv.description,
        atomic_ordering=list(adv.atomic_ordering),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns",
    response_model=CampaignsResponse,
    summary="List the curated campaigns",
)
def list_campaigns_endpoint(db: Session = Depends(get_db)) -> CampaignsResponse:
    """List the curated campaigns (``campaigns.curated`` rows) from the database."""
    rows = (
        db.execute(select(CaseRow).where(CaseRow.curated.is_(True)).order_by(CaseRow.id))
        .scalars()
        .all()
    )
    return CampaignsResponse(
        campaigns=[
            CaseListItem(
                id=row.id,
                name=row.name,
                translation_source=row.translation_source,
            )
            for row in rows
        ]
    )


@router.get(
    "/campaigns/{campaign}",
    response_model=CaseDetailResponse,
    summary="Detail a campaign (Abilities, Adversary, ordering, translation origin)",
)
def detail_campaign(
    campaign: str = Path(..., description="Campaign slug"),
    db: Session = Depends(get_db),
) -> CaseDetailResponse:
    """Return a campaign's Abilities, Adversary, ordering and origin.

    Database-only sourcing: Abilities, Adversary and ordering come from the
    ``abilities``/``adversaries`` rows. An unknown slug is a 404
    (``_known_campaign_or_404`` against ``stix_campaigns``).
    """
    _known_campaign_or_404(campaign, db, CURATED_VERSION)
    case = build_case_from_db(db, campaign)
    return CaseDetailResponse(
        id=case.id,
        name=case.name,
        translation_source=case.translation_source,
        abilities=[_ability_view(a) for a in case.abilities],
        adversary=_adversary_view(case.adversary) if case.adversary else None,
        atomic_ordering=list(case.atomic_ordering),
    )


@router.get(
    "/campaigns/{campaign}/report",
    response_model=ExecutionReport,
    summary="The execution phase: execution report + procedural-sufficiency metrics",
)
def get_report(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> ExecutionReport:
    """Return the consolidated report + the execution phase metrics (Req. 10.7, 15.7).

    It derives the :class:`ExecutionReport` from the Campaign's **persisted**
    per-Ability results (:meth:`SessionStateService.get_operation_results`) plus
    the persisted flow, via :class:`MeasurementService` (**pure and deterministic**
    measurement — no network/Docker/AI). The metrics (success rate, procedural
    coverage, time, failed nodes, per-technique telemetry and verdict) are
    recomputed on every read
    from the persisted results, so the report always reflects the current
    execution state (Req. 15.1-15.7).

    The per-Ability results of **all** the Campaign's Operations are flattened (a
    curated Campaign runs a single Operation; defensively we consolidate all) and
    normalized into :class:`AbilityMeasurementResult` for the measurement and into
    :class:`OperationAbilityResult` for the report. The ``environment_deprovisioned``
    flag reflects the aggregate result of the finished Operations: since the
    curated environment is **always** deprovisioned at the end of a completed
    execution (Req. 10.8, 12.1), it is ``True`` when there is at least one finished
    Operation; otherwise ``False`` (no execution completed yet).

    The structured ``deprovisioning`` field (Req. 7.2, 7.3) is populated from the
    persisted ``execution_reports`` row written at the end of the execution phase (task 1): the
    most recent row for the Campaign that recorded a teardown (non-null
    ``deprovisioned``) supplies the boolean outcome and the ``remaining_resources``
    that stayed provisioned on a failed teardown. When no teardown has been
    registered yet (no persisted row, or only legacy/pre-teardown rows), the field
    stays ``None`` — the legacy ``environment_deprovisioned`` boolean is preserved
    either way.

    An unknown Campaign *slug* is a 404. A Campaign with no execution still returns
    200 with zeroed metrics (attack flow as the denominator, zero techniques in success) —
    the report screen always receives a reportable result.
    """
    _known_campaign_or_404(campaign, db, CURATED_VERSION)

    # Database-only sourcing (Req. 1.1, 4.1): resolve the campaign's curated
    # content (attack flow + Abilities) from the DB (Req. 4.2).
    case = build_case_from_db(db, campaign)

    operations = SessionStateService(db).get_operation_results(campaign)
    ability_results: list[AbilityMeasurementResult] = []
    operation_results: list[OperationAbilityResult] = []
    any_finished = False
    # Real total execution time derived from the Operation instants (Req. 4.1,
    # 4.3): ``finished_at - started_at`` in seconds, accumulated over the
    # finished Operations that carry both instants, via the pure, DB-free
    # ``_derive_total_time_s`` helper (shared with its property test). It stays
    # ``None`` when no finished Operation provides a measurable duration.
    total_time_s: float | None = _derive_total_time_s(operations)
    for operation in operations:
        if operation.state is OperationState.FINISHED:
            any_finished = True
        for ability in operation.results:
            ability_results.append(
                AbilityMeasurementResult(ability_id=ability.ability_id, status=ability.status)
            )
            operation_results.append(
                OperationAbilityResult(
                    ability_id=ability.ability_id or "",
                    status=ability.status,
                    command_output=ability.command_output,
                )
            )

    report = MeasurementService().report_from_campaign(
        campaign=case,
        ability_results=ability_results,
        operation_results=operation_results,
        environment_deprovisioned=any_finished,
    )
    # Replace the per-technique ``execution_time_s`` sum (always ``None`` on the
    # real execution path) with the real total derived from the Operation
    # instants (Req. 4.1, 4.3).
    report.metrics.total_time_s = total_time_s

    # Populate the structured ``deprovisioning`` from the persisted teardown
    # (Req. 7.2, 7.3): read the most recent ``execution_reports`` row that
    # recorded a teardown (non-null ``deprovisioned``) for this Campaign. When no
    # such row exists (no execution finished, or only legacy rows), leave
    # ``deprovisioning`` as ``None``; the legacy ``environment_deprovisioned``
    # boolean above is preserved regardless.
    teardown_row = (
        db.query(ExecutionReportRow)
        .filter(
            ExecutionReportRow.campaign_id == campaign,
            ExecutionReportRow.deprovisioned.isnot(None),
        )
        .order_by(ExecutionReportRow.generated_at.desc(), ExecutionReportRow.id.desc())
        .first()
    )
    if teardown_row is not None:
        report.deprovisioning = DeprovisioningView(
            deprovisioned=bool(teardown_row.deprovisioned),
            remaining_resources=list(teardown_row.remaining_resources or []),
        )
    return report


@router.get(
    "/campaigns/{campaign}/operation",
    response_model=OperationResponse,
    summary="Per-Ability result and Operation aggregate",
)
def get_operation(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> OperationResponse:
    """Return the per-Ability results + aggregate for the case (Req. 4.1, 4.5).

    Delegates to :meth:`SessionStateService.get_operation_results`.
    """
    _known_campaign_or_404(campaign, db, CURATED_VERSION)
    operations = SessionStateService(db).get_operation_results(campaign)
    return OperationResponse(campaign_id=campaign, operations=operations)
