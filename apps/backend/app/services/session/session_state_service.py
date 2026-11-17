"""SessionStateService — read/write the Session State (task 9.1).

The Session State gives the Researcher continuity across machines (Req. 9):
it records the **current case** (``current_campaign``), the **completed phases per
case** and the **per-Ability operation results per case**. Only the current
case is stored directly on the ``session_state`` row (``SessionState``); the
completed phases and the results are **derived** from the ``PhaseRun`` and
``Operation``/``AbilityResult`` rows already persisted by the phase/Operation
services, associated with the case (Req. 9.1, 9.7). This avoids duplicating —
and risking divergence from — the authoritative phase/operation tables.

What this service does (task 9.1)
---------------------------------
* :meth:`SessionStateService.get_session_state` — retrieve the persisted state
  on open (Req. 9.4): the current case plus the completed-phases-per-case map.
  When **no** session state exists (Req. 9.5) it returns a clean *default*
  state (``current_campaign is None`` and an empty completed-phases map) that the UI
  renders as every case with all phases "nao_iniciado". If the persisted state
  cannot be read (Req. 9.6) it returns a typed result flagged
  ``restore_failed=True`` carrying a "could not restore progress" message
  **without discarding** the persisted row.
* :meth:`SessionStateService.set_current_case` — persist the current case
  (``current_campaign``) so re-opening on another machine restores it (Req. 9.4).
* :meth:`SessionStateService.get_completed_phases` — completed phases per case
  derived from ``PhaseRun`` (Req. 9.1).
* :meth:`SessionStateService.get_operation_results` — per-Ability operation
  results for a case, derived from ``Operation`` (``campaign_id``) →
  ``AbilityResult`` (Req. 9.1, 9.7).

Injectable Session, naive UTC timestamps
-----------------------------------------
Like :class:`~app.services.audit.AuditLogger`, this service takes an injected
SQLAlchemy :class:`~sqlalchemy.orm.Session` so the same code works with the
app's ``SessionLocal`` and an ephemeral test SQLite session — no global state.
``updated_at`` uses **naive UTC** (``datetime.utcnow``), consistent with the
rest of the backend, so it round-trips identically on PostgreSQL (the production
database) and the ephemeral test SQLite.

Scope boundaries
----------------
* This module implements reading/writing the current case + deriving the
  completed-phases/results views (task 9.1) **and** the "preserve the previously
  persisted state on a phase-completion persist failure" guard (task 9.2, via
  :meth:`SessionStateService.persist_phase_completion`). It does not run any
  phase/operation logic itself.
* Preferences (theme/language) persistence is handled at the API level in
  tasks 13/14; it is intentionally **not** part of this service.

Phase-completion persistence with previous-state preservation (task 9.2)
------------------------------------------------------------------------
When the Researcher concludes a phase of a case, that conclusion must be
persisted **before** it is reported as done (Req. 9.2), and the completed
phases per case are the ``PhaseRun`` rows in the ``completed`` state (Req. 9.1).
:meth:`SessionStateService.persist_phase_completion` upserts the
``PhaseRun`` for ``(campaign_id, phase)`` to ``completed`` and bumps the
session-state ``updated_at`` in a **single transaction**. If that commit
fails, the service rolls back — leaving the previously persisted state exactly
as it was, with **no partial mutation** — and returns a typed
:class:`PhaseCompletionResult` flagged ``saved=False`` carrying the "completion
was not saved" message (Req. 9.3). Either the completion is fully persisted or
nothing changes (atomicity).

_Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import (
    AbilityResult,
    Operation,
    PhaseRun,
    SessionState,
)
from app.models.enums import (
    PHASE_ORDER,
    AbilityResultStatus,
    OperationState,
    PhaseState,
)

# ---------------------------------------------------------------------------
# Typed views (Input/Output for the API / UI later — task 11.1)
# ---------------------------------------------------------------------------


class AbilityResultView(BaseModel):
    """One per-Ability operation result (status + command output) — Req. 9.1.

    Mirrors :class:`~app.models.domain.AbilityResult`, carrying the status and
    the command output so the UI can render the operationalization output per
    Ability, plus the owning ``operation_id`` for grouping.

    For the live technique-by-technique tracking of the execution phase (Req. 2.4), the view
    also carries the additive, backward-compatible fields now persisted on
    :class:`~app.models.domain.AbilityResult`: the concrete ``command`` of the
    link, the ``target_container`` (e.g. ``"nginx (192.168.20.30)"``) and the
    ``attacker_host`` (e.g. ``kali``). They are optional and default to ``None``
    for not-yet-collected data, which the UI renders as "não disponível"
    (Req. 2.5). The pre-existing ``command_output`` is unchanged.
    """

    operation_id: int | None = None
    ability_id: str | None = None
    status: AbilityResultStatus
    command_output: str | None = None
    # Additive per-Ability fields for execution-phase live tracking (Req. 2.4). ``None``
    # when not yet collected — the UI shows "não disponível" (Req. 2.5).
    command: str | None = None
    target_container: str | None = None
    attacker_host: str | None = None


class OperationResultView(BaseModel):
    """A case's Operation with its per-Ability results and aggregate — Req. 9.7.

    Groups the ``AbilityResult`` rows under their owning ``Operation`` so the
    result set stays associated with the case (Req. 9.7). ``total_success`` /
    ``total_failure`` are the persisted aggregate on the Operation (Req. 4.5).
    """

    operation_id: int
    campaign_id: str | None = None
    state: OperationState
    total_success: int = 0
    total_failure: int = 0
    # Operation lifecycle instants (Req. 4.1). ``None`` by default — the read
    # path populates them from the persisted ``Operation`` so ``get_report`` can
    # derive the real total execution time (``finished_at - started_at``)
    # through a single read port. Absent instants stay ``None`` (Req. 4.3).
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    results: list[AbilityResultView] = Field(default_factory=list)


class PreferencesView(BaseModel):
    """Persisted theme/language preferences echoed in the session state.

    Carries the persisted enum *values* (the pt-BR strings ``"claro"/"escuro"``
    and ``"pt-BR"/"en"``) so the frontend can restore them on open (Req. 7.5,
    8.5). Defined before :class:`SessionStateView` so it can be referenced by it.
    """

    theme: str
    language: str


class SessionStateView(BaseModel):
    """The retrieved Session State on open (Req. 9.4/9.5/9.6).

    Attributes
    ----------
    current_campaign:
        The persisted current case, or ``None`` when there is no session state
        yet (default state — Req. 9.5) or none was ever set.
    completed_phases_by_campaign:
        Map of ``campaign_id -> [completed Phase_Names]`` derived from
        ``PhaseRun`` (Req. 9.1), in phase order. A case absent from the map (or
        with an empty list) has no completed phase; the UI renders its phases as
        "nao_iniciado" (Req. 9.5).
    updated_at:
        When the session-state row was last updated, or ``None`` in the default
        state.
    exists:
        ``True`` when a persisted ``session_state`` row was found. ``False`` is
        the default all-not-started state (Req. 9.5).
    restore_failed:
        ``True`` when the persisted state could not be read (Req. 9.6). The
        persisted row is **not** discarded; ``message`` carries the
        "could not restore progress" message for the API to surface.
    message:
        Optional human-readable message. Set to the Req. 9.6 message when
        ``restore_failed`` is ``True``.
    """

    current_campaign: str | None = None
    completed_phases_by_campaign: dict[str, list[str]] = Field(default_factory=dict)
    updated_at: dt.datetime | None = None
    exists: bool = False
    restore_failed: bool = False
    message: str | None = None
    # Persisted theme/language preferences (Req. 7.5, 8.5). ``None`` when nothing
    # was persisted yet (first visit) so the frontend falls back to its defaults
    # (Light Mode / pt-BR). Preferences are owned at the API layer; this field is
    # populated by the session-state endpoint, not by the service itself.
    preferences: PreferencesView | None = None


class PhaseCompletionResult(BaseModel):
    """Outcome of persisting a phase completion (Req. 9.2, 9.3).

    Returned by :meth:`SessionStateService.persist_phase_completion`. It signals
    whether the completion was actually persisted and carries the completed
    phases per case as they stand **after** the attempt, so the caller (and the
    Property 9 test) can confirm the previously persisted state is intact when a
    persist failure occurred.

    Attributes
    ----------
    saved:
        ``True`` when the phase completion was fully persisted; ``False`` when
        the persist failed and the previously persisted state was preserved
        unchanged (Req. 9.3). The Backend must not report the conclusion to the
        Researcher when this is ``False`` (Req. 9.2).
    campaign_id / phase:
        The case and Phase_Name the completion was for.
    completed_phases_by_campaign:
        The completed-phases-per-case map (derived from ``PhaseRun``) as it
        stands after the attempt: it *includes* the new completion on success,
        and is *identical to the previously persisted state* on failure.
    message:
        ``None`` on success; the "completion was not saved" message
        (:data:`SAVE_FAILED_MESSAGE`) on failure, for the API/UI to surface
        (Req. 9.3).
    """

    saved: bool
    campaign_id: str
    phase: str
    completed_phases_by_campaign: dict[str, list[str]] = Field(default_factory=dict)
    message: str | None = None


# Message surfaced when the persisted state cannot be read (Req. 9.6). The API
# turns this into the user-facing "progress could not be restored" notice.
RESTORE_FAILED_MESSAGE = (
    "Não foi possível restaurar o progresso: o Estado_de_Sessão persistido "
    "está indisponível ou não pôde ser lido."
)

# Message surfaced when persisting a phase completion fails (Req. 9.3). The
# previously persisted Session State is preserved unchanged; the UI turns
# this into the user-facing "the completion was not saved" notice.
SAVE_FAILED_MESSAGE = (
    "A conclusão da fase não foi salva: falha ao persistir na base de dados. "
    "O progresso anteriormente registrado foi preservado sem alteração."
)


# Order in which completed phases are listed: the user ``input`` phase, then the
# agentic phases in ``PHASE_ORDER``.
_PHASE_SEQUENCE: tuple[str, ...] = ("input", *PHASE_ORDER)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class SessionStateService:
    """Read and write the Session State (task 9.1).

    Parameters
    ----------
    session:
        An open SQLAlchemy :class:`~sqlalchemy.orm.Session`, injected by the
        caller. The service does not open or close it; it commits on writes and
        rolls back on failure. Reads never mutate the session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- retrieval (Req. 9.4 / 9.5 / 9.6) --------------------------------

    def get_session_state(self) -> SessionStateView:
        """Retrieve the Session State on open.

        Behavior:

        * **State present** (Req. 9.4): returns the persisted ``current_campaign`` and
          the completed-phases-per-case map derived from ``PhaseRun``.
        * **No state** (Req. 9.5): returns a default view (``current_campaign`` is
          ``None``, empty completed-phases map, ``exists=False``) that the UI
          renders as every case with all phases "nao_iniciado".
        * **Unreadable state** (Req. 9.6): returns a view flagged
          ``restore_failed=True`` with :data:`RESTORE_FAILED_MESSAGE`, **without
          deleting or overwriting** the persisted row. The API turns this into
          the Req. 9.6 "progress could not be restored" message.

        This method never raises for a read failure — the failure is reported in
        the returned view.
        """
        try:
            state = self._load_state_row()
        except SQLAlchemyError:
            # The persisted state is unavailable/unreadable. Do NOT discard it —
            # only surface a clean signal (Req. 9.6). Roll back so the session
            # stays usable for the caller.
            self._session.rollback()
            return SessionStateView(
                exists=False,
                restore_failed=True,
                message=RESTORE_FAILED_MESSAGE,
            )

        if state is None:
            # No persisted state yet — default all-not-started (Req. 9.5).
            return SessionStateView(exists=False)

        try:
            completed = self.get_completed_phases()
        except SQLAlchemyError:
            # The session row exists but the derived view could not be read —
            # still a "could not restore progress" case (Req. 9.6), without
            # discarding the persisted state.
            self._session.rollback()
            return SessionStateView(
                current_campaign=state.current_campaign,
                updated_at=state.updated_at,
                exists=True,
                restore_failed=True,
                message=RESTORE_FAILED_MESSAGE,
            )

        return SessionStateView(
            current_campaign=state.current_campaign,
            completed_phases_by_campaign=completed,
            updated_at=state.updated_at,
            exists=True,
        )

    def get_completed_phases(self) -> dict[str, list[str]]:
        """Completed phases per case, derived from ``PhaseRun`` (Req. 9.1).

        Returns a map ``campaign_id -> [Phase_Names in ``completed`` state]`` in
        phase order (``input`` first, then the agentic phases). Cases with no
        completed phase are omitted (the UI treats them as all "nao_iniciado" — Req. 9.5). Rows with
        a ``NULL`` ``campaign_id`` are ignored.
        """
        stmt = (
            select(PhaseRun.campaign_id, PhaseRun.phase)
            .where(PhaseRun.state == PhaseState.COMPLETED)
            .where(PhaseRun.campaign_id.is_not(None))
        )
        completed: dict[str, set[str]] = {}
        for campaign_id, phase in self._session.execute(stmt).all():
            completed.setdefault(campaign_id, set()).add(phase)
        return {
            campaign_id: [phase for phase in _PHASE_SEQUENCE if phase in phases]
            for campaign_id, phases in completed.items()
        }

    def get_operation_results(self, campaign_id: str) -> list[OperationResultView]:
        """Per-Ability operation results for a case (Req. 9.1, 9.7).

        Walks ``Operation`` rows for the case (``Operation.campaign_id == campaign_id``)
        and, for each, its ``AbilityResult`` rows (``AbilityResult.operation_id``),
        so the results stay associated with the case (Req. 9.7). Operations are
        ordered by ``id`` (creation order); results within an operation by
        ``id`` too, for a stable view.
        """
        op_stmt = (
            select(Operation)
            .where(Operation.campaign_id == campaign_id)
            .order_by(Operation.id.asc())
        )
        operations = list(self._session.execute(op_stmt).scalars().all())

        views: list[OperationResultView] = []
        for op in operations:
            res_stmt = (
                select(AbilityResult)
                .where(AbilityResult.operation_id == op.id)
                .order_by(AbilityResult.id.asc())
            )
            results = list(self._session.execute(res_stmt).scalars().all())
            views.append(
                OperationResultView(
                    operation_id=op.id,
                    campaign_id=op.campaign_id,
                    state=op.state,
                    total_success=op.total_success,
                    total_failure=op.total_failure,
                    started_at=op.started_at,
                    finished_at=op.finished_at,
                    results=[
                        AbilityResultView(
                            operation_id=r.operation_id,
                            ability_id=r.ability_id,
                            status=r.status,
                            command_output=r.command_output,
                            command=r.command,
                            target_container=r.target_container,
                            attacker_host=r.attacker_host,
                        )
                        for r in results
                    ],
                )
            )
        return views

    # ---- writes (Req. 9.4) -----------------------------------------------

    def set_current_case(self, slug: str | None) -> SessionState:
        """Persist the current case (``current_campaign``) — Req. 9.4.

        Upserts the single ``session_state`` row (one session-state row is kept)
        so re-opening on any machine restores the current case. Updates
        ``updated_at`` to the current naive UTC instant.

        Passing ``slug=None`` clears the current case (leaves the row present
        with ``current_campaign`` NULL), which the UI renders as "no case selected".

        Note (task 9.2 seam): this write commits directly. Task 9.2 will add the
        "preserve the previously persisted state on a persist failure" guard
        around phase-completion persistence; the read side above does not need to
        change for that.

        Raises
        ------
        SQLAlchemyError
            Propagated if the commit fails, after rolling back so the session
            stays usable. (Task 9.2 will layer the preserve-previous-state
            behavior on top of this seam.)
        """
        state = self._get_or_create_state_row()
        state.current_campaign = slug
        state.updated_at = _utcnow()
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            raise
        self._session.refresh(state)
        return state

    def persist_phase_completion(self, campaign_id: str, phase: str) -> PhaseCompletionResult:
        """Persist the completion of a phase, preserving prior state on failure.

        When the Researcher concludes a phase of a case, that conclusion is the
        ``PhaseRun`` for ``(campaign_id, phase)`` transitioning to ``completed``
        (Req. 9.1). This method upserts that ``PhaseRun`` to ``completed`` and
        bumps the session-state ``updated_at`` in a **single transaction**,
        so the persisted Session State advances atomically before the
        conclusion is reported to the Researcher (Req. 9.2).

        Atomicity / previous-state preservation (Req. 9.3)
        --------------------------------------------------
        Either the completion is **fully** persisted, or **nothing** changes:

        * **Success** — returns :class:`PhaseCompletionResult` with
          ``saved=True`` and the completed-phases map *including* the new
          completion.
        * **Persist failure** — the session is rolled back, which discards the
          pending ``PhaseRun``/``session_state`` mutations, leaving the
          previously persisted state **exactly as it was** (no partial update).
          Returns :class:`PhaseCompletionResult` with ``saved=False``, the
          completed-phases map *as previously persisted* (re-read after
          rollback), and :data:`SAVE_FAILED_MESSAGE` so the UI can report that
          the conclusion was not saved. It does **not** raise — the failure is a
          typed signal in the returned result, matching the read-side contract.

        Parameters
        ----------
        campaign_id:
            The case whose phase was completed.
        phase:
            The Phase_Name that was completed (``input``, ``operationalization``,
            ``flow``, ``environment`` or ``execution``). A name with no persisted
            phase row raises ``ValueError``.

        Returns
        -------
        PhaseCompletionResult
            ``saved=True`` on success; ``saved=False`` (with
            :data:`SAVE_FAILED_MESSAGE`) on a persist failure, in which case the
            previously persisted state is preserved unchanged.
        """
        phase_run = self._get_or_create_phase_run(campaign_id, phase=phase)
        phase_run.state = PhaseState.COMPLETED
        phase_run.progress = 100
        # Clear any prior error markers now that the phase is concluded.
        phase_run.error_message = None
        phase_run.failed_step = None
        now = _utcnow()
        phase_run.updated_at = now

        # Advance the single session-state row's timestamp in the same
        # transaction so the completion and the Session State commit (or roll
        # back) together — no partial mutation (Req. 9.3).
        state = self._get_or_create_state_row()
        state.updated_at = now

        try:
            self._session.commit()
        except SQLAlchemyError:
            # Persist failed: roll back so the pending PhaseRun/session_state
            # mutations are discarded, leaving the previously persisted state
            # exactly as it was (Req. 9.3 — no partial update).
            self._session.rollback()
            # Re-read the completed phases as they stand *after* the rollback:
            # this is the previously persisted state, unchanged.
            try:
                preserved = self.get_completed_phases()
            except SQLAlchemyError:
                # The DB is unreadable even for the read-back; still report the
                # completion as not saved and surface an empty view rather than
                # raising. The persisted rows are not discarded.
                self._session.rollback()
                preserved = {}
            return PhaseCompletionResult(
                saved=False,
                campaign_id=campaign_id,
                phase=phase,
                completed_phases_by_campaign=preserved,
                message=SAVE_FAILED_MESSAGE,
            )

        self._session.refresh(phase_run)
        return PhaseCompletionResult(
            saved=True,
            campaign_id=campaign_id,
            phase=phase,
            completed_phases_by_campaign=self.get_completed_phases(),
        )

    # ---- helpers ----------------------------------------------------------

    def _load_state_row(self) -> SessionState | None:
        """Return the single persisted ``session_state`` row, or ``None``.

        Reads the lowest-``id`` row so behavior is deterministic even if more
        than one row were ever written. Never creates a row.
        """
        stmt = select(SessionState).order_by(SessionState.id.asc())
        return self._session.execute(stmt).scalars().first()

    def _get_or_create_state_row(self) -> SessionState:
        """Return the single ``session_state`` row, creating it if absent."""
        state = self._load_state_row()
        if state is None:
            state = SessionState()
            self._session.add(state)
        return state

    def _get_or_create_phase_run(self, campaign_id: str, *, phase: str) -> PhaseRun:
        """Return the ``PhaseRun`` for ``(campaign_id, phase)``, creating it if absent.

        One row per ``(campaign_id, phase)`` is kept (upsert contract), so persisting a
        completion updates the existing run rather than piling up rows.
        """
        if phase not in _PHASE_SEQUENCE:
            raise ValueError(f"Unknown phase for a persisted run: {phase!r}")
        stmt = (
            select(PhaseRun)
            .where(PhaseRun.campaign_id == campaign_id)
            .where(PhaseRun.phase == phase)
            .order_by(PhaseRun.id.asc())
        )
        phase_run = self._session.execute(stmt).scalars().first()
        if phase_run is None:
            # ``input`` keeps the former number 1 (``stix_package``) so the migration
            # ``downgrade`` can map every row back to a legacy number.
            subphase = "stix_package" if phase == "input" else None
            phase_run = PhaseRun(campaign_id=campaign_id, phase=phase, subphase=subphase)
            self._session.add(phase_run)
        return phase_run


__all__ = [
    "RESTORE_FAILED_MESSAGE",
    "SAVE_FAILED_MESSAGE",
    "AbilityResultView",
    "OperationResultView",
    "PhaseCompletionResult",
    "SessionStateService",
    "SessionStateView",
]
