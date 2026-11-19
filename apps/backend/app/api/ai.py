"""REST endpoints for AI curation runs (task 16.1).

Thin endpoints over :class:`~app.services.ai.run_service.AgentRunService`:

* ``POST /api/campaigns/{campaign}/ai/run`` — start an AI run (202). The service
  creates and commits the ``agent_runs`` row and dispatches the ``initial`` phase
  job in the background; the run then pauses for approval on each phase.
  409 ``llm_blocked`` (preflight) and 409 ``run_already_active`` answer before a
  row exists; a failure before the dispatch answers 503 and rolls the request
  session back (Req. 13.2).

* ``GET .../ai/runs/active`` — the active run of the campaign (404 ``no_active_run``).

* ``POST .../ai/runs/{id}/phases/{phase}/approve`` and
  ``.../phases/flow/regenerate`` — 202, the next round runs in the background.
  The frozen model profile is preflighted first (409 ``llm_blocked`` /
  ``profile_unavailable``). ``.../phases/flow/select`` and ``.../abort`` answer 200.
  ``RunTransitionError`` codes map to 404 (``run_not_found``,
  ``flow_proposal_not_found``), 409 (``wrong_phase``, ``phase_running``,
  ``run_not_awaiting``, ``flow_proposal_limit_reached``) and 422 (``invalid_phase``), as ``detail.reason``.

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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.llm import selected_profile
from app.db.session import get_db
from app.services.ai.llm_failure import classify_llm_failure
from app.services.ai.preflight import run_preflight
from app.services.ai.run_service import (
    AgentRunService,
    DependencyUnavailableError,
    PersistenceError,
    RunAlreadyActiveError,
    RunFailedError,
    RunTransitionError,
    flow_proposal_max,
)

router = APIRouter(prefix="/api", tags=["ai"])


# The explicit "not available" sentinel the read endpoint emits for any
# Agent_Processing element that is genuinely absent (recorded as ``None`` /
# missing by the trace sink — a phase that ran without an LLM call, a curation
# fallback that captured nothing). The serialization layer maps null/absent
# processing elements to this marker rather than omitting the phase or agent, so
# the frontend renders an explicit indicator instead of a blank field
# (Req. 1.6; design §A/§C "Not available" indicator).
NOT_AVAILABLE = "not_available"

# The Agent_Processing elements an ``agent_phase_traces.processing`` payload
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


def _resolve_processing(processing: dict | None) -> dict:
    """Map null/absent Agent_Processing elements to the "not available" sentinel.

    Every known processing key (:data:`_PROCESSING_KEYS`) is present in the
    result: a ``None`` or missing element resolves to :data:`NOT_AVAILABLE` so the
    frontend renders an explicit "not available" indicator rather than omitting
    the Phase or Agent (Req. 1.6). Any extra keys the agent captured (e.g. the
    Environment agent's ``fixed_infrastructure`` / ``agent_decided`` / ``gates`` /
    ``subphases``) are preserved as-is, with a ``None`` value likewise resolved to
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

    max_iterations: int | None = Field(
        default=None,
        description=(
            "Optional loop guard for the run. Omitted → the configured default "
            "(default 3); provided → clamped by the service to 1..100."
        ),
    )


class AiFlowSelectRequest(BaseModel):
    """Body of ``POST .../phases/flow/select``."""

    proposal_id: int = Field(..., description="``proposal_id`` of the history entry to restore.")


class AiTechniqueCoverage(BaseModel):
    """Emulation coverage of one campaign technique (Req. 22.3-22.5)."""

    technique_id: str = Field(..., description="ATT&CK technique id.")
    status: str = Field(
        ...,
        description=(
            "Planned emulability: planned_emulable / not_emulable. Once the run "
            "executed, the post-execution status is in ``achieved_status``."
        ),
    )
    reason_code: str | None = Field(
        default=None,
        description=(
            "Emulability reason code of a not_emulable technique (no_command, "
            "port_conflict, image_unavailable, os_unsupported, requires_vm); null "
            "when the technique is planned_emulable."
        ),
    )
    achieved_status: str | None = Field(
        default=None,
        description=(
            "Post-execution status: executed_success / executed_failure / "
            "not_executed; null until the execution phase finished."
        ),
    )


class AiRunCoverage(BaseModel):
    """Planned and achieved emulation coverage of a run (``agent_runs.coverage``).

    Built from the stored JSON, which accumulates the planned keys (after the
    operationalization/environment pause) and the achieved keys (after execution).
    """

    planned_coverage: float | None = Field(
        default=None, description="Percent of campaign techniques planned to be emulable."
    )
    achieved_coverage: float | None = Field(
        default=None,
        description="Percent of campaign techniques executed with success; null before execution.",
    )
    techniques: list[AiTechniqueCoverage] = Field(
        default_factory=list, description="Per-technique status and reason code."
    )

    @model_validator(mode="before")
    @classmethod
    def _from_stored(cls, value: Any) -> Any:
        """Shape the stored ``agent_runs.coverage`` JSON into the response fields."""
        if not isinstance(value, dict) or "achieved_techniques" not in value:
            # Already in response shape, or planned data only (techniques kept as is).
            return value
        achieved = {
            item.get("technique_id"): item.get("status")
            for item in value["achieved_techniques"] or []
            if isinstance(item, dict)
        }
        techniques = [
            {**item, "achieved_status": achieved.get(item.get("technique_id"))}
            for item in value.get("techniques") or []
            if isinstance(item, dict)
        ]
        return {
            "planned_coverage": value.get("planned_coverage"),
            "achieved_coverage": value.get("achieved_coverage"),
            "techniques": techniques,
        }


class AiCommandParameterView(BaseModel):
    """One parameter of a proposed command with its value and origin."""

    name: str = Field(..., description="Placeholder name in the command template.")
    value: str | None = Field(default=None, description="Final value; null when unresolved.")
    source: str = Field(
        ..., description="agent_inferred / atomic_default / unresolved (Parameter_Source)."
    )


class AiCommandProposalView(BaseModel):
    """A command proposed for a technique (one ability executor)."""

    command_ref: int | None = Field(
        default=None, description="Id of the technique_commands row of the template, if known."
    )
    template: str = Field(..., description="Command template with placeholders.")
    resolved_command: str = Field(..., description="Command with the parameters filled in.")
    parameters: list[AiCommandParameterView] = Field(default_factory=list)


class AiEnvironmentHostView(BaseModel):
    """A host a technique needs in the lab."""

    role: str = Field(..., description="Host kind (attacker/target) or its label.")
    os: str = Field(..., description="Operating system: linux or windows.")
    image_hint: str | None = Field(default=None, description="Best-effort image, if resolved.")


class AiEnvironmentRequirementsView(BaseModel):
    """Environment needs of one technique."""

    hosts: list[AiEnvironmentHostView] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)


class AiOperationalizedTechniqueView(BaseModel):
    """One technique of the operationalization review."""

    technique_id: str = Field(..., description="ATT&CK technique id.")
    technique_name: str | None = Field(
        default=None, description="Readable ATT&CK technique name, when known."
    )
    commands: list[AiCommandProposalView] = Field(default_factory=list)
    environment_requirements: AiEnvironmentRequirementsView | None = None
    emulability_status: str | None = Field(
        default=None, description="planned_emulable / not_emulable."
    )
    emulability_reason_code: str | None = Field(
        default=None, description="Reason code of a not_emulable technique."
    )


class AiOperationalizationReviewView(BaseModel):
    """Command proposals and environment needs shown at the operationalization approval."""

    techniques: list[AiOperationalizedTechniqueView] = Field(default_factory=list)
    planned_coverage: float | None = Field(
        default=None, description="Percent of techniques planned to be emulable."
    )


class AiRunResponse(BaseModel):
    """A single ``agent_runs`` row returned by the start/read endpoints.

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_runs row.")
    campaign_id: str = Field(..., description="Campaign this run curates (FK into campaigns).")
    status: str = Field(
        ...,
        description=(
            "Run status: pending / running / awaiting_approval / completed / failed / aborted."
        ),
    )
    thread_id: str = Field(
        ..., description="LangGraph checkpoint thread key making the run resumable."
    )
    max_iterations: int = Field(..., description="The run's loop guard.")
    iterations_used: int = Field(
        ..., description="Number of plan→execute→measure→improve passes performed."
    )
    final_verdict_success: bool | None = Field(
        default=None,
        description=("Overall verdict summary; null until the run reaches a terminal state."),
    )
    error: str | None = Field(
        default=None,
        description=(
            "Structured failure cause surfaced from agent_runs.error (the failing "
            "phase + the absent-input/cause message) when the run failed; null on "
            "runs that did not fail (Req. 2.6, 4.4)."
        ),
    )
    coverage: AiRunCoverage | None = Field(
        default=None,
        description=(
            "Planned/achieved emulation coverage with the per-technique status and "
            "reason code (Req. 22.3-22.5); null until the planned coverage exists."
        ),
    )
    pending_phase: str | None = Field(
        default=None,
        description="Phase awaiting the researcher's approval; set only while awaiting_approval.",
    )
    active_phase: str | None = Field(
        default=None,
        description="Phase currently executing in the background; set only while running.",
    )
    notice_code: str | None = Field(
        default=None,
        description="Non-fatal notice code (no_distinct_flow / regeneration_failed).",
    )
    failure: dict[str, Any] | None = Field(
        default=None,
        description="Structured failure (phase, code, cause_code) of a failed run.",
    )
    approvals: list[Any] = Field(
        default_factory=list, description="Approval log of the phases approved so far."
    )
    flow_proposals: list[Any] = Field(
        default_factory=list, description="Attack flow proposal history (one marked current)."
    )
    flow_proposal_max: int = Field(
        default_factory=flow_proposal_max,
        description="Maximum number of attack flow proposals per run (same for every run).",
    )
    negotiation_rounds_used: int = Field(
        default=0, description="Operationalization/environment negotiation rounds consumed."
    )
    operationalization_review: AiOperationalizationReviewView | None = Field(
        default=None,
        description=(
            "Command proposals, environment requirements and planned coverage of the "
            "operationalization phase; null until that phase produced its output."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _from_row(cls, value: Any) -> Any:
        """Derive ``pending_phase`` / ``active_phase`` from an ORM row's status and phase."""
        if isinstance(value, dict) or not hasattr(value, "current_phase"):
            return value
        run_status = getattr(value.status, "value", value.status)
        phase = value.current_phase
        return {
            "id": value.id,
            "campaign_id": value.campaign_id,
            "status": run_status,
            "thread_id": value.thread_id,
            "max_iterations": value.max_iterations,
            "iterations_used": value.iterations_used,
            "final_verdict_success": value.final_verdict_success,
            "error": value.error,
            "coverage": value.coverage,
            "pending_phase": phase if run_status == "awaiting_approval" else None,
            "active_phase": phase if run_status == "running" else None,
            "notice_code": value.notice_code,
            "failure": value.failure,
            "approvals": value.approvals or [],
            "flow_proposals": value.flow_proposals or [],
            "negotiation_rounds_used": value.negotiation_rounds_used or 0,
        }


class AiEvaluationView(BaseModel):
    """The evaluator's verdict for an iteration (from ``agent_evaluations``).

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_evaluations row.")
    report_id: int | None = Field(
        default=None, description="Execution-phase report the evaluator read."
    )
    success_rate: float = Field(..., description="Execution success rate read.")
    procedural_coverage: float = Field(
        ..., description="Procedural-sufficiency metric read from the report."
    )
    verdict_success: bool = Field(..., description="Whether the evaluator's verdict passed.")
    decision: str = Field(
        ..., description="Evaluator decision driving the loop: refinar / finalizar."
    )
    rationale: str = Field(..., description="LLM reasoning for the decision (≤10,000 chars).")


class AiAgentTraceView(BaseModel):
    """The Audit_Trace (Agent_Input + Agent_Processing) of a phase output.

    Carries the 1:1 ``agent_phase_traces`` row for a phase output (Req. 1.2, 1.3,
    2.1): ``agent_input`` is the data the agent received (prior-phase artifacts,
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
            "Agent_Input: prior-phase artifacts, Selected_Campaign context, "
            "retrieved cross-campaign experience, and the ATT&CK/STIX attack "
            "context the agent received (Req. 1.2)."
        ),
    )
    processing: dict = Field(
        default_factory=dict,
        description=(
            "Agent_Processing: the LLM prompt and response, reasoning, tools "
            "invoked, and repair attempts. Null/absent elements are the "
            '"not available" sentinel rather than omitted (Req. 1.3, 1.6).'
        ),
    )


class AiPhaseOutputView(BaseModel):
    """One ``agent_phase_outputs`` row with its 1:1 Audit_Trace (Req. 1.1, 1.4, 2.4).

    The Agent_Output artifact (``payload``) produced for a Phase and Iteration,
    stamped with the AI-generated ``origin``, paired with the ``trace`` carrying
    that agent's Agent_Input + Agent_Processing. Presented as the three distinct
    parts of an Agent's contribution (input / processing / output) grouped by
    Phase within an Iteration (Req. 1.1, 1.5).
    """

    id: int = Field(..., description="Primary key of the agent_phase_outputs row.")
    phase: str = Field(
        ...,
        description=(
            "Semantic AgentPhase value the output belongs to (flow / "
            "operationalization / environment / execution / evaluation)."
        ),
    )
    origin: str = Field(
        ...,
        description="Origin of the artifact; always ai_generated on the AI path.",
    )
    payload: dict = Field(
        default_factory=dict,
        description="The serialized Agent_Output artifact produced for the phase.",
    )
    trace: AiAgentTraceView = Field(
        ...,
        description="The Agent_Input + Agent_Processing Audit_Trace for the output.",
    )


class AiIterationView(BaseModel):
    """One iteration of a run with its phase outputs and evaluator verdict.

    Carries the ``agent_iterations`` row plus the per-phase ``agent_phase_outputs``
    rows (each with its 1:1 Audit_Trace, ordered by phase — Req. 1.1, 1.5, 2.4)
    and the optional ``agent_evaluations`` verdict recorded for that iteration
    (0..1 per iteration).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_iterations row.")
    index: int = Field(..., description="0-based iteration number within the run.")
    refined_phase: str | None = Field(
        default=None,
        description=("Phase the evaluator refined into this iteration; null on the first pass."),
    )
    notes: str | None = Field(default=None, description="Free-form iteration notes.")
    created_at: dt.datetime = Field(
        ..., description="Naive-UTC creation timestamp of the iteration."
    )
    phase_outputs: list[AiPhaseOutputView] = Field(
        default_factory=list,
        description=(
            "The iteration's per-phase Agent_Output artifacts, each with its 1:1 "
            "Audit_Trace, ordered by phase (Req. 1.1, 1.4, 2.4)."
        ),
    )
    failing_agent: str | None = Field(
        default=None,
        description=(
            "Identity of the failing Phase/Agent captured on a failed Run; null "
            "unless this iteration carries the failure anchor (Req. 2.6)."
        ),
    )
    evaluation: AiEvaluationView | None = Field(
        default=None,
        description="The evaluator verdict recorded for this iteration, if any.",
    )


class AiRunDetailResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/ai/runs/{id}`` payload (Req. 1.4, 2.4, 13.2).

    The run summary (with its ``error`` cause when the run failed) plus its
    iterations (oldest first), each carrying its per-phase Agent_Output artifacts
    with their 1:1 Audit_Trace and its evaluator verdict.
    """

    run: AiRunResponse = Field(..., description="The agent_runs summary row.")
    iterations: list[AiIterationView] = Field(
        default_factory=list,
        description=(
            "The run's iterations with their phase outputs + Audit_Trace and "
            "evaluator verdicts, oldest first."
        ),
    )


class GroundTruthPhaseComparison(BaseModel):
    """The AI-output-vs-human-curation comparison for one Phase (design §H).

    Compares the serialized sets the AI Run produced against the equivalent sets
    read from the campaign's human curation in the database — read-only and on demand.
    The two sets drive the symmetric difference so the Researcher sees what the
    AI matched, missed, and added relative to ground truth.
    """

    phase: str = Field(..., description="English AgentPhase value this comparison belongs to.")
    ai_items: list[str] = Field(
        default_factory=list,
        description="The identifiers the AI Run produced for this phase (sorted).",
    )
    curated_items: list[str] = Field(
        default_factory=list,
        description="The identifiers the human curation carries for this phase (sorted).",
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
    phases: list[GroundTruthPhaseComparison] = Field(
        default_factory=list,
        description="Per-phase AI-output-vs-curation comparisons.",
    )


# ---------------------------------------------------------------------------
# Operationalization review (read from the stored phase output payload)
# ---------------------------------------------------------------------------


def _review_environment(requirement: Any) -> AiEnvironmentRequirementsView | None:
    """Map a stored ``TechniqueEnvironmentRequirements`` dict to the review shape."""
    if not isinstance(requirement, dict):
        return None
    target_os = requirement.get("target_os") or "linux"
    hosts = [
        AiEnvironmentHostView(
            role=host.get("host_type") or host.get("label") or "target",
            os=target_os,
            image_hint=requirement.get("image_hint"),
        )
        for host in requirement.get("hosts") or []
        if isinstance(host, dict)
    ]
    return AiEnvironmentRequirementsView(
        hosts=hosts,
        ports=[p for p in requirement.get("ports") or [] if isinstance(p, int)],
        services=[str(s) for s in requirement.get("services") or []],
        files=[str(f) for f in requirement.get("files") or []],
    )


def _build_operationalization_review(
    payload: dict[str, Any], stored_coverage: Any, campaign_techniques: list[Any] | None = None
) -> AiOperationalizationReviewView:
    """Reshape a stored ``ProducedOperationalization`` payload into the review view.

    Every technique of the campaign (``campaign_techniques``, when given) is shown, even
    one with no Ability, no negotiation entry and no Environment_Requirements at all --
    it is listed as ``no_command``/``not_emulable`` so the Researcher sees what the agent
    still cannot operationalize. Techniques follow the campaign order when given, else the
    ability order, then any technique only known to the negotiation or the environment
    requirements. A technique without a command, or without an accepted negotiation, is
    ``not_emulable`` (same rules as :func:`app.services.ai.coverage.derive_planned_coverage`).
    """
    from app.services.ai.coverage import (
        REASON_NO_COMMAND,
        STATUS_NOT_EMULABLE,
        STATUS_PLANNED_EMULABLE,
    )

    abilities = (payload.get("abilities") or {}).get("abilities") or []
    negotiation = (payload.get("negotiation") or {}).get("techniques") or {}
    requirements = payload.get("environment_requirements") or {}

    commands_by_technique: dict[str, list[AiCommandProposalView]] = {}
    names_by_technique: dict[str, str] = {}
    for ability in abilities:
        technique_id = ability.get("technique_id") if isinstance(ability, dict) else None
        if not technique_id:
            continue
        technique_name = ability.get("technique_name") if isinstance(ability, dict) else None
        if technique_name and technique_id not in names_by_technique:
            names_by_technique[technique_id] = technique_name
        bucket = commands_by_technique.setdefault(technique_id, [])
        for executor in ability.get("executors") or []:
            command = executor.get("command")
            if not command:
                continue
            bucket.append(
                AiCommandProposalView(
                    command_ref=executor.get("command_ref"),
                    template=executor.get("template") or command,
                    resolved_command=command,
                    parameters=[
                        AiCommandParameterView(
                            name=p.get("name"), value=p.get("value"), source=p.get("source")
                        )
                        for p in executor.get("parameters") or []
                        if isinstance(p, dict)
                    ],
                )
            )

    technique_ids: list[str] = []
    for technique in campaign_techniques or []:
        technique_id = getattr(technique, "technique_id", None)
        if not technique_id or technique_id in technique_ids:
            continue
        technique_ids.append(technique_id)
        technique_name = getattr(technique, "technique_name", None)
        if technique_name and technique_id not in names_by_technique:
            names_by_technique[technique_id] = technique_name
    for technique_id in commands_by_technique:
        if technique_id not in technique_ids:
            technique_ids.append(technique_id)
    for source in (negotiation, requirements):
        for technique_id in source:
            if technique_id not in technique_ids:
                technique_ids.append(technique_id)

    techniques: list[AiOperationalizedTechniqueView] = []
    for technique_id in technique_ids:
        commands = commands_by_technique.get(technique_id, [])
        entry = negotiation.get(technique_id)
        if entry is None or not commands:
            status_value, reason = STATUS_NOT_EMULABLE, REASON_NO_COMMAND
        elif entry.get("status") != STATUS_PLANNED_EMULABLE:
            status_value = STATUS_NOT_EMULABLE
            reason = entry.get("reason_code") or REASON_NO_COMMAND
        else:
            status_value, reason = STATUS_PLANNED_EMULABLE, None
        techniques.append(
            AiOperationalizedTechniqueView(
                technique_id=technique_id,
                technique_name=names_by_technique.get(technique_id),
                commands=commands,
                environment_requirements=_review_environment(requirements.get(technique_id)),
                emulability_status=status_value,
                emulability_reason_code=reason,
            )
        )

    planned = stored_coverage.get("planned_coverage") if isinstance(stored_coverage, dict) else None
    if planned is None and techniques:
        emulable = sum(1 for t in techniques if t.emulability_status == STATUS_PLANNED_EMULABLE)
        planned = round(100.0 * emulable / len(techniques), 1)
    return AiOperationalizationReviewView(techniques=techniques, planned_coverage=planned)


def _campaign_techniques_or_none(campaign_id: str, db: Session) -> list[Any] | None:
    """The full technique list of the campaign (``stix_campaign_techniques``), or ``None``.

    Best effort: an unpersisted base or an unknown campaign yields ``None`` rather than
    failing the run read, so the review still shows the operationalized techniques alone.
    """
    from app.api.campaigns import CURATED_VERSION
    from app.services.stix.stix_package_read_service import (
        CampaignNotFoundError,
        StixPackageNotPersistedError,
        StixPackageReadService,
    )

    try:
        overview = StixPackageReadService(db).campaign_overview(CURATED_VERSION, campaign_id)
    except (CampaignNotFoundError, StixPackageNotPersistedError):
        return None
    return sorted(overview.techniques, key=lambda t: t.technique_id)


def _build_run_response(run: Any, db: Session) -> AiRunResponse:
    """Build the run response, adding the operationalization review from the database.

    The review comes from the payload of the newest ``operationalization`` phase
    output of the run (latest iteration first); ``None`` when none exists yet. Every
    technique of the campaign is listed, even one with no Ability at all (Req. 20.2,
    20.3). The stored payload format is not changed, only reshaped.
    """
    from app.models.domain import AgentIteration, AgentPhaseOutput
    from app.models.enums import AgentPhase

    response = AiRunResponse.model_validate(run)
    payload = (
        db.execute(
            select(AgentPhaseOutput.payload)
            .join(AgentIteration, AgentPhaseOutput.iteration_id == AgentIteration.id)
            .where(
                AgentIteration.run_id == run.id,
                AgentPhaseOutput.phase == AgentPhase.OPERATIONALIZATION,
            )
            .order_by(AgentIteration.index.desc(), AgentIteration.id.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if isinstance(payload, dict) and payload:
        response.operationalization_review = _build_operationalization_review(
            payload,
            run.coverage,
            campaign_techniques=_campaign_techniques_or_none(run.campaign_id, db),
        )
    return response


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign}/ai/run",
    response_model=AiRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start an AI run (creates the row and dispatches the first phase job)",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "The selected language model failed its preflight (reason "
                "'llm_blocked') or the campaign already has an active run (reason "
                "'run_already_active'); nothing was started or persisted."
            )
        },
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
    """Start an AI run for a campaign and answer 202 (Req. 13.1, 13.2).

    Delegates to :meth:`AgentRunService.start`, which creates the ``agent_runs``
    row, commits it and dispatches the ``initial`` phase job in the background
    without waiting for the graph. The response is the run, ``running`` on the
    ``orchestration`` phase; the job parks it on ``operationalization``
    (``awaiting_approval``) and the client polls for it.

    A campaign that already has an active run answers 409 ``run_already_active``.
    A failure before the job is dispatched (``DependencyUnavailableError``, a
    failed dispatch or any other error) rolls the request session back, so the
    materialized campaign row of the failed attempt is discarded, and answers 503.
    The service commits the run row itself, and the phase job writes the run
    through its own sessions, so the request session never rolls back run state.

    The campaign is validated against the persisted STIX campaigns
    (:func:`~app.api.campaigns._known_campaign_or_404` over ``stix_campaigns``)
    before the run starts: any of the 52 persisted campaigns — curated or not —
    is accepted, while a truly-absent id is refused with a 404 (Req. 2.2, 2.3,
    2.4, 4.4).

    After the campaign guard, the selected model profile is checked with a forced
    preflight; a blocked result answers 409 ``llm_blocked`` (codes and hints, no row
    created). The run then executes with that profile frozen. A ``RunFailedError``
    answers 503 ``run_failed`` with a ``cause_code`` classifying the model failure.

    After the guard and BEFORE the run starts, the campaign is materialized into
    the ``campaigns`` table via
    :meth:`~app.services.campaign_materializer.CampaignMaterializer.materialize`
    (an idempotent upsert from the STIX). This guarantees ``campaigns.id`` exists
    for ANY of the 52 campaigns, so every FK-bearing write the run performs
    (``agent_runs.campaign_id``, ``abilities.campaign_id``, ``attack_flows.campaign_id``)
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

    # Gate: check the selected model before creating any row. A blocked preflight
    # answers 409 immediately instead of letting the run wait on a dead server.
    profile = selected_profile(db)
    preflight = run_preflight(profile, force=True)
    if preflight.status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "llm_blocked",
                "profile_id": preflight.profile_id,
                "codes": preflight.blocking_codes,
                "hints": preflight.hints,
            },
        )

    try:
        # Materialize the campaigns row from the STIX BEFORE any FK-bearing write
        # (agent_runs/abilities/attack_flows -> campaigns.id), so the run — including the
        # graph's _materialize_ai_abilities — never violates a FK for a
        # non-curated campaign (Req. 2.4, 2.5, 2.6). Idempotent no-op for an
        # already-materialized (curated) campaign.
        CampaignMaterializer(db).materialize(campaign, CURATED_VERSION)
        # The profile is frozen on the run row; every phase job runs with it, so a
        # selection change mid-run cannot mix models.
        run = AgentRunService(db).start(
            campaign, max_iterations=body.max_iterations, llm_profile_id=profile.id
        )
    except RunAlreadyActiveError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "run_already_active", "run_id": exc.run_id},
        ) from exc
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
                "cause_code": classify_llm_failure(exc.cause),
            },
        ) from exc
    except PersistenceError as exc:
        # Per-iteration persistence failed; roll back so nothing partial survives.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "persistence_failed",
                "message": ("The AI run could not be started: run persistence failed."),
            },
        ) from exc
    except Exception as exc:
        # Any other error: roll back so no partial agent_runs row is persisted
        # (Req. 13.2), then surface a generic "could not start" error response.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "reason": "run_not_started",
                "message": ("The AI run could not be started due to an unexpected error."),
            },
        ) from exc

    return _build_run_response(run, db)


# ---------------------------------------------------------------------------
# Interactive run control: active run, approve, regenerate, select, abort
# ---------------------------------------------------------------------------

# HTTP status of each ``RunTransitionError`` code (design: API contract).
_TRANSITION_STATUS = {
    RunTransitionError.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RunTransitionError.PROPOSAL_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    RunTransitionError.WRONG_PHASE: status.HTTP_409_CONFLICT,
    RunTransitionError.PHASE_RUNNING: status.HTTP_409_CONFLICT,
    RunTransitionError.NOT_AWAITING: status.HTTP_409_CONFLICT,
    RunTransitionError.PROPOSAL_LIMIT_REACHED: status.HTTP_409_CONFLICT,
    RunTransitionError.INVALID_PHASE: 422,
}

_APPROVABLE_PHASES = ("operationalization", "flow", "environment")


def _transition_http_error(exc: RunTransitionError) -> HTTPException:
    """Map a service transition error to its HTTP error (``detail.reason`` is the code)."""
    return HTTPException(
        status_code=_TRANSITION_STATUS.get(exc.code, status.HTTP_409_CONFLICT),
        detail={"reason": exc.code, "run_id": exc.run_id},
    )


def _run_not_found(run_id: int) -> HTTPException:
    return _transition_http_error(RunTransitionError(RunTransitionError.NOT_FOUND, run_id))


def _require_campaign_run(db: Session, campaign: str, run_id: int) -> Any:
    """Load the run, answering 404 ``run_not_found`` when absent or of another campaign."""
    from app.models.domain import AgentRun

    run = db.get(AgentRun, run_id, populate_existing=True)
    if run is None or run.campaign_id != campaign:
        raise _run_not_found(run_id)
    return run


def _gate_frozen_profile(run: Any) -> None:
    """Preflight the model frozen on the run before a step that calls it.

    Answers 409 ``llm_blocked`` (codes and hints) when the preflight blocks, and
    409 ``profile_unavailable`` when the frozen profile no longer exists. The run
    row is not changed in either case.
    """
    from app.services.ai.phase_job import _frozen_profile

    profile = _frozen_profile(run)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"reason": "profile_unavailable", "profile_id": run.llm_profile_id},
        )
    preflight = run_preflight(profile, force=True)
    if preflight.status == "blocked":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "llm_blocked",
                "profile_id": preflight.profile_id,
                "codes": preflight.blocking_codes,
                "hints": preflight.hints,
            },
        )


@router.get(
    "/campaigns/{campaign}/ai/runs/active",
    response_model=AiRunResponse,
    summary="Read the active run of the campaign",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Reason 'no_active_run'."}},
)
def get_active_ai_run(
    campaign: str = Path(..., description="Campaign whose active run is read"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Return the newest pending, running or awaiting-approval run of the campaign."""
    run = AgentRunService(db).active_run(campaign)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail={"reason": "no_active_run"}
        )
    return _build_run_response(run, db)


@router.post(
    "/campaigns/{campaign}/ai/runs/{id}/phases/{phase}/approve",
    response_model=AiRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Approve a paused phase and start the next one in the background",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Reason 'run_not_found'."},
        status.HTTP_409_CONFLICT: {
            "description": (
                "'wrong_phase', 'phase_running', 'run_not_awaiting', 'llm_blocked' or "
                "'profile_unavailable'."
            )
        },
        422: {"description": "Reason 'invalid_phase'."},
    },
)
def approve_ai_phase(
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the run"),
    phase: str = Path(..., description="operationalization, flow or environment"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Approve ``phase``; approving twice starts the next round once (409 the second time)."""
    if phase not in _APPROVABLE_PHASES:
        raise _transition_http_error(RunTransitionError(RunTransitionError.INVALID_PHASE, id))
    run = _require_campaign_run(db, campaign, id)
    _gate_frozen_profile(run)
    try:
        updated = AgentRunService(db).approve(id, phase)
    except RunTransitionError as exc:
        raise _transition_http_error(exc) from exc
    return _build_run_response(updated, db)


@router.post(
    "/campaigns/{campaign}/ai/runs/{id}/phases/flow/regenerate",
    response_model=AiRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate another attack flow while paused on the flow phase",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Reason 'run_not_found'."},
        status.HTTP_409_CONFLICT: {
            "description": (
                "'wrong_phase', 'phase_running', 'run_not_awaiting', "
                "'flow_proposal_limit_reached', 'llm_blocked' or 'profile_unavailable'."
            )
        },
    },
)
def regenerate_ai_flow(
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the run"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Start the regeneration of the attack flow in the background."""
    run = _require_campaign_run(db, campaign, id)
    _gate_frozen_profile(run)
    try:
        updated = AgentRunService(db).regenerate(id)
    except RunTransitionError as exc:
        raise _transition_http_error(exc) from exc
    return _build_run_response(updated, db)


@router.post(
    "/campaigns/{campaign}/ai/runs/{id}/phases/flow/select",
    response_model=AiRunResponse,
    summary="Go back to an earlier attack flow proposal",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "'run_not_found' or 'flow_proposal_not_found'."},
        status.HTTP_409_CONFLICT: {
            "description": "'wrong_phase', 'phase_running' or 'run_not_awaiting'."
        },
    },
)
def select_ai_flow_proposal(
    body: AiFlowSelectRequest,
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the run"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Make an earlier proposal current again; runs in the request, without the model."""
    _require_campaign_run(db, campaign, id)
    try:
        updated = AgentRunService(db).select_proposal(id, body.proposal_id)
    except RunTransitionError as exc:
        raise _transition_http_error(exc) from exc
    return _build_run_response(updated, db)


@router.post(
    "/campaigns/{campaign}/ai/runs/{id}/abort",
    response_model=AiRunResponse,
    summary="Abort a run paused for approval and release its resources",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Reason 'run_not_found'."},
        status.HTTP_409_CONFLICT: {"description": "'phase_running' or 'run_not_awaiting'."},
    },
)
def abort_ai_run(
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the run"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Abort the run; aborting twice answers 409 ``run_not_awaiting``."""
    _require_campaign_run(db, campaign, id)
    try:
        updated = AgentRunService(db).abort(id)
    except RunTransitionError as exc:
        raise _transition_http_error(exc) from exc
    return _build_run_response(updated, db)


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
    """Read a run with its iterations, phase outputs + trace, and verdict.

    Loads the ``agent_runs`` row by ``id`` (scoped to ``campaign``), its
    ``agent_iterations`` rows (oldest first), and for each iteration its
    ``agent_phase_outputs`` rows ordered by phase — each paired with its 1:1
    ``agent_phase_traces`` row (Agent_Input + Agent_Processing) and the
    ``agent_evaluations`` verdict recorded for the iteration (Req. 1.1, 1.4,
    2.4, 13.2). Null/absent Agent_Processing elements are resolved to the
    explicit "not available" sentinel the frontend renders (Req. 1.6). When the
    run failed, ``AiRunResponse.error`` surfaces ``agent_runs.error`` and the
    failing Phase is reported on the iteration's ``failing_agent`` (Req. 2.6,
    4.4). An unknown id — or an id that belongs to a different campaign — is a
    404.
    """
    from app.models.domain import (
        AgentEvaluation,
        AgentIteration,
        AgentPhaseOutput,
        AgentPhaseTrace,
        AgentRun,
    )
    from app.models.enums import AgentRunStatus

    run = db.get(AgentRun, id)
    if run is None or run.campaign_id != campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(f"Unknown AI run: id={id!r} for campaign {campaign!r}."),
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
        # Load the iteration's phase outputs ordered by phase, each paired with
        # its 1:1 agent_phase_traces row (left outer join so a phase output with
        # no trace still surfaces — its trace renders fully "not available").
        phase_output_rows = db.execute(
            select(AgentPhaseOutput, AgentPhaseTrace)
            .outerjoin(
                AgentPhaseTrace,
                AgentPhaseTrace.phase_output_id == AgentPhaseOutput.id,
            )
            .where(AgentPhaseOutput.iteration_id == iteration.id)
            .order_by(AgentPhaseOutput.phase.asc(), AgentPhaseOutput.id.asc())
        ).all()

        phase_outputs: list[AiPhaseOutputView] = []
        failing_agent: str | None = None
        for phase_output, trace in phase_output_rows:
            phase_value = getattr(phase_output.phase, "value", phase_output.phase)
            origin_value = getattr(phase_output.origin, "value", phase_output.origin)
            agent_input = (
                trace.agent_input
                if trace is not None and isinstance(trace.agent_input, dict)
                else {}
            )
            processing = trace.processing if trace is not None else None
            phase_outputs.append(
                AiPhaseOutputView(
                    id=phase_output.id,
                    phase=phase_value,
                    origin=origin_value,
                    payload=phase_output.payload if isinstance(phase_output.payload, dict) else {},
                    trace=AiAgentTraceView(
                        agent_input=agent_input,
                        processing=_resolve_processing(processing),
                    ),
                )
            )
            # On a failed run the failing phase is anchored on a MINIMAL phase
            # output (empty payload sentinel — no valid artifact was produced);
            # surface that phase as the failing Agent so the failure is auditable
            # (Req. 2.6). The first such anchor in the iteration identifies it.
            if (
                run_failed
                and failing_agent is None
                and not (isinstance(phase_output.payload, dict) and phase_output.payload)
            ):
                failing_agent = phase_value

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
        view.phase_outputs = phase_outputs
        view.failing_agent = failing_agent
        if evaluation_row is not None:
            view.evaluation = AiEvaluationView.model_validate(evaluation_row)
        iterations.append(view)

    return AiRunDetailResponse(
        run=_build_run_response(run, db),
        iterations=iterations,
    )


# ---------------------------------------------------------------------------
# Ground_Truth_Comparison (curated-only, on-demand) — task 9.6, design §H
# ---------------------------------------------------------------------------
#
# A read-only, on-demand comparison of a completed Run's AI output against the
# human curation of a Curated_Campaign. Curated-ness is decided SERVER-SIDE from
# ``campaigns.curated`` flag, so a
# non-curated campaign gets a clear "not a curated campaign" error. Curation is
# NEVER invoked as part of running/completing a Run (Req. 5.4) — this endpoint
# only *reads* the curated rows from the database to build the comparison baseline.


def _technique_ids_from_flow_payload(payload: Any) -> set[str]:
    """Collect the technique ids an AI flow phase output produced.

    The attack flow phase output ``payload`` is a serialized ``ProducedFlow`` whose
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
    """Collect the ability ids an AI operationalization phase output produced.

    The operationalization ``payload`` is a serialized
    ``ProducedOperationalization`` whose ``abilities`` field is a serialized
    ``OperationalizationOutput`` carrying an ``abilities`` list; each entry carries an
    ``ability_id``. Returns the non-empty ability ids as a set.
    """
    if not isinstance(payload, dict):
        return set()
    operationalization_abilities = payload.get("abilities")
    operationalization_abilities = (
        operationalization_abilities if isinstance(operationalization_abilities, dict) else {}
    )
    ability_views = operationalization_abilities.get("abilities")
    ids: set[str] = set()
    if isinstance(ability_views, list):
        for ability in ability_views:
            if isinstance(ability, dict):
                aid = ability.get("ability_id")
                if isinstance(aid, str) and aid.strip():
                    ids.add(aid.strip())
    return ids


def _build_phase_comparison(
    phase: str, ai_items: set[str], curated_items: set[str]
) -> GroundTruthPhaseComparison:
    """Assemble one phase's symmetric AI-vs-curation comparison (sorted, stable)."""
    return GroundTruthPhaseComparison(
        phase=phase,
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
    """Compute the on-demand Ground_Truth_Comparison for a curated campaign (Req. 5.1-5.4).

    Curated-ness is determined SERVER-SIDE from the ``campaigns.curated`` flag:
    the comparison is only
    offered on the eight Curated_Campaigns. For any other campaign a clear
    "not a curated campaign" 404 is returned (Req. 5.2) rather than a comparison.

    For a curated campaign the handler reads the Run's AI phase outputs (the attack flow
    technique ids and the operationalization ability ids it produced) and
    compares them — on demand — against the campaign's human curation read from
    the database (the curated flow techniques and the curated ability ids). Curation is used ONLY as the
    comparison baseline and is never invoked as part of running or completing a
    Run (Req. 5.4). An unknown run id — or one belonging to a different campaign
    — is a 404.
    """
    from app.models.domain import Ability as AbilityModel
    from app.models.domain import (
        AgentIteration,
        AgentPhaseOutput,
        AgentRun,
        Case,
    )
    from app.models.enums import AgentPhase
    from app.services.curated_case_reader import resolve_flow_data_from_db

    run = db.get(AgentRun, id)
    if run is None or run.campaign_id != campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "reason": "run_not_found",
                "message": (f"No AI run id={id!r} exists for campaign {campaign!r}."),
            },
        )

    # Server-side curated-ness: ``campaigns.curated`` must be true (the eight
    # curated cases), else there is no human-curation baseline to compare against
    # and the action must not be offered (Req. 5.2).
    campaign_row = db.get(Case, campaign)
    if campaign_row is None or not campaign_row.curated:
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
    phase_output_rows = (
        db.execute(
            select(AgentPhaseOutput)
            .join(AgentIteration, AgentPhaseOutput.iteration_id == AgentIteration.id)
            .where(AgentIteration.run_id == run.id)
        )
        .scalars()
        .all()
    )

    ai_technique_ids: set[str] = set()
    ai_ability_ids: set[str] = set()
    for row in phase_output_rows:
        phase_value = getattr(row.phase, "value", row.phase)
        if phase_value == AgentPhase.FLOW.value:
            ai_technique_ids |= _technique_ids_from_flow_payload(row.payload)
        elif phase_value == AgentPhase.OPERATIONALIZATION.value:
            ai_ability_ids |= _ability_ids_from_op_payload(row.payload)

    # ---- Curation side: the baseline read from the database, on demand ------
    # Database-only sourcing (Req. 1.1, 4.1): the curated attack flow technique ids come
    # from the persisted ``attack_flows`` row (via the DB reader) and the curated ability
    # ids from the ``abilities`` rows keyed by ``campaign_id`` — both persisted by
    # the curated ingest (sub-step 2.1). The runtime no longer reads
    # the legacy CLI data directory for the comparison baseline (Req. 4.2).
    curated_technique_ids: set[str] = set()
    curated_ability_ids: set[str] = set()

    curated_flow = resolve_flow_data_from_db(db, campaign)
    if curated_flow is not None:
        for node in curated_flow.structural_nodes:
            tid = (node.technique_id or "").strip()
            if tid:
                curated_technique_ids.add(tid)

    curated_ability_rows = (
        db.execute(select(AbilityModel.ability_id).where(AbilityModel.campaign_id == campaign))
        .scalars()
        .all()
    )
    for ability_id in curated_ability_rows:
        aid = (ability_id or "").strip()
        if aid:
            curated_ability_ids.add(aid)

    phases = [
        _build_phase_comparison(AgentPhase.FLOW.value, ai_technique_ids, curated_technique_ids),
        _build_phase_comparison(
            AgentPhase.OPERATIONALIZATION.value,
            ai_ability_ids,
            curated_ability_ids,
        ),
    ]

    return GroundTruthComparisonResponse(
        campaign_id=campaign,
        run_id=run.id,
        phases=phases,
    )
