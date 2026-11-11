"""REST endpoints for AI curation runs (task 16.1).

Two thin endpoints over :class:`~app.services.ai.run_service.AgentRunService`:

* ``POST /api/campaigns/{campaign}/ai/run`` — start an AI curation run for a
  campaign. The handler delegates synchronously to
  :meth:`AgentRunService.start`, which creates the ``agent_runs`` row and invokes
  the compiled StateGraph; on success the row is committed and returned. The
  AI_Agent_System must create the row and invoke the graph within 2 seconds of
  the start request (Req. 13.1) — this handler just delegates to the service
  synchronously, so the 2s SLA is met by the service's own invocation path, not
  by any extra work here.

  On ANY failure — a missing dependency
  (:class:`~app.services.ai.run_service.DependencyUnavailableError`), the graph
  invocation failing (:class:`~app.services.ai.run_service.RunFailedError`),
  per-iteration persistence failing
  (:class:`~app.services.ai.run_service.PersistenceError`), or any other error —
  the handler rolls the session back so NO partial ``agent_runs`` row (nor any
  flushed-but-uncommitted bookkeeping row) is persisted, and returns an error
  response indicating the run could not be started (Req. 13.2). The service only
  ``flush``es, never ``commit``s, so a rollback here discards everything the
  failed attempt wrote.

* ``GET /api/campaigns/{campaign}/ai/runs/{id}`` — read a run with its iterations
  (the ``agent_iterations`` rows) and the evaluator verdict (from the
  ``agent_evaluations`` rows), scoped to the campaign (Req. 13.2).

Handlers stay thin and follow the existing router conventions (prefix ``/api``,
pydantic response models with ``Field`` descriptions, injected ``Session`` via
``Depends(get_db)``).

_Requirements: 13.1, 13.2_
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.ai.run_service import (
    AgentRunService,
    DependencyUnavailableError,
    PersistenceError,
    RunFailedError,
)

router = APIRouter(prefix="/api", tags=["ai"])


# The explicit "not available" sentinel the read endpoint emits for any
# Agent_Processing element that is genuinely absent (recorded as ``None`` /
# missing by the trace sink — a stage that ran without an LLM call, a curation
# fallback that captured nothing). The serialization layer maps null/absent
# processing elements to this marker rather than omitting the stage or agent, so
# the frontend renders an explicit indicator instead of a blank field
# (Req. 1.6; design §A/§C "Not available" indicator).
NOT_AVAILABLE = "not_available"

# The Agent_Processing elements an ``agent_stage_traces.processing`` payload
# always carries (mirrors ``_trace._PROCESSING_KEYS``): each is normalized to the
# explicit ``None`` sentinel at persist time, so the read layer resolves a null /
# missing element to :data:`NOT_AVAILABLE` for every one of these keys even when
# the stored payload dropped it.
_PROCESSING_KEYS = (
    "llm_prompt",
    "llm_response",
    "reasoning",
    "tools_invoked",
    "repair_attempts",
    "repair_hints",
)


def _resolve_processing(processing: Optional[dict]) -> dict:
    """Map null/absent Agent_Processing elements to the "not available" sentinel.

    Every known processing key (:data:`_PROCESSING_KEYS`) is present in the
    result: a ``None`` or missing element resolves to :data:`NOT_AVAILABLE` so the
    frontend renders an explicit "not available" indicator rather than omitting
    the Stage or Agent (Req. 1.6). Any extra keys the agent captured (e.g. the
    Environment agent's ``fixed_infrastructure`` / ``agent_decided`` / ``gates`` /
    ``substages``) are preserved as-is, with a ``None`` value likewise resolved to
    the sentinel.
    """
    source = dict(processing) if isinstance(processing, dict) else {}
    resolved: dict[str, Any] = {}
    for key in _PROCESSING_KEYS:
        value = source.get(key)
        resolved[key] = NOT_AVAILABLE if value is None else value
    for key, value in source.items():
        if key not in resolved:
            resolved[key] = NOT_AVAILABLE if value is None else value
    return resolved


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AiRunStartRequest(BaseModel):
    """Body of ``POST /api/campaigns/{campaign}/ai/run`` (Req. 13.1).

    ``max_iterations`` is the optional loop guard. When omitted the service
    resolves the configured default (``SepinfSettings.max_iterations``, default
    3); when provided it is clamped by the service to the inclusive ``1..100``
    range.
    """

    max_iterations: Optional[int] = Field(
        default=None,
        description=(
            "Optional loop guard for the run. Omitted → the configured default "
            "(default 3); provided → clamped by the service to 1..100."
        ),
    )


class AiRunResponse(BaseModel):
    """A single ``agent_runs`` row returned by the start/read endpoints.

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_runs row.")
    campaign_id: str = Field(
        ..., description="Campaign this run curates (FK into campaigns)."
    )
    status: str = Field(
        ...,
        description=(
            "Run status: pendente / em_execucao / concluido / falhou / abortado."
        ),
    )
    thread_id: str = Field(
        ..., description="LangGraph checkpoint thread key making the run resumable."
    )
    max_iterations: int = Field(..., description="The run's loop guard.")
    iterations_used: int = Field(
        ..., description="Number of plan→execute→measure→improve passes performed."
    )
    final_verdict_success: Optional[bool] = Field(
        default=None,
        description=(
            "Overall verdict summary; null until the run reaches a terminal state."
        ),
    )
    error: Optional[str] = Field(
        default=None,
        description=(
            "Structured failure cause surfaced from agent_runs.error (the failing "
            "stage + the absent-input/cause message) when the run failed; null on "
            "runs that did not fail (Req. 2.6, 4.4)."
        ),
    )


class AiEvaluationView(BaseModel):
    """The evaluator's verdict for an iteration (from ``agent_evaluations``).

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_evaluations row.")
    report_id: Optional[int] = Field(
        default=None, description="Execution-phase report the evaluator read."
    )
    success_rate: float = Field(..., description="Execution success rate read.")
    procedural_coverage: float = Field(
        ..., description="Procedural-sufficiency metric read from the report."
    )
    verdict_success: bool = Field(
        ..., description="Whether the evaluator's verdict passed."
    )
    decision: str = Field(
        ..., description="Evaluator decision driving the loop: refinar / finalizar."
    )
    rationale: str = Field(
        ..., description="LLM reasoning for the decision (≤10,000 chars)."
    )


class AiAgentTraceView(BaseModel):
    """The Audit_Trace (Agent_Input + Agent_Processing) of a stage output.

    Carries the 1:1 ``agent_stage_traces`` row for a stage output (Req. 1.2, 1.3,
    2.1): ``agent_input`` is the data the agent received (prior-stage artifacts,
    the Selected_Campaign context, the retrieved cross-campaign experience, the
    ATT&CK/STIX attack context); ``processing`` is what the agent performed (the
    LLM prompt sent, the LLM response received, reasoning, tools invoked, repair
    attempts — plus any extra keys the agent captured). Any null/absent
    processing element is resolved to the :data:`NOT_AVAILABLE` sentinel by the
    handler so the frontend renders an explicit "not available" indicator
    (Req. 1.6).
    """

    agent_input: dict = Field(
        default_factory=dict,
        description=(
            "Agent_Input: prior-stage artifacts, Selected_Campaign context, "
            "retrieved cross-campaign experience, and the ATT&CK/STIX attack "
            "context the agent received (Req. 1.2)."
        ),
    )
    processing: dict = Field(
        default_factory=dict,
        description=(
            "Agent_Processing: the LLM prompt and response, reasoning, tools "
            "invoked, and repair attempts. Null/absent elements are the "
            "\"not available\" sentinel rather than omitted (Req. 1.3, 1.6)."
        ),
    )


class AiStageOutputView(BaseModel):
    """One ``agent_stage_outputs`` row with its 1:1 Audit_Trace (Req. 1.1, 1.4, 2.4).

    The Agent_Output artifact (``payload``) produced for a Stage and Iteration,
    stamped with the AI-generated ``origin``, paired with the ``trace`` carrying
    that agent's Agent_Input + Agent_Processing. Presented as the three distinct
    parts of an Agent's contribution (input / processing / output) grouped by
    Stage within an Iteration (Req. 1.1, 1.5).
    """

    id: int = Field(..., description="Primary key of the agent_stage_outputs row.")
    stage: str = Field(
        ...,
        description=(
            "Semantic AgentStage value the output belongs to (dag / "
            "operationalization / environment / execution / evaluation)."
        ),
    )
    origin: str = Field(
        ...,
        description="Origin of the artifact; always ai_generated on the AI path.",
    )
    payload: dict = Field(
        default_factory=dict,
        description="The serialized Agent_Output artifact produced for the stage.",
    )
    trace: AiAgentTraceView = Field(
        ...,
        description="The Agent_Input + Agent_Processing Audit_Trace for the output.",
    )


class AiIterationView(BaseModel):
    """One iteration of a run with its stage outputs and evaluator verdict.

    Carries the ``agent_iterations`` row plus the per-stage ``agent_stage_outputs``
    rows (each with its 1:1 Audit_Trace, ordered by stage — Req. 1.1, 1.5, 2.4)
    and the optional ``agent_evaluations`` verdict recorded for that iteration
    (0..1 per iteration).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_iterations row.")
    index: int = Field(..., description="0-based iteration number within the run.")
    refined_stage: Optional[str] = Field(
        default=None,
        description=(
            "Stage the evaluator refined into this iteration; null on the first "
            "pass."
        ),
    )
    notes: Optional[str] = Field(default=None, description="Free-form iteration notes.")
    created_at: dt.datetime = Field(
        ..., description="Naive-UTC creation timestamp of the iteration."
    )
    stage_outputs: list[AiStageOutputView] = Field(
        default_factory=list,
        description=(
            "The iteration's per-stage Agent_Output artifacts, each with its 1:1 "
            "Audit_Trace, ordered by stage (Req. 1.1, 1.4, 2.4)."
        ),
    )
    failing_agent: Optional[str] = Field(
        default=None,
        description=(
            "Identity of the failing Stage/Agent captured on a failed Run; null "
            "unless this iteration carries the failure anchor (Req. 2.6)."
        ),
    )
    evaluation: Optional[AiEvaluationView] = Field(
        default=None,
        description="The evaluator verdict recorded for this iteration, if any.",
    )


class AiRunDetailResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/ai/runs/{id}`` payload (Req. 1.4, 2.4, 13.2).

    The run summary (with its ``error`` cause when the run failed) plus its
    iterations (oldest first), each carrying its per-stage Agent_Output artifacts
    with their 1:1 Audit_Trace and its evaluator verdict.
    """

    run: AiRunResponse = Field(..., description="The agent_runs summary row.")
    iterations: list[AiIterationView] = Field(
        default_factory=list,
        description=(
            "The run's iterations with their stage outputs + Audit_Trace and "
            "evaluator verdicts, oldest first."
        ),
    )


class GroundTruthStageComparison(BaseModel):
    """The AI-output-vs-human-curation comparison for one Stage (design §H).

    Compares the serialized sets the AI Run produced against the equivalent sets
    read from the campaign's human curation on disk — read-only and on demand.
    The two sets drive the symmetric difference so the Researcher sees what the
    AI matched, missed, and added relative to ground truth.
    """

    stage: str = Field(
        ..., description="English AgentStage value this comparison belongs to."
    )
    ai_items: list[str] = Field(
        default_factory=list,
        description="The identifiers the AI Run produced for this stage (sorted).",
    )
    curated_items: list[str] = Field(
        default_factory=list,
        description="The identifiers the human curation carries for this stage (sorted).",
    )
    matched: list[str] = Field(
        default_factory=list,
        description="Identifiers present in both the AI output and the curation.",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="Curated identifiers the AI output did not produce.",
    )
    extra: list[str] = Field(
        default_factory=list,
        description="AI-produced identifiers absent from the curation.",
    )


class GroundTruthComparisonResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/ai/runs/{id}/ground-truth`` payload (Req. 5.3).

    The on-demand Ground_Truth_Comparison of a completed Run's AI output against
    the human curation of a Curated_Campaign. Curation is read only as the
    comparison baseline and is NEVER invoked as part of running or completing a
    Run (Req. 5.4).
    """

    campaign_id: str = Field(
        ..., description="The Curated_Campaign slug compared (server-side curated)."
    )
    run_id: int = Field(..., description="The completed Run compared.")
    stages: list[GroundTruthStageComparison] = Field(
        default_factory=list,
        description="Per-stage AI-output-vs-curation comparisons.",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign}/ai/run",
    response_model=AiRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start an AI curation run (create row + invoke graph within 2s)",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "The run could not be started: a required dependency "
                "(checkpointer / pgvector) was unavailable, the graph invocation "
                "failed, or persistence failed. No partial agent_runs row was "
                "persisted (Req. 13.2)."
            )
        },
    },
)
def start_ai_run(
    body: AiRunStartRequest,
    campaign: str = Path(..., description="Campaign to curate with the AI agents"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Start an AI curation run for a campaign (Req. 13.1, 13.2).

    Delegates synchronously to :meth:`AgentRunService.start`, which creates the
    ``agent_runs`` row and invokes the compiled StateGraph. Per Req. 13.1 the row
    must be created and the graph invoked within 2 seconds of the start request;
    the handler adds no extra work on the hot path — it just hands off to the
    service, which owns the invocation — so the 2s SLA is bounded by the
    service's own synchronous invocation.

    On success the created row is committed and returned. On ANY failure
    (``DependencyUnavailableError`` / ``RunFailedError`` / ``PersistenceError`` or
    any other error) the session is rolled back so NO partial ``agent_runs`` row
    is persisted, and an error response indicating the run could not be started is
    returned (Req. 13.2). Because the service only ``flush``es (never ``commit``s),
    the rollback discards every row the failed attempt wrote.

    The campaign is validated against the persisted STIX campaigns
    (:func:`~app.api.campaigns._known_campaign_or_404` over ``stix_campaigns``)
    before the run starts: any of the 52 persisted campaigns — curated or not —
    is accepted, while a truly-absent id is refused with a 404 (Req. 2.2, 2.3,
    2.4, 4.4).

    After the guard and BEFORE the run starts, the campaign is materialized into
    the ``campaigns`` table via
    :meth:`~app.services.campaign_materializer.CampaignMaterializer.materialize`
    (an idempotent upsert from the STIX). This guarantees ``campaigns.id`` exists
    for ANY of the 52 campaigns, so every FK-bearing write the run performs
    (``agent_runs.campaign_id``, ``abilities.campaign_id``, ``dags.campaign_id``)
    is satisfied — the AI_Graph's ``_materialize_ai_abilities`` can then rely on
    the parent row already existing without violating ``abilities.campaign_id``
    (Req. 2.4, 2.5, 2.6). The materialization is a no-op that preserves
    provenance for an already-ingested curated campaign. Any failure here (as
    with the run start itself) rolls the session back so NO partial row survives
    (Req. 13.2).
    """
    from app.api.campaigns import CURATED_VERSION, _known_campaign_or_404
    from app.services.campaign_materializer import CampaignMaterializer

    _known_campaign_or_404(campaign, db, CURATED_VERSION)

    try:
        # Materialize the campaigns row from the STIX BEFORE any FK-bearing write
        # (agent_runs/abilities/dags -> campaigns.id), so the run — including the
        # graph's _materialize_ai_abilities — never violates a FK for a
        # non-curated campaign (Req. 2.4, 2.5, 2.6). Idempotent no-op for an
        # already-materialized (curated) campaign.
        CampaignMaterializer(db).materialize(campaign, CURATED_VERSION)
        run = AgentRunService(db).start(
            campaign, max_iterations=body.max_iterations
        )
    except DependencyUnavailableError as exc:
        # A required dependency (checkpointer / pgvector) was unavailable: the
        # service created no row, but roll back defensively so nothing partial
        # survives, then report the run could not be started (Req. 13.2).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "dependency_unavailable",
                "message": (
                    "The AI run could not be started: the dependency "
                    f"{exc.dependency!r} is unavailable."
                ),
                "dependency": exc.dependency,
            },
        ) from exc
    except RunFailedError as exc:
        # The graph invocation failed. The service flushed a FAILED row; roll it
        # back so NO partial agent_runs row is persisted (Req. 13.2).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "run_failed",
                "message": (
                    "The AI run could not be started: the graph invocation "
                    f"failed at node {exc.node!r}."
                ),
                "node": exc.node,
            },
        ) from exc
    except PersistenceError as exc:
        # Per-iteration persistence failed; roll back so nothing partial survives.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "persistence_failed",
                "message": (
                    "The AI run could not be started: run persistence "
                    "failed."
                ),
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 - leave no partial row on any failure
        # Any other error: roll back so no partial agent_runs row is persisted
        # (Req. 13.2), then surface a generic "could not start" error response.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "run_not_started",
                "message": (
                    "The AI run could not be started due to an unexpected "
                    "error."
                ),
            },
        ) from exc

    # Commit the created row (and any bookkeeping) so the started run is durable;
    # the service only flushed it within the request-scoped session.
    db.commit()
    return AiRunResponse.model_validate(run)


@router.get(
    "/campaigns/{campaign}/ai/runs/{id}",
    response_model=AiRunDetailResponse,
    summary="Read an AI run with its iterations and the evaluator verdict",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No AI run with that id exists for the campaign."
        },
    },
)
def get_ai_run(
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the agent_runs row to read"),
    db: Session = Depends(get_db),
) -> AiRunDetailResponse:
    """Read a run with its iterations, stage outputs + trace, and verdict.

    Loads the ``agent_runs`` row by ``id`` (scoped to ``campaign``), its
    ``agent_iterations`` rows (oldest first), and for each iteration its
    ``agent_stage_outputs`` rows ordered by stage — each paired with its 1:1
    ``agent_stage_traces`` row (Agent_Input + Agent_Processing) and the
    ``agent_evaluations`` verdict recorded for the iteration (Req. 1.1, 1.4,
    2.4, 13.2). Null/absent Agent_Processing elements are resolved to the
    explicit "not available" sentinel the frontend renders (Req. 1.6). When the
    run failed, ``AiRunResponse.error`` surfaces ``agent_runs.error`` and the
    failing Stage is reported on the iteration's ``failing_agent`` (Req. 2.6,
    4.4). An unknown id — or an id that belongs to a different campaign — is a
    404.
    """
    from app.models.domain import (
        AgentEvaluation,
        AgentIteration,
        AgentRun,
        AgentStageOutput,
        AgentStageTrace,
    )
    from app.models.enums import AgentRunStatus

    run = db.get(AgentRun, id)
    if run is None or run.campaign_id != campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Execução de IA desconhecida: id={id!r} para a campanha "
                f"{campaign!r}."
            ),
        )

    run_failed = run.status is AgentRunStatus.FAILED

    iteration_rows = (
        db.execute(
            select(AgentIteration)
            .where(AgentIteration.run_id == run.id)
            .order_by(AgentIteration.index.asc(), AgentIteration.id.asc())
        )
        .scalars()
        .all()
    )

    iterations: list[AiIterationView] = []
    for iteration in iteration_rows:
        # Load the iteration's stage outputs ordered by stage, each paired with
        # its 1:1 agent_stage_traces row (left outer join so a stage output with
        # no trace still surfaces — its trace renders fully "not available").
        stage_output_rows = db.execute(
            select(AgentStageOutput, AgentStageTrace)
            .outerjoin(
                AgentStageTrace,
                AgentStageTrace.stage_output_id == AgentStageOutput.id,
            )
            .where(AgentStageOutput.iteration_id == iteration.id)
            .order_by(AgentStageOutput.stage.asc(), AgentStageOutput.id.asc())
        ).all()

        stage_outputs: list[AiStageOutputView] = []
        failing_agent: Optional[str] = None
        for stage_output, trace in stage_output_rows:
            stage_value = getattr(stage_output.stage, "value", stage_output.stage)
            origin_value = getattr(stage_output.origin, "value", stage_output.origin)
            agent_input = (
                trace.agent_input
                if trace is not None and isinstance(trace.agent_input, dict)
                else {}
            )
            processing = trace.processing if trace is not None else None
            stage_outputs.append(
                AiStageOutputView(
                    id=stage_output.id,
                    stage=stage_value,
                    origin=origin_value,
                    payload=stage_output.payload
                    if isinstance(stage_output.payload, dict)
                    else {},
                    trace=AiAgentTraceView(
                        agent_input=agent_input,
                        processing=_resolve_processing(processing),
                    ),
                )
            )
            # On a failed run the failing stage is anchored on a MINIMAL stage
            # output (empty payload sentinel — no valid artifact was produced);
            # surface that stage as the failing Agent so the failure is auditable
            # (Req. 2.6). The first such anchor in the iteration identifies it.
            if (
                run_failed
                and failing_agent is None
                and not (isinstance(stage_output.payload, dict) and stage_output.payload)
            ):
                failing_agent = stage_value

        # The evaluator records 0..1 verdict per iteration; take the latest row
        # for the iteration when present.
        evaluation_row = (
            db.execute(
                select(AgentEvaluation)
                .where(AgentEvaluation.iteration_id == iteration.id)
                .order_by(AgentEvaluation.id.desc())
            )
            .scalars()
            .first()
        )
        view = AiIterationView.model_validate(iteration)
        view.stage_outputs = stage_outputs
        view.failing_agent = failing_agent
        if evaluation_row is not None:
            view.evaluation = AiEvaluationView.model_validate(evaluation_row)
        iterations.append(view)

    return AiRunDetailResponse(
        run=AiRunResponse.model_validate(run),
        iterations=iterations,
    )


# ---------------------------------------------------------------------------
# Ground_Truth_Comparison (curated-only, on-demand) — task 9.6, design §H
# ---------------------------------------------------------------------------
#
# A read-only, on-demand comparison of a completed Run's AI output against the
# human curation of a Curated_Campaign. Curated-ness is decided SERVER-SIDE from
# the existing curated set (``CaseService.list_cases`` / ``SLUG_TO_STIX``), so a
# non-curated campaign gets a clear "not a curated campaign" error. Curation is
# NEVER invoked as part of running/completing a Run (Req. 5.4) — this endpoint
# only *reads* the curated files on disk to build the comparison baseline.


def _technique_ids_from_dag_payload(payload: Any) -> set[str]:
    """Collect the technique ids an AI DAG stage output produced.

    The DAG stage output ``payload`` is a serialized ``ProducedDag`` whose
    ``content`` mirrors the curation format
    ``{"structural_nodes": [...], "arestas": [...]}``; each structural node
    carries an optional ``technique_id``. Returns the non-empty technique ids as
    a set (deduplicated, order-independent) so the comparison is a set diff.
    """
    if not isinstance(payload, dict):
        return set()
    content = payload.get("content")
    content = content if isinstance(content, dict) else payload
    nodes = content.get("structural_nodes")
    ids: set[str] = set()
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict):
                tid = node.get("technique_id")
                if isinstance(tid, str) and tid.strip():
                    ids.add(tid.strip())
    return ids


def _ability_ids_from_op_payload(payload: Any) -> set[str]:
    """Collect the ability ids an AI operationalization stage output produced.

    The operationalization ``payload`` is a serialized
    ``ProducedOperationalization`` whose ``abilities`` field is a serialized
    ``Stage2Output`` carrying an ``abilities`` list; each entry carries an
    ``ability_id``. Returns the non-empty ability ids as a set.
    """
    if not isinstance(payload, dict):
        return set()
    stage2 = payload.get("abilities")
    stage2 = stage2 if isinstance(stage2, dict) else {}
    ability_views = stage2.get("abilities")
    ids: set[str] = set()
    if isinstance(ability_views, list):
        for ability in ability_views:
            if isinstance(ability, dict):
                aid = ability.get("ability_id")
                if isinstance(aid, str) and aid.strip():
                    ids.add(aid.strip())
    return ids


def _build_stage_comparison(stage: str, ai_items: set[str], curated_items: set[str]) -> GroundTruthStageComparison:
    """Assemble one stage's symmetric AI-vs-curation comparison (sorted, stable)."""
    return GroundTruthStageComparison(
        stage=stage,
        ai_items=sorted(ai_items),
        curated_items=sorted(curated_items),
        matched=sorted(ai_items & curated_items),
        missing=sorted(curated_items - ai_items),
        extra=sorted(ai_items - curated_items),
    )


@router.get(
    "/campaigns/{campaign}/ai/runs/{id}/ground-truth",
    response_model=GroundTruthComparisonResponse,
    summary="Compare a completed AI Run against the campaign's human curation",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": (
                "No AI run with that id exists for the campaign, or the campaign "
                "is not one of the eight Curated_Campaigns so no human-curation "
                "baseline exists to compare against (Req. 5.2)."
            )
        },
    },
)
def compare_ai_run_with_curation(
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the completed agent_runs row to compare"),
    db: Session = Depends(get_db),
) -> GroundTruthComparisonResponse:
    """Compute the on-demand Ground_Truth_Comparison for a curated campaign (Req. 5.1–5.4).

    Curated-ness is determined SERVER-SIDE from the existing curated set
    (:meth:`CaseService.list_cases`, keyed identically to
    :data:`~app.services.stix.stix_service.SLUG_TO_STIX`): the comparison is only
    offered on the eight Curated_Campaigns. For any other campaign a clear
    "not a curated campaign" 404 is returned (Req. 5.2) rather than a comparison.

    For a curated campaign the handler reads the Run's AI stage outputs (the DAG
    technique ids and the operationalization ability ids it produced) and
    compares them — on demand — against the campaign's human curation read from
    disk via :class:`~app.services.campaign_service.CaseService` (the curated DAG
    techniques and the curated ability ids). Curation is used ONLY as the
    comparison baseline and is never invoked as part of running or completing a
    Run (Req. 5.4). An unknown run id — or one belonging to a different campaign
    — is a 404.
    """
    from app.models.domain import (
        AgentIteration,
        AgentRun,
        AgentStageOutput,
    )
    from app.models.domain import Ability as AbilityModel
    from app.models.enums import AgentStage
    from app.services.campaign_service import CaseService
    from app.services.dag.dag_service import DagService

    run = db.get(AgentRun, id)
    if run is None or run.campaign_id != campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "run_not_found",
                "message": (
                    f"No AI run id={id!r} exists for campaign {campaign!r}."
                ),
            },
        )

    # Server-side curated-ness: the campaign slug must be one of the eight
    # curated cases, else there is no human-curation baseline to compare against
    # and the action must not be offered (Req. 5.2).
    case_service = CaseService()
    curated_cases = case_service.list_cases()
    if campaign not in curated_cases:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "not_a_curated_campaign",
                "message": (
                    f"Campaign {campaign!r} is not a curated campaign, so there "
                    "is no human-curation ground truth to compare against."
                ),
                "campaign": campaign,
            },
        )

    # ---- AI side: the technique ids / ability ids the Run produced ----------
    stage_output_rows = (
        db.execute(
            select(AgentStageOutput)
            .join(AgentIteration, AgentStageOutput.iteration_id == AgentIteration.id)
            .where(AgentIteration.run_id == run.id)
        )
        .scalars()
        .all()
    )

    ai_technique_ids: set[str] = set()
    ai_ability_ids: set[str] = set()
    for row in stage_output_rows:
        stage_value = getattr(row.stage, "value", row.stage)
        if stage_value == AgentStage.DAG.value:
            ai_technique_ids |= _technique_ids_from_dag_payload(row.payload)
        elif stage_value == AgentStage.OPERATIONALIZATION.value:
            ai_ability_ids |= _ability_ids_from_op_payload(row.payload)

    # ---- Curation side: the baseline read from the database, on demand ------
    # Database-only sourcing (Req. 1.1, 4.1): the curated DAG technique ids come
    # from the persisted ``dags`` row (via DagService) and the curated ability
    # ids from the ``abilities`` rows keyed by ``campaign_id`` — both persisted by
    # the curated ingest (sub-step 2.1). The runtime no longer reads
    # ``packages/cli/data`` for the comparison baseline (Req. 4.2).
    curated_technique_ids: set[str] = set()
    curated_ability_ids: set[str] = set()

    curated_dag = DagService(session=db).resolve_dag_data_from_db(campaign)
    if curated_dag is not None:
        for node in curated_dag.structural_nodes:
            tid = (node.technique_id or "").strip()
            if tid:
                curated_technique_ids.add(tid)

    curated_ability_rows = (
        db.execute(
            select(AbilityModel.ability_id).where(
                AbilityModel.campaign_id == campaign
            )
        )
        .scalars()
        .all()
    )
    for ability_id in curated_ability_rows:
        aid = (ability_id or "").strip()
        if aid:
            curated_ability_ids.add(aid)

    stages = [
        _build_stage_comparison(
            AgentStage.DAG.value, ai_technique_ids, curated_technique_ids
        ),
        _build_stage_comparison(
            AgentStage.OPERATIONALIZATION.value,
            ai_ability_ids,
            curated_ability_ids,
        ),
    ]

    return GroundTruthComparisonResponse(
        campaign_id=campaign,
        run_id=run.id,
        stages=stages,
    )
