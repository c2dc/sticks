"""REST endpoints for emulation preview + execution with the containment
pre-flight (task 11.2) — SECURITY CORE (the HTTP surface of containment).

This module exposes the two emulation endpoints of the design's "REST endpoints"
section and the emulation sequence diagram (the ``.../emulation/preview`` +
``.../emulation`` pair), wiring them to the already-built security core:

* ``POST /api/campaigns/{campaign}/stage/5/emulation/preview`` — returns the **complete
  list of concrete commands with the target container of each, without executing**
  anything. It is a pure presentation of :func:`preview_abilities` over the
  case's Abilities (Req. 6.5). The confirmation modal (``EmulationConfirmModal``)
  renders this before any confirmation.
* ``POST /api/campaigns/{campaign}/stage/5/emulation`` — accepts a body with ``confirmed`` and
  runs the four-gate pre-flight through :meth:`OperationRunner.run`
  (confirmation -> destinations -> isolation -> availability). It maps the
  runner's typed :class:`RunOutcome` and the propagated
  :class:`CalderaUnavailable` to the HTTP contract the design mandates:

    - ``RunOutcome.CONTAINMENT_REFUSED`` (external destination, Req. 6.2) and
      ``RunOutcome.ISOLATION_FAILED`` (container not isolated, Req. 6.4) ->
      **HTTP 409** with the offending Ability/command or container in the body.
    - ``CalderaUnavailable`` (Caldera silent within 10s, Req. 4.6) -> **HTTP 503**.
    - ``RunOutcome.NOT_CONFIRMED`` (``confirmed`` not true / cancelled,
      Req. 6.6, 6.7) -> **HTTP 200** with a body indicating no operation started
      (the key requirement being that *nothing ran*).
    - success (``COMPLETED`` / ``ABORTED``) -> the operation result (state +
      aggregate + per-Ability results). ``ABORTED`` (audit-trail failure,
      Req. 6.9) surfaces its message so the UI can flag the interrupted trail.

Handlers are kept thin: they resolve the case (its Abilities + Adversary) via
:class:`CaseService`, then delegate to the preview builder or the
:class:`OperationRunner`. All the enforcement lives in the runner and the
``containment`` package — this layer only translates outcomes into HTTP.

Dependency injection (Windows dev; NO real Caldera/Docker)
----------------------------------------------------------
The :class:`OperationRunner` needs a :class:`CalderaClient` + :class:`AuditLogger`
+ session. Those are provided through FastAPI dependencies so task 11.4 (and the
lightweight verification here) can override them with a ``httpx.MockTransport``
backed client + a fake isolation verifier, without a real Caldera or Docker
daemon:

* :func:`get_caldera_client` — yields a :class:`CalderaClient` (default settings
  in production; overridden with a MockTransport-backed client in tests).
* :func:`get_runner_factory` — yields a small factory
  ``(session) -> OperationRunner`` so the handler builds the runner bound to the
  request-scoped session while keeping the Caldera client (and any test-injected
  isolation verifier) overridable via a single dependency.

Overriding either dependency via ``app.dependency_overrides`` is enough to drive
the whole endpoint fully mocked.

_Requirements: 6.5, 6.6, 6.7, 6.2, 6.4, 4.6_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit import AuditLogger
from app.services.caldera import CalderaApiError, CalderaClient, CalderaUnavailable
from app.services.campaign_service import AbilityData, CaseData, CaseService
from app.services.containment import EmulationPreview, preview_abilities
from app.services.operation.runner import (
    OperationRunResult,
    OperationRunner,
    RunOutcome,
)

router = APIRouter(prefix="/api", tags=["emulation"])


@dataclass(frozen=True)
class _RunnerAbility:
    """Runner-facing view of a curated Ability with plain-dict executors.

    The :class:`OperationRunner` builds the Caldera ``POST /api/v2/abilities``
    payload straight from ``ability.executors``, which must be JSON-serialisable.
    The curated :class:`AbilityData` carries Pydantic :class:`Executor` models, so
    this adapter exposes the same duck-typed surface the runner / containment /
    preview consume (``ability_id``/``name``/``executors``) with ``executors`` as
    plain ``{name, platform, command}`` dicts.
    """

    ability_id: str
    name: str | None
    tactic: str | None
    technique_name: str | None
    technique_id: str | None
    description: str | None
    executors: tuple[dict[str, object], ...]


def _runner_abilities(abilities: list[AbilityData]) -> list[_RunnerAbility]:
    """Adapt curated :class:`AbilityData` to runner-facing abilities.

    Converts each Pydantic :class:`Executor` to a plain dict so the runner's
    Caldera payload builder can JSON-serialise it, preserving order and identity.
    """
    return [
        _RunnerAbility(
            ability_id=a.ability_id,
            name=a.name,
            tactic=a.tactic,
            technique_name=a.technique_name,
            technique_id=a.technique_id,
            description=a.description,
            executors=tuple(e.model_dump() for e in a.executors),
        )
        for a in abilities
    ]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class EmulationRequest(BaseModel):
    """Body of ``POST /api/campaigns/{campaign}/stage/5/emulation`` (Req. 6.6, 6.7).

    ``confirmed`` is the **explicit** confirmation flag. The Operation starts
    iff this is exactly ``True``; anything else (absent/false/cancelled) means
    "not confirmed" and nothing runs.
    """

    confirmed: bool = Field(
        default=False,
        description=(
            "Explicit confirmation from the Researcher. The emulation only "
            "starts with confirmed=true; otherwise no command is executed."
        ),
    )


class PreviewDestinationView(BaseModel):
    """One destination of a command, resolved to its target container (Req. 6.5)."""

    value: str
    origin: str
    external: bool
    container: Optional[str] = None
    target: str


class CommandPreviewView(BaseModel):
    """A single concrete command and its resolved target(s) (Req. 6.5)."""

    command: str
    local: bool
    has_external: bool
    destinations: list[PreviewDestinationView] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)


class AbilityPreviewView(BaseModel):
    """The preview of every command of a single Ability (Req. 6.5)."""

    ability_id: Optional[str] = None
    ability_name: Optional[str] = None
    has_external: bool
    commands: list[CommandPreviewView] = Field(default_factory=list)


class EmulationPreviewResponse(BaseModel):
    """``POST /api/campaigns/{campaign}/stage/5/emulation/preview`` payload (Req. 6.5).

    The complete, concrete list of commands + target container of each, plus a
    presentation-only ``has_external`` flag so the modal can highlight any
    external destination. Nothing is executed to build this.
    """

    campaign_id: str
    has_external: bool
    abilities: list[AbilityPreviewView] = Field(default_factory=list)


class AbilityResultView(BaseModel):
    """One per-Ability result of a run (part of the emulation response)."""

    ability_id: Optional[str] = None
    status: AbilityResultStatus
    command: Optional[str] = None
    target_container: str
    output: Optional[str] = None


class EmulationResponse(BaseModel):
    """``POST /api/campaigns/{campaign}/stage/5/emulation`` success payload (COMPLETED/ABORTED).

    Carries the runner's typed outcome, the final Operation state, the aggregate
    (``total_success`` / ``total_failure``, Req. 4.5), the per-Ability results and
    the human-readable message (which, for ABORTED, explains the audit-trail
    failure — Req. 6.9).
    """

    campaign_id: str
    started: bool
    result: RunOutcome
    state: OperationState
    operation_id: Optional[int] = None
    caldera_operation_id: Optional[str] = None
    total_success: int = 0
    total_failure: int = 0
    results: list[AbilityResultView] = Field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# Dependencies (overridable for tests — no real Caldera/Docker in dev)
# ---------------------------------------------------------------------------

#: A factory building an :class:`OperationRunner` bound to a request-scoped
#: session. Injected so the handler stays thin and tests can override the whole
#: construction (Caldera client + isolation verifier) via a single dependency.
RunnerFactory = Callable[[Session], OperationRunner]


def get_caldera_client() -> Iterator[CalderaClient]:
    """FastAPI dependency yielding a :class:`CalderaClient`.

    In production this builds a client from default settings (which reads the
    Caldera URL + ``KEY`` header from config). In tests, override this dependency
    with one yielding a :class:`CalderaClient` backed by
    :class:`httpx.MockTransport` so no real Caldera is contacted. The client is
    always closed after the request.
    """
    client = CalderaClient()
    try:
        yield client
    finally:
        client.close()


def get_runner_factory(
    caldera: CalderaClient = Depends(get_caldera_client),
) -> RunnerFactory:
    """FastAPI dependency yielding a runner factory bound to the given Caldera.

    Returns a ``(session) -> OperationRunner`` so the handler builds the runner
    with the **request-scoped** SQLAlchemy session (and its :class:`AuditLogger`)
    while the Caldera client comes from the overridable
    :func:`get_caldera_client`. Tests that need a fake isolation verifier can
    override *this* dependency instead and inject
    ``isolation_verifier=`` into the built runner.
    """

    def _factory(session: Session) -> OperationRunner:
        return OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
        )

    return _factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_case_or_error(campaign: str, case_service: CaseService) -> CaseData:
    """Resolve a curated case, mapping unknown/failed reads to HTTP errors.

    An unknown slug is a 404; a missing/malformed case file is a descriptive 422
    (mirroring the campaigns router's per-case handling). The returned
    :class:`CaseData` exposes the Abilities and Adversary the emulation needs.
    """
    if campaign not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {campaign!r}.",
        )
    result = case_service.try_load_case(campaign)
    if not result.ok or result.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                result.error.detail if result.error else f"Falha ao ler {campaign!r}."
            ),
        )
    return result.case


def _preview_view(campaign: str, preview: EmulationPreview) -> EmulationPreviewResponse:
    """Map the domain :class:`EmulationPreview` to the response model (Req. 6.5)."""
    abilities: list[AbilityPreviewView] = []
    for ability in preview.abilities:
        commands: list[CommandPreviewView] = []
        for command in ability.commands:
            destinations = [
                PreviewDestinationView(
                    value=dest.value,
                    origin=dest.raw,
                    external=dest.is_external,
                    container=dest.container,
                    target=dest.target_label,
                )
                for dest in command.destinations
            ]
            commands.append(
                CommandPreviewView(
                    command=command.command,
                    local=command.is_local,
                    has_external=command.has_external,
                    destinations=destinations,
                    targets=command.target_labels,
                )
            )
        abilities.append(
            AbilityPreviewView(
                ability_id=ability.ability_id,
                ability_name=ability.ability_name,
                has_external=ability.has_external,
                commands=commands,
            )
        )
    return EmulationPreviewResponse(
        campaign_id=campaign,
        has_external=preview.has_external,
        abilities=abilities,
    )


def _success_view(campaign: str, result: OperationRunResult) -> EmulationResponse:
    """Map a COMPLETED/ABORTED :class:`OperationRunResult` to the response model."""
    return EmulationResponse(
        campaign_id=campaign,
        started=result.started,
        result=result.outcome,
        state=result.state,
        operation_id=result.operation_id,
        caldera_operation_id=result.caldera_operation_id,
        total_success=result.total_success,
        total_failure=result.total_failure,
        results=[
            AbilityResultView(
                ability_id=ar.ability_id,
                status=ar.status,
                command=ar.command,
                target_container=ar.target_container,
                output=ar.output,
            )
            for ar in result.ability_results
        ],
        message=result.message,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign}/stage/5/emulation/preview",
    response_model=EmulationPreviewResponse,
    summary="Complete list of concrete commands + target container (without executing)",
)
def preview_emulation(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> EmulationPreviewResponse:
    """Return the complete command/destination preview for a case (Req. 6.5).

    Builds :func:`preview_abilities` over the case's curated Abilities and maps
    it to the response model. Nothing is executed and no Operation is created —
    this is the presentation the confirmation modal shows *before* any
    confirmation. External destinations are flagged (``has_external``) but not
    refused here; refusal is the emulation endpoint's job.
    """
    case_service = CaseService()
    case = _load_case_or_error(campaign, case_service)
    preview = preview_abilities(case.abilities)
    return _preview_view(campaign, preview)


@router.post(
    "/campaigns/{campaign}/stage/5/emulation",
    response_model=EmulationResponse,
    summary="Run the emulation with the containment pre-flight (confirmed=true)",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "Containment violated — external destination (Req. 6.2) or non-isolated "
                "target container (Req. 6.4). Nothing was executed."
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Caldera did not respond within 10s (Req. 4.6). Nothing was executed."
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Caldera responded with an error when receiving the operation."
        },
    },
)
def execute_emulation(
    body: EmulationRequest,
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
    runner_factory: RunnerFactory = Depends(get_runner_factory),
) -> EmulationResponse:
    """Run the emulation pre-flight + execution for a case (Req. 6.6/6.7/6.2/6.4/4.6).

    Resolves the case's Abilities + Adversary, builds an
    :class:`OperationRunner` bound to the request-scoped session (via the
    overridable :func:`get_runner_factory`), and calls :meth:`OperationRunner.run`
    with the body's ``confirmed`` flag. The runner enforces the four gates in
    order (confirmation -> destinations -> isolation -> availability); this
    handler only maps the typed outcome to HTTP:

    * ``CONTAINMENT_REFUSED`` (external destination) / ``ISOLATION_FAILED``
      (container not isolated) -> **409** with the offending detail.
    * ``CalderaUnavailable`` (propagated from the availability gate) -> **503**.
    * ``NOT_CONFIRMED`` -> **200** with a body indicating nothing started.
    * ``COMPLETED`` / ``ABORTED`` -> **200** with the operation result (ABORTED
      surfaces the audit-failure message).
    """
    case_service = CaseService()
    case = _load_case_or_error(campaign, case_service)

    adversary_id = case.adversary.id if case.adversary else ""
    # Build the raw Adversary payload for Caldera's `POST /api/v2/adversaries`
    # (v2 expects `adversary_id`, not `id`). Loading it before the Operation
    # is what gives the Operation a non-empty chain to execute (Req. 4.2).
    adversary_payload = (
        {
            "adversary_id": case.adversary.id,
            "name": case.adversary.name or case.adversary.id,
            "description": case.adversary.description or "",
            "atomic_ordering": list(case.adversary.atomic_ordering),
        }
        if case.adversary
        else None
    )

    runner = runner_factory(db)
    try:
        result = runner.run(
            campaign_id=campaign,
            abilities=_runner_abilities(case.abilities),
            adversary_id=adversary_id,
            confirmed=body.confirmed,
            adversary_payload=adversary_payload,
        )
    except CalderaUnavailable as exc:
        # Gate 4 (Req. 4.6): Caldera silent within 10s -> 503. Nothing started.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CalderaApiError as exc:
        # Caldera responded, but refused/failed to process the payload. This is an
        # upstream failure (502), not a generic internal API error.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "caldera_api_error",
                "mensagem": str(exc),
                "endpoint": exc.path,
                "status_caldera": exc.status_code,
            },
        ) from exc

    if result.outcome is RunOutcome.CONTAINMENT_REFUSED:
        # Gate 2 (Req. 6.2): external destination -> 409, identifying the
        # offending Ability/command/destination.
        violations = (
            result.containment.describe_violations() if result.containment else []
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "contencao_recusada",
                "mensagem": result.message,
                "violacoes": violations,
            },
        )

    if result.outcome is RunOutcome.ISOLATION_FAILED:
        # Gate 3 (Req. 6.4): a target container is not isolated -> 409,
        # identifying the offending container(s).
        containers = (
            [v.container for v in result.isolation.offending]
            if result.isolation
            else []
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "isolamento_falhou",
                "mensagem": result.message,
                "containers": containers,
            },
        )

    # After the renumbering (five-stage pipeline spec): the old "Emulation"
    # becomes Stage 5 — Campaign Execution. When the execution completes
    # successfully, Stage 5 (`STAGE_EXECUTION`) moves to `concluido` —
    # persisted through the single completion path (persist_stage_completion).
    # Only COMPLETED + OperationState.FINISHED persists;
    # ABORTED/NOT_CONFIRMED/409/503 do not. A failure to mark the completion does
    # NOT bring down the execution already performed. (The isolation verification
    # stays in the runner's pre-flight for now; task 4 of the spec moves/duplicates
    # that responsibility to Stage 4 — Environment Provisioning.)
    if result.outcome is RunOutcome.COMPLETED and result.state is OperationState.FINISHED:
        try:
            from app.services.session.session_state_service import SessionStateService
            from app.services.stage.stage_service import STAGE_EXECUTION

            SessionStateService(db).persist_stage_completion(campaign, STAGE_EXECUTION)
        except Exception:  # noqa: BLE001 - best-effort; does not fail the success response
            pass

    # NOT_CONFIRMED (Req. 6.6/6.7): nothing ran — 200 with a "not started" body.
    # COMPLETED / ABORTED: the operation result (ABORTED surfaces its message).
    return _success_view(campaign, result)
