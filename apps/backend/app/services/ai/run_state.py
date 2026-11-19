"""Run state transitions in their own session (interactive-emulation-run, D4).

The phase job writes the ``agent_runs`` row through this store and NOT through
the working session that executes the graph. Every transition opens a fresh
session, updates the row and commits immediately, so:

* a rollback of the working session (or of the HTTP request) never discards the
  run state;
* a failure never leaves a half-written row: each transition sets all the
  columns it owns in one commit (for example ``fail`` writes ``status``,
  ``failure``, ``error`` and clears ``final_verdict_success`` together).

Transitions are guarded: a run already in a terminal state (``completed``,
``failed`` or ``aborted``) is never moved by ``mark_running``,
``pause_for_approval`` or ``finish``, which keeps a researcher abort from being
overwritten by a late phase job. ``fail`` and ``ensure_not_running`` only act on
non-terminal rows.

``project_checkpoint`` reads ``get_state(config)`` from the compiled graph and
projects the pause position on ``status`` and ``current_phase``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.domain import AgentRun
from app.models.enums import AgentRunStatus

logger = logging.getLogger(__name__)

# Graph node name -> phase name stored in ``agent_runs.current_phase`` and used
# by the approval routes.
NODE_TO_PHASE: dict[str, str] = {
    "orchestrate": "orchestration",
    "operationalize": "operationalization",
    "flow": "flow",
    "environment": "environment",
    "execute": "execution",
    "evaluate": "evaluation",
}

# The phase waiting for approval, given the node the graph will run next. The
# graph pauses AFTER ``operationalize``, ``flow`` and ``environment``.
_PENDING_PHASE_BY_NEXT_NODE: dict[str, str] = {
    "flow": "operationalization",
    "environment": "flow",
    "execute": "environment",
}

_TERMINAL = frozenset({AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.ABORTED})

SessionFactory = Callable[[], Session]


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp, matching ``app.models.domain._utcnow``."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def phase_for_node(node: str | None) -> str | None:
    """Return the phase name of a graph node, or ``None`` when unknown."""
    return NODE_TO_PHASE.get(node) if node else None


def pending_phase_from_next(next_nodes: tuple[str, ...] | list[str]) -> str | None:
    """Return the phase awaiting approval for a checkpoint's ``next`` nodes."""
    for node in next_nodes:
        phase = _PENDING_PHASE_BY_NEXT_NODE.get(node)
        if phase is not None:
            return phase
    return None


def build_flow_proposal_entry(flow_payload: dict[str, Any], *, proposal_id: int) -> dict[str, Any]:
    """Build one ``flow_proposals`` entry from a serialized ``ProducedFlow``.

    ``flow_id`` is the documented (corpus) flow that an ``exact`` copy reproduces,
    the only id the exclusion policy can match; it is ``None`` for ``adapted`` and
    ``created`` flows, which are told apart by ``flow_hash``.
    """
    provenance = flow_payload.get("provenance") or {}
    kind = provenance.get("kind")
    references = list(provenance.get("references") or [])
    flow_id = references[0] if kind == "exact" and references else None
    return {
        "proposal_id": proposal_id,
        "flow_id": flow_id if isinstance(flow_id, int) else None,
        # ``origin``, ``identifier`` and ``gaps`` let ``select_proposal`` rebuild the
        # ``ProducedFlow`` from its ``attack_flows`` row.
        "origin": flow_payload.get("origin"),
        "identifier": flow_payload.get("identifier"),
        "gaps": list(flow_payload.get("gaps") or []),
        "flow_hash": provenance.get("flow_hash"),
        "kind": kind,
        "references": references,
        "similarity": provenance.get("similarity"),
        "reason_code": provenance.get("reason_code"),
        "coverage": provenance.get("coverage"),
        "presented_at": _utcnow().isoformat(),
        "current": True,
    }


def exclusions_from_proposals(proposals: list[Any] | None) -> dict[str, list]:
    """Collect ``excluded_flow_ids`` and ``excluded_hashes`` from the proposal history."""
    flow_ids: list[int] = []
    hashes: list[str] = []
    for proposal in proposals or []:
        if not isinstance(proposal, dict):
            continue
        flow_id = proposal.get("flow_id")
        if isinstance(flow_id, int) and flow_id not in flow_ids:
            flow_ids.append(flow_id)
        flow_hash = proposal.get("flow_hash")
        if isinstance(flow_hash, str) and flow_hash not in hashes:
            hashes.append(flow_hash)
    return {"excluded_flow_ids": flow_ids, "excluded_hashes": hashes}


class RunStateStore:
    """Transitions of ``agent_runs`` in their own session, committed at once."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        """Create the store.

        Args:
            session_factory: Zero-argument callable returning a new session.
                ``None`` resolves ``app.db.session.SessionLocal`` lazily so
                tests can override it.
        """
        self._session_factory = session_factory

    @property
    def session_factory(self) -> SessionFactory | None:
        """The injected session factory (``None`` means ``SessionLocal``)."""
        return self._session_factory

    def _open(self) -> Session:
        factory = self._session_factory
        if factory is None:
            from app.db.session import SessionLocal

            factory = SessionLocal
        return factory()

    def _transition(self, run_id: int, apply: Callable[[AgentRun], bool]) -> bool:
        """Load the run, let ``apply`` mutate it and commit; roll back on error.

        ``apply`` returns ``False`` to skip the write (guard not satisfied).
        Returns whether the row was changed.
        """
        with self._open() as session:
            run = session.get(AgentRun, run_id)
            if run is None:
                logger.warning("Run %s not found for a state transition.", run_id)
                return False
            try:
                if not apply(run):
                    session.rollback()
                    return False
                run.updated_at = _utcnow()
                session.commit()
            except Exception:
                session.rollback()
                raise
            return True

    def begin_phase(
        self,
        run_id: int,
        expected_phase: str,
        next_phase: str,
        *,
        approval: dict[str, Any] | None = None,
    ) -> bool:
        """Atomically move ``awaiting_approval`` on ``expected_phase`` to ``running``.

        One conditional ``UPDATE ... WHERE status = 'awaiting_approval' AND
        current_phase = :expected_phase`` committed at once (D4b). It returns
        ``True`` only for the single caller whose update matched a row, so two
        concurrent approvals of the same phase start one round. ``approval``, when
        given, is appended to the ``approvals`` log in the same statement.
        """
        with self._open() as session:
            current = session.get(AgentRun, run_id)
            if current is None:
                return False
            values: dict[str, Any] = {
                "status": AgentRunStatus.RUNNING,
                "current_phase": next_phase,
                "notice_code": None,
                "updated_at": _utcnow(),
            }
            if approval is not None:
                values["approvals"] = [*(current.approvals or []), approval]
            try:
                result = session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.status == AgentRunStatus.AWAITING_APPROVAL,
                        AgentRun.current_phase == expected_phase,
                    )
                    .values(**values)
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            return result.rowcount == 1

    def release_phase(
        self, run_id: int, running_phase: str, back_to_phase: str, *, undo_approval: bool = False
    ) -> bool:
        """Undo :meth:`begin_phase` when the round could not be dispatched.

        Moves ``running`` on ``running_phase`` back to ``awaiting_approval`` on
        ``back_to_phase`` and, with ``undo_approval``, drops the approval entry that
        :meth:`begin_phase` appended.
        """

        def apply(run: AgentRun) -> bool:
            if run.status != AgentRunStatus.RUNNING or run.current_phase != running_phase:
                return False
            run.status = AgentRunStatus.AWAITING_APPROVAL
            run.current_phase = back_to_phase
            if undo_approval and run.approvals:
                run.approvals = list(run.approvals)[:-1]
            return True

        return self._transition(run_id, apply)

    def abort_run(self, run_id: int) -> bool:
        """Atomically move ``awaiting_approval`` to ``aborted`` (researcher abort).

        Same guard style as :meth:`begin_phase`: one conditional ``UPDATE`` that
        matches only a paused run, so it cannot race an approval. Returns ``True``
        for the single caller that won.
        """
        with self._open() as session:
            try:
                result = session.execute(
                    update(AgentRun)
                    .where(
                        AgentRun.id == run_id,
                        AgentRun.status == AgentRunStatus.AWAITING_APPROVAL,
                    )
                    .values(
                        status=AgentRunStatus.ABORTED,
                        current_phase=None,
                        final_verdict_success=None,
                        notice_code=None,
                        updated_at=_utcnow(),
                    )
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            return result.rowcount == 1

    def select_flow_proposal(self, run_id: int, proposal_id: int) -> bool:
        """Mark ``proposal_id`` as the only ``current`` entry of ``flow_proposals``."""

        def apply(run: AgentRun) -> bool:
            history = [dict(p) for p in run.flow_proposals or [] if isinstance(p, dict)]
            if not any(p.get("proposal_id") == proposal_id for p in history):
                return False
            for proposal in history:
                proposal["current"] = proposal.get("proposal_id") == proposal_id
            run.flow_proposals = history
            run.notice_code = None
            return True

        return self._transition(run_id, apply)

    def mark_running(self, run_id: int, phase: str | None = None) -> bool:
        """Move a pending or paused run to ``running``, optionally naming the phase."""

        def apply(run: AgentRun) -> bool:
            if run.status in _TERMINAL:
                return False
            run.status = AgentRunStatus.RUNNING
            if phase is not None:
                run.current_phase = phase
            run.notice_code = None
            return True

        return self._transition(run_id, apply)

    def pause_for_approval(self, run_id: int, phase: str) -> bool:
        """Park the run in ``awaiting_approval``, waiting for ``phase`` to be approved."""

        def apply(run: AgentRun) -> bool:
            if run.status in _TERMINAL:
                return False
            run.status = AgentRunStatus.AWAITING_APPROVAL
            run.current_phase = phase
            return True

        return self._transition(run_id, apply)

    def finish(
        self,
        run_id: int,
        status: AgentRunStatus,
        *,
        verdict_success: bool | None = None,
        iterations_used: int | None = None,
    ) -> bool:
        """Write a ``completed`` or ``aborted`` outcome with its verdict summary."""
        if status not in (AgentRunStatus.COMPLETED, AgentRunStatus.ABORTED):
            raise ValueError(f"finish() only accepts completed or aborted, got {status!r}")

        def apply(run: AgentRun) -> bool:
            if run.status in _TERMINAL:
                return False
            run.status = status
            run.final_verdict_success = verdict_success
            if iterations_used is not None:
                run.iterations_used = iterations_used
            run.current_phase = None
            return True

        return self._transition(run_id, apply)

    def fail(
        self,
        run_id: int,
        phase: str | None,
        code: str = "phase_failed",
        cause_code: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Mark the run ``failed`` with a structured cause, in one commit.

        Retries once with a new session. If it still fails, the run id is
        logged and ``False`` is returned: ``ensure_not_running`` and the
        startup reconciler are the remaining defenses.
        """
        failure: dict[str, Any] = {"phase": phase, "code": code, "cause_code": cause_code}

        def apply(run: AgentRun) -> bool:
            if run.status in _TERMINAL:
                return False
            run.status = AgentRunStatus.FAILED
            run.failure = failure
            run.error = error
            run.final_verdict_success = None
            run.current_phase = phase
            run.notice_code = None
            return True

        for attempt in (1, 2):
            try:
                return self._transition(run_id, apply)
            except Exception:
                logger.exception(
                    "Could not record the failure of run %s (attempt %s).", run_id, attempt
                )
        logger.error("Run %s could not be marked failed; it may be left inconsistent.", run_id)
        return False

    def record_remaining_resources(self, run_id: int, resources: list[str]) -> bool:
        """Record the lab resources a failed teardown left behind.

        The list is merged into ``failure`` (kept next to the original cause, never
        replacing it). A run without a ``failure`` gets a minimal one plus the
        ``cleanup_incomplete`` notice so the researcher can still see the leftovers.
        """

        def apply(run: AgentRun) -> bool:
            failure = dict(run.failure or {})
            if not failure:
                failure = {"phase": run.current_phase, "code": "cleanup_incomplete"}
                run.notice_code = "cleanup_incomplete"
            failure["remaining_resources"] = list(resources)
            run.failure = failure
            return True

        return self._transition(run_id, apply)

    def set_notice(self, run_id: int, notice_code: str | None) -> bool:
        """Set (or clear, with ``None``) the non-fatal ``notice_code``."""

        def apply(run: AgentRun) -> bool:
            run.notice_code = notice_code
            return True

        return self._transition(run_id, apply)

    def record_flow_proposal(self, run_id: int, flow_payload: dict[str, Any]) -> int | None:
        """Append the attack flow just produced to ``agent_runs.flow_proposals``.

        ``flow_payload`` is the serialized ``ProducedFlow`` (``artifacts.flow``).
        The new entry gets the next sequential ``proposal_id`` and becomes the
        only one with ``current = true``. When the current proposal already has
        the same ``flow_hash`` (a resume that persisted the same flow twice) no
        entry is added.

        Returns the ``proposal_id`` that is current after the call, or ``None``
        when the run does not exist or the payload carries no flow.
        """
        if not flow_payload:
            return None
        result: list[int] = []

        def apply(run: AgentRun) -> bool:
            history = [dict(p) for p in run.flow_proposals or [] if isinstance(p, dict)]
            entry = build_flow_proposal_entry(flow_payload, proposal_id=len(history) + 1)
            current = next((p for p in history if p.get("current")), None)
            if (
                current is not None
                and entry["flow_hash"] is not None
                and current.get("flow_hash") == entry["flow_hash"]
            ):
                result.append(int(current["proposal_id"]))
                return False
            for proposal in history:
                proposal["current"] = False
            history.append(entry)
            run.flow_proposals = history
            result.append(entry["proposal_id"])
            return True

        self._transition(run_id, apply)
        return result[0] if result else None

    def flow_exclusions(self, run_id: int) -> dict[str, list]:
        """Return ``excluded_flow_ids``/``excluded_hashes`` built from the history."""
        with self._open() as session:
            run = session.get(AgentRun, run_id)
            proposals = run.flow_proposals if run is not None else None
        return exclusions_from_proposals(proposals)

    def set_planned_coverage(self, run_id: int, planned: dict[str, Any]) -> bool:
        """Merge the planned coverage into ``agent_runs.coverage``.

        Keys written by later phases (such as the achieved coverage) are kept.
        """

        def apply(run: AgentRun) -> bool:
            run.coverage = {**(run.coverage or {}), **planned}
            return True

        return self._transition(run_id, apply)

    def set_achieved_coverage(self, run_id: int, achieved: dict[str, Any]) -> bool:
        """Merge the achieved coverage into ``agent_runs.coverage`` (keeps other keys)."""
        return self.set_planned_coverage(run_id, achieved)

    def project_achieved_coverage(self, run_id: int, state: dict[str, Any]) -> None:
        """Persist the achieved coverage from the final execution report.

        Best effort: a failure here is logged and never breaks the run finish.
        """
        from app.services.ai.coverage import derive_achieved_coverage

        try:
            achieved = derive_achieved_coverage(state.get("artifacts") or {}, state.get("report"))
            if achieved is not None:
                self.set_achieved_coverage(run_id, achieved)
        except Exception:
            logger.exception("Achieved coverage of run %s could not be stored.", run_id)

    def ensure_not_running(self, run_id: int) -> bool:
        """Last line of defense: never leave the run in ``running``.

        If the row is still ``running`` it becomes ``failed`` with
        ``failure.code = "phase_failed"`` (keeping an existing ``failure``).
        Returns whether a correction was needed.
        """

        def apply(run: AgentRun) -> bool:
            if run.status != AgentRunStatus.RUNNING:
                return False
            run.status = AgentRunStatus.FAILED
            if run.failure is None:
                run.failure = {
                    "phase": run.current_phase,
                    "code": "phase_failed",
                    "cause_code": None,
                }
            run.final_verdict_success = None
            return True

        try:
            changed = self._transition(run_id, apply)
        except Exception:
            logger.exception("ensure_not_running failed for run %s.", run_id)
            return False
        if changed:
            logger.warning("Run %s was still 'running' on exit; marked failed.", run_id)
        return changed

    def _project_planned_coverage(self, run_id: int, snapshot: Any) -> None:
        """Persist the planned coverage once the operationalization artifact exists.

        Best effort: a failure here is logged and never breaks the run projection.
        """
        from app.services.ai.coverage import derive_planned_coverage

        try:
            values = dict(getattr(snapshot, "values", None) or {})
            planned = derive_planned_coverage(values.get("artifacts") or {})
            if planned is not None:
                self.set_planned_coverage(run_id, planned)
        except Exception:
            logger.exception("Planned coverage of run %s could not be stored.", run_id)

    def project_checkpoint(self, run_id: int, graph: Any, config: dict[str, Any]) -> str | None:
        """Project the graph checkpoint on the run row and return the pending phase.

        Reads ``graph.get_state(config)``. When the graph is paused before a
        node that needs approval, the run is parked with
        :meth:`pause_for_approval` and the approved-pending phase is returned.
        Otherwise (graph ended) ``None`` is returned and the caller decides the
        terminal status with :meth:`finish`, which also owns
        ``iterations_used`` and the verdict.
        """
        snapshot = graph.get_state(config)
        pending = pending_phase_from_next(tuple(getattr(snapshot, "next", ()) or ()))
        self._project_planned_coverage(run_id, snapshot)
        if pending is not None:
            self.pause_for_approval(run_id, pending)
        return pending
