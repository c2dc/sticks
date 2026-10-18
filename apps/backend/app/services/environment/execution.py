"""ExecutionStageService — Stage 6 Execution seam (task 8.2).

Stage 6 (Execution + measurement + report) of the six-stage pipeline **fully
reuses** the already-built :class:`~app.services.operation.runner.OperationRunner`
(the old Stage 5 "Campaign Execution" of the five-stage scheme), without
reimplementing any of its safety logic. This module is the *session-bound*
**seam** that ties the runner to the new stage number
(:data:`~app.services.stage.stage_service.STAGE_EXECUTION` ``= 6``), exposing the
**preview** as a visible precondition and marking the Stage 6 completion through
the single completion path
(:meth:`SessionStateService.persist_stage_completion`).

It is, for Execution, the counterpart of the environment auto-completion
(:class:`~app.services.environment.stage_service.EnvironmentStageService`): same
*service with injected ``Session``* seam, same **injectable** external dependency
(here the ``OperationRunner`` via a small *factory*, after the fashion of
``api/emulation.py``), and same completion-persistence path.

The four gates — full REUSE of the ``OperationRunner``
------------------------------------------------------
:meth:`ExecutionStageService.execute` only delegates to
:meth:`OperationRunner.run`, which already applies, **in the design order** (Req.
13.1), the four gates before starting any Operation:

1. **Explicit confirmation** (Req. 13.2) — the Operation starts *if and only if*
   ``confirmed is True``; otherwise nothing is executed and the state returns to
   ``NOT_STARTED`` (``RunOutcome.NOT_CONFIRMED``).
2. **Target containment** (``validate_abilities``, Req. 13.3, 13.4, 13.5) — if
   some command has an external target, the Operation is **refused** and does not
   start; the violation identifies the Ability/command
   (``RunOutcome.CONTAINMENT_REFUSED``).
3. **Target-container isolation** (read-only, Req. 14.4, 14.5) — if some
   target container is not isolated, the Operation is **aborted** and does not
   start (``RunOutcome.ISOLATION_FAILED``).
4. **Caldera availability** (Req. 10.1) — ``ensure_available`` raises
   :class:`~app.services.caldera.CalderaUnavailable` when Caldera does not respond
   within 10s; no Operation is created before that.

**SINGLE execution** on the curated campaign (Req. 10.3): the runner drives the
Operation only once — **no automatic retry** (non-goal, Req. 18.3). This seam
does **not** add any repetition loop; it calls ``run(...)`` exactly once per
execution request.

**Per-command persistence** (Req. 10.2): the runner itself persists one
:class:`~app.models.domain.AbilityResult` per Ability and writes an
:class:`~app.models.domain.AuditLogEntry` row per executed command (via the
``AuditLogger`` bound to the same ``Session``). This seam does not duplicate that
persistence — it only passes the ``Session`` to the runner.

**Preview as a visible precondition** (Req. 13.6, 13.7): before any execution,
:meth:`ExecutionStageService.preview` exposes
:func:`~app.services.containment.preview_abilities` — the full list of concrete
commands with each one's target container and the external-target flag. It is the
same presentation the confirmation modal renders *before* the confirmation; the
preview **presents**, it does not **execute** nor create an Operation.

Stage 6 completion
------------------
Only when the execution actually completes (``RunOutcome.COMPLETED`` **and**
``OperationState.FINISHED``) is Stage 6 (:data:`STAGE_EXECUTION`) marked
``completed`` through the single completion path
(:meth:`SessionStateService.persist_stage_completion`, transactional and
idempotent). ``ABORTED`` (audit-trail failure, Req. 10.2), ``NOT_CONFIRMED``
and the gate refusals do **not** complete the stage. A failure to persist the
completion does **not** bring down the execution already performed — it is a typed
signal in the result.

.. note::
    The **deprovisioning** at the end (Req. 10.8, 12) and the **measurement** of
    procedural sufficiency + the report (Req. 15) are stitched by tasks 8.3/8.4
    on top of this same seam; here the completion reflects only the execution
    (the REST endpoints are task 8.5). This task confirms/wires the Stage 6
    Execution seam reusing the ``OperationRunner`` with the four gates.

_Requirements: 10.1, 10.2, 10.3, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 14.4,
14.5, 18.3_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.enums import AbilityResultStatus, OperationState
from app.services.caldera import CalderaUnavailable
from app.services.campaign_service import AbilityData, CaseData, CaseService
from app.services.containment import EmulationPreview, preview_abilities
from app.services.operation.runner import (
    OperationRunner,
    OperationRunResult,
    RunOutcome,
)
from app.services.session.session_state_service import SessionStateService
from app.services.stage.stage_service import STAGE_EXECUTION


# ---------------------------------------------------------------------------
# Runner factory (injection — same pattern as ``api/emulation.py``)
# ---------------------------------------------------------------------------

#: A *factory* that builds an :class:`OperationRunner` bound to a request
#: ``Session``. Injected so this seam stays thin and the tests replace the whole
#: construction (Caldera client + isolation checker) with a single dependency —
#: without real Caldera/Docker (same contract as the ``RunnerFactory`` of
#: ``api/emulation.py``).
RunnerFactory = Callable[[Session], OperationRunner]


# ---------------------------------------------------------------------------
# Ability adapter for the runner (executors as a serializable dict)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunnerAbility:
    """View of the curated Ability that the runner/containment/preview consume.

    The :class:`OperationRunner` builds the ``POST /api/v2/abilities`` Caldera
    payload directly from ``ability.executors``, which must be JSON-serializable.
    :class:`~app.services.campaign_service.AbilityData` carries Pydantic
    :class:`Executor` models, so this adapter exposes the same *duck-typed*
    surface (``ability_id``/``name``/``executors``) with ``executors`` as flat
    dicts ``{name, platform, command}``. It reuses exactly the same projection as
    ``api/emulation.py`` to keep the payload coherent.
    """

    ability_id: str
    name: Optional[str]
    tactic: Optional[str]
    technique_name: Optional[str]
    technique_id: Optional[str]
    description: Optional[str]
    executors: tuple[dict[str, object], ...]


def _runner_abilities(abilities: list[AbilityData]) -> list[_RunnerAbility]:
    """Adapt curated :class:`AbilityData` to runner abilities.

    Converts each Pydantic :class:`Executor` into a flat dict so the Caldera
    payload builder in the runner can JSON-serialize it, preserving order and
    identity.
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


def _adversary_payload(case: CaseData) -> Optional[dict[str, object]]:
    """Build the ``POST /api/v2/adversaries`` payload (v2 uses ``adversary_id``).

    Loading the Adversary — with its ``atomic_ordering`` — before the Operation is
    what gives the Operation a non-empty chain to execute in the DAG order (Req.
    10.3). Without a curated Adversary it returns ``None`` (the Abilities load
    still happens).
    """
    adversary = case.adversary
    if adversary is None:
        return None
    return {
        "adversary_id": adversary.id,
        "name": adversary.name or adversary.id,
        "description": adversary.description or "",
        "atomic_ordering": list(adversary.atomic_ordering),
    }


# ---------------------------------------------------------------------------
# Typed result of the Stage 6 execution
# ---------------------------------------------------------------------------


class ExecutionAbilityResult(BaseModel):
    """Per-Ability result of a Stage 6 execution.

    Serializable projection of the
    :class:`~app.services.operation.runner.AbilityRunResult` that the runner
    produces and persists, to compose the typed result and the JSON contracts.

    Attributes:
        ability_id: The executed Ability (``None`` when Caldera did not associate
            the link with an ability).
        status: The derived :class:`~app.models.enums.AbilityResultStatus`.
        command: The concrete executed command (decoded), if known.
        target_container: The label of the target container used in the audit.
        output: The command output (stdout/stderr), if collected.
    """

    ability_id: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    command: Optional[str] = None
    target_container: str
    output: Optional[str] = None


class ExecutionResult(BaseModel):
    """Typed result of :meth:`ExecutionStageService.execute` (Stage 6).

    Instead of raising exceptions for the expected paths, the execution returns
    this typed result (same contract as ``AutoProvisioningResult`` /
    ``StageCompletionResult``): ``started`` indicates whether the Operation
    actually ran; ``result`` is the runner's :class:`RunOutcome` (confirmation,
    containment, isolation, availability or execution/abort); ``completed``
    indicates whether Stage 6 moved to ``completed`` by this call.

    Attributes:
        campaign_id: The Campaign whose execution was requested.
        started: ``True`` iff the Operation actually started in Caldera
            (``COMPLETED``/``ABORTED``).
        result: The runner's :class:`RunOutcome` (why it ended that way).
        state: The final persisted :class:`OperationState` of the Operation.
        operation_id: The local ``Operation.id``, when an Operation was created.
        caldera_operation_id: The Operation's id in Caldera, when created.
        total_success: Aggregate of Abilities in ``success`` (Req. 10.4 source).
        total_failure: Aggregate of Abilities in ``failure``.
        results: Per-Ability results, in execution order.
        completed: ``True`` iff Stage 6 was marked ``completed`` by this
            execution (only ``COMPLETED`` + ``FINISHED``).
        completion_persisted: ``True`` iff the Stage 6 completion was actually
            saved; ``False`` if the completion ``commit`` failed (execution
            preserved, previous stage state preserved).
        violations: Descriptions of the containment violations, when refused.
        non_isolated_containers: Containers that failed isolation, when aborted
            by isolation.
        message: Human-readable text of the result (summary/reason), coming from
            the runner.
    """

    campaign_id: str
    started: bool = False
    result: RunOutcome = RunOutcome.NOT_CONFIRMED
    state: OperationState = OperationState.NOT_STARTED
    operation_id: Optional[int] = None
    caldera_operation_id: Optional[str] = None
    total_success: int = 0
    total_failure: int = 0
    results: list[ExecutionAbilityResult] = Field(default_factory=list)
    completed: bool = False
    completion_persisted: bool = False
    violations: list[str] = Field(default_factory=list)
    non_isolated_containers: list[str] = Field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# The session-bound Stage 6 Execution seam
# ---------------------------------------------------------------------------


class ExecutionStageService:
    """Stage 6 Execution seam reusing the ``OperationRunner`` (task 8.2).

    Same mold as the other *session-bound* services
    (:class:`~app.services.environment.stage_service.EnvironmentStageService` /
    :class:`~app.services.dag.dag_service.DagService`): it receives an injected
    :class:`~sqlalchemy.orm.Session` and builds the
    :class:`OperationRunner` through an **injectable** *factory*
    (``runner_factory``), so the tests replace the whole construction (Caldera
    client + isolation checker) with *fakes* — without real Caldera or Docker —,
    exactly as ``api/emulation.py`` does. The :class:`CaseService`
    (resolution/reading of the curated Campaign) is also injectable.

    The seam does **not** reimplement any of the four gates nor the per-command
    persistence: it delegates to :meth:`OperationRunner.run` (single application
    source of the gates, of the single execution and of the per-command audit)
    and only:

    * exposes the **preview** as a visible precondition (:meth:`preview`);
    * calls ``run(...)`` **exactly once** per request (no retry — Req. 18.3);
    * marks Stage 6 ``completed`` through the single completion path when — and
      only when — the execution completes (``COMPLETED`` + ``FINISHED``).

    _Requirements: 10.1, 10.2, 10.3, 13.1, 13.2, 13.3, 13.6, 14.4, 14.5, 18.3_
    """

    def __init__(
        self,
        session: Session,
        *,
        runner_factory: RunnerFactory,
        case_service: Optional[CaseService] = None,
    ) -> None:
        """Assemble the Stage 6 Execution seam.

        Args:
            session: Open :class:`~sqlalchemy.orm.Session`, injected by the
                caller. The service does not open or close it; the
                ``OperationRunner`` ``commit``s the transitions/aggregation and the
                stage completion is persisted through the single path
                (``persist_stage_completion``).
            runner_factory: ``(session) -> OperationRunner`` factory that builds
                the runner bound to the request ``Session``. Injected to keep the
                seam thin and allow a full *override* in the tests (Caldera client
                + isolation checker), without real Caldera/Docker.
            case_service: :class:`CaseService` to resolve/read the curated Campaign
                (Abilities + Adversary). Default: a production instance.
        """
        self._session = session
        self._runner_factory = runner_factory
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )

    @property
    def case_service(self) -> CaseService:
        """The :class:`CaseService` used to resolve/read the curated Campaign."""
        return self._case_service

    # -- preview (visible precondition, Req. 13.6, 13.7) ---------------------

    def preview(self, campaign: str) -> EmulationPreview:
        """Expose the preview of the Campaign's commands — execution precondition.

        Builds :func:`~app.services.containment.preview_abilities` over the
        Campaign's curated Abilities: the full list of concrete commands with each
        one's target container and the external-target flag (Req. 13.6, 13.7).
        **Nothing is executed** and **no Operation is created** — it is the
        presentation the confirmation modal shows *before* any confirmation.
        External targets are **flagged**, not refused here; the refusal is one of
        the :meth:`execute` gates (via the runner).
        """
        case = self._load_case(campaign)
        return preview_abilities(case.abilities)

    # -- execution (full REUSE of the OperationRunner, Req. 13.1) ------------

    def execute(
        self, campaign: str, *, confirmed: bool, complete: bool = True
    ) -> ExecutionResult:
        """Execute Stage 6 of Campaign ``campaign`` reusing the ``OperationRunner``.

        Resolves the Campaign's curated Abilities + Adversary, builds the
        :class:`OperationRunner` bound to the request ``Session`` (via the
        injectable ``runner_factory``) and calls :meth:`OperationRunner.run`
        **exactly once** (single execution, no retry — Req. 10.3, 18.3). The
        runner applies the four gates in the design order (confirmation →
        containment → isolation → availability, Req. 13.1), persists one
        ``AbilityResult`` per Ability and one ``AuditLogEntry`` row per command
        (Req. 10.2).

        This seam does **not** reimplement any gate nor the audit; it only
        projects the runner's typed :class:`OperationRunResult` into an
        :class:`ExecutionResult` and, **only** when the execution actually
        completes (``COMPLETED`` + ``FINISHED``), marks Stage 6
        (:data:`STAGE_EXECUTION`) ``completed`` through the single completion
        path (:meth:`SessionStateService.persist_stage_completion`). A failure to
        persist the completion does **not** bring down the execution already
        performed — it is a typed signal in ``completion_persisted=False``.

        The :class:`~app.services.caldera.CalderaUnavailable` of gate 4 is left to
        **propagate** (the caller/endpoint maps it to HTTP 503, Req. 10.1): no
        Operation is created before the gate passes, so the state stays untouched.

        When ``complete`` is ``False`` the execution runs normally but the
        **Stage 6 completion is deferred** to the caller — it is the point the
        Stage 6 completion seam (:class:`CompletionStage6Service`, task 8.4) uses
        to only complete **after** measuring, generating the report and
        **deprovisioning** the environment (Req. 10.9). The default ``True``
        preserves the historical behavior (execution + immediate completion).
        """
        case = self._load_case(campaign)
        adversary_id = case.adversary.id if case.adversary else ""

        runner = self._runner_factory(self._session)
        # SINGLE execution — called only once, with no retry loop (Req. 10.3, 18.3).
        result = runner.run(
            campaign_id=campaign,
            abilities=_runner_abilities(case.abilities),
            adversary_id=adversary_id,
            confirmed=confirmed,
            adversary_payload=_adversary_payload(case),
        )
        return self._to_result(campaign, result, complete=complete)

    def executed_successfully(self, result: ExecutionResult) -> bool:
        """``True`` iff the execution of ``result`` actually completed (COMPLETED + FINISHED).

        Single criterion for "the execution completed" (Req. 10.9) reused by the
        Stage 6 completion seam: only a ``COMPLETED`` + ``FINISHED`` execution
        enables the completion (after the deprovisioning). ``ABORTED`` /
        ``NOT_CONFIRMED`` / gate refusal do **not** enable the completion.
        """
        return (
            result.result is RunOutcome.COMPLETED
            and result.state is OperationState.FINISHED
        )

    def complete_stage6(self, campaign: str) -> bool:
        """Mark Stage 6 ``completed`` through the single path; return whether it persisted.

        Delegates to :meth:`SessionStateService.persist_stage_completion`
        (transactional and idempotent). Called by the completion seam (task 8.4)
        **only after** the execution completes and the environment is
        deprovisioned (Req. 10.9). A persistence failure returns ``False`` without
        bringing down the execution already performed.
        """
        completion = SessionStateService(self._session).persist_stage_completion(
            campaign, STAGE_EXECUTION
        )
        return completion.saved

    # -- projection + completion ---------------------------------------------

    def _to_result(
        self, campaign: str, result: OperationRunResult, *, complete: bool = True
    ) -> ExecutionResult:
        """Project the runner's result and complete Stage 6 on a success case.

        When ``complete`` is ``False`` the projection is made but the completion
        is **deferred** (``completed``/``completion_persisted`` stay ``False``);
        the caller completes after the deprovisioning (Req. 10.9).
        """
        results = [
            ExecutionAbilityResult(
                ability_id=ar.ability_id,
                status=ar.status,
                command=ar.command,
                target_container=ar.target_container,
                output=ar.output,
            )
            for ar in result.ability_results
        ]
        violations = (
            result.containment.describe_violations()
            if result.outcome is RunOutcome.CONTAINMENT_REFUSED and result.containment
            else []
        )
        non_isolated_containers = (
            [v.container for v in result.isolation.offending]
            if result.outcome is RunOutcome.ISOLATION_FAILED and result.isolation
            else []
        )

        completed = False
        completion_persisted = False
        # Completion ONLY when the execution actually completes (COMPLETED +
        # FINISHED). ABORTED/NOT_CONFIRMED/containment/isolation do NOT complete.
        # When ``complete`` is False the completion is deferred to the caller (the
        # task 8.4 seam only completes after the deprovisioning — Req. 10.9).
        if (
            complete
            and result.outcome is RunOutcome.COMPLETED
            and result.state is OperationState.FINISHED
        ):
            completion = SessionStateService(self._session).persist_stage_completion(
                campaign, STAGE_EXECUTION
            )
            completion_persisted = completion.saved
            # A persistence failure does NOT bring down the execution already
            # performed; the stage is not marked completed, but the execution
            # result is returned in full (previous stage state preserved).
            completed = completion.saved

        return ExecutionResult(
            campaign_id=campaign,
            started=result.started,
            result=result.outcome,
            state=result.state,
            operation_id=result.operation_id,
            caldera_operation_id=result.caldera_operation_id,
            total_success=result.total_success,
            total_failure=result.total_failure,
            results=results,
            completed=completed,
            completion_persisted=completion_persisted,
            violations=list(violations),
            non_isolated_containers=non_isolated_containers,
            message=result.message,
        )

    # -- helpers -------------------------------------------------------------

    def _load_case(self, campaign: str) -> CaseData:
        """Resolve and read the curated Campaign ``campaign``, raising on invalid paths.

        A *slug* outside the curated catalog is a :class:`KeyError`; a
        missing/malformed Campaign file is a descriptive :class:`ValueError` — the
        same distinction the API layer maps to 404/422 (the endpoints are task
        8.5). The returned :class:`CaseData` exposes the Abilities and the
        Adversary the execution needs.
        """
        if campaign not in self._case_service.list_cases():
            raise KeyError(f"Unknown campaign: {campaign!r}.")
        result = self._case_service.try_load_case(campaign)
        if not result.ok or result.case is None:
            detail = (
                result.error.detail
                if result.error
                else f"Failed to read Campaign {campaign!r}."
            )
            raise ValueError(detail)
        return result.case


__all__ = [
    "RunnerFactory",
    "ExecutionAbilityResult",
    "ExecutionResult",
    "ExecutionStageService",
    "CalderaUnavailable",
]
