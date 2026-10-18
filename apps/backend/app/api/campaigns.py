"""REST endpoints for campaigns and stages (six-stage pipeline).

Wires the curated-campaign / stage endpoints from the design's "Backend — REST
endpoints (1..6)" section to the existing services. Handlers are kept thin: each
one constructs the relevant service with the *request-scoped* SQLAlchemy session
(via the ``get_db`` dependency) and delegates, returning the services'
already-typed Pydantic results as the response models.

The stages follow the six-stage scheme (design `pipeline-six-stages`):

====  ===============================  ======================================
Nº    Stage                            Service / seam
====  ===============================  ======================================
1     STIX base selection              ``StageService.read_stage1`` /
                                       ``run_stage1``
2     Campaign selection               ``Stage2Service`` (list + extraction)
3     DAG generation                   ``DagService`` (``/dags``,
                                       ``/dags/{id}``, ``choose-dag``;
                                       curated auto-completion)
4     Operationalization               ``StageService`` (Abilities +
                                       environment parameters)
5     Simulation-environment              ``EnvironmentStageService`` /
      creation                         ``CurationEnvironmentProvisioner``
6     Execution + measurement + report ``ExecutionStageService`` /
                                       ``MeasurementService`` /
                                       ``CompletionStage6Service``
====  ===============================  ======================================

Endpoints implemented here:

* ``GET  /api/campaigns`` — the curated campaigns with per-stage state (6 stages)
  and aggregate progress; per-Campaign error isolation (Req. 5.1, 5.6).
* ``GET  /api/campaigns/{campaign}`` — Campaign detail with the 6 stages' state.
* ``GET  /api/campaigns/{campaign}/stage/1`` — STIX base selection (read-only).
* ``POST /api/campaigns/{campaign}/stage/1/execute`` — triggers the modeling.
* ``GET  /api/campaigns/{campaign}/stage/2`` — Campaign selection (list + attributes).
* ``POST /api/campaigns/{campaign}/stage/2/select`` — selects a campaign, extracts.
* ``GET  /api/campaigns/{campaign}/stage/3`` — DAG generation (DagView/Mermaid).
* ``GET  /api/campaigns/{campaign}/dags`` — persisted DAGs (curated backfill).
* ``GET  /api/campaigns/{campaign}/dags/{dag_id}`` — persisted DAG (graph + Mermaid).
* ``POST /api/campaigns/{campaign}/stage/3/choose-dag`` — chooses the DAG, completes 3.
* ``POST /api/campaigns/{campaign}/stage/3/complete`` — (transitional) completes the DAG generation.
* ``GET  /api/campaigns/{campaign}/stage/4`` — Operationalization (Abilities + parameters).
* ``POST /api/campaigns/{campaign}/stage/4/complete`` — completes the Operationalization.
* ``GET  /api/campaigns/{campaign}/stage/5`` — Environment creation (topology + isolation).
* ``POST /api/campaigns/{campaign}/stage/5/provision`` — ensures/starts the provisioning
  (idempotent); delegates to the active seam ``EnvironmentStageService.provision_stage5``.
* ``GET  /api/campaigns/{campaign}/stage/5/logs`` — current buffer of provisioning log
  lines (read-only; live-terminal recovery — Req. 2.5).
* ``GET  /api/campaigns/{campaign}/stage/6`` — Execution (input + accumulated output).
* ``POST /api/campaigns/{campaign}/stage/6/execution/preview`` — previews the execution.
* ``POST /api/campaigns/{campaign}/stage/6/execution`` — executes, measures and completes (deprovisions).
* ``GET  /api/campaigns/{campaign}/report`` — the Stage 6 execution report.
* ``GET  /api/campaigns/{campaign}/operation`` — per-Ability result + aggregate.

The legacy ``GET /api/campaigns/{campaign}/dag`` endpoint (read-only DagView) is
kept unchanged.

Automatic completion of curated stages (Req. 9.1-9.4)
-----------------------------------------------------
Curated campaigns pre-complete Stages 3 (DAG) and 5 (Environment) as soon as the
previous stage completes. The auto-completion is triggered on the state reads the
wizard consumes — ``GET /api/campaigns`` and ``GET /api/campaigns/{campaign}`` — and
also on opening the respective stage's screen (``GET .../stage/3`` and
``.../stage/5``), delegating to the ``DagService.auto_complete_stage2`` seam
(operates over ``ESTAGIO_GERACAO_DAG == 3``, historical name) and
``EnvironmentStageService.auto_complete_stage4``. Both respect the sequential
gate, are idempotent/transactional and do **not** complete without the curated
material (derivable DAG / environment verified isolated), so repeating the read
never duplicates state nor completes improperly.

_Requirements: 8.7, 1.4, 5.4, 9.1, 9.2, 9.3, 9.4_
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.campaign_service import (
    AbilityData,
    AdversaryData,
    CaseLoadError,
    CaseService,
)
from app.models.enums import OperationState, TranslationSource
# Injection dependencies + preview models/mapping reused from the emulation
# router (task 11.2): Stage 6 (Execution) reuses the same runner *factory*
# (injectable Caldera client + isolation checker) and the same preview
# presentation, without duplicating them. ``api.emulation`` is a leaf module (it
# does not import ``api.campaigns``), so the import creates no cycle.
from app.api.emulation import (
    EmulationPreviewResponse,
    RunnerFactory,
    _preview_view,
    get_runner_factory,
)
from app.services.caldera import CalderaApiError, CalderaUnavailable
from app.services.environment.completion import (
    CompletionStage6Result,
    CompletionStage6Service,
)
from app.services.environment.execution import ExecutionStageService
from app.services.environment.measurement import MeasurementService, AbilityMeasurementResult
from app.services.environment.log_buffer import (
    ProvisioningLogBuffer,
    get_log_buffer,
)
from app.services.environment.provisioner import (
    ProvisionedEnvironment,
    CurationEnvironmentProvisioner,
)
from app.services.environment.health import HealthSummary
from app.services.environment.report import ExecutionReport
from app.services.operation.runner import RunOutcome
from app.services.dag.dag_service import (
    DagDetail,
    DagSummary,
    DagService,
    DagView,
    DagChoiceFailure,
)
from app.services.environment.stage_service import EnvironmentStageService
from app.services.environment.topology_builder import HostView, NetworkView
# Imports the operationalization seam (Stage 4) and its DTOs straight from the
# submodules, not from the ``app.services.environment`` package: there is a known
# import cycle when re-exporting from the package root, so the submodules are the
# direct source (same precedent as Stage 2 in ``app.services.stix``).
from app.services.environment.operationalization import EnvironmentParameters
from app.services.environment.operationalizer import (
    CurationOperationalizer,
    ProducedOperationalization,
)
from app.services.session.session_state_service import (
    OperationResultView,
    SessionStateService,
)
from app.services.stage.stage_service import (
    ESTAGIO_GERACAO_DAG,
    ESTAGIO_OPERACIONALIZACAO,
    AggregateProgress,
    CaseStageStates,
    LogLineView,
    Stage1Result,
    Stage2Output,
    Stage3AbilityResult,
    Stage3Input,
    StageService,
)
from app.services.stix.estagio1_service import (
    Stage1Service,
    Stage1ResultStix,
)
# Imports the Stage 2 service directly from the submodule (not from the
# ``app.services.stix`` package): there is a known import cycle when re-exporting
# the service from the package root, so the submodule is the direct source.
from app.services.stix.estagio2_service import (
    ListedCampaign,
    Stage2Service,
    CampaignSelectionResult,
)
from app.services.stix.stix_service import (
    StixFileError,
)

router = APIRouter(prefix="/api", tags=["casos"])


# ---------------------------------------------------------------------------
# Response models specific to the casos listing / detail endpoints
# ---------------------------------------------------------------------------


class CaseListItem(BaseModel):
    """One curated case in ``GET /api/campaigns`` (Req. 5.1, 5.6).

    Carries the case identity plus its per-stage state (``states`` from
    :class:`CaseStageStates`). When the case failed to load (missing/malformed
    files, Req. 5.6) ``error`` is populated with the descriptive per-case error
    and ``states`` is ``None`` — the case is still listed so one bad case never
    drops the rest.
    """

    id: str
    name: str
    translation_source: Optional[TranslationSource] = None
    states: Optional[CaseStageStates] = None
    error: Optional[CaseLoadError] = None


class CampaignsResponse(BaseModel):
    """``GET /api/campaigns`` payload — the 8 cases + aggregate progress (Req. 5.1)."""

    campaigns: list[CaseListItem] = Field(default_factory=list)
    progress: AggregateProgress


class CaseExecutorView(BaseModel):
    """A concrete executor (command) of a curated Ability (case detail)."""

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class CaseAbilityView(BaseModel):
    """One curated Ability in the case-detail response (Req. 3.2)."""

    ability_id: str
    name: Optional[str] = None
    tactic: Optional[str] = None
    technique_name: Optional[str] = None
    technique_id: Optional[str] = None
    description: Optional[str] = None
    executors: list[CaseExecutorView] = Field(default_factory=list)


class CaseAdversaryView(BaseModel):
    """The curated Adversary in the case-detail response (Req. 3.5)."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class CaseDetailResponse(BaseModel):
    """``GET /api/casos/{campaign}`` payload — case detail.

    Abilities, Adversary, ordering and translation source. ``atomic_ordering``
    mirrors the adversary's ordering (empty when there is no Adversary).
    ``states`` carries the per-stage state (same :class:`CaseStageStates` shape
    as ``GET /api/casos``, covering all 6 Stages) so the wizard can tell which
    stages are ``completed`` and enable "Next"/Advance.
    """

    id: str
    name: str
    translation_source: TranslationSource
    states: CaseStageStates
    abilities: list[CaseAbilityView] = Field(default_factory=list)
    adversary: Optional[CaseAdversaryView] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class Stage2Response(BaseModel):
    """``GET /api/campaigns/{campaign}/stage/2`` payload — Campaign selection (Req. 6.2, 6.4).

    In the **six**-stage scheme, Stage 2 is "Campaign selection": the screen lists
    the campaigns contained in the bundle selected in Stage 1 and shows the
    attributes extracted from the campaign once it has been selected (Req. 6.2,
    6.4). DAG generation became Stage 3 (the DAG route renumbering is done in task
    4.1).

    This view is **read-only**: it builds the listing via
    :meth:`Stage2Service.list_campaigns` and reflects the selection state via
    :meth:`Stage2Service.read` (identity/aliases + techniques, derived without
    persisting) — opening the screen never runs the extraction nor completes the
    stage.

    Attributes:
        campaign_id: Slug of the selected Campaign/campaign.
        version: STIX bundle version (Stage 1/2 input, Req. 6.1).
        campaigns: The bundle's campaigns, each with ``curada``/``curated_slug``/
            ``selectable`` (the 8 curated ones are selectable on version 18, Req.
            6.2, 6.5).
        selection: The read-only view of the current selection — identity/aliases,
            extracted techniques and the completion verdict (``completed``); the
            techniques only appear once the stage has been completed (Req. 6.3,
            6.4, 6.6).
    """

    campaign_id: str
    version: Optional[str] = None
    campaigns: list[ListedCampaign] = Field(default_factory=list)
    selection: CampaignSelectionResult


class SelectCampaignRequest(BaseModel):
    """Body of ``POST /api/campaigns/{campaign}/stage/2/select`` (Req. 6.3).

    Carries the curated ``slug`` of the campaign chosen from the list and,
    optionally, the STIX bundle ``version`` selected in Stage 1 (Stage 2 input,
    Req. 6.1). When ``version`` is omitted, the default curated version applies.
    """

    slug: str
    version: Optional[str] = None


class Stage4Response(BaseModel):
    """``GET /api/campaigns/{campaign}/stage/4`` payload — Operationalization (Req. 8.4, 8.7).

    In the **six**-stage scheme, Stage 4 is "Operationalization": the output is
    **two separate typed artifacts** (Req. 8.4, 8.7) — the **DAG-ordered
    Abilities** (reuse of ``Stage2*``/``CaseService``) and the
    **``EnvironmentParameters``** derived from the curated DAG nodes
    (hosts/services/``network_context``, destined for Stage 5). The input is the
    DAG (Stage 3 output).

    This view is **read-only**: it builds the two artifacts via
    :class:`~app.services.environment.operationalizer.CurationOperationalizer`
    (which reuses ``assemble_stage2`` and derives the parameters purely, **with no
    network and no AI** — Req. 11.3, 11.4) and reflects the per-stage state;
    opening the screen never completes the stage (the completion is the
    ``POST .../complete``).

    Attributes:
        campaign_id: *Slug* of the Campaign/campaign.
        abilities: 1st artifact — the ordered Abilities (``Stage2Output`` with
            ``atomic_ordering`` and ``translation_source``); ``None`` when the
            operationalization is suppressed (no upstream technique — Req. 8.1).
        parameters: 2nd artifact — the :class:`EnvironmentParameters` derived from
            the curated nodes (hosts/services/``network_context``), destined for
            Stage 5 (Req. 8.3, 8.4).
        origin: Operationalization origin (``curadoria_humana`` today; extensible
            to ``gerado_ia`` without redesign — Req. 8.6, 16.2, 16.4).
        both_produced: ``True`` iff **both** artifacts were produced — the Stage 4
            completion criterion (Req. 8.8). The completion itself is the
            ``POST .../stage/4/complete``.
        states: Per-stage state (6 stages), for the wizard to derive the
            sequential blocking.
    """

    campaign_id: str
    abilities: Optional[Stage2Output] = None
    parameters: EnvironmentParameters
    origin: TranslationSource = TranslationSource.HUMAN_CURATION
    both_produced: bool = False
    states: CaseStageStates


class Stage4ProvisioningResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/stage/5`` payload — Provisioning (Req. 9.6, 9.7).

    In the six-stage scheme, the simulation-environment creation is **Stage 5**
    (the old Provisioning). The screen shows the contained lab's topology (hosts,
    internal networks, Mermaid) and the isolation state. Read-only: it builds the
    topology and **verifies** the isolation via
    :class:`~app.services.environment.CurationEnvironmentProvisioner` without
    starting/creating anything.

    Attributes:
        campaign_id: Slug of the Campaign.
        name: Display name of the Campaign.
        isolated: ``True`` iff all target containers are isolated — the
            environment is ready for the Execution (Stage 6).
        isolation_unavailable: ``True`` when the isolation **could not** be
            verified (Docker/daemon unavailable); the topology is still shown, but
            Stage 5 does not complete and Stage 6 is blocked (fail-closed).
        reason: Reason when the environment is not isolated/verified, for the
            screen to show; ``None`` when isolated.
        hosts: Lab hosts (name, container, role, fixed IPs).
        networks: Internal networks (``internal: true``) and their hosts.
        mermaid: Mermaid diagram of the topology (delivered straight to the
            frontend's ``MermaidDiagram``).
        compose_file: Path of the lab's ``docker-compose.yml`` (display).
        states: Per-stage state (6 stages), already reflecting the Stage 5
            auto-completion when the environment is verified isolated and Stage 4
            is completed.
        health: Health summary of the target containers
            (:class:`~app.services.environment.health.HealthSummary`) — per-
            container verdict (``running`` + ``healthy``) and overall result, for
            the screen's prerequisite checklist (Req. 5.6, 6.1). It is
            **read-only**: it comes from the **persisted**
            :class:`ProvisionedEnvironment` output of a previous/current
            provisioning, if any; in the absence of a persisted output it returns
            an empty (not approved) :class:`HealthSummary` — opening the screen
            never brings up containers to measure the health.
        logs: Current buffer of the provisioning log lines
            (:class:`LogLineView` with monotonic ``seq``), read from the
            process-level shared
            :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`
            (Req. 2.5). It reflects the current/previous run's history, so a
            late-opened screen or a reconnection recover the live terminal (Req.
            2.7). Empty when there is no provisioning recorded for the Campaign.
            The read is **read-only** — the ``GET`` never provisions (Req. 6.1).
    """

    campaign_id: str
    name: str
    isolated: bool
    isolation_unavailable: bool = False
    reason: Optional[str] = None
    hosts: list[HostView] = Field(default_factory=list)
    networks: list[NetworkView] = Field(default_factory=list)
    mermaid: str = ""
    compose_file: Optional[str] = None
    states: CaseStageStates
    # DELTA (task 6.1): container health + provisioning log buffer.
    health: HealthSummary = Field(default_factory=HealthSummary)
    logs: list[LogLineView] = Field(default_factory=list)


class Stage5Response(BaseModel):
    """``GET /api/campaigns/{campaign}/stage/5`` payload — Execution (Req. 1.3, 1.7).

    In the six-stage scheme, Execution + measurement + report is **Stage 6** (the
    old Campaign Execution). Input = Adversary + curated Abilities
    (:class:`Stage3Input`, assembled with no side effect); accumulated output =
    the per-Ability operation results + aggregate persisted so far for the case
    (from :class:`SessionStateService.get_operation_results`). The execution
    itself is not triggered here (that is task 8.2).
    """

    input: Stage3Input
    output: list[OperationResultView] = Field(default_factory=list)


class OperationResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/operation`` payload (Req. 4.1, 4.5).

    Per-Ability result + aggregate for every Operation of the case.
    """

    campaign_id: str
    operations: list[OperationResultView] = Field(default_factory=list)


class DagListResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/dags`` payload — the Campaign's persisted DAGs (Req. 4.8).

    Mirrors the design contract: the Campaign's ``campaign_id`` and the ``dags``
    list of :class:`DagSummary` (``id``, ``origin``, ``identifier``, ``label``,
    ``created_at``) — without the graph ``content``. The listing is preceded by an
    idempotent backfill of the curated DAGs (``sync_curated_dags``), so a curated
    Campaign always shows at least its curation DAG.
    """

    campaign_id: str
    dags: list[DagSummary] = Field(default_factory=list)


class ChooseDagRequest(BaseModel):
    """Body of ``POST /api/campaigns/{campaign}/stage/3/choose-dag`` (Req. 7.2, 7.3).

    Carries the ``dag_id`` of the persisted DAG to be chosen for the execution. In
    the six-stage scheme, DAG generation is Stage 3, so the choice completes Stage
    3 (DAG generation) and releases Stage 4 (Operationalization).
    """

    dag_id: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _known_slug_or_404(slug: str, case_service: CaseService) -> None:
    """Raise 404 when ``slug`` is not one of the 8 curated cases."""
    if slug not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {slug!r}.",
        )


def _auto_complete_curated(db: Session, case_service: CaseService, campaign: str) -> None:
    """Trigger the auto-completion of the curated Stages 2 (DAG) and 4 (Environment).

    Curated campaigns pre-complete Stages 2 and 4 as soon as the previous stage
    completes (Req. 9.1–9.4). This function is the single point where the state
    reads (``GET /api/campaigns`` / ``GET /api/campaigns/{campaign}``) trigger both
    auto-completions, keeping the UI coherent without requiring an explicit step.
    Both seams:

    * respect the sequential gate (Stage 1 ``completed`` for 2; Stage 3
      ``completed`` for 4) — nothing happens while the gate has not released;
    * are idempotent and transactional (repeating the read does not duplicate
      state);
    * do **not** complete without the curated material (derivable DAG /
      environment verified isolated), so a Campaign with no material stays
      incomplete and the advance is blocked.

    Order matters: completing Stage 2 may release 3 (via the choice/curation) and,
    in a chain, enable 4; here we trigger the two independently — each call
    re-reads the current state, so on the next read the chaining stabilizes.
    Persistence/verification errors are signaled in the seams' typed results (they
    do not raise), so this function is safe on any read path.
    """
    DagService(session=db, case_service=case_service).auto_complete_stage2(campaign)
    EnvironmentStageService(db).auto_complete_stage4(campaign)


def _operationalize(db: Session, case_service: CaseService, campaign: str) -> ProducedOperationalization:
    """Produce the two Stage 4 (Operationalization) artifacts for ``campaign``.

    The single point where ``GET .../stage/4`` and ``POST .../stage/4/complete``
    build the Stage 4 dual output (Req. 8.4, 8.7), so the read and the completion
    apply exactly the same criterion. It builds the curation seam
    (:class:`CurationOperationalizer`) with the *session-bound*
    :class:`StageService` — so the ordered Abilities are assembled through the
    canonical path (``assemble_stage2``) and the :class:`EnvironmentParameters`
    are derived from the curated DAG nodes purely, **with no network and no AI**
    (Req. 11.3, 11.4).

    The Stage 4 input is the DAG (Stage 3 output): we read the curated
    :class:`~app.services.dag.dag_service.DagView` (:meth:`DagService.get_dag`,
    read-only) and pass it to :meth:`CurationOperationalizer.operationalize`, which
    uses it as a signal of the curated path and derives the parameters from the
    curated nodes read from disk.
    """
    stage_service = StageService(db, case_service=case_service)
    dag = DagService(session=db, case_service=case_service).get_dag(campaign)
    operationalizer = CurationOperationalizer(
        stage_service=stage_service, case_service=case_service
    )
    return operationalizer.operationalize(campaign, dag)


def _both_artifacts_produced(produced: ProducedOperationalization) -> bool:
    """Stage 4 completion criterion: the **two** artifacts were produced.

    Stage 4 completes when **both** typed artifacts exist (Req. 8.8): the ordered
    Abilities (present and with at least one Ability — not ``suppressed``/empty,
    Req. 8.1) **and** the :class:`EnvironmentParameters` (always a typed artifact
    derived from the curated nodes).
    """
    abilities = produced.abilities
    has_abilities = abilities is not None and abilities.has_abilities
    has_parameters = produced.parameters is not None
    return has_abilities and has_parameters


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
    summary="List the 8 curated campaigns with per-stage state and aggregate progress",
)
def list_campaigns_endpoint(db: Session = Depends(get_db)) -> CampaignsResponse:
    """List the 8 curated cases with per-stage state and aggregate progress.

    Uses :class:`CaseService` (per-case error isolation, Req. 5.6),
    :meth:`StageService.get_stage_states` (per-stage state, Req. 5.3) and
    :meth:`StageService.aggregate_progress` (Req. 5.5). A case that fails to load
    is reported with its descriptive error and still listed — one bad case never
    drops the rest (Req. 5.1, 5.6).

    For each loadable Campaign, it triggers the auto-completion of the curated
    Stages 2 (DAG) and 4 (Environment) before reading the state, so the listing
    already reflects the curated pre-fill (Req. 9.1–9.4). The seams respect the
    gate, are idempotent and do not complete without curated material.
    """
    case_service = CaseService()
    stage_service = StageService(db, case_service=case_service)

    catalog = case_service.load_all_cases_safe()
    display_names = case_service.list_cases()

    campaigns: list[CaseListItem] = []
    for slug, name in display_names.items():
        if slug in catalog.errors:
            campaigns.append(
                CaseListItem(id=slug, name=name, error=catalog.errors[slug])
            )
            continue
        case = catalog.cases.get(slug)
        # Auto-completion of the curated Stages 2 and 4 (idempotent, respects the gate).
        _auto_complete_curated(db, case_service, slug)
        campaigns.append(
            CaseListItem(
                id=slug,
                name=name,
                translation_source=(
                    case.translation_source if case else TranslationSource.HUMAN_CURATION
                ),
                states=stage_service.get_stage_states(slug),
            )
        )

    progress = stage_service.aggregate_progress()
    return CampaignsResponse(campaigns=campaigns, progress=progress)


@router.get(
    "/campaigns/{campaign}",
    response_model=CaseDetailResponse,
    summary="Detail a campaign (Abilities, Adversary, ordering, translation origin)",
)
def detail_campaign(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> CaseDetailResponse:
    """Return a curated case's Abilities, Adversary, ordering and origin.

    A missing/malformed case surfaces a descriptive 422 error via the per-case
    load result (Req. 5.6). An unknown slug is a 404.

    It triggers the auto-completion of the curated Stages 2 and 4 before reading
    the state, so the returned ``states`` reflects the curated pre-fill
    (Req. 9.1–9.4) — idempotent and respecting the sequential gate.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    result = case_service.try_load_case(campaign)
    if not result.ok or result.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(result.error.detail if result.error else f"Falha ao ler {campaign!r}."),
        )

    case = result.case
    # Auto-completion of the curated Stages 2 and 4 before re-reading the per-
    # stage state, so the UI sees the curated pre-fill (Req. 9.1–9.4).
    _auto_complete_curated(db, case_service, campaign)
    states = StageService(db, case_service=case_service).get_stage_states(campaign)
    return CaseDetailResponse(
        id=case.id,
        name=case.name,
        translation_source=case.translation_source,
        states=states,
        abilities=[_ability_view(a) for a in case.abilities],
        adversary=_adversary_view(case.adversary) if case.adversary else None,
        atomic_ordering=list(case.atomic_ordering),
    )


# ---------------------------------------------------------------------------
# Stage 1 — STIX base selection (SIX-stage scheme, task 2.3).
#
# In the six-stage scheme, Stage 1 is "STIX base selection": the Researcher
# chooses the STIX package version, the Backend downloads the package if there is
# no copy on disk yet (the single network egress) and presents the bundle
# dashboard (Req. 5.1, 5.2, 5.7, 5.8). These handlers are thin: they build a
# ``Stage1Service`` (a thin service that reuses ``StixService``) and delegate,
# returning the service's typed results.
#
# "Reuse/align, do not duplicate": the base listing (``GET /api/stix/bases``)
# stays in the ``stix`` router — there it gained the Stage 1 ``curada`` flag,
# delegating to ``Stage1Service.list_bases`` so as not to duplicate the route.
# The Stage 1 download (the single network egress) was reconciled onto the
# canonical ``POST /api/stix/bases/{version}/download`` route of the ``stix``
# router (§3a, D3) — the old ``POST .../baixar`` was removed. Here lives the
# bundle dashboard at ``GET .../stage/1``. The other STIX intelligence contracts
# (dashboard/campaigns/actors per base) stay in the ``stix`` router.
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign}/stage/1",
    response_model=Stage1ResultStix,
    summary="Stage 1 (STIX base selection): bundle dashboard + verdict",
)
def get_stage1(
    campaign: str = Path(..., description="Curated campaign slug"),
    version: Optional[str] = Query(
        None, description="STIX base version selected in the flow (e.g. 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Stage1ResultStix:
    """Return the Stage 1 "STIX base selection" result (Req. 5.1, 5.7, 5.8).

    In the six-stage scheme, Stage 1 is the STIX base selection. It delegates to
    :meth:`Stage1Service.complete`, which orchestrates: ensuring the ``version``
    bundle is on disk (downloading it only if absent — the single network egress),
    building the bundle dashboard (per-type counts) and the campaigns, and
    deriving the completion verdict — ``completed == True`` **only when** the
    bundle is on disk **and** the dashboard was built (Req. 5.8, 5.10). The output
    (bundle + dashboard + campaigns) is the Stage 2 input (Req. 2.2).

    The service **never** raises: a download/read failure is reflected in
    ``completed == False`` with ``error_message`` filled (Req. 5.9), so the stage
    screen always receives a reportable result — that is why the handler returns
    200 with the typed result on all paths.

    When ``version`` is omitted, it applies the curated version
    (:data:`~app.services.stix.stix_service.VERSAO_CURADA`) as the flow default, so
    opening the screen without an explicit selection starts from the base that
    loads the 8 curated campaigns (Req. 5.5, 5.6). The ``campaign`` is validated
    against the 8 curated campaigns (404 for an unknown slug), keeping consistency
    with the other routes of ``campaigns.py``.
    """
    from app.services.stix.stix_service import VERSAO_CURADA

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    effective_version = version if version is not None else VERSAO_CURADA
    return Stage1Service().complete(effective_version)


@router.post(
    "/campaigns/{campaign}/stage/1/execute",
    response_model=Stage1Result,
    summary="(Transitional) trigger the structural modeling (future Stage 2)",
)
def execute_stage1(
    campaign: str = Path(..., description="Curated campaign slug"),
    version: Optional[str] = Query(
        None, description="STIX base version selected in the flow (e.g. 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Stage1Result:
    """Trigger the structural modeling (:meth:`StageService.run_stage1`).

    **Transitional.** In the six-stage scheme, the Structural Modeling stops being
    Stage 1 (now "STIX base selection", above) and starts serving the Campaign
    selection (Stage 2) per the design's number→class mapping ("the ``Stage1*``
    DTO starts serving Stage 2"). The **renumbering** of this route to
    ``.../stage/2/...`` is done in tasks 3.x (which also migrate the frontend and
    the tests). Until then, the modeling execution stays accessible here so the
    Modeling is not left orphaned.

    It delegates to :meth:`StageService.run_stage1`, which persists the extracted
    elements and marks the stage ``completed`` on success (or ``erro`` with the
    failure step — reported in the typed result, not as an HTTP error). The
    optional ``version`` query param selects the STIX base used to fill the
    ``campaign_stix`` input; without ``version`` the service applies the
    informative fallback (``versao_ausente``) without raising an HTTP error
    (Req. 5.2, 5.4, 3.2).
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)
    return StageService(db, case_service=case_service).run_stage1(campaign, version=version)


@router.get(
    "/campaigns/{campaign}/stage/2",
    response_model=Stage2Response,
    summary="Campaign selection (Stage 2): bundle campaigns + extracted attributes",
    responses={
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "STIX bundle missing/malformed/invalid."
        },
    },
)
def get_stage2(
    campaign: str = Path(..., description="Curated campaign slug"),
    version: Optional[str] = Query(
        None, description="STIX base version selected in the flow (e.g. 18.0)"
    ),
    db: Session = Depends(get_db),
) -> Stage2Response:
    """Return the Stage-2 (Campaign selection) view (Req. 6.2, 6.3, 6.4).

    In the **six**-stage scheme, Stage 2 is "Campaign selection". It delegates to
    the :class:`Stage2Service` (a thin service that reuses ``StixService`` for the
    listing and ``StageService`` for the extraction), presenting:

    * the **campaign list** of the bundle selected in Stage 1, each one marked
      with ``curada``/``curated_slug``/``selectable``
      (:meth:`Stage2Service.list_campaigns`, Req. 6.2, 6.5);
    * the **extracted attributes** of the current campaign in a **read-only** view
      (:meth:`Stage2Service.read`): identity/aliases + techniques when the stage
      has already been completed — opening the screen never runs the extraction
      nor completes the stage (Req. 6.3, 6.4).

    The input is the STIX bundle selected in Stage 1 (Req. 6.1); when ``version``
    is omitted the curated version
    (:data:`~app.services.stix.stix_service.VERSAO_CURADA`) applies, so opening the
    screen without an explicit selection starts from the base that loads the 8
    curated campaigns.

    A missing/malformed bundle propagates :class:`StixFileError`, mapped to the
    ``bundle_invalido`` contract (422), consistent with the Stage 1 precedent. An
    unknown Campaign slug is a 404.
    """
    from app.services.stix.stix_service import VERSAO_CURADA

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    effective_version = version if version is not None else VERSAO_CURADA
    stage2 = Stage2Service(StageService(db, case_service=case_service))

    try:
        campaigns = stage2.list_campaigns(effective_version)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc

    # Read-only view of the current selection (never executes/persists): the
    # curated Campaign slug is the curated campaign slug (Req. 6.3, 6.4).
    selection = stage2.read(campaign, effective_version)
    return Stage2Response(
        campaign_id=campaign,
        version=effective_version,
        campaigns=campaigns,
        selection=selection,
    )


@router.post(
    "/campaigns/{campaign}/stage/2/select",
    response_model=CampaignSelectionResult,
    summary="Select a campaign and extract its attributes (completes Stage 2)",
)
def select_campaign(
    body: SelectCampaignRequest,
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> CampaignSelectionResult:
    """Select the campaign ``body.slug`` and extract its attributes (Req. 6.3, 6.4, 6.6).

    It delegates to :meth:`Stage2Service.select`, which extracts the
    identity/aliases (via ``StageService._build_campaign_stix``, defensive and
    network-free on the curated paths) and the associated techniques (structural
    extraction of the curated DAG), and derives the verdict: ``completed == True``
    **only when** the extraction succeeded (Req. 6.6); an extraction failure
    (nonexistent slug, missing/unreadable DAG) keeps the stage incomplete with the
    cause reported in ``error_message``/``failed_step`` (Req. 6.7).

    The service **never** raises for an invalid campaign — the failure is reflected
    in the typed result —, so the handler returns 200 with the result on all
    selection paths. When ``version`` is omitted in the body, it applies the
    default curated version (Stage 1/2 input, Req. 6.1). The Campaign slug in the
    route is validated against the 8 curated campaigns (404 for an unknown slug).
    """
    from app.services.stix.stix_service import VERSAO_CURADA

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    effective_version = body.version if body.version is not None else VERSAO_CURADA
    stage2 = Stage2Service(StageService(db, case_service=case_service))
    return stage2.select(body.slug, effective_version)


@router.get(
    "/campaigns/{campaign}/stage/3",
    response_model=DagView,
    summary="DAG generation (Stage 3): structural graph + Mermaid of the curated DAG",
)
def get_stage3(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> DagView:
    """Return the Stage-3 (DAG generation) view — DagView/Mermaid (Req. 7.2, 7.3).

    In the **six**-stage scheme, DAG generation is **Stage 3**
    (``ESTAGIO_GERACAO_DAG == 3``): the input is the attributes of the campaign
    selected in Stage 2 and the output is the ready curated DAG, with the explicit
    ``origin`` (``curadoria_humana``). The stage screen shows the DAG as a graph
    (nodes + edges) and the Mermaid diagram.

    This view is **read-only** and has no execution side effect: it delegates to
    :meth:`DagService.get_dag`, which reads ``data/dag/{slug}_dag.json`` via
    ``CaseService`` — **with no network and no AI** on any path (Req. 7.4, 11.1).
    Before building the view, it triggers the idempotent Stage 3 auto-completion
    (:meth:`DagService.auto_complete_stage2`, whose name is historical but operates
    over ``ESTAGIO_GERACAO_DAG``): it respects the sequential gate, does the
    curated backfill and completes Stage 3 only when there is a derivable DAG,
    without duplicating state on repeated reads (Req. 7.4, 11.3, 11.4).

    An unknown Campaign slug is a 404, consistent with the other routes of
    ``campaigns.py``. A curated Campaign whose DAG is missing/malformed is **not**
    a 404: the :class:`DagView` itself carries ``curado``/``pode_avancar``/``erro``
    for the screen to show the error state (Req. 7.6), and Stage 3 stays
    incomplete reporting the absence. The persisted DAGs (with ``dag_id``) are
    served by the ``GET .../dags`` and ``GET .../dags/{dag_id}`` endpoints (graph +
    Mermaid), kept/reused below.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)
    # Idempotent Stage 3 auto-completion (curated DAG): respects the gate, runs
    # the curated backfill and completes only when a derivable DAG exists.
    DagService(session=db, case_service=case_service).auto_complete_stage2(campaign)
    return DagService(session=db, case_service=case_service).get_dag(campaign)


@router.get(
    "/campaigns/{campaign}/stage/4",
    response_model=Stage4Response,
    summary="Operationalization (Stage 4): ordered Abilities + EnvironmentParameters",
)
def get_stage4(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> Stage4Response:
    """Return the Stage-4 (Operationalization) view — Abilities + parameters (Req. 8.4, 8.7).

    In the **six**-stage scheme, Stage 4 is "Operationalization": the output is
    **two separate typed artifacts** (Req. 8.4, 8.7) — the **DAG-ordered
    Abilities** (reuse of ``Stage2*``/``CaseService`` via ``assemble_stage2``) and
    the **``EnvironmentParameters``** derived from the curated DAG nodes
    (hosts/services/``network_context``), destined for Stage 5. The input is the
    DAG (Stage 3 output).

    It delegates to the curation seam
    (:class:`~app.services.environment.operationalizer.CurationOperationalizer`,
    via :func:`_operationalize`), which reuses ``assemble_stage2`` for the
    Abilities and **derives** the parameters purely from the curated nodes read
    from disk — **with no network and no AI** (Req. 11.3, 11.4). This view is
    **read-only** and has no execution side effect: opening the screen never
    completes the stage (the completion is the ``POST .../stage/4/complete``), it
    only exposes the two artifacts and the ``both_produced`` verdict (Req. 8.8).

    An unknown Campaign slug is a 404, consistent with the other routes. A curated
    Campaign whose operationalization is suppressed (no upstream technique) still
    responds 200 with ``abilities=None`` and ``both_produced=False`` (Req. 8.1).
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    produced = _operationalize(db, case_service, campaign)
    states = StageService(db, case_service=case_service).get_stage_states(campaign)
    return Stage4Response(
        campaign_id=campaign,
        abilities=produced.abilities,
        parameters=produced.parameters,
        origin=produced.origin,
        both_produced=_both_artifacts_produced(produced),
        states=states,
    )


@router.post(
    "/campaigns/{campaign}/stage/4/complete",
    response_model=CaseStageStates,
    summary="Complete the Operationalization (Stage 4) when both artifacts are produced",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "Stage 3 not completed (sequential blocking) or incomplete "
                "artifacts (Abilities + EnvironmentParameters)."
            )
        },
    },
)
def complete_stage4(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Complete Stage 4 (Operationalization) when the two artifacts exist (Req. 8.7, 8.8).

    In the **six**-stage scheme, the Operationalization is Stage 4
    (:data:`ESTAGIO_OPERACIONALIZACAO`). The operationalization is curated (the
    artifacts derive from the on-disk material), so "advancing from 4" is,
    semantically, "completing 4". Two preconditions, in this order:

    * **Sequential blocking (Req. 3.x):** it only completes Stage 4 if **Stage 3**
      (DAG generation, :data:`ESTAGIO_GERACAO_DAG`) is already ``completed`` —
      otherwise 409, without persisting.
    * **Both artifacts produced (Req. 8.8):** it builds the dual output via
      :func:`_operationalize` and requires that the **ordered Abilities** and the
      **``EnvironmentParameters``** both be present
      (:func:`_both_artifacts_produced`) — otherwise 409, without persisting,
      reporting the cause.

    With both satisfied, it persists the Stage 4 completion via
    :meth:`SessionStateService.persist_stage_completion` (atomic; preserves the
    previous state on failure → 500) and re-reads the per-stage state (with 4
    ``completed`` and 5 released by the gate). Thin handler: without
    reimplementing the persistence rule.
    """
    from app.models.enums import StageState  # local import: avoids a cycle at the top

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    stage_service = StageService(db, case_service=case_service)
    states = stage_service.get_stage_states(campaign)

    # (1) Sequential blocking: Stage 4 only completes if 3 is completed.
    stage3 = next(
        (e for e in states.stages if e.stage == ESTAGIO_GERACAO_DAG), None
    )
    if stage3 is None or stage3.state is not StageState.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "bloqueio_sequencial",
                "mensagem": (
                    "O Estágio 4 não pode ser concluído enquanto o Estágio 3 "
                    "não estiver concluído."
                ),
            },
        )

    # (2) Both artifacts produced (ordered Abilities + EnvironmentParameters).
    produced = _operationalize(db, case_service, campaign)
    if not _both_artifacts_produced(produced):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "artefatos_incompletos",
                "mensagem": (
                    "O Estágio 4 só conclui quando ambos os artefatos são "
                    "produzidos: as Abilities ordenadas e os EnvironmentParameters."
                ),
            },
        )

    result = SessionStateService(db).persist_stage_completion(
        campaign, ESTAGIO_OPERACIONALIZACAO
    )
    if not result.saved:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "motivo": "falha_persistencia",
                "mensagem": result.message or "Conclusão não foi salva.",
            },
        )

    # Re-read the per-stage state with 4 completed (5 released by the gate).
    return stage_service.get_stage_states(campaign)


@router.get(
    "/campaigns/{campaign}/stage/5",
    response_model=Stage4ProvisioningResponse,
    summary="Environment creation (Stage 5): topology + isolation",
)
def get_stage5_environment(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
    log_buffer: ProvisioningLogBuffer = Depends(get_log_buffer),
) -> Stage4ProvisioningResponse:
    """Return the Stage-5 (Environment creation) view (Req. 9.6, 9.7).

    In the **six**-stage scheme, the simulation-environment creation is **Stage 5**
    (the old Provisioning, which in the five-stage scheme was 4). The screen shows
    the contained lab's topology (hosts, internal networks, Mermaid) and the
    isolation state.

    Note (task 7.3): this route is the renumbering of the old Provisioning
    ``.../stage/4`` to ``.../stage/5``, moved here because Stage 4 started serving
    the Operationalization. The provisioning/isolation seam itself is task 7.3's
    responsibility; this move only preserves the endpoint under the correct number
    of the six-stage scheme.

    Flow: it triggers the Stage 4 auto-completion
    (:meth:`EnvironmentStageService.auto_complete_stage4`) — which respects the gate
    (Stage 3 ``completed``), requires the curated environment material and
    **verifies** the isolation, completing Stage 4 only when isolated (idempotent).
    The typed result carries the topology and the isolation signals, which we map
    to the screen payload; even when not isolated (or not verifiable), the topology
    is shown with the reason and Stage 5 stays blocked (Req. 5.6). The returned
    per-stage state already reflects the (non-)completion of Stage 4.

    Health + log buffer (task 6.1 — Req. 2.5, 5.6, 6.1)
    ---------------------------------------------------
    Besides the topology and the isolation, the read returns two provisioning
    signals, **both read-only** (opening the screen never brings up containers):

    * ``health`` — the :class:`~app.services.environment.health.HealthSummary` of
      the **persisted** :class:`ProvisionedEnvironment` output of a
      previous/current provisioning (via
      :meth:`EnvironmentStageService.persisted_environment`), or an empty (not
      approved) summary when there is no persisted output yet — the health is
      measured during the active provisioning, not in this read (Req. 5.6).
    * ``logs`` — the current buffer of provisioning log lines, read from the
      process-level shared
      :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`
      (``get_log_buffer``) — the **same** instance the ``EnvironmentStageService``
      feeds and the WebSocket channel reads —, so a late-opened screen or a
      reconnection recover the live terminal (Req. 2.5, 2.7). Empty when there is
      no provisioning recorded for the Campaign.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    env_service = EnvironmentStageService(db)
    auto = env_service.auto_complete_stage4(campaign)
    # The topology (always displayable) and the isolation verdicts come from the
    # curation provisioner; `prepare` is read-only (creates/starts nothing).
    result = env_service.provisioner.prepare(campaign)
    topo = result.topology

    # Health: comes from the persisted output of a previous/current provisioning,
    # if any (read-only); in its absence, an empty (not approved) summary. The read
    # does NOT provision (does not call `provision`) — Req. 6.1.
    environment = env_service.persisted_environment(campaign)
    health = environment.health if environment is not None else HealthSummary()

    # The Campaign's current log buffer, read from the shared buffer (Req. 2.5, 2.7).
    logs = log_buffer.read(campaign)

    states = StageService(db, case_service=case_service).get_stage_states(campaign)
    return Stage4ProvisioningResponse(
        campaign_id=campaign,
        name=topo.name,
        isolated=result.isolated,
        isolation_unavailable=auto.isolation_unavailable,
        reason=(None if result.isolated else (auto.message or result.isolation.summary())),
        hosts=topo.hosts,
        networks=topo.networks,
        mermaid=topo.mermaid,
        compose_file=topo.compose_file,
        states=states,
        health=health,
        logs=logs,
    )


def get_provisioner() -> CurationEnvironmentProvisioner:
    """FastAPI dependency providing the :class:`EnvironmentProvisioner`.

    Shared by the Environment creation (Stage 5, ``POST .../stage/5/provision``)
    and the Execution (Stage 6, ``POST .../stage/6/execution``). In production it
    returns the curation implementation with the production defaults (real Docker
    inspector + real Caldera client, built lazily only when the verification/load
    runs — importing never opens a socket). The tests **override** this dependency
    to inject a provisioner with *fake* isolation inspector and Caldera client, so
    the provisioning/execution endpoints are exercisable end-to-end without real
    Caldera or Docker — exactly as
    :func:`~app.api.emulation.get_runner_factory` allows for the runner. The same
    provisioner is reused for the deprovisioning at the end of Stage 6 (Req. 12).
    """
    return CurationEnvironmentProvisioner()


class ProvisionStage5Response(BaseModel):
    """``POST /api/campaigns/{campaign}/stage/5/provision`` payload — Environment creation (Req. 1.1, 1.6).

    In the **six**-stage scheme, the simulation-environment creation is **Stage
    5**. This endpoint is the idempotent **ensure/start** point of the active
    provisioning: it delegates to the
    :meth:`EnvironmentStageService.provision_stage5` seam — the **same** streaming
    pipeline the Stage 5 automatic start triggers (Req. 1.1) —, which brings up the
    lab (bring up containers → await health → load Abilities → verify isolation),
    **forwards each log line to the shared buffer** (which the screen's live
    terminal reads — Req. 2.2/2.5) and completes Stage 5 **only** when the three
    prerequisites pass (health + Abilities + isolation). The service is built with
    the shared buffer (:func:`get_log_buffer`) and the injected ``provisioner``
    (:func:`get_provisioner`), so the endpoint tests keep injecting *fakes* of
    compose/state/Caldera/isolation.

    **Idempotency (Req. 1.6):** when the environment has already been provisioned
    successfully for the Campaign and remains provisioned, the seam **reuses** the
    persisted output (``reused``) and returns ``completed=True`` **without**
    bringing up a new environment — calling the endpoint twice is safe.

    The exposed output is the complete :class:`ProvisionedEnvironment` — target
    containers, isolation summary (per-container + overall verdict),
    ``abilities_carregadas``, ``compose_file`` and ``origin``.

    Attributes:
        environment: The Stage 5 typed :class:`ProvisionedEnvironment` output. On
            all paths that **ran** the provisioning (success or step failure) the
            output accompanies the response; ``None`` only on the defensive path
            where the sequential gate blocked (Stage 4 is not completed) and the
            pipeline did not run.
        completed: ``True`` iff Stage 5 became completed — that is, the three
            prerequisites passed (``result.ready``) or a ready environment was
            reused (Req. 1.6). When ``False``, Stage 6 stays blocked.
        reason: Reason when Stage 5 does **not** complete — derived from the last
            error line of the failed step's console (screen ↔ console consistency —
            Req. 7.5), from the gate blocking or from a persistence failure;
            ``None`` on success. The screen uses this to show the cause.
        states: Per-stage state (6 stages), already reflecting the (non-)completion
            of Stage 5 — the source of the sequential blocking for the wizard.
    """

    environment: Optional[ProvisionedEnvironment] = None
    completed: bool = False
    reason: Optional[str] = None
    states: CaseStageStates


@router.post(
    "/campaigns/{campaign}/stage/5/provision",
    response_model=ProvisionStage5Response,
    summary="Stage 5: provisions + loads Abilities + verifies isolation (does not complete without isolation)",
)
def provision_stage5(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
    provisioner: CurationEnvironmentProvisioner = Depends(get_provisioner),
    log_buffer: ProvisioningLogBuffer = Depends(get_log_buffer),
) -> ProvisionStage5Response:
    """Ensure/start the active Stage 5 provisioning (idempotent — Req. 1.1, 1.6).

    In the **six**-stage scheme, the simulation-environment creation is **Stage 5**
    (``stage=5``). This handler is **thin**: it delegates all the provisioning to
    the active seam :meth:`EnvironmentStageService.provision_stage5` — the **same**
    streaming pipeline the Stage 5 automatic start triggers (Req. 1.1), so the
    endpoint is the idempotent "ensure/start" point and not a parallel flow. The
    service is built with:

    * ``log_buffer`` — the **process-level shared** :class:`ProvisioningLogBuffer`
      (:func:`get_log_buffer`), the same instance the WebSocket channel and the
      ``GET .../stage/5/logs`` read, so each line emitted by the pipeline reaches
      the screen's live terminal (Req. 2.2, 2.5);
    * ``provisioner`` — the :class:`CurationEnvironmentProvisioner` injected via
      :func:`get_provisioner` (the tests override it to inject *fakes* of
      compose/state/Caldera/isolation, exercising the endpoint without real Docker
      or Caldera);
    * ``params_provider`` — resolves the :class:`EnvironmentParameters` (Stage 5
      input) through the **same canonical path** the ``GET .../stage/4`` uses
      (:func:`_operationalize`), **with no network and no AI** (Req. 11.3, 11.4),
      avoiding recomputing the parameters on a divergent path.

    The seam:

    1. **Idempotency (Req. 1.6):** if the environment has already been provisioned
       successfully and remains provisioned (``ProvisionedEnvironment`` ready
       persisted + the Stage 5 ``StageRun`` already ``completed``), it **reuses**
       the output without bringing up a new environment (``result.reused``) and
       returns ``ready``.
    2. **Streaming pipeline:** it resolves the parameters, brings up the lab,
       awaits the health, loads the Abilities (DAG order) and **verifies the
       isolation** read-only, forwarding each line to the shared buffer.
    3. **Completion and ``StageRun`` (Req. 5.5, 6.2, 6.4, 7.1):** the seam itself
       completes Stage 5 (``StageRun`` → ``completed``/100 and persistence of the
       output) **only** when the three prerequisites pass (``result.ready``); on a
       step failure it writes ``failed_step`` + ``error_message`` (derived from the
       last console error line) and keeps Stage 6 blocked. We do **not** re-persist
       the completion here — the service owns that transition.

    Mapping to the response: ``environment = result.environment`` (``None`` only on
    the defensive path where the gate blocked), ``completed = result.ready``,
    ``motivo = result.error_message`` when not completed, and ``states`` re-read to
    reflect the (non-)completion. An unknown Campaign *slug* is a 404, consistent
    with the other routes of ``campaigns.py``.
    """
    from app.models.enums import StageState  # local import: avoids a cycle at the top

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    stage_service = StageService(db, case_service=case_service)

    # Ensures Stage 4 (Operationalization) is completed before the active seam's
    # sequential gate (Req. 1.1 — Stage 5 only starts with Stage 4 completed). The
    # curated campaigns' operationalization is derived purely from the on-disk
    # material; when the two artifacts exist, we complete Stage 4 idempotently via
    # :meth:`SessionStateService.persist_stage_completion` (absolute upsert;
    # preserves the previous state on failure), so reaching the Stage 5 screen (and
    # therefore this endpoint) implies Stage 4 completed — exactly the invariant the
    # guided flow guarantees.
    states = stage_service.get_stage_states(campaign)
    stage4 = next(
        (e for e in states.stages if e.stage == ESTAGIO_OPERACIONALIZACAO), None
    )
    if stage4 is None or stage4.state is not StageState.COMPLETED:
        produced = _operationalize(db, case_service, campaign)
        if _both_artifacts_produced(produced):
            SessionStateService(db).persist_stage_completion(
                campaign, ESTAGIO_OPERACIONALIZACAO
            )

    # Delegates to the active seam: streaming provisioning with the shared buffer
    # (live terminal) + injected provisioner (fakes in the tests). The
    # ``params_provider`` reuses the canonical Stage 4 path to resolve the Stage 5
    # input identically to the ``GET .../stage/4`` (no network/AI).
    service = EnvironmentStageService(
        db,
        provisioner=provisioner,
        log_buffer=log_buffer,
        params_provider=lambda _c: _operationalize(db, case_service, _c).parameters,
    )
    result = service.provision_stage5(campaign)

    states = stage_service.get_stage_states(campaign)
    return ProvisionStage5Response(
        environment=result.environment,
        completed=result.ready,
        reason=(None if result.ready else result.error_message),
        states=states,
    )


class Stage5LogsResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/stage/5/logs`` payload — current log buffer (Req. 2.5).

    Returns the **current buffer** of provisioning log lines of the Campaign's
    Stage 5, read from the process-level shared
    :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer` — the
    **same** instance the :class:`EnvironmentStageService` feeds during the
    pipeline (via ``on_log``) and the WebSocket channel reads for the live
    terminal. It serves the terminal **recovery** when the screen is opened late or
    the live connection is reestablished (Req. 2.5, 2.7): the client gets the
    current run's history without losing lines.

    The read is **read-only**: it never provisions nor mutates the buffer; it
    returns a copy of the :class:`LogLineView` in monotonic ``seq`` order. Empty
    when there is no provisioning recorded for the Campaign.

    Attributes:
        campaign_id: *Slug* of the Campaign whose buffer is returned.
        logs: The current log lines (``seq``, ``step``, ``stream``, ``text``,
            ``at``), in ``seq`` order.
    """

    campaign_id: str
    logs: list[LogLineView] = Field(default_factory=list)


@router.get(
    "/campaigns/{campaign}/stage/5/logs",
    response_model=Stage5LogsResponse,
    summary="Stage 5: current buffer of provisioning log lines (read-only)",
)
def get_stage5_logs(
    campaign: str = Path(..., description="Curated campaign slug"),
    log_buffer: ProvisioningLogBuffer = Depends(get_log_buffer),
) -> Stage5LogsResponse:
    """Return the current buffer of provisioning log lines (Req. 2.5, 2.7).

    Thin and **read-only** handler: it reads the Campaign's current buffer from the
    shared :class:`ProvisioningLogBuffer` (:func:`get_log_buffer`) — the same
    instance the ``POST .../stage/5/provision`` feeds via the active seam and the
    WebSocket channel reads for the live terminal —, so a late-opened screen or a
    reconnection recover the current run's history without losing lines (Req. 2.5,
    2.7). It provisions nothing. An unknown Campaign *slug* is a 404, consistent
    with the other routes of ``campaigns.py``.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)
    return Stage5LogsResponse(campaign_id=campaign, logs=log_buffer.read(campaign))


@router.get(
    "/campaigns/{campaign}/stage/6",
    response_model=Stage5Response,
    summary="Input (Adversary + Abilities) and accumulated output of Stage 6 (Execution)",
)
def get_stage6(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> Stage5Response:
    """Return the Stage-6 (Execution) input + accumulated output (Req. 10.x).

    In the **six**-stage scheme, the Execution, measurement and report is **Stage
    6** (the old Execution, which in the five-stage scheme was 5). Input =
    Adversary + curated Abilities (assembled, no side effects). Accumulated output
    = the per-Ability operation results persisted so far for the case. Execution
    itself is not triggered here (that is task 8.2/8.5).

    Note (task 8.5): this route is the renumbering of the old Execution
    ``.../stage/5`` to ``.../stage/6``, moved here because, in the six-stage
    scheme, Stage 5 became the Environment creation and 6 the Execution.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    stage_service = StageService(db, case_service=case_service)
    input = stage_service.assemble_stage3(campaign)
    output = SessionStateService(db).get_operation_results(campaign)
    return Stage5Response(input=input, output=output)


# ---------------------------------------------------------------------------
# Stage 6 — Execution, measurement and report (SIX-stage scheme, task 8.5)
#
# Stage 6 (Execution + measurement + report) **fully reuses** the already-built
# ``OperationRunner`` (the four gates: confirmation → containment → isolation →
# availability) through the *session-bound* seams of tasks 8.2/8.3/8.4:
#
# * :class:`ExecutionStageService` (task 8.2) — preview + single execution reusing
#   the runner, with the gates;
# * :class:`MeasurementService` (task 8.3) — procedural-sufficiency measurement +
#   report;
# * :class:`CompletionStage6Service` (task 8.4) — ``execute_and_complete``:
#   execution (deferred completion) → measurement/report → **ALWAYS** deprovisions
#   → completes Stage 6 only when the execution completed, the environment was
#   deprovisioned and the report was persisted.
#
# This task (8.5) is the **renumbering** of the old Stage 5 emulation pair
# (``api/emulation.py``: ``.../stage/5/emulation/preview`` + ``.../emulation``)
# to the six-stage scheme, moving the execution HTTP surface to
# ``.../stage/6/execution/preview`` + ``.../stage/6/execution`` and tying it to
# the Stage 6 seams (which add the measurement/report and the always-on
# deprovisioning). The injection dependencies (``get_runner_factory``, Caldera
# client) and the preview presentation are **reused** from ``api/emulation.py`` —
# without duplication. Result-to-HTTP mapping:
#
#   * ``CONTAINMENT_REFUSED`` (external destination) / ``ISOLATION_FAILED``
#     (non-isolated target container) → **409** with the offending detail (Req.
#     13.4, 13.5, 14.2, 14.4);
#   * :class:`CalderaUnavailable` (Caldera silent within 10s) → **503** (Req.
#     10.1); nothing was executed;
#   * :class:`CalderaApiError` (Caldera responded with an error) → **502** (upstream);
#   * ``NOT_CONFIRMED`` (confirmation missing/false) → **200** with a body
#     indicating nothing started (Req. 13.2);
#   * ``COMPLETED`` / ``ABORTED`` → **200** with the complete result (execution +
#     report + deprovisioning).
#
# ``GET /api/campaigns/{campaign}/report`` returns the consolidated
# :class:`ExecutionReport` (metrics + results + flags) derived from the
# **persisted** per-Ability results plus the curated DAG, via
# :class:`MeasurementService` (Req. 15.7).
# ---------------------------------------------------------------------------


class ExecutionRequest(BaseModel):
    """Body of ``POST /api/campaigns/{campaign}/stage/6/execution`` (Req. 13.2).

    ``confirmed`` is the Researcher's **explicit** confirmation. The Operation
    starts *if and only if* this field is exactly ``True``; any other value
    (missing/false/cancelled) means "not confirmed" and nothing is executed — gate
    1 of the ``OperationRunner`` (Req. 13.2).
    """

    confirmed: bool = Field(
        default=False,
        description=(
            "The Researcher's explicit confirmation. The execution only starts "
            "with confirmed=true; otherwise no command is executed."
        ),
    )


def _report_sink_memory(_report: ExecutionReport) -> bool:
    """*No-op* report persister that always confirms (pre-table, task 10.x).

    The ``execution_reports`` table is introduced by the task 10.x migration;
    until then the Stage 6 completion seam (:class:`CompletionStage6Service`)
    requires only that *some* sink confirms the persistence (``True``) to release
    the completion (Req. 10.9). This sink confirms without writing — the report is
    deterministically recomputable from the persisted per-Ability results + the
    curated DAG (see :func:`get_report`), so no information is lost. When the table
    exists, this sink is swapped for one that writes the ``execution_reports`` row,
    without changing the handlers.
    """
    return True


@router.post(
    "/campaigns/{campaign}/stage/6/execution/preview",
    response_model=EmulationPreviewResponse,
    summary="Stage 6: list of concrete commands + target container + external destination (without executing)",
)
def preview_execution_stage6(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> EmulationPreviewResponse:
    """Preview of the Stage 6 commands — visible precondition (Req. 13.6, 13.7).

    It exposes the complete list of concrete commands with each one's
    **target container** and the **external-destination** flag (``has_external``),
    **without executing anything** and **without creating an Operation** — it is
    the presentation the confirmation modal shows *before* any confirmation. It
    delegates to :meth:`ExecutionStageService.preview` (which reuses
    :func:`~app.services.containment.preview_abilities`) and reuses the response
    mapping of ``api/emulation.py`` (:func:`_preview_view`), without duplicating
    the presentation.

    External destinations are **flagged**, not refused here; the refusal is one of
    the execution gates (:func:`execute_stage6`, via the runner). An unknown
    Campaign *slug* is a 404; a missing/malformed Campaign file is a 422 — the same
    distinction the seam raises (``KeyError``/``ValueError``).
    """
    case_service = CaseService()
    service = ExecutionStageService(
        db,
        runner_factory=lambda _s: None,  # type: ignore[arg-type,return-value]
        case_service=case_service,
    )
    try:
        preview = service.preview(campaign)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {campaign!r}.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _preview_view(campaign, preview)


@router.post(
    "/campaigns/{campaign}/stage/6/execution",
    response_model=CompletionStage6Result,
    summary="Stage 6: confirmation + execution (gates) + measurement + report + deprovisioning",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "Containment violated — external destination (Req. 13.4) or "
                "non-isolated target container (Req. 14.2). Nothing was executed."
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Caldera did not respond within 10s (Req. 10.1). Nothing was executed."
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Caldera responded with an error when receiving the operation."
        },
    },
)
def execute_stage6(
    body: ExecutionRequest,
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
    runner_factory: RunnerFactory = Depends(get_runner_factory),
    provisioner: CurationEnvironmentProvisioner = Depends(get_provisioner),
) -> CompletionStage6Result:
    """Execute Stage 6 with the four gates, measure, generate the report and deprovision.

    Flow (Req. 10.1–10.9, 12, 13):

    1. It derives the Stage 4 :class:`EnvironmentParameters` (via
       :func:`_operationalize`) and **provisions/verifies** the curated
       environment via :class:`CurationEnvironmentProvisioner`, obtaining the
       :class:`ProvisionedEnvironment` — the target of the deprovisioning at the
       end.
    2. It delegates to :meth:`CompletionStage6Service.execute_and_complete`, which:
       executes **exactly once** reusing the ``OperationRunner`` with the four
       gates (confirmation → containment → isolation → availability, Req. 13.1);
       **measures** the procedural sufficiency and generates the
       :class:`ExecutionReport`; **ALWAYS** deprovisions the environment (success
       or failure, Req. 10.8, 12.1); and completes Stage 6 **only** when the
       execution completed, the environment was deprovisioned and the report was
       persisted (Req. 10.9).

    The typed result (:class:`CompletionStage6Result`) carries the execution, the
    report, the deprovisioning and the completion verdict. The handler only maps
    the **refusal**/unavailability paths to the design's HTTP:

    * ``CONTAINMENT_REFUSED`` → **409** with the violations (external destination);
    * ``ISOLATION_FAILED`` → **409** with the non-isolated containers;
    * :class:`CalderaUnavailable` → **503** (nothing started);
    * :class:`CalderaApiError` → **502** (upstream);
    * ``NOT_CONFIRMED`` → **200** (nothing started);
    * ``COMPLETED`` / ``ABORTED`` → **200** with the complete result.

    Persistence note: until the ``execution_reports`` table exists (task 10.x), the
    report is persisted by a *no-op* sink that confirms (the report is recomputable
    in ``GET .../report``), so the completion is not blocked by a dependency not
    yet created (Req. 10.9).
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    # (1) Stage 4 parameters + provisioning/verification of the curated environment.
    # The same provisioner (injectable via get_provisioner) is reused for the
    # deprovisioning at the end (Req. 12).
    parameters = _operationalize(db, case_service, campaign).parameters
    environment: ProvisionedEnvironment = provisioner.provision(parameters)

    service = CompletionStage6Service(
        db,
        runner_factory=runner_factory,
        provisioner=provisioner,
        report_sink=_report_sink_memory,
        case_service=case_service,
    )

    try:
        result = service.execute_and_complete(
            campaign, confirmed=body.confirmed, environment=environment
        )
    except CalderaUnavailable as exc:
        # Gate 4 (Req. 10.1): Caldera silent within 10s → 503. Nothing started.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CalderaApiError as exc:
        # Caldera responded, but refused/failed to process the payload → 502.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "caldera_api_error",
                "mensagem": str(exc),
                "endpoint": exc.path,
                "status_caldera": exc.status_code,
            },
        ) from exc

    execution = result.execution

    if execution.result is RunOutcome.CONTAINMENT_REFUSED:
        # Gate 2 (Req. 13.4): external destination → 409, identifying the violation.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "contencao_recusada",
                "mensagem": execution.message,
                "violations": execution.violations,
            },
        )

    if execution.result is RunOutcome.ISOLATION_FAILED:
        # Gate 3 (Req. 14.2): non-isolated target container → 409, identifying it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "isolamento_falhou",
                "mensagem": execution.message,
                "containers": execution.non_isolated_containers,
            },
        )

    # NOT_CONFIRMED (Req. 13.2): nothing started — 200. COMPLETED/ABORTED: complete
    # result (execution + report + deprovisioning). The deprovisioning happened on
    # ALL these paths (Req. 10.8, 12.1).
    return result


@router.get(
    "/campaigns/{campaign}/report",
    response_model=ExecutionReport,
    summary="Stage 6: execution report + procedural-sufficiency metrics",
)
def get_report(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> ExecutionReport:
    """Return the consolidated report + the Stage 6 metrics (Req. 10.7, 15.7).

    It derives the :class:`ExecutionReport` from the Campaign's **persisted**
    per-Ability results (:meth:`SessionStateService.get_operation_results`) plus
    the curated DAG, via :class:`MeasurementService` (**pure and deterministic**
    measurement — no network/Docker/AI). The metrics (success rate, procedural
    coverage, time, failed nodes, per-technique telemetry and verdict) are
    recomputed on every read
    from the persisted results, so the report always reflects the current
    execution state (Req. 15.1–15.7).

    The per-Ability results of **all** the Campaign's Operations are flattened (a
    curated Campaign runs a single Operation; defensively we consolidate all) and
    normalized into :class:`AbilityMeasurementResult` for the measurement and into
    :class:`Stage3AbilityResult` for the report. The ``environment_deprovisioned``
    flag reflects the aggregate result of the finished Operations: since the
    curated environment is **always** deprovisioned at the end of a completed
    execution (Req. 10.8, 12.1), it is ``True`` when there is at least one finished
    Operation; otherwise ``False`` (no execution completed yet).

    An unknown Campaign *slug* is a 404. A Campaign with no execution still returns
    200 with zeroed metrics (DAG as the denominator, zero techniques in success) —
    the report screen always receives a reportable result.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    # Missing/malformed Campaign → 422 (consistent with the other routes).
    campaign_result = case_service.try_load_case(campaign)
    if not campaign_result.ok or campaign_result.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                campaign_result.error.detail
                if campaign_result.error
                else f"Falha ao ler o Caso {campaign!r}."
            ),
        )
    case = campaign_result.case

    operations = SessionStateService(db).get_operation_results(campaign)
    ability_results: list[AbilityMeasurementResult] = []
    stage3_results: list[Stage3AbilityResult] = []
    any_finished = False
    for operation in operations:
        if operation.state is OperationState.FINISHED:
            any_finished = True
        for ability in operation.results:
            ability_results.append(
                AbilityMeasurementResult(ability_id=ability.ability_id, status=ability.status)
            )
            stage3_results.append(
                Stage3AbilityResult(
                    ability_id=ability.ability_id or "",
                    status=ability.status,
                    command_output=ability.command_output,
                )
            )

    return MeasurementService().report_from_campaign(
        campaign=case,
        ability_results=ability_results,
        stage3_results=stage3_results,
        environment_deprovisioned=any_finished,
    )


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
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)
    operations = SessionStateService(db).get_operation_results(campaign)
    return OperationResponse(campaign_id=campaign, operations=operations)


@router.get(
    "/campaigns/{campaign}/dag",
    response_model=DagView,
    summary="Structural DAG of the campaign (nodes, edges and Mermaid) or error state",
)
def get_dag(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> DagView:
    """Return the structural DAG view for a curated case (Req. 5.2, 5.3, 6.x).

    READ-ONLY and side-effect free: mirrors the invariant of
    :func:`get_stage1` / ``topology.get_topology`` — opening the DAG screen only
    reads ``{slug}_dag.json`` through :class:`DagService`, never executes
    anything, performs no Docker/network access and invokes **no AI service** on
    any path (Req. 9.1, 9.2).

    An unknown slug is a 404, consistent with every other ``campaigns.py`` route
    (:func:`_known_slug_or_404` checks the 8 curated cases from
    ``CaseService.list_cases``). The pass-through non-curated stub in
    :meth:`DagService.get_dag` is intended for future STIX campaigns that are not
    among the curated 8; this endpoint is keyed by a curated-case ``{campaign}``
    slug, so a slug outside the 8 is unknown here.

    A curated case whose DAG is merely missing/malformed is NOT a 404: the
    :class:`DagView` itself carries ``curado=True``/``pode_avancar=False``/``erro``
    so the UI can show the error state (Req. 5.3, 6.4). The handler stays thin —
    :meth:`DagService.get_dag` returns a typed value (never raises), so the
    ``DagView`` fields express curado/stub/erro and FastAPI serializes it
    directly; no translation to the ``{"detail": ...}`` shape is needed.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)
    return DagService(case_service=case_service).get_dag(campaign)


# ---------------------------------------------------------------------------
# Stage 3 — DAG generation: listing and choice of persisted DAGs
# (Req. 7.2, 7.3; ``ESTAGIO_GERACAO_DAG == 3`` in the six-stage scheme).
# The stage view is served by ``GET .../stage/3`` (DagView/Mermaid, above);
# these endpoints keep/reuse the *persisted* DAGs (with ``dag_id``): the list
# ``GET .../dags``, the detail ``GET .../dags/{dag_id}`` and the choice
# ``POST .../stage/3/choose-dag`` (completes Stage 3). Thin handlers: they build
# the DagService with the request session (get_db) and delegate, mapping the typed
# result of choose_dag to the corresponding HTTP code.
# ---------------------------------------------------------------------------


@router.get(
    "/campaigns/{campaign}/dags",
    response_model=DagListResponse,
    summary="List the persisted DAGs of a Campaign (Stage 3)",
)
def list_dags(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> DagListResponse:
    """List the persisted DAGs of a Campaign (Req. 4.8, 6.1).

    It precedes the listing with an idempotent backfill of the curated DAGs
    (:meth:`DagService.sync_curated_dags`), which reads ``data/dag/{slug}_dag.json``
    via ``CaseService`` — **with no network and no AI** (Req. 5.5, 9.1) — and
    persists 1 ``curadoria_humana`` DAG per curated Campaign if it does not exist
    yet. Running the backfill lazily on listing guarantees that a curated Campaign
    always shows at least its DAG, without requiring an explicit synchronization
    step, and, being idempotent, it does not duplicate on repeated accesses.
    ``sync_curated_dags`` already returns the resulting listing
    (:class:`DagSummary`), so the handler only passes it through.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)
    dags = DagService(session=db, case_service=case_service).sync_curated_dags(
        campaign
    )
    return DagListResponse(campaign_id=campaign, dags=dags)


@router.get(
    "/campaigns/{campaign}/dags/{dag_id}",
    response_model=DagDetail,
    summary="Graph + Mermaid of a persisted DAG (Stage 3)",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "nonexistent dag_id or one not belonging to the Campaign."
        },
    },
)
def get_persisted_dag(
    campaign: str = Path(..., description="Curated campaign slug"),
    dag_id: int = Path(..., description="Id of the persisted DAG"),
    db: Session = Depends(get_db),
) -> DagDetail:
    """Return the graph + Mermaid of a persisted DAG (Req. 4.4).

    It delegates to :meth:`DagService.get_persisted_dag`, which rebuilds the graph
    from the persisted ``Dag.content`` and builds the Mermaid consistently with
    :func:`get_dag`. A nonexistent ``dag_id`` or one not belonging to the Campaign
    results in a 404 Not Found (the service returns ``None``). The handler stays
    thin: it validates the curated slug (:func:`_known_slug_or_404`) and maps the
    absence to 404.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    detail = DagService(session=db, case_service=case_service).get_persisted_dag(
        campaign, dag_id
    )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "motivo": "dag_nao_encontrado",
                "mensagem": (
                    "O DAG solicitado não existe ou não pertence a esta Campanha."
                ),
            },
        )
    return detail


@router.post(
    "/campaigns/{campaign}/stage/3/choose-dag",
    response_model=CaseStageStates,
    summary="Choose a DAG for the execution, completing Stage 3 (DAG generation)",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "nonexistent dag_id or one not belonging to the Campaign."
        },
        status.HTTP_409_CONFLICT: {
            "description": "No persisted DAG for the Campaign (advance blocked)."
        },
    },
)
def choose_dag(
    body: ChooseDagRequest,
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Choose a DAG for the execution, completing Stage 3 (Req. 7.2, 7.3).

    In the **six**-stage scheme, DAG generation is **Stage 3**
    (``ESTAGIO_GERACAO_DAG == 3``), so the route becomes ``.../stage/3/choose-dag``,
    aligned with the single source of the stage number in ``stage_service``. It
    delegates to :meth:`DagService.choose_dag`, which writes ``chosen_dag_id``
    on the Stage 3 (DAG generation) ``StageRun`` and completes it (progress 100),
    releasing Stage 4 (Operationalization) through the sequential gate. The typed
    result (:class:`DagChoiceResult`) is mapped to the corresponding HTTP — the
    handler stays thin, without reimplementing business rules:

    * **success** → 200 with the per-stage state already with Stage 3
      ``completed`` and 4 released (:class:`CaseStageStates`, same shape as
      ``GET /api/campaigns/{campaign}``);
    * **``SEM_DAG``** → 409 Conflict (the Campaign has no persisted DAG —
      Req. 6.2);
    * **``DAG_NAO_ENCONTRADO``** → 404 Not Found (nonexistent ``dag_id`` / one not
      belonging to the Campaign — Req. 6.3);
    * **``FALHA_PERSISTENCIA``** → 500 (persistence failure; the previous state is
      preserved by the service's rollback).

    The result's message is propagated in the error's ``detail``.
    """
    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    dag_service = DagService(session=db, case_service=case_service)
    result = dag_service.choose_dag(campaign, body.dag_id)

    if result.success:
        # Re-read the per-stage state with Stage 3 completed and 4 released.
        return StageService(db, case_service=case_service).get_stage_states(campaign)

    # Typed refusal/failure → corresponding HTTP code, with the message.
    _FAILURE_TO_STATUS = {
        DagChoiceFailure.NO_DAG: status.HTTP_409_CONFLICT,
        DagChoiceFailure.DAG_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        DagChoiceFailure.PERSISTENCE_FAILURE: status.HTTP_500_INTERNAL_SERVER_ERROR,
    }
    http_code = _FAILURE_TO_STATUS.get(
        result.failure, status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    raise HTTPException(
        status_code=http_code,
        detail={
            "motivo": result.failure.value if result.failure else "failure",
            "mensagem": result.message or "A escolha do DAG não foi concluída.",
        },
    )


# ---------------------------------------------------------------------------
# Guided Execution Wizard — stage completion and campaign reset
# (spec: wizard-execution-guiada). Every completion goes through
# SessionStateService.persist_stage_completion (atomicity + state preservation
# already guaranteed); the reset clears the persisted state to repeat the demo.
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign}/stage/3/complete",
    response_model=CaseStageStates,
    summary="Complete Stage 3 (DAG generation) and unlock Stage 4",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": "Stage 2 not completed yet (sequential blocking)."
        },
    },
)
def complete_stage3(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Persist the completion of Stage 3 (DAG generation) — Req. 1.3, 1.4, 1.7.

    In the six-stage scheme, DAG generation is Stage 3. The curated DAG already
    exists, so "advancing from 3" is, semantically, "completing 3". It respects
    the sequential blocking: it only completes 3 if **Stage 2** (Campaign
    selection) is already ``completed`` (otherwise 409, without persisting), so
    the gate falls on Stage 2.
    """
    from app.models.enums import StageState  # local import: avoids a cycle at the top

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    stage_service = StageService(db, case_service=case_service)
    states = stage_service.get_stage_states(campaign)

    # Sequential blocking (Req. 1.4): Stage 3 only completes if 2 is completed.
    stage2 = next((e for e in states.stages if e.stage == 2), None)
    if stage2 is None or stage2.state is not StageState.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "bloqueio_sequencial",
                "mensagem": (
                    "O Estágio 3 não pode ser concluído enquanto o Estágio 2 "
                    "não estiver concluído."
                ),
            },
        )

    result = SessionStateService(db).persist_stage_completion(campaign, 3)
    if not result.saved:
        # Persistence failure: previous state preserved (Req. 3.6).
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "motivo": "falha_persistencia",
                "mensagem": result.message or "Conclusão não foi salva.",
            },
        )

    # Re-read the per-stage state with 3 completed (4 unlocked).
    return stage_service.get_stage_states(campaign)


@router.post(
    "/campaigns/{campaign}/reset",
    response_model=CaseStageStates,
    summary="Reset the campaign state (to repeat the demonstration)",
)
def reset_campaign(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> CaseStageStates:
    """Remove completed stages and operation results of the campaign (Req. 6).

    In a single transaction, it deletes the campaign's ``StageRun`` and the
    associated ``Operation`` (with their ``AbilityResult`` and audit records),
    returning the campaign to the initial state (Stage 1 ``nao_iniciado``, 2/3/4/5
    ``bloqueado``).
    """
    from app.models.domain import (  # local import: avoids a cycle at the top
        AbilityResult,
        AuditLogEntry,
        Operation,
        StageRun,
    )

    case_service = CaseService()
    _known_slug_or_404(campaign, case_service)

    # The campaign's operation ids, to clear the dependent results/audit.
    op_ids = [
        row[0]
        for row in db.query(Operation.id).filter(Operation.campaign_id == campaign).all()
    ]
    if op_ids:
        db.query(AbilityResult).filter(
            AbilityResult.operation_id.in_(op_ids)
        ).delete(synchronize_session=False)
        db.query(AuditLogEntry).filter(
            AuditLogEntry.operation_id.in_(op_ids)
        ).delete(synchronize_session=False)
    db.query(Operation).filter(Operation.campaign_id == campaign).delete(
        synchronize_session=False
    )
    db.query(StageRun).filter(StageRun.campaign_id == campaign).delete(
        synchronize_session=False
    )
    db.commit()

    return StageService(db, case_service=case_service).get_stage_states(campaign)
