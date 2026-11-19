"""Background phase job of an interactive run (interactive-emulation-run, D3/D4).

One *round* of the graph (up to its next pause or its end) runs in its own
thread with its own working session, so the approval routes can answer 202 at
once. The ``agent_runs`` row is written only through ``RunStateStore`` (own
session, immediate commit), never through the working session.

Actions (``PhaseAction``):

* ``initial``: orchestrate + operationalize, pauses on ``operationalization``;
* ``approve_operationalization``: flow, pauses on ``flow``;
* ``approve_flow``: environment, pauses on ``environment``;
* ``approve_environment``: execute + evaluate; ends the run or, when the
  evaluator refines, pauses again on the re-executed phase;
* ``regenerate_flow``: only ``flow_agent.node`` on the checkpoint state, written
  back with ``update_state(as_node="flow")`` so the graph stays paused after
  ``flow``.

Concurrency (one round per run): ``dispatch_phase_job`` keeps an in-process
registry ``run_id -> thread`` guarded by a lock and refuses to start a second
job for a run whose job is alive (``PhaseJobAlreadyRunningError``). That lock is
the fast, local guard. The authoritative guard across requests is the atomic
conditional ``UPDATE`` (``awaiting_approval`` -> ``running``) done by the routes
before dispatching (task 4.1), which also holds across processes; the registry
covers the window where a job is still finishing after the row was already
parked.

Every exit leaves the run out of ``running`` (``ensure_not_running`` in the
``finally``). When the round leaves the run terminal, the ``finally`` also stops
the run's Caldera operation and deprovisions its lab (``cleanup_run_resources``,
D7), best effort and without changing the recorded status or error.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain import AgentRun
from app.models.enums import AgentRunStatus
from app.services.ai.run_service import AgentRunService, RunFailedError
from app.services.ai.run_state import RunStateStore, phase_for_node

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

NOTICE_NO_DISTINCT_FLOW = "no_distinct_flow"
NOTICE_REGENERATION_FAILED = "regeneration_failed"


class PhaseAction(StrEnum):
    """What a phase job does; the value is the action name used by the routes."""

    INITIAL = "initial"
    APPROVE_OPERATIONALIZATION = "approve_operationalization"
    APPROVE_FLOW = "approve_flow"
    APPROVE_ENVIRONMENT = "approve_environment"
    REGENERATE_FLOW = "regenerate_flow"


# Phase shown as active while the action runs.
_ACTIVE_PHASE: dict[PhaseAction, str] = {
    PhaseAction.INITIAL: "orchestration",
    PhaseAction.APPROVE_OPERATIONALIZATION: "flow",
    PhaseAction.APPROVE_FLOW: "environment",
    PhaseAction.APPROVE_ENVIRONMENT: "execution",
    PhaseAction.REGENERATE_FLOW: "flow",
}


def active_phase_of(action: PhaseAction | str) -> str:
    """Phase shown as active while ``action`` runs (the ``current_phase`` it sets)."""
    return _ACTIVE_PHASE[PhaseAction(action)]


class PhaseJobAlreadyRunningError(RuntimeError):
    """Raised when a job for the same run is still alive."""

    def __init__(self, run_id: int) -> None:
        self.run_id = run_id
        super().__init__(f"a phase job for run {run_id} is already running.")


_registry_lock = threading.Lock()
_active_jobs: dict[int, threading.Thread] = {}


def is_job_active(run_id: int) -> bool:
    """Return whether a phase job thread of ``run_id`` is alive in this process."""
    with _registry_lock:
        thread = _active_jobs.get(run_id)
        return thread is not None and thread.is_alive()


def dispatch_phase_job(
    run_id: int,
    action: PhaseAction | str,
    *,
    session_factory: SessionFactory | None = None,
) -> threading.Thread:
    """Start the job of ``action`` for ``run_id`` in a background thread.

    The thread runs inside a copy of the caller's ``contextvars`` context. The
    lock-protected registry guarantees at most one live job per run.

    Raises:
        PhaseJobAlreadyRunningError: a job of the same run is still alive.
    """
    resolved = PhaseAction(action)
    context = contextvars.copy_context()

    def target() -> None:
        try:
            context.run(run_phase_job, run_id, resolved, session_factory=session_factory)
        finally:
            with _registry_lock:
                if _active_jobs.get(run_id) is threading.current_thread():
                    del _active_jobs[run_id]

    thread = threading.Thread(target=target, name=f"phase-job-run-{run_id}", daemon=True)
    with _registry_lock:
        existing = _active_jobs.get(run_id)
        if existing is not None and existing.is_alive():
            raise PhaseJobAlreadyRunningError(run_id)
        _active_jobs[run_id] = thread
        # Started under the lock so the registry never holds an unstarted thread
        # that a concurrent dispatch could mistake for a finished one.
        thread.start()
    return thread


def run_phase_job(
    run_id: int,
    action: PhaseAction | str,
    *,
    session_factory: SessionFactory | None = None,
) -> None:
    """Run one round of the graph for ``run_id`` and record its outcome.

    Never raises: every failure is written to the run row (``failed``, or back to
    ``awaiting_approval`` with a notice for a failed regeneration) and logged.
    """
    resolved = PhaseAction(action)
    factory = session_factory
    if factory is None:
        from app.db.session import SessionLocal

        factory = SessionLocal
    store = RunStateStore(factory)
    phase = _ACTIVE_PHASE[resolved]
    try:
        with factory() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                logger.error("Phase job for run %s: the run does not exist.", run_id)
                return
            profile = _frozen_profile(run)
            if profile is None:
                store.fail(
                    run_id,
                    phase,
                    code="profile_unavailable",
                    error=f"LLM profile {run.llm_profile_id!r} is no longer available.",
                )
                return

            from app.services.ai.llm_selection import run_profile

            store.mark_running(run_id, phase)
            service = AgentRunService(session, store)
            with run_profile(profile):
                if resolved is PhaseAction.REGENERATE_FLOW:
                    _regenerate_flow(service, session, run, store)
                else:
                    graph_input = (
                        service._initial_state(run) if resolved is PhaseAction.INITIAL else None
                    )
                    service._run_until_pause(run, graph_input)
    except RunFailedError:
        # ``_run_until_pause`` already wrote ``failed`` with the failing node.
        logger.info("Phase job %s of run %s ended in failure.", resolved.value, run_id)
    except Exception as exc:
        logger.exception("Phase job %s of run %s crashed.", resolved.value, run_id)
        node = getattr(exc, "node", None)
        store.fail(
            run_id,
            (phase_for_node(node) or node) if isinstance(node, str) else phase,
            code="phase_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        _cleanup_if_terminal(run_id, factory, store)
        store.ensure_not_running(run_id)


_TERMINAL_STATUSES = frozenset(
    {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.ABORTED}
)
_CLEANUP_ABORT_REASON = "run_ended"


def _cleanup_if_terminal(run_id: int, factory: SessionFactory, store: RunStateStore) -> None:
    """Release the run's resources once the run reached a terminal state (D7)."""
    try:
        with factory() as session:
            run = session.get(AgentRun, run_id)
            terminal = run is not None and run.status in _TERMINAL_STATUSES
        if terminal:
            cleanup_run_resources(run_id, session_factory=factory, store=store)
    except Exception:
        logger.exception("Cleanup check of run %s failed.", run_id)


def cleanup_run_resources(
    run_id: int,
    *,
    session_factory: SessionFactory | None = None,
    store: RunStateStore | None = None,
) -> None:
    """Stop the Caldera operation of the run and tear down its lab, best effort.

    Used when a run ends (``completed``, ``failed``, ``aborted``), including the
    researcher abort. It never raises and never changes the run status, so the
    original error stays visible. The teardown only touches the lab services
    (``ComposeExecutor.down`` names them); ``sticks-postgres`` and its volume are
    never removed. A failed teardown records ``remaining_resources`` on the run.
    """
    factory = session_factory
    if factory is None:
        from app.db.session import SessionLocal

        factory = SessionLocal
    state_store = store or RunStateStore(factory)
    try:
        _abort_caldera_operations(run_id, factory)
    except Exception:
        logger.warning("Could not stop the Caldera operation of run %s.", run_id, exc_info=True)
    try:
        _deprovision_lab(run_id, factory, state_store)
    except Exception:
        logger.warning("Could not tear down the lab of run %s.", run_id, exc_info=True)


def _abort_caldera_operations(run_id: int, factory: SessionFactory) -> None:
    """Abort in Caldera the operations of the run's campaign still marked running."""
    from sqlalchemy import select

    from app.models.domain import Operation
    from app.models.enums import OperationState

    with factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        stmt = select(Operation).where(
            Operation.campaign_id == run.campaign_id,
            Operation.state == OperationState.RUNNING,
            Operation.caldera_operation_id.is_not(None),
        )
        operations = list(session.scalars(stmt))
        if not operations:
            return

        from app.models.domain import _utcnow
        from app.services.caldera import CalderaClient

        caldera = CalderaClient()
        try:
            for operation in operations:
                try:
                    caldera.abort_operation(str(operation.caldera_operation_id))
                except Exception:
                    logger.warning(
                        "Could not abort Caldera operation %s.",
                        operation.caldera_operation_id,
                        exc_info=True,
                    )
                operation.state = OperationState.ABORTED
                operation.abort_reason = _CLEANUP_ABORT_REASON
                operation.finished_at = _utcnow()
            session.commit()
        finally:
            close = getattr(caldera, "close", None)
            if callable(close):
                close()


def _deprovision_lab(run_id: int, factory: SessionFactory, store: RunStateStore) -> None:
    """Deprovision the environment of the run's checkpoint, if one was provisioned."""
    from app.services.ai.agents.env_agent import AiEnvironmentProvisioner
    from app.services.environment.provisioner import ProvisionedEnvironment

    with factory() as session:
        run = session.get(AgentRun, run_id)
        if run is None:
            return
        graph = AgentRunService(session, store)._build_graph()
        snapshot = graph.get_state({"configurable": {"thread_id": run.thread_id}})
    payload = ((snapshot.values or {}).get("artifacts") or {}).get("environment")
    if not payload:
        return
    environment = ProvisionedEnvironment.model_validate(payload)
    result = AiEnvironmentProvisioner().deprovision(environment)
    if not result.deprovisioned:
        logger.warning("Lab of run %s not fully removed: %s", run_id, result.message)
        store.record_remaining_resources(run_id, result.remaining_resources)


def _frozen_profile(run: AgentRun) -> Any | None:
    """Rebuild the profile frozen at run creation (``None`` when it vanished)."""
    from app.services.ai.llm_profiles import build_work_profile, find_profile

    if not run.llm_profile_id:
        return build_work_profile()
    return find_profile(run.llm_profile_id)


def _regenerate_flow(
    service: AgentRunService, session: Session, run: AgentRun, store: RunStateStore
) -> None:
    """Produce another attack flow without re-running any graph node (D2, D5).

    A failure keeps the current proposal: the run goes back to
    ``awaiting_approval`` on ``flow`` with a notice instead of ``failed``.
    """
    from app.services.ai.agents import flow_agent

    run_id = run.id
    config = {"configurable": {"thread_id": run.thread_id}}
    graph = service._build_graph()
    snapshot = graph.get_state(config)
    state = dict(snapshot.values or {})
    state.update(_exclusions(run_id, state, store))
    try:
        delta = flow_agent.node(state, session=session)
        graph.update_state(config, delta, as_node="flow")
        merged = {**state, **delta}
        service.persist_phase(run, "flow", merged)
    except Exception as exc:  # noqa: BLE001 -- any regeneration failure keeps the proposal
        session.rollback()
        notice = (
            NOTICE_NO_DISTINCT_FLOW
            if isinstance(exc, flow_agent.NoDistinctFlowError)
            else NOTICE_REGENERATION_FAILED
        )
        logger.warning(
            "Regeneration of the flow of run %s failed (%s: %s); keeping the current proposal.",
            run_id,
            type(exc).__name__,
            exc,
        )
        store.set_notice(run_id, notice)
    store.pause_for_approval(run_id, "flow")


def _exclusions(run_id: int, state: dict, store: RunStateStore) -> dict[str, list]:
    """Union of the flows already presented (run history plus checkpoint state)."""
    history = store.flow_exclusions(run_id)
    flow_ids = list(state.get("excluded_flow_ids") or [])
    hashes = list(state.get("excluded_hashes") or [])
    flow_ids += [i for i in history["excluded_flow_ids"] if i not in flow_ids]
    hashes += [h for h in history["excluded_hashes"] if h not in hashes]
    return {"excluded_flow_ids": flow_ids, "excluded_hashes": hashes}
