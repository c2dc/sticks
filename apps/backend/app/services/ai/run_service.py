"""Session-injected orchestration and persistence of AI curation runs.

Hosts ``AgentRunService`` which creates ``agent_runs`` rows, invokes the
compiled graph with the checkpointer, persists per-iteration bookkeeping
atomically and supports resume from the last checkpoint (tasks 15.1-15.3).

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
  final state, one ``agent_iterations`` row plus its ``agent_stage_outputs``
  rows (and the ``agent_evaluations`` row when an evaluation is present) inside a
  SINGLE atomic transaction — a nested transaction (``SAVEPOINT``) — rolling back
  so no partial iteration rows are retained on any failure and raising a
  :class:`PersistenceError` reporting the failure (Req. 11.5, 11.6, 11.7, 11.8).

Task 15.3 (this change) adds the **SEPINF-failure / ``FAILED`` handling** and the
**startup seam fallback**:

* ``start``/``resume`` wrap the graph invocation so that when SEPINF is
  unreachable or exceeds its 30-second timeout, the model returns an error, or an
  LLM output cannot be parsed into a valid DAG/command set after the one repair
  (``DagGenerationError`` and analogous node errors), the run is flipped to
  ``FAILED`` recording a failure cause that identifies the FAILING NODE, NO AI
  artifact is persisted for the failing node (``_persist_iterations`` is skipped
  entirely on failure) and, once ``FAILED``, the run is never transitioned to a
  successful/completed status (Req. 14.1, 14.2, 14.6). The ``agent_runs`` schema
  has no dedicated error column, so the failure cause is recorded by logging the
  failing-node diagnostic and leaving ``final_verdict_success`` null while the
  status stays ``FAILED`` — consistent with the model (Req. 14.6).
* :func:`resolve_stage_agent` is the **startup seam fallback** (Req. 12.2, 12.3):
  it resolves a stage agent (Stage 3 DAG / Stage 4 operationalization / Stage 5
  environment) behind the pipeline's existing seam, and on an initialization
  failure falls back to that stage's CURATION implementation
  (``CurationDagGenerator`` / ``CurationOperationalizer`` /
  ``CurationEnvironmentProvisioner``) behind the SAME seam, recording a diagnostic
  that names the affected stage and the fallback.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from sqlalchemy.orm import Session

    from app.models.domain import AgentIteration, AgentRun

logger = logging.getLogger(__name__)

# Default max-iteration guard applied when the caller passes no explicit value
# and configuration cannot be read; mirrors ``SepinfSettings.max_iterations``
# (Req. 3.6). The configured value is preferred via :func:`get_sepinf_settings`.
_DEFAULT_MAX_ITERATIONS = 3

# Inclusive bounds on the per-run iteration guard (Req. 3.5): a run performs
# ``1..max_iterations`` iterations, so the guard itself is bounded to [1, 100].
_MIN_MAX_ITERATIONS = 1
_MAX_MAX_ITERATIONS = 100


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

    def __init__(
        self, dependency: str, cause: Optional[BaseException] = None
    ) -> None:
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
    its ``agent_stage_outputs`` rows and the optional ``agent_evaluations`` row)
    fails for any reason, the whole write is rolled back so no partial iteration
    rows are retained, and this error is raised to report the persistence failure
    to the caller. It carries the originating exception as ``cause``.
    """

    def __init__(self, message: str, cause: Optional[BaseException] = None) -> None:
        self.cause = cause
        super().__init__(message)


class RunFailedError(RuntimeError):
    """Raised when the graph invocation fails and the run was marked ``FAILED``.

    Signals Requirement 14.1/14.2/14.4: SEPINF was unreachable or timed out, the
    model returned an error, or an LLM output could not be parsed into a valid
    DAG/command set after the single permitted repair. Before this error is
    raised the owning ``agent_runs`` row has already been flipped to ``FAILED``
    with its failing-node cause recorded (logged; ``final_verdict_success`` left
    null), and NO AI artifact has been persisted for the failing node — the
    per-iteration persistence is skipped entirely on failure (Req. 14.1, 14.2,
    14.6).

    The exception carries the ``node`` that failed (e.g. ``"dag"``) and the
    originating ``cause`` so callers (the API layer) can surface an error
    response identifying the failing node without mutating the run further.
    """

    def __init__(
        self, node: str, cause: Optional[BaseException] = None
    ) -> None:
        self.node = node
        self.cause = cause
        detail = f" ({type(cause).__name__}: {cause})" if cause is not None else ""
        super().__init__(
            f"the AI curation run failed at node '{node}'; the run was marked "
            f"FAILED and no AI artifact was persisted for it{detail}."
        )


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

    def __init__(self, session: "Session") -> None:
        """Bind the service to an open :class:`~sqlalchemy.orm.Session`.

        No graph, checkpointer or LLM is built here — all heavy collaborators
        are resolved lazily on :meth:`start` so construction is side-effect free
        (Req. 1.7).
        """
        self._session = session

    # -- public API -----------------------------------------------------------

    def start(
        self, campaign_id: str, *, max_iterations: Optional[int] = None
    ) -> "AgentRun":
        """Create an ``agent_runs`` row, invoke the graph, and mark the outcome.

        Steps (Req. 3.2, 3.4, 4.2, 11.4):

        1. Create the ``agent_runs`` row with status ``PENDING``, a non-empty,
           unique, immutable ``thread_id`` (a ``uuid4`` hex) and the resolved
           ``max_iterations`` (the explicit argument clamped to ``1..100``, else
           the configured default, else 3), then flip it to ``RUNNING`` and flush
           so the row (and its ``thread_id``) is persisted before the graph runs.
        2. Build the compiled graph over the PostgreSQL checkpointer and invoke
           it with ``config={"configurable": {"thread_id": run.thread_id}}`` and
           the seeded initial state (``campaign_id`` / ``max_iterations``), so the
           checkpointer snapshots the run under the row's own thread (Req. 4.2).
        3. From the final state, read the evaluator verdict and the iterations
           performed, then mark the run ``COMPLETED`` when the final verdict
           passes or ``ABORTED`` when the loop stopped at the guard without a
           passing verdict (Req. 3.2, 3.4), persisting ``iterations_used`` and
           ``final_verdict_success``.

        Before any of this the checkpointer and pgvector store are probed; an
        unavailable dependency raises :class:`DependencyUnavailableError` and the
        run is not started (no row, no graph — Req. 14.5). After the graph runs,
        the completed iteration(s) are persisted atomically by
        :meth:`_persist_iterations` (Req. 11.5-11.8).

        Failure handling (Req. 14.1, 14.2, 14.6): if the graph invocation fails —
        SEPINF unreachable or no response within the 30-second timeout, a model
        error, or an LLM output that cannot be parsed into a valid DAG/command set
        after the one repair (:class:`~app.services.ai.agents.dag_agent.DagGenerationError`
        and analogous node errors) — the run is flipped to ``FAILED`` with its
        failing-node cause recorded, NO AI artifact is persisted for the failing
        node, and a :class:`RunFailedError` is raised. Once ``FAILED`` the run is
        never transitioned to a completed/aborted status.

        Args:
            campaign_id: The campaign this run curates (FK into ``campaigns``).
            max_iterations: Optional explicit loop guard; when provided it is
                clamped to the inclusive range ``1..100``. When ``None`` the
                configured default (``SepinfSettings.max_iterations``, default 3)
                is used.

        Returns:
            The persisted :class:`~app.models.domain.AgentRun`, flushed with its
            terminal status, ``iterations_used`` and ``final_verdict_success``.
        """
        from app.models.enums import AgentRunStatus

        # Report checkpointer/pgvector unavailability BEFORE creating the run row
        # or invoking the graph, so an unavailable dependency neither starts the
        # run nor leaves a partial ``agent_runs`` row behind (Req. 14.5).
        self._require_dependencies()

        resolved_max = self._resolve_max_iterations(max_iterations)
        run = self._create_run(campaign_id, resolved_max)

        # The graph persists its state under the run's thread, so the run row —
        # and its immutable thread_id — must exist first (Req. 4.2). A SEPINF
        # failure (unreachable / 30s timeout / model error) or an unrepairable
        # LLM output flips the run to FAILED with its failing-node cause and
        # persists NO AI artifact for that node (Req. 14.1, 14.2, 14.6).
        try:
            final_state = self._invoke_graph(run)
        except Exception as exc:  # noqa: BLE001 - classify + mark FAILED (14.1/14.2)
            raise self._fail_run(run, exc) from exc

        verdict_success = self._verdict_success(final_state)
        iterations_used = self._iterations_used(final_state, resolved_max)

        # COMPLETED on a passing verdict; ABORTED when the guard stopped the loop
        # without one (Req. 3.2, 3.4). The router (``route_after_eval``) already
        # ended the graph in both cases; the COMPLETED-vs-ABORTED distinction is
        # drawn here from the final verdict.
        run.status = (
            AgentRunStatus.COMPLETED if verdict_success else AgentRunStatus.ABORTED
        )
        run.final_verdict_success = verdict_success
        run.iterations_used = iterations_used
        run.updated_at = _utcnow()

        # Persist the completed iteration(s) atomically (Req. 11.5-11.8). Any
        # persistence failure rolls back its own write and raises PersistenceError.
        self._persist_iterations(run, final_state)

        self._session.flush()
        return run

    def resume(self, run_id: int) -> "AgentRun":
        """Resume a run from its last checkpoint and mark the outcome (Req. 4.5).

        Loads the ``agent_runs`` row by ``run_id`` and re-invokes the compiled
        graph under the run's OWN ``thread_id`` using the LangGraph resume idiom —
        ``invoke(None, config)`` with ``config={"configurable": {"thread_id":
        run.thread_id}}``. Passing ``None`` as the input tells LangGraph to
        restore the last persisted checkpoint for that thread and continue at the
        node following it, rather than seeding a fresh state; the checkpointer is
        the single source of the resumed state.

        As with :meth:`start`, the checkpointer and pgvector store are probed
        first; if either is unavailable the run is NOT resumed
        (:class:`DependencyUnavailableError`, Req. 14.5). After the graph
        continues to a terminal state the run is flipped to ``COMPLETED`` on a
        passing verdict or ``ABORTED`` when the guard stopped the loop without one
        (Req. 3.2, 3.4), and the completed iteration(s) are persisted atomically
        (Req. 11.5-11.8).

        Args:
            run_id: The id of the ``agent_runs`` row to resume.

        Returns:
            The resumed :class:`~app.models.domain.AgentRun`, flushed with its
            terminal status, ``iterations_used`` and ``final_verdict_success``.

        Raises:
            DependencyUnavailableError: when the checkpointer or pgvector store is
                unavailable — the run is not resumed (Req. 14.5).
            RunFailedError: when the resumed graph invocation fails (SEPINF
                unreachable / 30s timeout / model error / unrepairable LLM
                output); the run is flipped to ``FAILED`` with its failing-node
                cause and no AI artifact is persisted for that node (Req. 14.1,
                14.2, 14.6).
            ValueError: when no ``agent_runs`` row exists for ``run_id``.
        """
        from app.models.domain import AgentRun
        from app.models.enums import AgentRunStatus

        # Report checkpointer/pgvector unavailability BEFORE touching the graph so
        # an unavailable dependency does not resume the run (Req. 14.5).
        self._require_dependencies()

        run = self._session.get(AgentRun, run_id)
        if run is None:
            raise ValueError(f"no agent_runs row exists for run_id={run_id!r}.")

        run.status = AgentRunStatus.RUNNING
        run.updated_at = _utcnow()
        self._session.flush()

        # LangGraph resume idiom: invoke(None, config) restores the last
        # checkpoint for the run's thread and continues at the following node. A
        # SEPINF failure or an unrepairable LLM output on the resumed pass flips
        # the run to FAILED with its failing-node cause and persists NO AI
        # artifact for that node (Req. 14.1, 14.2, 14.6).
        try:
            final_state = self._resume_graph(run)
        except Exception as exc:  # noqa: BLE001 - classify + mark FAILED (14.1/14.2)
            raise self._fail_run(run, exc) from exc

        verdict_success = self._verdict_success(final_state)
        iterations_used = self._iterations_used(final_state, run.max_iterations)

        run.status = (
            AgentRunStatus.COMPLETED if verdict_success else AgentRunStatus.ABORTED
        )
        run.final_verdict_success = verdict_success
        run.iterations_used = iterations_used
        run.updated_at = _utcnow()

        self._persist_iterations(run, final_state)

        self._session.flush()
        return run

    # -- run creation ---------------------------------------------------------

    def _create_run(self, campaign_id: str, max_iterations: int) -> "AgentRun":
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

    def _invoke_graph(self, run: "AgentRun") -> dict:
        """Invoke the compiled graph under the run's checkpoint thread.

        Builds the compiled graph over the PostgreSQL checkpointer (lazily; see
        :meth:`_build_graph`) and invokes it with the run's own
        ``thread_id`` as the checkpoint key
        (``config={"configurable": {"thread_id": run.thread_id}}``) so the
        ``AgentState`` is snapshotted under this run and the run is resumable
        (Req. 4.2). The initial state seeds the ``campaign_id`` and the resolved
        ``max_iterations`` guard the graph's orchestrator/guard read.

        Returns:
            The final ``AgentState`` dict produced by the graph's ``invoke``.
        """
        graph = self._build_graph()
        initial_state = self._initial_state(run)
        config = {"configurable": {"thread_id": run.thread_id}}
        final_state = graph.invoke(initial_state, config=config)
        return final_state if isinstance(final_state, dict) else dict(final_state)

    def _resume_graph(self, run: "AgentRun") -> dict:
        """Continue the compiled graph from the run's last checkpoint (Req. 4.5).

        Builds the compiled graph over the PostgreSQL checkpointer (lazily; see
        :meth:`_build_graph`) and invokes it with ``None`` as the input under the
        run's own ``thread_id`` — the LangGraph resume idiom
        ``invoke(None, config)``. Passing ``None`` restores the last persisted
        checkpoint for the thread and continues at the node following it instead
        of seeding a new state, so the run picks up exactly where it was
        snapshotted.

        Returns:
            The final ``AgentState`` dict produced after the graph resumes and
            reaches a terminal state.
        """
        graph = self._build_graph()
        config = {"configurable": {"thread_id": run.thread_id}}
        final_state = graph.invoke(None, config=config)
        return final_state if isinstance(final_state, dict) else dict(final_state)

    def _build_graph(self) -> Any:
        """Build the compiled graph over the PostgreSQL checkpointer (lazy).

        Resolves the checkpointer factory and ``build_graph`` lazily so importing
        this module opens no socket and touches no database (Req. 1.7). This is
        the single seam where task 15.2 can thread a resume-aware checkpointer /
        graph; task 15.1 wires the straightforward build.
        """
        from app.services.ai.checkpointer import get_checkpointer
        from app.services.ai.graph import build_graph

        return build_graph(checkpointer=get_checkpointer())

    # -- failure handling (Req. 14.1, 14.2, 14.6) -----------------------------

    def _fail_run(
        self, run: "AgentRun", exc: BaseException
    ) -> "RunFailedError":
        """Mark ``run`` ``FAILED`` with its failing-node cause and raise (14.1/14.2).

        Invoked when the graph invocation in :meth:`start` / :meth:`resume` raises
        — SEPINF unreachable or no response within its 30-second timeout, a model
        error response, or an LLM output that could not be parsed into a valid
        DAG/command set after the single permitted repair
        (:class:`~app.services.ai.agents.dag_agent.DagGenerationError` and
        analogous node errors). It:

        * classifies the FAILING NODE from the raised exception
          (:meth:`_failing_node`) so the recorded cause identifies it (Req. 14.1,
          14.2, 14.4);
        * flips the run to ``FAILED`` and records the failure cause. The
          ``agent_runs`` schema has no dedicated error column, so the cause is
          recorded by LOGGING the failing-node diagnostic and leaving
          ``final_verdict_success`` null while the status stays ``FAILED`` —
          consistent with the model (Req. 14.6);
        * persists NO AI artifact for the failing node: it deliberately does NOT
          call :meth:`_persist_iterations`, so no ``agent_iterations`` /
          ``agent_stage_outputs`` / ``agent_evaluations`` rows are written for the
          failing iteration (Req. 14.1, 14.2);
        * flushes the ``FAILED`` status and RETURNS a :class:`RunFailedError` for
          the caller to raise (``raise self._fail_run(...)``), so once ``FAILED``
          the surrounding ``start`` / ``resume`` never reaches the
          COMPLETED/ABORTED transition (Req. 14.6).

        A :class:`DependencyUnavailableError` is NOT funnelled here — it is raised
        before the graph is invoked (Req. 14.5) and must not be reclassified as a
        node failure; callers catch only exceptions from the graph invocation.
        """
        from app.models.enums import AgentRunStatus

        node = self._failing_node(exc)

        # Record the failing-node cause. There is no error column on agent_runs,
        # so the cause is captured in the log and the run is left with a null
        # verdict and FAILED status (Req. 14.6).
        logger.error(
            "AI curation run %s failed at node '%s' (%s: %s); marking the run "
            "FAILED and persisting no AI artifact for the failing node.",
            getattr(run, "id", None),
            node,
            type(exc).__name__,
            exc,
        )

        run.status = AgentRunStatus.FAILED
        # Leave final_verdict_success null: a failed run has no passing verdict and
        # must never read as completed/aborted (Req. 14.6).
        run.final_verdict_success = None
        run.updated_at = _utcnow()
        # Deliberately NOT calling _persist_iterations: no AI artifact is persisted
        # for the failing node (Req. 14.1, 14.2).
        self._session.flush()

        return RunFailedError(node, exc)

    @staticmethod
    def _failing_node(exc: BaseException) -> str:
        """Identify the failing node from a graph-invocation exception (14.1/14.2).

        Prefers an explicit ``node`` attribute carried by the raising exception —
        :class:`~app.services.ai.agents.dag_agent.DagGenerationError` sets
        ``node="dag"`` (Req. 14.4), and any analogous node error that follows the
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

    def _initial_state(self, run: "AgentRun") -> dict:
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

    def _resolve_max_iterations(self, max_iterations: Optional[int]) -> int:
        """Resolve the loop guard: explicit arg (clamped) else configured default.

        An explicit ``max_iterations`` is clamped to the inclusive range
        ``1..100`` (Req. 3.5). When ``None`` the configured default is read from
        ``SepinfSettings.max_iterations`` (default 3 per Req. 3.6); if that value
        is absent, non-integer, out of range, or the settings cannot be read, the
        default of 3 is used. A ``bool`` is rejected (it is an ``int`` subclass)
        and treated as absent.
        """
        if max_iterations is not None and not isinstance(max_iterations, bool):
            if isinstance(max_iterations, int):
                return self._clamp(max_iterations)

        try:
            from app.services.ai.config import get_sepinf_settings

            configured: Any = get_sepinf_settings().max_iterations
        except Exception as exc:  # noqa: BLE001 - unreadable config -> default
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
            checkpointer.get_tuple(
                {"configurable": {"thread_id": "__availability_probe__"}}
            )
        except Exception as exc:  # noqa: BLE001 - any failure -> unavailable
            logger.warning(
                "Checkpointer availability probe failed (%s: %s); the run cannot "
                "start or resume.",
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
            self._session.execute(
                select(func.count()).select_from(ExperienceMemoryRow).limit(1)
            )
        except Exception as exc:  # noqa: BLE001 - any failure -> unavailable
            logger.warning(
                "pgvector store availability probe failed (%s: %s); the run "
                "cannot start or resume.",
                type(exc).__name__,
                exc,
            )
            raise DependencyUnavailableError("pgvector", exc) from exc

    # -- atomic per-iteration persistence (Req. 11.5-11.8) --------------------

    def _persist_iterations(self, run: "AgentRun", final_state: dict) -> None:
        """Persist the completed iteration(s) atomically (Req. 11.5-11.8).

        Reconstructs the iteration(s) to persist from the final ``AgentState`` and
        writes, for each, ONE ``agent_iterations`` row plus its
        ``agent_stage_outputs`` rows (and the ``agent_evaluations`` row when an
        evaluation is present) inside a SINGLE atomic transaction — a nested
        transaction (``SAVEPOINT``) opened on the injected ``Session`` so the
        write either lands whole or not at all without disturbing the surrounding
        unit of work (Req. 11.5, 11.6).

        The checkpointer overwrites ``state["artifacts"]`` / ``state["evaluation"]``
        each pass, so the final state carries the artifacts and evaluation of the
        last completed iteration; this method persists that iteration's row
        keyed on its 0-based ``index`` (``iterations_used - 1``), reusing the
        factored, ``Session``-driven persistence hooks each stage agent and the
        evaluator expose — none of which commit, leaving the transaction boundary
        to this method (Req. 11.5).

        On ANY failure the nested transaction is rolled back so no partial
        ``agent_iterations`` / ``agent_stage_outputs`` / ``agent_evaluations``
        rows for the iteration are retained, and a :class:`PersistenceError`
        reporting the failure is raised (Req. 11.7). When the run performed no
        iteration (nothing to persist) the method is a no-op.

        Args:
            run: The owning ``agent_runs`` row (already flushed, so ``run.id`` is
                populated).
            final_state: The final ``AgentState`` dict produced by the graph.
        """
        iteration_index = run.iterations_used - 1
        if iteration_index < 0:
            # No iteration completed (the guard stopped before a pass): nothing to
            # persist. Not a failure, so no rollback and no error.
            return None

        artifacts = final_state.get("artifacts") or {}
        evaluation = final_state.get("evaluation") or None
        refine_stage = final_state.get("refine_stage")

        try:
            # A nested transaction (SAVEPOINT) makes the whole iteration write
            # atomic on the injected Session: begin_nested() commits the savepoint
            # on a clean exit and rolls it back on any exception, so no partial
            # rows survive a mid-write failure (Req. 11.6, 11.7).
            with self._session.begin_nested():
                iteration = self._persist_iteration_row(
                    run, iteration_index, refine_stage
                )
                self._persist_stage_outputs(
                    final_state, artifacts, iteration_id=iteration.id, run=run
                )
                if evaluation:
                    self._persist_evaluation_row(
                        final_state, evaluation, iteration_id=iteration.id
                    )
        except Exception as exc:  # noqa: BLE001 - roll back + report (Req. 11.7)
            # The nested transaction already rolled the SAVEPOINT back on the way
            # out; surface the failure as a PersistenceError so the caller sees an
            # error indication rather than a silently partial run (Req. 11.7).
            logger.warning(
                "Persisting iteration %d of run %s failed (%s: %s); the "
                "iteration's rows were rolled back.",
                iteration_index,
                getattr(run, "id", None),
                type(exc).__name__,
                exc,
            )
            raise PersistenceError(
                f"persistence of iteration {iteration_index} for run "
                f"{getattr(run, 'id', None)} failed and was rolled back: "
                f"{type(exc).__name__}: {exc}",
                exc,
            ) from exc

        return None

    def _persist_iteration_row(
        self, run: "AgentRun", index: int, refine_stage: Optional[str]
    ) -> "AgentIteration":
        """Create and flush the ``agent_iterations`` row for the completed pass.

        The row belongs to ``run`` via ``run_id`` and records the 0-based
        ``index`` of the pass; ``refined_stage`` carries the stage the evaluator
        routed back into for this iteration (``None`` on the first pass), mapped
        from the final state's ``refine_stage`` onto an :class:`AgentStage` member
        when it names one. The row is flushed so its ``id`` is available for the
        stage-output / evaluation FKs written within the same transaction.
        """
        from app.models.domain import AgentIteration
        from app.models.enums import AgentStage

        refined = None
        if refine_stage is not None:
            try:
                refined = AgentStage(refine_stage)
            except ValueError:
                # The state's refine_stage uses the router's short keys ("dag" /
                # "operationalize" / "environment"); when it is not a direct
                # AgentStage value leave refined_stage null rather than guess.
                refined = None

        iteration = AgentIteration(
            run_id=run.id,
            index=index,
            refined_stage=refined,
            created_at=_utcnow(),
        )
        self._session.add(iteration)
        self._session.flush()
        return iteration

    def _persist_stage_outputs(
        self,
        final_state: dict,
        artifacts: dict,
        *,
        iteration_id: int,
        run: "AgentRun",
    ) -> None:
        """Persist the iteration's ``agent_stage_outputs`` via the factored hooks.

        Reuses each stage agent's ``Session``-driven persistence hook so this
        service never reimplements artifact serialization and every row lands with
        ``origin = gerado_ia`` (Req. 11.5). For each artifact present in the final
        state:

        * ``artifacts.dag`` is reconstructed into a ``ProducedDag`` and persisted
          via :func:`dag_agent.persist_dag_stage_output` (which also reconciles the
          AI-origin ``Dag`` row and links ``dag_id``);
        * ``artifacts.operationalization`` is reconstructed into a
          ``ProducedOperationalization`` and persisted via
          :func:`op_agent.persist_stage_output` (which persists the derived
          ``EnvironmentParameters`` and links ``params_id``);
        * ``artifacts.environment`` is persisted via
          :func:`env_agent.persist_stage_output`, which reads the serialized
          environment straight off the state.

        Missing artifacts are skipped (a stage that did not run this pass simply
        has no row). All hooks only ``add``/``flush`` onto the shared ``Session``;
        the enclosing nested transaction in :meth:`_persist_iterations` owns the
        commit/rollback boundary.
        """
        from app.services.ai.agents import dag_agent, env_agent, op_agent

        dag_payload = artifacts.get("dag")
        if dag_payload:
            from app.services.dag.generator import ProducedDag

            produced_dag = ProducedDag.model_validate(dag_payload)
            dag_agent.persist_dag_stage_output(
                self._session,
                iteration_id=iteration_id,
                campaign_id=run.campaign_id,
                produced=produced_dag,
            )

        op_payload = artifacts.get("operationalization")
        if op_payload:
            from app.services.environment.operationalizer import (
                ProducedOperationalization,
            )

            produced_op = ProducedOperationalization.model_validate(op_payload)
            op_agent.persist_stage_output(
                self._session, iteration_id, produced_op
            )

        if artifacts.get("environment"):
            env_agent.persist_stage_output(
                self._session, final_state, iteration_id=iteration_id
            )

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

        The Stage 6 report id is passed through when the final state carries one,
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


# ---------------------------------------------------------------------------
# Startup seam fallback (Req. 12.2, 12.3)
# ---------------------------------------------------------------------------
#
# A deployment may ship only a subset of the AI stage agents; every non-deployed
# stage runs on its existing CURATION implementation behind the same seam without
# error (Req. 12.2). And when a DEPLOYED stage agent fails to INITIALIZE at
# startup, the system must fall back to that stage's curation implementation
# behind the SAME seam and record a diagnostic naming the affected stage and the
# fallback (Req. 12.3).
#
# ``resolve_stage_agent`` is that fallback-aware factory: it tries to initialize
# the AI implementation for a stage and, on ANY initialization failure, returns
# the stage's curation implementation instead — both satisfy the same
# ``@runtime_checkable`` seam Protocol (``DagGenerator`` /
# ``OperationalizerByOrigin`` / ``EnvironmentProvisioner``), so the caller is
# origin-agnostic. All imports are lazy so importing this module opens no socket
# and requires neither LangChain nor the ORM at import time (Req. 1.7).

# The three stages that expose a per-origin seam an AI agent can plug into.
_STAGE_DAG = "dag"
_STAGE_OPERATIONALIZE = "operationalize"
_STAGE_ENVIRONMENT = "environment"

_SEAM_STAGES = (_STAGE_DAG, _STAGE_OPERATIONALIZE, _STAGE_ENVIRONMENT)


def _init_ai_dag_generator() -> Any:
    """Initialize the Stage 3 AI DAG generator (lazy)."""
    from app.services.ai.agents.dag_agent import AiDagGenerator

    return AiDagGenerator()


def _init_ai_operationalizer() -> Any:
    """Initialize the Stage 4 AI operationalizer (lazy)."""
    from app.services.ai.agents.op_agent import AiOperationalizer

    return AiOperationalizer()


def _init_ai_environment_provisioner() -> Any:
    """Initialize the Stage 5 AI environment provisioner (lazy)."""
    from app.services.ai.agents.env_agent import AiEnvironmentProvisioner

    return AiEnvironmentProvisioner()


def _init_curation_dag_generator() -> Any:
    """Initialize the Stage 3 curation DAG generator (lazy, fallback)."""
    from app.services.dag.generator import CurationDagGenerator

    return CurationDagGenerator()


def _init_curation_operationalizer() -> Any:
    """Initialize the Stage 4 curation operationalizer (lazy, fallback)."""
    from app.services.environment.operationalizer import CurationOperationalizer

    return CurationOperationalizer()


def _init_curation_environment_provisioner() -> Any:
    """Initialize the Stage 5 curation environment provisioner (lazy, fallback)."""
    from app.services.environment.provisioner import CurationEnvironmentProvisioner

    return CurationEnvironmentProvisioner()


# Per-stage initializers: (AI initializer, curation-fallback initializer). Both
# sides of each pair satisfy the SAME seam Protocol, so swapping one for the other
# is transparent to the caller (Req. 12.2, 12.3).
_STAGE_INITIALIZERS: dict[str, tuple[Any, Any]] = {
    _STAGE_DAG: (_init_ai_dag_generator, _init_curation_dag_generator),
    _STAGE_OPERATIONALIZE: (
        _init_ai_operationalizer,
        _init_curation_operationalizer,
    ),
    _STAGE_ENVIRONMENT: (
        _init_ai_environment_provisioner,
        _init_curation_environment_provisioner,
    ),
}


def resolve_stage_agent(stage: str, *, prefer_ai: bool = True) -> Any:
    """Resolve a stage's seam implementation, falling back to curation (Req. 12.3).

    Resolves the implementation that plugs into a stage's existing per-origin seam
    (``DagGenerator`` for ``"dag"``, ``OperationalizerByOrigin`` for
    ``"operationalize"``, ``EnvironmentProvisioner`` for ``"environment"``). When
    ``prefer_ai`` is true the AI implementation is tried first; if initializing it
    raises for ANY reason (a missing dependency, a configuration error, an import
    failure — a stage agent failing to initialize at startup), the stage's
    CURATION implementation is initialized and returned instead, behind the SAME
    seam, and a diagnostic is logged naming the AFFECTED STAGE and the FALLBACK
    (Req. 12.3). Both implementations satisfy the same ``@runtime_checkable``
    Protocol, so the caller consumes either one identically (Req. 12.2).

    When ``prefer_ai`` is false the curation implementation is returned directly —
    the case of a stage whose AI agent is simply not deployed (Req. 12.2), which
    runs on curation behind the same seam without raising.

    All collaborators are imported lazily so calling this never opens a socket and
    requires neither LangChain nor the ORM unless a stage is actually resolved
    (Req. 1.7).

    Args:
        stage: One of ``"dag"``, ``"operationalize"`` or ``"environment"``.
        prefer_ai: When true (default) try the AI implementation first and fall
            back to curation on an initialization failure; when false return the
            curation implementation directly.

    Returns:
        A seam implementation for ``stage`` — the AI one when it initializes and
        ``prefer_ai`` is set, otherwise the curation one.

    Raises:
        ValueError: when ``stage`` is not one of the three seam stages.
    """
    try:
        init_ai, init_curation = _STAGE_INITIALIZERS[stage]
    except KeyError:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {_SEAM_STAGES}."
        ) from None

    if not prefer_ai:
        # The AI agent for this stage is not deployed: run on curation behind the
        # same seam without raising (Req. 12.2).
        return init_curation()

    try:
        return init_ai()
    except Exception as exc:  # noqa: BLE001 - any init failure -> curation fallback
        # A deployed stage agent failed to initialize at startup: fall back to the
        # curation implementation behind the same seam and record a diagnostic
        # naming the affected stage and the fallback (Req. 12.3).
        logger.error(
            "Stage '%s' agent failed to initialize at startup (%s: %s); falling "
            "back to its curation implementation behind the same seam.",
            stage,
            type(exc).__name__,
            exc,
        )
        return init_curation()
