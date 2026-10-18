"""ExecutionStageService — Stage 6 auto-start orchestration (task 7.1).

Stage 6 of the STICKS pipeline is *Execução, medição e relatório*. Unlike the
previous manual flow (click "provisionar"/"solicitar emulação"/"executar"), the
redesign starts the campaign execution **automatically** when the Researcher
enters Stage 6 (Req. 1.1). This service orchestrates that auto-start in
isolation from the execution itself: it enforces the Stage-5 prerequisite,
guarantees idempotency on re-entry, and delegates the real work to the
``OperationRunner`` through the structural :class:`OperationRunnerProtocol`
seam already defined in :mod:`app.services.stage.stage_service`.

Responsibilities of :meth:`ExecutionStageService.autostart` (Req. 1.1, 1.4, 1.5)
--------------------------------------------------------------------------------
* **Stage-5 prerequisite (Req. 1.4):** environment preparation/verification
  belongs to Stage 5 (``STAGE_ENVIRONMENT`` = 5). If the Stage-5
  :class:`~app.models.domain.StageRun` for the campaign is not ``concluido``,
  the environment is not ready: the service returns a **blocked** result,
  **does not** call the runner, and signals that the Researcher must be
  redirected back to Stage 5. The Stage-6 isolation gate itself is enforced by
  the ``OperationRunner`` (Req. 6), so this service never re-runs any Stage-5
  environment verification (Req. 5.2).
* **Idempotency (Req. 1.5):** if an :class:`~app.models.domain.Operation` in a
  terminal state (``FINISHED`` / ``ABORTED`` / ``INCOMPLETE_TIMEOUT``) is
  already persisted for the campaign, the service re-displays that persisted
  snapshot (rebuilt from the persisted ``AbilityResult`` rows + the Operation
  aggregate) **without** starting a new run.
* **Delegation (Req. 1.1):** otherwise it loads the full curated case and
  delegates to ``OperationRunner.run(case, confirmed=confirmed)`` through the
  injectable/mockable :class:`OperationRunnerProtocol`, forwarding the
  ``confirmed`` flag from the inline consent (Req. 1.6). The runner enforces the
  explicit-confirmation and isolation gates; this service only forwards the
  flag and maps the runner result into a :class:`Stage6StartResult`.

Injectable Session + runner
---------------------------
Like :class:`~app.services.stage.stage_service.StageService`, the service takes
an injected SQLAlchemy :class:`~sqlalchemy.orm.Session` (so it works with the
app's ``SessionLocal`` and an ephemeral test SQLite session) and an injected
object implementing :class:`OperationRunnerProtocol` (so the runner can be
mocked in tests). A custom :class:`~app.services.campaign_service.CaseService`
may also be injected to point at alternative local fixtures.

_Requirements: 1.1, 1.4, 1.5_
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import AbilityResult, Operation, StageRun
from app.models.enums import AbilityResultStatus, OperationState, StageState
from app.services.campaign_service import CaseService
from app.services.stage.stage_service import (
    STAGE_ENVIRONMENT,
    OperationRunnerProtocol,
    Stage3Aggregate,
    Stage3AbilityResult,
    Stage3Output,
)

# Terminal Operation states that make a Stage-6 re-entry idempotent (Req. 1.5):
# a persisted Operation already in one of these states is a *finished* run (be
# it a success, an abort or a collection timeout), so the service re-displays it
# instead of starting a new one.
TERMINAL_OPERATION_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.FINISHED,
        OperationState.ABORTED,
        OperationState.INCOMPLETE_TIMEOUT,
    }
)


class Stage6StartResult(BaseModel):
    """The typed result of a Stage-6 auto-start attempt (Req. 1.1, 1.4, 1.5).

    Exactly one of the following situations is encoded, readable from the flags:

    * **blocked** (``blocked=True``) — Stage 5 is not ``concluido`` (environment
      not ready, Req. 1.4): the execution was NOT started, no runner call was
      made, and the Researcher must be redirected back to Stage 5.
      ``redirect_to_stage`` carries ``5`` and ``message`` the reason.
    * **idempotent re-display** (``blocked=False``, ``executed=False``,
      ``reused=True``) — a terminal Operation is already persisted for the
      campaign (Req. 1.5): ``output`` carries the persisted snapshot rebuilt
      from the ``AbilityResult`` rows + Operation aggregate; no new run was
      started.
    * **started** (``blocked=False``, ``executed=True``) — the runner ran: the
      ``output`` carries the per-Ability results + aggregate the runner
      produced, and ``operation_id`` links the persisted Operation.
    * **not executed by the runner** (``blocked=False``, ``executed=False``,
      ``reused=False``) — the runner refused to execute (missing confirmation,
      containment/isolation violation, Caldera unavailable, audit failure):
      ``message`` / ``error_message`` carry the runner's reason and ``output``
      is ``None`` (Req. 1.6, 6.2).
    """

    campaign_id: str
    executed: bool = False
    blocked: bool = False
    reused: bool = False
    redirect_to_stage: Optional[int] = None
    output: Optional[Stage3Output] = None
    operation_id: Optional[int] = None
    message: Optional[str] = None
    error_message: Optional[str] = None


class ExecutionStageService:
    """Orchestrates the Stage-6 auto-start for a curated campaign (task 7.1).

    Parameters
    ----------
    session:
        An open SQLAlchemy :class:`~sqlalchemy.orm.Session`, injected by the
        caller. Used for the pure reads of the Stage-5 ``StageRun`` and the
        persisted ``Operation`` / ``AbilityResult`` rows (idempotency snapshot).
    operation_runner:
        An object implementing :class:`OperationRunnerProtocol` (the
        ``OperationRunner``). Stage 6 delegates the real execution to it; this
        service never re-implements the Caldera calls, the pre-flight gates or
        the collection loop. Required when a run is actually started.
    case_service:
        Optional :class:`~app.services.campaign_service.CaseService`. Defaults to
        a new one pointing at ``<repo>/sticks/data``. Inject a custom one (e.g.
        pointing at temp fixtures) for tests.
    """

    def __init__(
        self,
        session: Session,
        operation_runner: Optional[OperationRunnerProtocol] = None,
        case_service: Optional[CaseService] = None,
    ) -> None:
        self._session = session
        self._operation_runner = operation_runner
        self._case_service = case_service or CaseService()

    # ---- public API -------------------------------------------------------

    def autostart(
        self, campaign_id: str, *, confirmed: bool = False
    ) -> Stage6StartResult:
        """Start the Stage-6 execution for ``campaign_id`` (Req. 1.1, 1.4, 1.5).

        The orchestration is, in order:

        1. **Stage-5 prerequisite (Req. 1.4):** if Stage 5 is not ``concluido``
           the environment is not ready — return a blocked result pointing back
           to Stage 5 and do NOT run.
        2. **Idempotency (Req. 1.5):** if a terminal Operation is already
           persisted for the campaign, re-display its snapshot without a new
           run.
        3. **Delegate (Req. 1.1):** otherwise load the case and delegate to
           ``OperationRunner.run(case, confirmed=confirmed)``, forwarding the
           inline consent (Req. 1.6), and map the runner result.

        Args:
            campaign_id: The curated campaign slug.
            confirmed: The explicit inline-consent flag forwarded to the runner
                (Req. 1.6). The runner enforces the gate; this service never
                bypasses it.

        Returns:
            A typed :class:`Stage6StartResult`.

        Raises:
            RuntimeError: if a run must be started (Stage 5 concluded and no
                terminal Operation persisted) but no OperationRunner was
                injected.
        """
        # 1. Stage-5 prerequisite — environment must be ready (Req. 1.4, 5.2).
        if not self._is_stage5_concluded(campaign_id):
            return Stage6StartResult(
                campaign_id=campaign_id,
                blocked=True,
                executed=False,
                redirect_to_stage=STAGE_ENVIRONMENT,
                message=(
                    "O Estágio 5 (preparação do ambiente) não está concluído: "
                    "o ambiente não está pronto. A execução do Estágio 6 "
                    "permanece bloqueada; retorne ao Estágio 5."
                ),
            )

        # 2. Idempotency — a terminal Operation re-displays its snapshot without
        #    a new run (Req. 1.5).
        terminal = self._latest_terminal_operation(campaign_id)
        if terminal is not None:
            return Stage6StartResult(
                campaign_id=campaign_id,
                executed=False,
                reused=True,
                output=self._snapshot_from_operation(terminal),
                operation_id=terminal.id,
                message=(
                    "Já existe um resultado de execução persistido para a "
                    "campanha; reexibindo-o sem reexecutar (idempotência)."
                ),
            )

        # 3. Delegate the real execution to the runner (Req. 1.1). The runner
        #    enforces the explicit-confirmation and isolation gates (Req. 1.6,
        #    6.x); this service only forwards ``confirmed``.
        if self._operation_runner is None:
            raise RuntimeError(
                "Nenhum OperationRunner injetado: o auto-start do Estágio 6 "
                "requer um OperationRunner."
            )

        case = self._case_service.load_case(campaign_id)
        run_result = self._operation_runner.run(case, confirmed=confirmed)

        # Runner refused to execute (missing confirmation, containment/isolation
        # violation, Caldera unavailable, audit failure) — surface the reason
        # without an output (Req. 1.6, 6.2).
        if not run_result.executed:
            return Stage6StartResult(
                campaign_id=campaign_id,
                executed=False,
                reused=False,
                message=run_result.message,
                error_message=run_result.error_message,
            )

        output = Stage3Output(
            results=list(run_result.results),
            aggregate=Stage3Aggregate(
                total_success=run_result.total_success,
                total_failure=run_result.total_failure,
                state=run_result.state,
            ),
            operation_id=run_result.operation_id,
        )
        return Stage6StartResult(
            campaign_id=campaign_id,
            executed=True,
            reused=False,
            output=output,
            operation_id=run_result.operation_id,
            message=run_result.message,
        )

    # ---- helpers (pure reads over persisted rows) -------------------------

    def _is_stage5_concluded(self, campaign_id: str) -> bool:
        """Whether Stage 5 (environment) is ``concluido`` for the campaign.

        Reads the latest Stage-5 :class:`~app.models.domain.StageRun` row (the
        highest ``id`` when more than one exists, matching ``StageService``) and
        returns ``True`` only when its state is ``concluido`` (Req. 1.4, 5.2).
        A missing row means Stage 5 never completed, so the environment is not
        ready.
        """
        stmt = (
            select(StageRun.state)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == STAGE_ENVIRONMENT)
            .order_by(StageRun.id.asc())
        )
        states = list(self._session.execute(stmt).scalars().all())
        if not states:
            return False
        return states[-1] is StageState.COMPLETED

    def _latest_terminal_operation(
        self, campaign_id: str
    ) -> Optional[Operation]:
        """Return the latest terminal Operation persisted for the campaign.

        Idempotency key (Req. 1.5): an Operation whose ``state`` is one of
        :data:`TERMINAL_OPERATION_STATES` (``FINISHED`` / ``ABORTED`` /
        ``INCOMPLETE_TIMEOUT``) is a finished run. When more than one exists the
        highest-``id`` (latest) one wins. A non-terminal (``RUNNING`` /
        ``NOT_STARTED``) Operation is NOT a finished run and does not block a
        fresh auto-start.
        """
        stmt = (
            select(Operation)
            .where(Operation.campaign_id == campaign_id)
            .where(Operation.state.in_(tuple(TERMINAL_OPERATION_STATES)))
            .order_by(Operation.id.asc())
        )
        rows = list(self._session.execute(stmt).scalars().all())
        return rows[-1] if rows else None

    def _snapshot_from_operation(self, operation: Operation) -> Stage3Output:
        """Rebuild the Stage-6 snapshot from a persisted terminal Operation.

        Reads the Operation's persisted :class:`~app.models.domain.AbilityResult`
        rows in insertion order and maps them into the per-Ability view, and
        carries the persisted aggregate (``total_success`` / ``total_failure`` /
        ``state``). This is a pure read — it never re-runs the Operation
        (Req. 1.5).
        """
        stmt = (
            select(AbilityResult)
            .where(AbilityResult.operation_id == operation.id)
            .order_by(AbilityResult.id.asc())
        )
        rows = list(self._session.execute(stmt).scalars().all())
        results = [
            Stage3AbilityResult(
                ability_id=row.ability_id or "",
                status=row.status or AbilityResultStatus.PENDING,
                command_output=row.command_output,
            )
            for row in rows
        ]
        return Stage3Output(
            results=results,
            aggregate=Stage3Aggregate(
                total_success=operation.total_success,
                total_failure=operation.total_failure,
                state=operation.state,
            ),
            operation_id=operation.id,
        )


__all__ = [
    "ExecutionStageService",
    "Stage6StartResult",
    "TERMINAL_OPERATION_STATES",
]
