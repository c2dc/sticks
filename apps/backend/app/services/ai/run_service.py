"""Session-injected orchestration and persistence of AI curation runs.

Hosts ``AgentRunService`` which creates ``agent_runs`` rows, invokes the
pausable compiled graph with the checkpointer, persists each phase output
atomically and supports resume from the last checkpoint (tasks 15.1-15.3).

Interactive runs (interactive-emulation-run, tasks 3.3 and 4.1): ``start`` creates
the row and dispatches the ``initial`` phase job without running the graph;
``approve``, ``regenerate``, ``select_proposal`` and ``abort`` move a paused run
under an atomic guard, and ``active_run`` finds the active run of a campaign.
The phase job (and ``resume``) invoke the graph once, up to its next pause. The result is written to the run
row through ``RunStateStore`` (``mark_running`` / ``pause_for_approval`` /
``finish`` / ``fail``), which commits in its own session, and ``persist_phase``
stores the output of the phase that just ran. The older text below describes the
synchronous flow that always ran to the end; ``COMPLETED`` / ``ABORTED`` is now
set only when the graph ends.

The service receives its ``Session`` by injection and builds the graph,
checkpointer and LLM lazily; importing this module performs no I/O
(Requirement 1.7).

Task scope (15.1 + 15.2): this module implements run **creation**, graph
**invocation**, the terminal **status transitions**, the atomic
**per-iteration persistence**, **resume** from the last checkpoint and the
**dependency-availability** precheck —

* ``start(campaign_id, max_iterations)`` creates the ``agent_runs`` row with a
  non-empty, unique, immutable ``thread_id`` (Req. 4.2, 11.4), invokes the
  compiled graph with the PostgreSQL checkpointer over
  ``{"configurable": {"thread_id": run.thread_id}}``, and marks the run
  ``COMPLETED`` on a passing final verdict or ``ABORTED`` when the max-iteration
  guard stopped the loop without one (Req. 3.2, 3.4);
* ``resume(run_id)`` restores a run from its last checkpoint — it re-invokes the
  compiled graph under the run's own ``thread_id`` with the LangGraph resume
  idiom ``invoke(None, config)`` so execution continues at the node following
  the last persisted checkpoint (Req. 4.5), then persists and marks the outcome
  exactly like ``start``;
* both entry points first assert the checkpointer and the pgvector store are
  reachable; if either is unavailable the service reports the unavailability
  naming the failed dependency and does NOT start or resume (Req. 14.5);
* :meth:`_persist_iterations` persists, for the completed iteration(s) in the
  final state, one ``agent_iterations`` row plus its ``agent_phase_outputs``
  rows (and the ``agent_evaluations`` row when an evaluation is present) inside a
  SINGLE atomic transaction — a nested transaction (``SAVEPOINT``) — rolling back
  so no partial iteration rows are retained on any failure and raising a
  :class:`PersistenceError` reporting the failure (Req. 11.5, 11.6, 11.7, 11.8).

Task 15.3 (this change) adds the **SEPINF-failure / ``FAILED`` handling** and the
**startup seam fallback**:

* ``start``/``resume`` wrap the graph invocation so that when SEPINF is
  unreachable or exceeds the configured read timeout (``LLM_READ_TIMEOUT_SECONDS``), the model returns an error, an
  LLM output cannot be parsed into a valid attack flow/command set after the one repair
  (``FlowGenerationError`` and analogous node errors), or a phase cannot be filled
  because its required Agent_Input is absent, the run is flipped to ``FAILED``
  recording a failure cause that identifies the FAILING PHASE, NO FULL AI artifact
  is persisted for the failing node (``_persist_iterations`` is skipped entirely
  on failure) and, once ``FAILED``, the run is never transitioned to a
  successful/completed status (Req. 2.6, 4.4, 14.1, 14.2, 14.6). The structured
  failure cause (failing phase + absent-input/cause) is written to the nullable
  ``agent_runs.error`` column (task 3.2) and ``final_verdict_success`` is left
  null; when the failing node captured a partial Audit_Trace in its sink up to the
  failure, ``_fail_run`` also persists that partial trace with the failing agent's
  identity — anchored on a minimal ``agent_phase_outputs`` row and its 1:1
  ``agent_phase_traces`` row inside a SAVEPOINT — so the failure is auditable
  (Req. 2.6). The AI path is the only processing path: ``_fail_run`` does NOT fall
  back to human curation for the Run (Req. 4.4).
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any

from app.services.ai.llm_failure import classify_llm_failure

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from sqlalchemy.orm import Session

    from app.models.domain import AgentIteration, AgentRun
    from app.models.enums import AgentPhase
    from app.services.ai.run_state import RunStateStore

logger = logging.getLogger(__name__)

# Default max-iteration guard applied when the caller passes no explicit value
# and configuration cannot be read; mirrors ``SepinfSettings.max_iterations``
# (Req. 3.6). The configured value is preferred via :func:`get_sepinf_settings`.
_DEFAULT_MAX_ITERATIONS = 3

# Inclusive bounds on the per-run iteration guard (Req. 3.5): a run performs
# ``1..max_iterations`` iterations, so the guard itself is bounded to [1, 100].
_MIN_MAX_ITERATIONS = 1
_MAX_MAX_ITERATIONS = 100

# Approvable phase -> ``PhaseAction`` value of the job it starts (the enum lives in
# ``phase_job``, which imports this module).
_APPROVE_ACTIONS = {
    "operationalization": "approve_operationalization",
    "flow": "approve_flow",
    "environment": "approve_environment",
}

# Run states in which no phase can run anymore.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted"})


class DependencyUnavailableError(RuntimeError):
    """Raised when the checkpointer or pgvector store is unavailable (Req. 14.5).

    A run cannot start or resume without its short-term checkpoint store (the
    LangGraph PostgreSQL checkpointer) and its long-term pgvector experience
    store. When either is unreachable at the moment a run is requested the
    service raises this error, which NAMES the unavailable dependency
    (``"checkpointer"`` or ``"pgvector"``) and reports the underlying cause,
    and the run is neither started nor resumed (no ``agent_runs`` row is created
    and no graph is invoked).
    """

    def __init__(self, dependency: str, cause: BaseException | None = None) -> None:
        self.dependency = dependency
        self.cause = cause
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None else ""
        super().__init__(
            f"required dependency '{dependency}' is unavailable; the run cannot "
            f"start or resume{detail}."
        )


class PersistenceError(RuntimeError):
    """Raised when per-iteration persistence fails and was rolled back (Req. 11.7).

    When persisting a completed iteration's rows (the ``agent_iterations`` row,
    its ``agent_phase_outputs`` rows and the optional ``agent_evaluations`` row)
    fails for any reason, the whole write is rolled back so no partial iteration
    rows are retained, and this error is raised to report the persistence failure
    to the caller. It carries the originating exception as ``cause``.
    """

    def __init__(self, message: str, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(message)


class RunFailedError(RuntimeError):
    """Raised when the graph invocation fails and the run was marked ``FAILED``.

    Signals Requirement 14.1/14.2/14.4: SEPINF was unreachable or timed out, the
    model returned an error, or an LLM output could not be parsed into a valid
    Attack flow/command set after the single permitted repair. Before this error is
    raised the owning ``agent_runs`` row has already been flipped to ``FAILED``
    with its failing-node cause recorded (logged; ``final_verdict_success`` left
    null), and NO AI artifact has been persisted for the failing node — the
    per-iteration persistence is skipped entirely on failure (Req. 14.1, 14.2,
    14.6).

    The exception carries the ``node`` that failed (e.g. ``"flow"``) and the
    originating ``cause`` so callers (the API layer) can surface an error
    response identifying the failing node without mutating the run further.
    """

    def __init__(self, node: str, cause: BaseException | None = None) -> None:
        self.node = node
        self.cause = cause
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None else ""
        super().__init__(
            f"the AI curation run failed at node '{node}'; the run was marked "
            f"FAILED and no AI artifact was persisted for it{detail}."
        )


class RunTransitionError(RuntimeError):
    """Raised when an approval, regeneration or selection cannot be applied.

    ``code`` is the contract code the route maps to an HTTP status: 404
    ``run_not_found`` / ``flow_proposal_not_found``; 409 ``wrong_phase``,
    ``phase_running`` or ``run_not_awaiting``; 422 ``invalid_phase``.
    """

    NOT_FOUND = "run_not_found"
    PROPOSAL_NOT_FOUND = "flow_proposal_not_found"
    WRONG_PHASE = "wrong_phase"
    PHASE_RUNNING = "phase_running"
    NOT_AWAITING = "run_not_awaiting"
    INVALID_PHASE = "invalid_phase"

    def __init__(self, code: str, run_id: int) -> None:
        self.code = code
        self.run_id = run_id
        super().__init__(f"run {run_id}: {code}")


class RunAlreadyActiveError(RuntimeError):
    """Raised by ``start`` when the campaign already has an active run (409 ``run_already_active``)."""

    def __init__(self, campaign_id: str, run_id: int) -> None:
        self.campaign_id = campaign_id
        self.run_id = run_id
        super().__init__(f"campaign {campaign_id!r} already has an active run ({run_id}).")


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp, matching the model/service convention.

    The AI bookkeeping rows stamp their timestamps with a timezone-naive UTC
    value (see ``app.models.domain._utcnow``) so the columns stay portable
    between PostgreSQL (the product database) and the ephemeral SQLite used by
    the unit tests.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class AgentRunService:
    """Session-injected orchestration + persistence of AI curation runs.

    Creates the ``agent_runs`` row (with a ``thread_id``), invokes the compiled
    graph with the PostgreSQL checkpointer, and marks the final status —
    ``COMPLETED`` on a passing verdict, ``ABORTED`` when the max-iteration guard
    stops the loop without one (Req. 3.2, 3.4, 4.2, 11.4).

    The service follows the project's service conventions: it is constructed
    with an open SQLAlchemy :class:`~sqlalchemy.orm.Session` and builds the
    graph / checkpointer lazily, so constructing it and importing this module
    perform no I/O (Req. 1.7).
    """

    def __init__(self, session: Session, state_store: RunStateStore | None = None) -> None:
        """Bind the service to an open :class:`~sqlalchemy.orm.Session`.

        No graph, checkpointer or LLM is built here — all heavy collaborators
        are resolved lazily on :meth:`start` so construction is side-effect free
        (Req. 1.7).

        Args:
            session: The working session that runs the graph and stores the
                phase outputs.
            state_store: Writer of the ``agent_runs`` transitions (own session,
                immediate commit). Defaults to a store on ``SessionLocal``.
        """
        from app.services.ai.run_state import RunStateStore

        self._session = session
        self._state_store = state_store if state_store is not None else RunStateStore()

    # -- public API -----------------------------------------------------------

    def start(
        self,
        campaign_id: str,
        *,
        max_iterations: int | None = None,
        llm_profile_id: str | None = None,
    ) -> AgentRun:
        """Create an ``agent_runs`` row and start its first phase job (no graph here).

        Steps (Req. 3.2, 3.3, 4.1, 4.2, 5.1, 11.4):

        1. Probe the checkpointer and the pgvector store; an unavailable
           dependency raises :class:`DependencyUnavailableError` and no row is
           created (Req. 14.5).
        2. Refuse a second active run of the campaign
           (:class:`RunAlreadyActiveError`).
        3. Create the ``agent_runs`` row (``uuid4`` hex ``thread_id``, resolved
           ``max_iterations`` clamped to ``1..100``, frozen ``llm_profile_id``) and
           commit it at once, so the phase job and the
           :class:`~app.services.ai.run_state.RunStateStore`, which write in their
           own sessions, can see it.
        4. Dispatch the ``initial`` phase job (orchestrate + operationalize) and
           return without waiting. The run is ``running`` on ``orchestration`` and
           the job parks it on ``operationalization`` (``awaiting_approval``).

        Args:
            campaign_id: Campaign the run emulates.
            max_iterations: Loop guard; the configured default when ``None``.
            llm_profile_id: Profile frozen for the whole run; the persisted
                selection when ``None``.

        Raises:
            DependencyUnavailableError: a required dependency is unavailable.
            RunAlreadyActiveError: the campaign already has an active run.
        """
        from app.services.ai import phase_job

        # Report checkpointer/pgvector unavailability BEFORE creating the run row
        # or invoking the graph (Req. 14.5).
        self._require_dependencies()

        active = self.active_run(campaign_id)
        if active is not None:
            raise RunAlreadyActiveError(campaign_id, active.id)

        if llm_profile_id is None:
            from app.services.ai.llm_selection import load_selected_profile_id

            llm_profile_id = load_selected_profile_id(self._session)

        resolved_max = self._resolve_max_iterations(max_iterations)
        run = self._create_run(campaign_id, resolved_max, llm_profile_id=llm_profile_id)
        # The store opens its own sessions: the row has to be committed first.
        self._session.commit()

        store = self._state_store
        try:
            phase_job.dispatch_phase_job(
                run.id, phase_job.PhaseAction.INITIAL, session_factory=store.session_factory
            )
        except Exception as exc:
            # Never leave a run that nothing is going to execute.
            store.fail(
                run.id,
                phase_job.active_phase_of(phase_job.PhaseAction.INITIAL),
                code="dispatch_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            self._session.refresh(run)
            raise
        self._session.refresh(run)
        return run

    def active_run(self, campaign_id: str) -> AgentRun | None:
        """Return the newest non-terminal run of the campaign, or ``None``.

        Active means ``pending``, ``running`` or ``awaiting_approval``. The row is
        read fresh from the database (the phase job writes it through other
        sessions).
        """
        from sqlalchemy import select

        from app.models.domain import AgentRun
        from app.models.enums import AgentRunStatus

        active_statuses = (
            AgentRunStatus.PENDING,
            AgentRunStatus.RUNNING,
            AgentRunStatus.AWAITING_APPROVAL,
        )
        return self._session.scalars(
            select(AgentRun)
            .where(AgentRun.campaign_id == campaign_id, AgentRun.status.in_(active_statuses))
            .order_by(AgentRun.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        ).first()

    def abort(self, run_id: int) -> AgentRun:
        """Abort a run parked on an approval, then release its resources.

        The status change is one atomic conditional update (``awaiting_approval``
        -> ``aborted``), so an abort cannot overlap an approval, a regeneration
        or a selection, and aborting twice aborts once. Only after it wins, the
        Caldera operation is stopped and the lab torn down
        (:func:`~app.services.ai.phase_job.cleanup_run_resources`, best effort:
        it never raises and never changes the recorded status).

        Raises:
            RunTransitionError: ``run_not_found``, ``phase_running`` (a phase is
                executing) or ``run_not_awaiting`` (the run is not paused).
        """
        from app.services.ai import phase_job

        self._require_run(run_id)
        store = self._state_store
        if not store.abort_run(run_id):
            run = self._require_run(run_id)
            code = (
                RunTransitionError.PHASE_RUNNING
                if run.status.value == "running"
                else RunTransitionError.NOT_AWAITING
            )
            raise RunTransitionError(code, run_id)
        phase_job.cleanup_run_resources(run_id, session_factory=store.session_factory, store=store)
        self._session.expire_all()
        return self._require_run(run_id)

    def resume(self, run_id: int) -> AgentRun:
        """Continue a paused run from its last checkpoint up to the next pause (Req. 4.5).

        Re-invokes the compiled graph under the run's OWN ``thread_id`` with the
        LangGraph resume idiom ``invoke(None, config)``: execution continues at
        the node after the checkpoint, and the outcome is reflected on the row
        like in :meth:`start`.

        Raises:
            DependencyUnavailableError: a required dependency is unavailable.
            RunFailedError: the resumed graph failed; the run was marked ``failed``.
            ValueError: no row exists for ``run_id`` or the run is already terminal.
        """
        from app.models.domain import AgentRun

        self._require_dependencies()

        run = self._session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"no agent_runs row exists for run_id={run_id!r}.")
        if run.status.value in _TERMINAL_STATUSES:
            raise ValueError(f"run {run_id!r} is already {run.status.value} and cannot resume.")

        self._run_until_pause(run, None)
        return run

    # -- approvals, regeneration and selection (D4b, D9) ----------------------

    def approve(self, run_id: int, phase: str) -> AgentRun:
        """Approve ``phase`` and start the next round in a background phase job.

        The authoritative guard is one atomic conditional update
        (``awaiting_approval`` on ``phase`` -> ``running``) committed before the
        job is dispatched, so approving twice starts one round.

        Args:
            run_id: The run to advance.
            phase: ``operationalization``, ``flow`` or ``environment``.

        Returns:
            The run, refreshed; it is ``running`` on the phase now executing.

        Raises:
            RunTransitionError: ``invalid_phase``, ``run_not_found``,
                ``phase_running``, ``wrong_phase`` or ``run_not_awaiting``.
        """
        from app.services.ai import phase_job

        action = _APPROVE_ACTIONS.get(phase)
        if action is None:
            raise RunTransitionError(RunTransitionError.INVALID_PHASE, run_id)
        next_phase = phase_job.active_phase_of(action)
        approval: dict[str, Any] = {"phase": phase, "approved_at": _utcnow().isoformat()}
        if phase == "flow":
            current = self._current_proposal_id(self._require_run(run_id))
            if current is not None:
                approval["proposal_id"] = current
        return self._begin_and_dispatch(run_id, phase, next_phase, action, approval)

    def regenerate(self, run_id: int) -> AgentRun:
        """Start the generation of another attack flow while paused on ``flow``.

        Same atomic guard and dispatch as :meth:`approve`, without an approval entry.

        Raises:
            RunTransitionError: ``run_not_found``, ``phase_running``,
                ``wrong_phase`` or ``run_not_awaiting``.
        """
        from app.services.ai import phase_job

        action = phase_job.PhaseAction.REGENERATE_FLOW
        return self._begin_and_dispatch(run_id, "flow", phase_job.active_phase_of(action), action)

    def select_proposal(self, run_id: int, proposal_id: int) -> AgentRun:
        """Go back to a proposal of the attack flow history instead of regenerating.

        Runs in the request, without the language model. It takes the same atomic
        ``awaiting_approval`` -> ``running`` guard as an approval, so it cannot
        overlap an approval or a regeneration, rewrites ``artifacts.flow`` in the
        checkpoint (``update_state(..., as_node="flow")``), replaces the stored flow
        phase output, moves the ``current`` mark and parks the run on ``flow`` again.

        Raises:
            RunTransitionError: ``run_not_found``, ``flow_proposal_not_found``,
                ``phase_running``, ``wrong_phase`` or ``run_not_awaiting``.
        """
        run = self._require_run(run_id)
        proposal = next(
            (
                p
                for p in run.flow_proposals or []
                if isinstance(p, dict) and p.get("proposal_id") == proposal_id
            ),
            None,
        )
        if proposal is None:
            raise RunTransitionError(RunTransitionError.PROPOSAL_NOT_FOUND, run_id)

        store = self._state_store
        if not store.begin_phase(run_id, "flow", "flow"):
            raise self._rejection(run_id, "flow")
        try:
            payload = self._load_proposal_payload(run, proposal)
            config = {"configurable": {"thread_id": run.thread_id}}
            graph = self._build_graph()
            state = dict((graph.get_state(config)).values or {})
            artifacts = {**(state.get("artifacts") or {}), "flow": payload}
            graph.update_state(config, {"artifacts": artifacts}, as_node="flow")
            # No trace: the checkpoint traces belong to the flow generated last.
            self.persist_phase(
                run,
                "flow",
                {**state, "artifacts": artifacts, "traces": []},
                record_proposal=False,
            )
            store.select_flow_proposal(run_id, proposal_id)
        finally:
            store.pause_for_approval(run_id, "flow")
            self._session.expire_all()
        return self._require_run(run_id)

    def _begin_and_dispatch(
        self,
        run_id: int,
        phase: str,
        next_phase: str,
        action: Any,
        approval: dict[str, Any] | None = None,
    ) -> AgentRun:
        """Take the atomic guard for ``phase`` and dispatch the job of ``action``."""
        from app.services.ai import phase_job

        self._require_run(run_id)
        store = self._state_store
        if not store.begin_phase(run_id, phase, next_phase, approval=approval):
            raise self._rejection(run_id, phase)
        try:
            phase_job.dispatch_phase_job(run_id, action, session_factory=store.session_factory)
        except phase_job.PhaseJobAlreadyRunningError as exc:
            # The previous job thread is still finishing: give the approval back.
            store.release_phase(run_id, next_phase, phase, undo_approval=approval is not None)
            raise RunTransitionError(RunTransitionError.PHASE_RUNNING, run_id) from exc
        except Exception:
            store.release_phase(run_id, next_phase, phase, undo_approval=approval is not None)
            raise
        self._session.expire_all()
        return self._require_run(run_id)

    def _require_run(self, run_id: int) -> AgentRun:
        """Load the run fresh from the database or raise ``run_not_found``."""
        from app.models.domain import AgentRun

        run = self._session.get(AgentRun, run_id, populate_existing=True)
        if run is None:
            raise RunTransitionError(RunTransitionError.NOT_FOUND, run_id)
        return run

    def _rejection(self, run_id: int, phase: str) -> RunTransitionError:
        """Name why the atomic guard of ``phase`` matched no row (re-reading the run)."""
        run = self._require_run(run_id)
        status = run.status.value
        if status == "running":
            code = RunTransitionError.PHASE_RUNNING
        elif status == "awaiting_approval" and run.current_phase != phase:
            code = RunTransitionError.WRONG_PHASE
        else:
            code = RunTransitionError.NOT_AWAITING
        return RunTransitionError(code, run_id)

    @staticmethod
    def _current_proposal_id(run: AgentRun) -> int | None:
        """``proposal_id`` of the proposal marked ``current``, if any."""
        for proposal in run.flow_proposals or []:
            if isinstance(proposal, dict) and proposal.get("current"):
                value = proposal.get("proposal_id")
                return value if isinstance(value, int) else None
        return None

    def _load_proposal_payload(self, run: AgentRun, proposal: dict) -> dict:
        """Rebuild the serialized ``ProducedFlow`` of a history entry.

        The content and provenance come from the ``attack_flows`` row stored when
        the proposal was presented; ``gaps`` come from the history entry.
        """
        from sqlalchemy import select

        from app.models.domain import AttackFlowRow
        from app.models.enums import FlowOrigin
        from app.services.flow.generator import ProducedFlow

        identifier = proposal.get("identifier")
        origin = proposal.get("origin")
        row = None
        if identifier and origin:
            row = self._session.scalars(
                select(AttackFlowRow).where(
                    AttackFlowRow.campaign_id == run.campaign_id,
                    AttackFlowRow.origin == FlowOrigin(origin),
                    AttackFlowRow.identifier == identifier,
                )
            ).first()
        if row is None:
            raise RunTransitionError(RunTransitionError.PROPOSAL_NOT_FOUND, run.id)
        return ProducedFlow(
            origin=row.origin,
            identifier=row.identifier,
            label=row.label,
            content=row.content or {},
            gaps=list(proposal.get("gaps") or []),
            provenance=row.provenance,
        ).model_dump()

    def _run_until_pause(self, run: AgentRun, graph_input: dict | None) -> None:
        """Invoke the graph once and project its outcome on the run row.

        ``graph_input`` is the seeded state on the first call and ``None`` when
        resuming a checkpoint. After the invocation the checkpoint decides what
        happens (all row writes go through the :class:`RunStateStore`):

        * paused before a node: the phase output awaiting approval is persisted
          with :meth:`persist_phase`, then ``pause_for_approval``;
        * ended: the evaluation is persisted and the run is finished as
          ``completed`` (passing verdict) or ``aborted`` (guard stopped the loop);
        * any exception: ``fail`` with the failing node (no full artifact is
          persisted for it, only the partial trace) and a
          :class:`RunFailedError` is raised.

        ``ensure_not_running`` runs last so the run never stays ``running``.
        """
        from app.models.enums import AgentRunStatus
        from app.services.ai.run_state import pending_phase_from_next

        store = self._state_store
        run_id = run.id
        max_iterations = run.max_iterations
        config = {"configurable": {"thread_id": run.thread_id}}
        try:
            store.mark_running(run_id)
            graph = self._build_graph()
            graph.invoke(graph_input, config=config)
            snapshot = graph.get_state(config)
            state = dict(snapshot.values or {})
            pending = pending_phase_from_next(tuple(getattr(snapshot, "next", ()) or ()))

            # Persist first: a run is only parked for approval once the output the
            # researcher is going to review is stored.
            self.persist_phase(run, pending or "evaluation", state)

            if pending is not None:
                store.project_checkpoint(run_id, graph, config)
            else:
                verdict_success = self._verdict_success(state)
                store.project_achieved_coverage(run_id, state)
                store.finish(
                    run_id,
                    AgentRunStatus.COMPLETED if verdict_success else AgentRunStatus.ABORTED,
                    verdict_success=verdict_success,
                    iterations_used=self._iterations_used(state, max_iterations),
                )
        except Exception as exc:
            self._session.rollback()
            raise self._fail_run(run, exc) from exc
        finally:
            store.ensure_not_running(run_id)
            # The store wrote through other sessions; reload the working copy.
            self._session.refresh(run)

    # -- run creation ---------------------------------------------------------

    def _create_run(
        self, campaign_id: str, max_iterations: int, *, llm_profile_id: str | None = None
    ) -> AgentRun:
        """Create and persist the ``agent_runs`` row in ``RUNNING`` (Req. 11.4).

        The row is created in ``PENDING`` with a freshly minted, non-empty,
        unique, immutable ``thread_id`` (a ``uuid4`` hex — unique by
        construction and never rewritten for the life of the run), then flipped
        to ``RUNNING`` and flushed so the row and its ``thread_id`` are
        addressable before the graph is invoked (the checkpointer snapshots the
        run under this thread). ``iterations_used`` starts at 0 and
        ``final_verdict_success`` is left null until the run reaches its terminal
        state.
        """
        from app.models.domain import AgentRun
        from app.models.enums import AgentRunStatus

        now = _utcnow()
        run = AgentRun(
            campaign_id=campaign_id,
            status=AgentRunStatus.PENDING,
            max_iterations=max_iterations,
            iterations_used=0,
            thread_id=self._new_thread_id(),
            llm_profile_id=llm_profile_id,
            current_phase="orchestration",
            final_verdict_success=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(run)
        # Flush so the row exists (and its thread_id is persisted) before the
        # graph runs; the PENDING -> RUNNING transition marks the run in-flight.
        self._session.flush()
        run.status = AgentRunStatus.RUNNING
        run.updated_at = _utcnow()
        self._session.flush()
        return run

    @staticmethod
    def _new_thread_id() -> str:
        """Mint a non-empty, unique ``thread_id`` for the run (Req. 11.4).

        A ``uuid4`` hex is unique by construction (collision probability
        negligible) and non-empty, satisfying the ``agent_runs.thread_id``
        non-null contract that makes a run addressable by its checkpoint thread.
        The value is set once at creation and never rewritten — it is immutable
        for the life of the run.
        """
        return uuid.uuid4().hex

    # -- graph invocation -----------------------------------------------------

    def _build_graph(self) -> Any:
        """Build the compiled graph over the PostgreSQL checkpointer (lazy).

        Resolves the checkpointer factory and ``build_graph`` lazily so importing
        this module opens no socket and touches no database (Req. 1.7). This is
        the single seam where task 15.2 can thread a resume-aware checkpointer /
        graph; task 15.1 wires the straightforward build.
        """
        from app.services.ai.checkpointer import get_checkpointer
        from app.services.ai.graph import build_graph

        # Thread the request-scoped Session into the graph so the phase agents
        # assemble their Agent_Input from the DATABASE (Campaign_Overview + RAG),
        # never from disk (task 14.1, Req. 5.1, 5.2, 5.4). It is bound into the
        # nodes as a keyword argument, so the live Session never lands on the
        # checkpointed AgentState.
        return build_graph(checkpointer=get_checkpointer(), session=self._session)

    # -- failure handling (Req. 14.1, 14.2, 14.6) -----------------------------

    def _fail_run(self, run: AgentRun, exc: BaseException) -> RunFailedError:
        """Mark ``run`` ``FAILED``, record the cause + partial trace, and raise (2.6/4.4/14.x).

        Invoked when the graph invocation in :meth:`start` / :meth:`resume` raises
        — SEPINF unreachable or no response within the configured read timeout, a model
        error response, an LLM output that could not be parsed into a valid
        Attack flow/command set after the single permitted repair
        (:class:`~app.services.ai.agents.flow_agent.FlowGenerationError` and
        analogous node errors), or a phase that could not be filled because its
        required Agent_Input was absent. It:

        * classifies the FAILING PHASE from the raised exception
          (:meth:`_failing_node`) so the recorded cause identifies it (Req. 14.1,
          14.2, 14.4, 4.4);
        * flips the run to ``FAILED`` and writes a structured cause — the failing
          phase plus the absent-input/cause message — into the nullable
          ``agent_runs.error`` column (added in task 3.2), leaving
          ``final_verdict_success`` null while the status stays ``FAILED`` so a
          failed run never reads as completed/aborted (Req. 2.6, 4.4, 14.6);
        * persists NO full AI artifact for the failing node, but when a partial
          Audit_Trace was captured in the agent's sink up to the failure (carried
          on the exception by the failing node — design §A "Failure capture"),
          persists that partial trace together with the identity of the failing
          agent so the failure is auditable (Req. 2.6). The trace is anchored on a
          minimal ``agent_phase_outputs`` row for the failing phase (an empty
          payload sentinel — the phase produced no valid artifact) and its 1:1
          ``agent_phase_traces`` row, written inside a SAVEPOINT so the whole
          partial-trace write lands atomically or not at all (design §B /
          §Error Handling). When no partial trace was captured (a SEPINF transport
          error / bare timeout that reached no LLM call), no bookkeeping row is
          written at all — only ``agent_runs.error`` records the cause;
        * does NOT fall back to human curation for the Run — the AI path is the
          only processing path (Req. 4.4). the AI agents are the only implementations;
        * flushes the ``FAILED`` status and RETURNS a :class:`RunFailedError` for
          the caller to raise (``raise self._fail_run(...)``), so once ``FAILED``
          the surrounding ``start`` / ``resume`` never reaches the
          COMPLETED/ABORTED transition (Req. 14.6).

        A :class:`DependencyUnavailableError` is NOT funnelled here — it is raised
        before the graph is invoked (Req. 14.5) and must not be reclassified as a
        node failure; callers catch only exceptions from the graph invocation.
        """
        node = self._failing_node(exc)
        error_message = self._failure_message(node, exc)

        # Record the failing-phase cause. The structured message (failing phase +
        # absent-input/cause) is persisted to the nullable agent_runs.error column
        # and also logged for operability (Req. 2.6, 4.4, 14.6).
        logger.error(
            "AI curation run %s failed at phase '%s' (%s: %s); marking the run "
            "FAILED, recording the cause in agent_runs.error and persisting no "
            "full AI artifact for the failing phase.",
            getattr(run, "id", None),
            node,
            type(exc).__name__,
            exc,
        )

        # The store writes ``status``, ``failure`` (phase, code, cause code),
        # ``error`` and a null ``final_verdict_success`` in one commit of its own
        # session, so the failure survives any rollback of the working session
        # (Req. 14.6).
        cause = getattr(exc, "cause", None)
        self._state_store.fail(
            run.id,
            self._failure_phase(node),
            code="phase_failed",
            cause_code=classify_llm_failure(cause if isinstance(cause, BaseException) else exc),
            error=error_message,
        )
        self._session.refresh(run)

        # Persist the partial Audit_Trace captured up to the failure together with
        # the failing agent's identity, when the failing node carried one on the
        # exception (design §A / §B). No FULL AI artifact is persisted — only a
        # minimal anchor phase-output row + its 1:1 trace row — and the write stays
        # inside its own SAVEPOINT (Req. 2.6). A SEPINF transport error / bare
        # timeout carries no partial trace, so nothing is written here.
        self._persist_partial_trace(run, node, exc)
        self._session.commit()

        return RunFailedError(node, exc)

    @staticmethod
    def _failure_message(node: str, exc: BaseException) -> str:
        """Compose the structured ``agent_runs.error`` cause (Req. 2.6, 4.4).

        The message names the FAILING PHASE and the absent-input/cause carried by
        the exception, so the read endpoint can surface a failure that identifies
        both the phase that failed and why. A node error that carries an explicit
        ``cause`` (``FlowGenerationError`` and analogous) contributes it verbatim;
        otherwise the exception's own text (a SEPINF transport/timeout/model
        error) is used.
        """
        cause = getattr(exc, "cause", None)
        if isinstance(cause, str) and cause.strip():
            detail = cause.strip()
        else:
            detail = str(exc).strip() or type(exc).__name__
        return f"phase '{node}' failed: {detail}"

    def _persist_partial_trace(self, run: AgentRun, node: str, exc: BaseException) -> None:
        """Persist the partial Audit_Trace captured up to a phase failure (Req. 2.6).

        When the failing node attached the partial trace it captured in its sink
        (up to the failure) to the raised exception — the node appends its
        :class:`~app.services.ai.state.AgentTrace` in a ``finally`` even when its
        LLM call raised, so the sink holds the ``agent_input`` and the processing
        gathered so far — this writes that partial trace so the failure is
        auditable. Because the failing phase produced no valid artifact, the trace
        is anchored on a MINIMAL ``agent_phase_outputs`` row (empty payload
        sentinel, ``origin = ai_generated``, the failing phase) linked to an
        ``agent_iterations`` row, plus the single 1:1 ``agent_phase_traces`` row
        for it. The whole write is wrapped in a nested transaction (``SAVEPOINT``)
        so the partial-trace rows land atomically or roll back together without
        disturbing the already-flushed ``FAILED`` status (design §B).

        No FULL AI artifact is persisted for the failing phase — only this
        audit anchor (Req. 14.1, 14.2). When the exception carries no partial
        trace (a SEPINF transport error / bare timeout that never reached an LLM
        call) this is a no-op: no bookkeeping row is written, only
        ``agent_runs.error`` records the cause.
        """
        trace = self._partial_trace(exc, node)
        if trace is None:
            return None

        from sqlalchemy import select

        from app.models.domain import AgentIteration, AgentPhaseOutput
        from app.models.enums import TranslationSource
        from app.services.ai.agents._trace import persist_phase_trace

        phase = self._phase_for_node(node)
        if phase is None:
            # The failing node could not be mapped to a persistable AgentPhase
            # (e.g. a transport failure reported as "unknown"); the cause is still
            # recorded in agent_runs.error, but there is no phase slot to anchor a
            # trace row on, so nothing more is written.
            return None

        try:
            with self._session.begin_nested():
                # Anchor on the latest iteration row of the run (created when the
                # failure happened before any phase output was stored).
                iteration = self._session.scalars(
                    select(AgentIteration)
                    .where(AgentIteration.run_id == run.id)
                    .order_by(AgentIteration.index.desc())
                ).first()
                if iteration is None:
                    iteration = AgentIteration(
                        run_id=run.id,
                        index=0,
                        refined_phase=None,
                        created_at=_utcnow(),
                    )
                    self._session.add(iteration)
                    self._session.flush()

                # A previous valid output of this phase (a failed regeneration)
                # stays untouched; only the audit anchor is skipped.
                if self._session.scalars(
                    select(AgentPhaseOutput.id).where(
                        AgentPhaseOutput.iteration_id == iteration.id,
                        AgentPhaseOutput.phase == phase,
                    )
                ).first():
                    return None

                # A minimal anchor: the phase produced no valid artifact, so the
                # payload is an explicit empty sentinel rather than an Agent_Output
                # (no FULL AI artifact is persisted for the failing phase).
                phase_output = AgentPhaseOutput(
                    iteration_id=iteration.id,
                    phase=phase,
                    origin=TranslationSource.AI_GENERATED,
                    payload={},
                    created_at=_utcnow(),
                )
                self._session.add(phase_output)
                self._session.flush()

                persist_phase_trace(self._session, phase_output, trace)
        except Exception as persist_exc:  # noqa: BLE001 -- audit write is best-effort
            # The partial-trace write is an auditability aid layered on top of an
            # already-FAILED run; a failure here must not mask the original run
            # failure. The SAVEPOINT already rolled the partial rows back, so the
            # FAILED status + error remain intact — log and move on.
            logger.warning(
                "Persisting the partial Audit_Trace for failed run %s at phase "
                "'%s' did not complete (%s: %s); the run stays FAILED with its "
                "recorded cause and no partial-trace rows were retained.",
                getattr(run, "id", None),
                node,
                type(persist_exc).__name__,
                persist_exc,
            )

        return None

    @staticmethod
    def _partial_trace(exc: BaseException, node: str) -> dict | None:
        """Extract the partial ``AgentTrace`` the failing node captured (Req. 2.6).

        The failing node attaches the partial trace it accumulated in its sink
        (up to the failure) to the raised exception so :meth:`_fail_run` can
        persist it — design §A "Failure capture". This reads it off the exception,
        accepting either a single ``AgentTrace`` dict (``exc.trace`` /
        ``exc.partial_trace``) or the raw sink list (``exc.traces`` /
        ``exc.partial_traces``), from which the LAST captured entry is used (the
        most recent pass of the failing phase). The returned trace is stamped with
        the failing ``phase`` when it carries none, so the anchor row and trace
        agree. When no partial trace is present (a SEPINF transport error / bare
        timeout) ``None`` is returned and no trace is persisted.
        """
        candidate: Any = None
        for attr in ("partial_trace", "trace"):
            value = getattr(exc, attr, None)
            if isinstance(value, dict):
                candidate = value
                break
        if candidate is None:
            for attr in ("partial_traces", "traces"):
                value = getattr(exc, attr, None)
                if isinstance(value, list) and value:
                    last = value[-1]
                    if isinstance(last, dict):
                        candidate = last
                        break
        if not isinstance(candidate, dict):
            return None

        trace = dict(candidate)
        if not trace.get("phase"):
            phase = AgentRunService._phase_for_node(node)
            if phase is not None:
                trace["phase"] = phase.value
        return trace

    @staticmethod
    def _phase_for_node(node: str) -> AgentPhase | None:
        """Map a short node key (``"flow"`` …) onto an :class:`AgentPhase` member.

        The node errors name the failing phase with the router's short key
        (``"flow"`` / ``"operationalize"`` / ``"environment"``); the persisted
        ``agent_phase_outputs.phase`` / ``agent_phase_traces`` rows key on the
        :class:`~app.models.enums.AgentPhase` value. Returns the matching member,
        or ``None`` when the key is not one of the three persistable AI phases (a
        transport failure reported as ``"unknown"`` has no phase slot).
        """
        from app.models.enums import AgentPhase

        mapping = {
            "flow": AgentPhase.FLOW,
            AgentPhase.FLOW.value: AgentPhase.FLOW,
            "operationalize": AgentPhase.OPERATIONALIZATION,
            "operationalization": AgentPhase.OPERATIONALIZATION,
            AgentPhase.OPERATIONALIZATION.value: AgentPhase.OPERATIONALIZATION,
            "environment": AgentPhase.ENVIRONMENT,
            AgentPhase.ENVIRONMENT.value: AgentPhase.ENVIRONMENT,
        }
        return mapping.get(node)

    @staticmethod
    def _failure_phase(node: str) -> str | None:
        """Phase name stored in ``agent_runs.failure.phase`` for a failing node."""
        from app.services.ai.run_state import phase_for_node

        if node == "unknown":
            return None
        return phase_for_node(node) or node

    @staticmethod
    def _failing_node(exc: BaseException) -> str:
        """Identify the failing node from a graph-invocation exception (14.1/14.2).

        Prefers an explicit ``node`` attribute carried by the raising exception —
        :class:`~app.services.ai.agents.flow_agent.FlowGenerationError` sets
        ``node="flow"`` (Req. 14.4), and any analogous node error that follows the
        same convention is honoured. When no such attribute is present (a SEPINF
        transport error — unreachable / timeout — or a bare model error surfaced
        by the client carries no node), the node cannot be read off the exception
        and is reported as ``"unknown"`` so the recorded cause still names a node
        slot (Req. 14.1, 14.2).
        """
        node = getattr(exc, "node", None)
        if isinstance(node, str) and node.strip():
            return node.strip()
        return "unknown"

    def _initial_state(self, run: AgentRun) -> dict:
        """Seed the initial ``AgentState`` the graph is invoked with.

        Carries the ``campaign_id`` and the resolved ``max_iterations`` guard;
        ``iteration`` starts at 0 (the orchestrator re-seeds it) and ``history``
        starts empty (the append-only reducer accumulates the per-node trace).
        """
        return {
            "campaign_id": run.campaign_id,
            "iteration": 0,
            "max_iterations": run.max_iterations,
            "history": [],
        }

    # -- outcome reading ------------------------------------------------------

    @staticmethod
    def _verdict_success(final_state: dict) -> bool:
        """Read the evaluator's passing verdict from the final state (Req. 3.2).

        The evaluator writes its reading into ``state["evaluation"]`` with a
        ``verdict_success`` flag (see ``AgentState``/``EvaluationResult``). The
        run is ``COMPLETED`` iff that flag is truthy; anything else (false,
        missing, or no evaluation at all) means the loop stopped at the guard
        without a passing verdict and the run is ``ABORTED`` (Req. 3.4).
        """
        evaluation = final_state.get("evaluation") or {}
        return bool(evaluation.get("verdict_success"))

    @staticmethod
    def _iterations_used(final_state: dict, max_iterations: int) -> int:
        """Derive how many iterations the run performed for the summary row.

        The graph's ``iteration`` counter is 0-based, so the number of iterations
        performed is ``iteration + 1`` once at least one pass has run. The value
        is clamped to the ``1..max_iterations`` range the guard enforces so the
        persisted ``iterations_used`` can never exceed the guard (Req. 3.5); when
        no iteration ran (no counter present) it is 0.
        """
        raw = final_state.get("iteration")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return 0
        performed = raw + 1
        if performed < 0:
            return 0
        if performed > max_iterations:
            return max_iterations
        return performed

    # -- configuration --------------------------------------------------------

    def _resolve_max_iterations(self, max_iterations: int | None) -> int:
        """Resolve the loop guard: explicit arg (clamped) else configured default.

        An explicit ``max_iterations`` is clamped to the inclusive range
        ``1..100`` (Req. 3.5). When ``None`` the configured default is read from
        ``SepinfSettings.max_iterations`` (default 3 per Req. 3.6); if that value
        is absent, non-integer, out of range, or the settings cannot be read, the
        default of 3 is used. A ``bool`` is rejected (it is an ``int`` subclass)
        and treated as absent.
        """
        if (
            max_iterations is not None
            and not isinstance(max_iterations, bool)
            and isinstance(max_iterations, int)
        ):
            return self._clamp(max_iterations)

        try:
            from app.services.ai.config import get_sepinf_settings

            configured: Any = get_sepinf_settings().max_iterations
        except Exception as exc:  # noqa: BLE001 -- unreadable config -> default
            logger.warning(
                "max_iterations configuration could not be read (%s); applying "
                "the default value of %d.",
                type(exc).__name__,
                _DEFAULT_MAX_ITERATIONS,
            )
            return _DEFAULT_MAX_ITERATIONS

        if isinstance(configured, bool) or not isinstance(configured, int):
            return _DEFAULT_MAX_ITERATIONS
        if configured < _MIN_MAX_ITERATIONS or configured > _MAX_MAX_ITERATIONS:
            return _DEFAULT_MAX_ITERATIONS
        return configured

    @staticmethod
    def _clamp(value: int) -> int:
        """Clamp an iteration guard to the inclusive ``1..100`` range (Req. 3.5)."""
        if value < _MIN_MAX_ITERATIONS:
            return _MIN_MAX_ITERATIONS
        if value > _MAX_MAX_ITERATIONS:
            return _MAX_MAX_ITERATIONS
        return value

    # -- dependency availability (Req. 14.5) ----------------------------------

    def _require_dependencies(self) -> None:
        """Assert the checkpointer and pgvector store are reachable (Req. 14.5).

        Probes the two dependencies a run needs to start or resume — the
        LangGraph PostgreSQL checkpointer (short-term memory) and the pgvector
        experience store (long-term memory) — and raises
        :class:`DependencyUnavailableError` naming the FIRST unavailable one. The
        probe runs before any ``agent_runs`` row is created or any graph invoked,
        so an unavailable dependency never starts or resumes the run.

        Both probes are lazy and side-effect free beyond a single cheap read, so
        importing this module and constructing the service touch no network
        (Req. 1.7); the connection is opened only here, when a run is actually
        requested.
        """
        self._probe_checkpointer()
        self._probe_pgvector()

    def _probe_checkpointer(self) -> None:
        """Probe the LangGraph checkpointer, raising if it is unavailable.

        Resolves the checkpointer factory lazily and performs one trivial read
        against a sentinel thread (``get_tuple``) so an unreachable checkpoint
        store surfaces here rather than mid-run. A ``None`` result (no checkpoint
        for the sentinel) is the expected healthy answer; only a raised exception
        — a closed pool, a refused connection, a missing checkpoint table — marks
        the checkpointer unavailable.
        """
        try:
            from app.services.ai.checkpointer import get_checkpointer

            checkpointer = get_checkpointer()
            # A trivial lookup forces the lazy pool to open a connection; the
            # sentinel thread is expected to have no checkpoint, so a None result
            # is healthy and only an exception signals unavailability.
            checkpointer.get_tuple({"configurable": {"thread_id": "__availability_probe__"}})
        except Exception as exc:
            logger.warning(
                "Checkpointer availability probe failed (%s: %s); the run cannot start or resume.",
                type(exc).__name__,
                exc,
            )
            raise DependencyUnavailableError("checkpointer", exc) from exc

    def _probe_pgvector(self) -> None:
        """Probe the pgvector experience store, raising if it is unavailable.

        Runs one lightweight parameterized read against the ``experience_memory``
        pgvector table on the injected ``Session``; its cosine-distance operator
        (and thus the ``vector`` extension) must be present for the RAG memory to
        function. Any failure — the extension not enabled, the table missing, the
        connection refused — marks pgvector unavailable so the run does not start
        or resume (Req. 14.5). The probe is read-only and leaves the session's
        transaction usable.
        """
        try:
            from sqlalchemy import func, select

            from app.models.domain import ExperienceMemory as ExperienceMemoryRow

            # A bounded existence check that references the pgvector-backed table;
            # it succeeds (returning a count) only when the table and the vector
            # extension it depends on are present and reachable.
            self._session.execute(select(func.count()).select_from(ExperienceMemoryRow).limit(1))
        except Exception as exc:
            logger.warning(
                "pgvector store availability probe failed (%s: %s); the run "
                "cannot start or resume.",
                type(exc).__name__,
                exc,
            )
            raise DependencyUnavailableError("pgvector", exc) from exc

    # -- atomic per-iteration persistence (Req. 11.5-11.8) --------------------

    def persist_phase(
        self, run: AgentRun, phase: str, state: dict, *, record_proposal: bool = True
    ) -> None:
        """Persist the output of ONE phase atomically and commit (Req. 4.1, 4.2).

        ``phase`` is ``operationalization``, ``flow`` or ``environment`` (the
        artifact in ``state["artifacts"]``) or ``evaluation`` (the evaluator row of
        the finished iteration). The iteration row of the state's ``iteration``
        index is fetched or created. The write runs in a SAVEPOINT and is
        committed, so each phase is durable before the run is parked for approval.

        ``(iteration, phase)`` is unique: a previous output (and its trace) for the
        same pair, left by an earlier pass or a regenerated attack flow, is removed
        inside the same SAVEPOINT and replaced. Nothing is kept on failure.

        ``record_proposal=False`` skips the append to the attack flow proposal
        history; ``select_proposal`` uses it because the flow is already there.

        Raises:
            PersistenceError: the write failed and was rolled back.
        """
        index = self._iteration_index(state)
        artifacts = state.get("artifacts") or {}
        try:
            with self._session.begin_nested():
                iteration = self._get_or_create_iteration(run, index, state)
                if phase == "evaluation":
                    evaluation = state.get("evaluation")
                    if evaluation:
                        self._replace_evaluation(iteration.id, state, dict(evaluation))
                        iteration.notes = self._iteration_notes(state, evaluation)
                else:
                    agent_phase = self._phase_for_node(phase)
                    if agent_phase is None:
                        raise ValueError(f"unknown phase {phase!r}")
                    self._delete_phase_output(iteration.id, agent_phase)
                    self._persist_phase_outputs(
                        state,
                        artifacts,
                        iteration_id=iteration.id,
                        iteration_index=index,
                        run=run,
                        only=agent_phase,
                    )
            self._session.commit()
        except Exception as exc:
            self._session.rollback()
            logger.warning(
                "Persisting phase '%s' (iteration %d) of run %s failed (%s: %s); rolled back.",
                phase,
                index,
                getattr(run, "id", None),
                type(exc).__name__,
                exc,
            )
            raise PersistenceError(
                f"persistence of phase '{phase}' (iteration {index}) for run "
                f"{getattr(run, 'id', None)} failed and was rolled back: "
                f"{type(exc).__name__}: {exc}",
                exc,
            ) from exc
        if record_proposal and phase == "flow" and artifacts.get("flow"):
            # Presented attack flow history (own session, after the phase output
            # is durable): feeds the exclusions of a regeneration and the selection.
            self._state_store.record_flow_proposal(run.id, artifacts["flow"])

    @staticmethod
    def _iteration_index(state: dict) -> int:
        """0-based iteration index of the state (0 when absent or invalid)."""
        raw = state.get("iteration")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
        return 0

    def _get_or_create_iteration(self, run: AgentRun, index: int, state: dict) -> AgentIteration:
        """Return the ``agent_iterations`` row of ``index``, creating it when missing."""
        from sqlalchemy import select

        from app.models.domain import AgentIteration

        existing = self._session.scalars(
            select(AgentIteration).where(
                AgentIteration.run_id == run.id, AgentIteration.index == index
            )
        ).first()
        if existing is not None:
            return existing
        refine_phase = state.get("refine_phase") if index > 0 else None
        return self._persist_iteration_row(run, index, refine_phase)

    def _delete_phase_output(self, iteration_id: int, phase: AgentPhase) -> None:
        """Delete the ``(iteration, phase)`` output and its 1:1 trace, if present."""
        from sqlalchemy import delete, select

        from app.models.domain import AgentPhaseOutput, AgentPhaseTrace

        ids = list(
            self._session.scalars(
                select(AgentPhaseOutput.id).where(
                    AgentPhaseOutput.iteration_id == iteration_id,
                    AgentPhaseOutput.phase == phase,
                )
            )
        )
        if not ids:
            return
        self._session.execute(
            delete(AgentPhaseTrace).where(AgentPhaseTrace.phase_output_id.in_(ids))
        )
        self._session.execute(delete(AgentPhaseOutput).where(AgentPhaseOutput.id.in_(ids)))
        self._session.flush()

    def _persist_iteration_row(
        self,
        run: AgentRun,
        index: int,
        refine_phase: str | None,
        notes: str | None = None,
    ) -> AgentIteration:
        """Create and flush the ``agent_iterations`` row for the completed pass.

        The row belongs to ``run`` via ``run_id`` and records the 0-based
        ``index`` of the pass; ``refined_phase`` carries the phase the evaluator
        routed back into for this iteration (``None`` on the first pass), mapped
        from the final state's ``refine_phase`` onto an :class:`AgentPhase` member
        when it names one, and ``notes`` records the per-iteration "why" — the
        evaluator's Refinement_Route / finish rationale (Req. 15.6). The row is
        flushed so its ``id`` is available for the phase-output / evaluation FKs
        written within the same transaction.
        """
        from app.models.domain import AgentIteration

        iteration = AgentIteration(
            run_id=run.id,
            index=index,
            refined_phase=self._refined_phase(refine_phase),
            notes=notes,
            created_at=_utcnow(),
        )
        self._session.add(iteration)
        self._session.flush()
        return iteration

    @staticmethod
    def _refined_phase(refine_phase: str | None) -> AgentPhase | None:
        """Map the state's ``refine_phase`` onto an :class:`AgentPhase` (Req. 15.1).

        The evaluator's ``_diagnose_failing_phase`` writes an :class:`AgentPhase`
        *value* onto ``state["refine_phase"]`` (``flow`` /
        ``operationalization`` / ``environment``), so a refining
        iteration lands a direct value here. For robustness this also accepts the
        router's short refine keys (``"flow"`` / ``"operationalize"`` /
        ``"environment"``) in case either shape reaches the state, so
        ``refined_phase`` is populated whenever the evaluator refined (Req. 15.1,
        15.2). An absent or unrecognised value leaves ``refined_phase`` null
        (the first pass, or a FINISH that refined nothing) rather than guessing.
        """
        from app.models.enums import AgentPhase

        if refine_phase is None:
            return None
        try:
            return AgentPhase(refine_phase)
        except ValueError:
            pass
        short_keys = {
            "flow": AgentPhase.FLOW,
            "operationalize": AgentPhase.OPERATIONALIZATION,
            "operationalization": AgentPhase.OPERATIONALIZATION,
            "environment": AgentPhase.ENVIRONMENT,
        }
        return short_keys.get(refine_phase)

    @staticmethod
    def _iteration_notes(final_state: dict, evaluation: dict | None) -> str | None:
        """Record the per-iteration "why" for ``agent_iterations.notes`` (Req. 15.6).

        The evaluator produces, for every iteration, a ``rationale`` explaining
        the decision it made — the Refinement_Route reasoning (which phase it
        chose to re-enter and why) on a refining pass, or the finish reasoning on
        a passing verdict. That "why" is carried on
        ``final_state["evaluation"]["rationale"]``; this records it on the
        iteration's ``notes`` column so the reason each iteration was refined (or
        finished) is auditable on the iteration row itself, not only on the
        evaluation row (Req. 15.6). An explicit ``final_state["notes"]`` takes
        precedence when a node set one; absent any rationale the notes stay null.
        """
        explicit = final_state.get("notes")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()
        if evaluation:
            rationale = evaluation.get("rationale")
            if isinstance(rationale, str) and rationale.strip():
                return rationale.strip()
        return None

    def _persist_phase_outputs(
        self,
        final_state: dict,
        artifacts: dict,
        *,
        iteration_id: int,
        iteration_index: int,
        run: AgentRun,
        only: AgentPhase | None = None,
    ) -> None:
        """Persist the iteration's ``agent_phase_outputs`` via the factored hooks.

        Reuses each phase agent's ``Session``-driven persistence hook so this
        service never reimplements artifact serialization and every row lands with
        ``origin = ai_generated`` (Req. 11.5). For each artifact present in the final
        state:

        * ``artifacts.flow`` is reconstructed into a ``ProducedFlow`` and persisted
          via :func:`flow_agent.persist_flow_phase_output` (which also reconciles the
          AI-origin ``AttackFlowRow`` row and links ``flow_id``);
        * ``artifacts.operationalization`` is reconstructed into a
          ``ProducedOperationalization`` and persisted via
          :func:`op_agent.persist_phase_output` (which persists the derived
          ``EnvironmentParameters`` and links ``params_id``);
        * ``artifacts.environment`` is persisted via
          :func:`env_agent.persist_phase_output`, which reads the serialized
          environment straight off the state.

        Each hook is also handed the matching :class:`~app.services.ai.state.AgentTrace`
        dict — looked up from ``final_state["traces"]`` by ``(phase, iteration)``
        (:meth:`_trace_for`) — so it writes the 1:1 ``agent_phase_traces`` row for
        the just-persisted output in the SAME flush, inside this iteration's
        SAVEPOINT (Req. 2.1, 2.5). A genuinely-absent trace is passed as ``None``
        and no trace row is written for that output.

        Missing artifacts are skipped (a phase that did not run this pass simply
        has no row). All hooks only ``add``/``flush`` onto the shared ``Session``;
        the enclosing nested transaction in :meth:`_persist_iterations` owns the
        commit/rollback boundary.
        """
        from app.models.enums import AgentPhase
        from app.services.ai.agents import env_agent, flow_agent, op_agent

        traces = final_state.get("traces")

        # ``only`` restricts the write to one phase (per-phase persistence).
        flow_payload = artifacts.get("flow")
        if flow_payload and only in (None, AgentPhase.FLOW):
            from app.services.flow.generator import ProducedFlow

            produced_flow = ProducedFlow.model_validate(flow_payload)
            flow_agent.persist_flow_phase_output(
                self._session,
                iteration_id=iteration_id,
                campaign_id=run.campaign_id,
                produced=produced_flow,
                trace=self._trace_for(traces, AgentPhase.FLOW.value, iteration_index),
            )

        op_payload = artifacts.get("operationalization")
        if op_payload and only in (None, AgentPhase.OPERATIONALIZATION):
            from app.services.environment.operationalizer import (
                ProducedOperationalization,
            )

            produced_op = ProducedOperationalization.model_validate(op_payload)
            op_agent.persist_phase_output(
                self._session,
                iteration_id,
                produced_op,
                trace=self._trace_for(
                    traces,
                    AgentPhase.OPERATIONALIZATION.value,
                    iteration_index,
                ),
            )

        if artifacts.get("environment") and only in (None, AgentPhase.ENVIRONMENT):
            env_agent.persist_phase_output(
                self._session,
                final_state,
                iteration_id=iteration_id,
                trace=self._trace_for(traces, AgentPhase.ENVIRONMENT.value, iteration_index),
            )

    @staticmethod
    def _trace_for(traces: Any, phase: str, iteration_index: int) -> dict | None:
        """Look up the ``AgentTrace`` for ``(phase, iteration_index)`` (Req. 2.1).

        The append-only ``traces`` channel accumulates one
        :class:`~app.services.ai.state.AgentTrace` dict per AI node per pass, each
        keyed by its English ``phase`` value (``flow`` /
        ``operationalization`` / ``environment``) and the 0-based
        ``iteration`` the node ran in. This returns the trace matching the
        persisting phase output's phase and iteration, or ``None`` when no such
        trace was captured (a curation fallback, or an AI path that recorded
        nothing) — in which case the hook writes no ``agent_phase_traces`` row.

        When several traces match (a refine loop re-entered the same phase across
        passes, all accumulated in the channel), the LAST matching one is
        returned so the persisted trace reflects the most recent pass of that
        phase — consistent with the checkpointer overwriting ``artifacts`` each
        pass so the persisted output is also the last pass's.
        """
        if not isinstance(traces, list):
            return None
        match: dict | None = None
        for entry in traces:
            if not isinstance(entry, dict):
                continue
            if entry.get("phase") == phase and entry.get("iteration") == iteration_index:
                match = entry
        return match

    def _replace_evaluation(self, iteration_id: int, state: dict, evaluation: dict) -> None:
        """Replace the iteration's ``agent_evaluations`` row (0..1 per iteration)."""
        from sqlalchemy import delete

        from app.models.domain import AgentEvaluation

        self._session.execute(
            delete(AgentEvaluation).where(AgentEvaluation.iteration_id == iteration_id)
        )
        self._persist_evaluation_row(state, evaluation, iteration_id=iteration_id)

    def _persist_evaluation_row(
        self, final_state: dict, evaluation: dict, *, iteration_id: int
    ) -> None:
        """Persist the iteration's ``agent_evaluations`` row via the evaluator hook.

        Delegates to :func:`evaluator.persist_evaluation`, which writes one
        ``agent_evaluations`` row for ``iteration_id`` with the metrics the
        decision was derived from, the decision and the rationale bounded to
        10,000 characters (Req. 11.8, enforced by ``evaluator._bounded``). The
        hook only ``add``/``flush``es, so the enclosing nested transaction owns
        the atomicity.

        The Execution phase report id is passed through when the final state carries one,
        so the persisted evaluation links the ``execution_reports`` row it read;
        the Run_Service does not persist the report itself here.
        """
        from app.services.ai.agents import evaluator

        report = final_state.get("report") or {}
        report_id = report.get("report_id") if isinstance(report, dict) else None

        evaluator.persist_evaluation(
            self._session,
            iteration_id,
            dict(evaluation),
            report_id=report_id,
        )
