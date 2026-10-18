"""SessionStateService — read/write the Session State (task 9.1).

The Session State gives the Researcher continuity across machines (Req. 9):
it records the **current case** (``current_campaign``), the **completed stages per
case** and the **per-Ability operation results per case**. Only the current
case is stored directly on the ``session_state`` row (``SessionState``); the
completed stages and the results are **derived** from the ``StageRun`` and
``Operation``/``AbilityResult`` rows already persisted by the Stage/Operation
services, associated with the case (Req. 9.1, 9.7). This avoids duplicating —
and risking divergence from — the authoritative stage/operation tables.

What this service does (task 9.1)
---------------------------------
* :meth:`SessionStateService.get_session_state` — retrieve the persisted state
  on open (Req. 9.4): the current case plus the completed-stages-per-case map.
  When **no** session state exists (Req. 9.5) it returns a clean *default*
  state (``current_campaign is None`` and an empty completed-stages map) that the UI
  renders as every case with all stages "nao_iniciado". If the persisted state
  cannot be read (Req. 9.6) it returns a typed result flagged
  ``restore_failed=True`` carrying a "could not restore progress" message
  **without discarding** the persisted row.
* :meth:`SessionStateService.set_current_case` — persist the current case
  (``current_campaign``) so re-opening on another machine restores it (Req. 9.4).
* :meth:`SessionStateService.get_completed_stages` — completed stages per case
  derived from ``StageRun`` (Req. 9.1).
* :meth:`SessionStateService.get_operation_results` — per-Ability operation
  results for a case, derived from ``Operation`` (``campaign_id``) →
  ``AbilityResult`` (Req. 9.1, 9.7).

Injectable Session, naive UTC timestamps
-----------------------------------------
Like :class:`~app.services.audit.AuditLogger` and
:class:`~app.services.stage.StageService`, this service takes an injected
SQLAlchemy :class:`~sqlalchemy.orm.Session` so the same code works with the
app's ``SessionLocal`` and an ephemeral test SQLite session — no global state.
``updated_at`` uses **naive UTC** (``datetime.utcnow``), consistent with the
rest of the backend, so it round-trips identically on PostgreSQL (the production
database) and the ephemeral test SQLite.

Scope boundaries
----------------
* This module implements reading/writing the current case + deriving the
  completed-stages/results views (task 9.1) **and** the "preserve the previously
  persisted state on a stage-completion persist failure" guard (task 9.2, via
  :meth:`SessionStateService.persist_stage_completion`). It does not run any
  stage/operation logic itself.
* Preferences (theme/language) persistence is handled at the API level in
  tasks 13/14; it is intentionally **not** part of this service.

Stage-completion persistence with previous-state preservation (task 9.2)
------------------------------------------------------------------------
When the Researcher concludes a Stage of a case, that conclusion must be
persisted **before** it is reported as done (Req. 9.2), and the completed
stages per case are the ``StageRun`` rows in the ``completed`` state (Req. 9.1).
:meth:`SessionStateService.persist_stage_completion` upserts the
``StageRun`` for ``(campaign_id, stage)`` to ``completed`` and bumps the
session-state ``updated_at`` in a **single transaction**. If that commit
fails, the service rolls back — leaving the previously persisted state exactly
as it was, with **no partial mutation** — and returns a typed
:class:`StageCompletionResult` flagged ``saved=False`` carrying the "completion
was not saved" message (Req. 9.3). Either the completion is fully persisted or
nothing changes (atomicity).

_Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.domain import (
    AbilityResult,
    Operation,
    SessionState,
    StageRun,
)
from app.models.enums import AbilityResultStatus, OperationState, StageState


# ---------------------------------------------------------------------------
# Typed views (Input/Output for the API / UI later — task 11.1)
# ---------------------------------------------------------------------------


class AbilityResultView(BaseModel):
    """One per-Ability operation result (status + command output) — Req. 9.1.

    Mirrors :class:`~app.models.domain.AbilityResult`, carrying the status and
    the command output so the UI can render the Emulation (Stage 4) output per
    Ability, plus the owning ``operation_id`` for grouping.

    For the live technique-by-technique tracking of Stage 6 (Req. 2.4), the view
    also carries the additive, backward-compatible fields now persisted on
    :class:`~app.models.domain.AbilityResult`: the concrete ``command`` of the
    link, the ``target_container`` (e.g. ``"nginx (192.168.20.30)"``) and the
    ``attacker_host`` (e.g. ``kali``). They are optional and default to ``None``
    for not-yet-collected data, which the UI renders as "não disponível"
    (Req. 2.5). The pre-existing ``command_output`` is unchanged.
    """

    operation_id: Optional[int] = None
    ability_id: Optional[str] = None
    status: AbilityResultStatus
    command_output: Optional[str] = None
    # Additive per-Ability fields for Stage 6 live tracking (Req. 2.4). ``None``
    # when not yet collected — the UI shows "não disponível" (Req. 2.5).
    command: Optional[str] = None
    target_container: Optional[str] = None
    attacker_host: Optional[str] = None


class OperationResultView(BaseModel):
    """A case's Operation with its per-Ability results and aggregate — Req. 9.7.

    Groups the ``AbilityResult`` rows under their owning ``Operation`` so the
    result set stays associated with the case (Req. 9.7). ``total_success`` /
    ``total_failure`` are the persisted aggregate on the Operation (Req. 4.5).
    """

    operation_id: int
    campaign_id: Optional[str] = None
    state: OperationState
    total_success: int = 0
    total_failure: int = 0
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
    completed_stages_by_campaign:
        Map of ``campaign_id -> [completed stage numbers]`` derived from
        ``StageRun`` (Req. 9.1). A case absent from the map (or with an empty
        list) has no completed stage; the UI renders its stages as
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

    current_campaign: Optional[str] = None
    completed_stages_by_campaign: dict[str, list[int]] = Field(default_factory=dict)
    updated_at: Optional[dt.datetime] = None
    exists: bool = False
    restore_failed: bool = False
    message: Optional[str] = None
    # Persisted theme/language preferences (Req. 7.5, 8.5). ``None`` when nothing
    # was persisted yet (first visit) so the frontend falls back to its defaults
    # (Light Mode / pt-BR). Preferences are owned at the API layer; this field is
    # populated by the session-state endpoint, not by the service itself.
    preferences: Optional[PreferencesView] = None


class StageCompletionResult(BaseModel):
    """Outcome of persisting a Stage completion (Req. 9.2, 9.3).

    Returned by :meth:`SessionStateService.persist_stage_completion`. It signals
    whether the completion was actually persisted and carries the completed
    stages per case as they stand **after** the attempt, so the caller (and the
    Property 9 test) can confirm the previously persisted state is intact when a
    persist failure occurred.

    Attributes
    ----------
    saved:
        ``True`` when the Stage completion was fully persisted; ``False`` when
        the persist failed and the previously persisted state was preserved
        unchanged (Req. 9.3). The Backend must not report the conclusion to the
        Researcher when this is ``False`` (Req. 9.2).
    campaign_id / stage:
        The case and Stage number the completion was for.
    completed_stages_by_campaign:
        The completed-stages-per-case map (derived from ``StageRun``) as it
        stands after the attempt: it *includes* the new completion on success,
        and is *identical to the previously persisted state* on failure.
    message:
        ``None`` on success; the "completion was not saved" message
        (:data:`SAVE_FAILED_MESSAGE`) on failure, for the API/UI to surface
        (Req. 9.3).
    """

    saved: bool
    campaign_id: str
    stage: int
    completed_stages_by_campaign: dict[str, list[int]] = Field(default_factory=dict)
    message: Optional[str] = None


# Message surfaced when the persisted state cannot be read (Req. 9.6). The API
# turns this into the user-facing "progress could not be restored" notice.
RESTORE_FAILED_MESSAGE = (
    "Não foi possível restaurar o progresso: o Estado_de_Sessão persistido "
    "está indisponível ou não pôde ser lido."
)

# Message surfaced when persisting a Stage completion fails (Req. 9.3). The
# previously persisted Session State is preserved unchanged; the UI turns
# this into the user-facing "the completion was not saved" notice.
SAVE_FAILED_MESSAGE = (
    "A conclusão do Estágio não foi salva: falha ao persistir na base de dados. "
    "O progresso anteriormente registrado foi preservado sem alteração."
)


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
          the completed-stages-per-case map derived from ``StageRun``.
        * **No state** (Req. 9.5): returns a default view (``current_campaign`` is
          ``None``, empty completed-stages map, ``exists=False``) that the UI
          renders as every case with all stages "nao_iniciado".
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
            completed = self.get_completed_stages()
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
            completed_stages_by_campaign=completed,
            updated_at=state.updated_at,
            exists=True,
        )

    def get_completed_stages(self) -> dict[str, list[int]]:
        """Completed stages per case, derived from ``StageRun`` (Req. 9.1).

        Returns a map ``campaign_id -> [stage numbers in ``completed`` state]`` with
        the stage numbers sorted ascending. Cases with no completed stage are
        omitted (the UI treats them as all "nao_iniciado" — Req. 9.5). Rows with
        a ``NULL`` ``campaign_id`` are ignored.
        """
        stmt = (
            select(StageRun.campaign_id, StageRun.stage)
            .where(StageRun.state == StageState.COMPLETED)
            .where(StageRun.campaign_id.is_not(None))
        )
        completed: dict[str, list[int]] = {}
        for campaign_id, stage in self._session.execute(stmt).all():
            completed.setdefault(campaign_id, []).append(stage)
        for campaign_id in completed:
            completed[campaign_id] = sorted(set(completed[campaign_id]))
        return completed

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

    def set_current_case(self, slug: Optional[str]) -> SessionState:
        """Persist the current case (``current_campaign``) — Req. 9.4.

        Upserts the single ``session_state`` row (one session-state row is kept)
        so re-opening on any machine restores the current case. Updates
        ``updated_at`` to the current naive UTC instant.

        Passing ``slug=None`` clears the current case (leaves the row present
        with ``current_campaign`` NULL), which the UI renders as "no case selected".

        Note (task 9.2 seam): this write commits directly. Task 9.2 will add the
        "preserve the previously persisted state on a persist failure" guard
        around stage-completion persistence; the read side above does not need to
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

    def persist_stage_completion(
        self, campaign_id: str, stage: int
    ) -> StageCompletionResult:
        """Persist the completion of a Stage, preserving prior state on failure.

        When the Researcher concludes a Stage of a case, that conclusion is the
        ``StageRun`` for ``(campaign_id, stage)`` transitioning to ``completed``
        (Req. 9.1). This method upserts that ``StageRun`` to ``completed`` and
        bumps the session-state ``updated_at`` in a **single transaction**,
        so the persisted Session State advances atomically before the
        conclusion is reported to the Researcher (Req. 9.2).

        Atomicity / previous-state preservation (Req. 9.3)
        --------------------------------------------------
        Either the completion is **fully** persisted, or **nothing** changes:

        * **Success** — returns :class:`StageCompletionResult` with
          ``saved=True`` and the completed-stages map *including* the new
          completion.
        * **Persist failure** — the session is rolled back, which discards the
          pending ``StageRun``/``session_state`` mutations, leaving the
          previously persisted state **exactly as it was** (no partial update).
          Returns :class:`StageCompletionResult` with ``saved=False``, the
          completed-stages map *as previously persisted* (re-read after
          rollback), and :data:`SAVE_FAILED_MESSAGE` so the UI can report that
          the conclusion was not saved. It does **not** raise — the failure is a
          typed signal in the returned result, matching the read-side contract.

        Parameters
        ----------
        campaign_id:
            The case whose Stage was completed.
        stage:
            The Stage number (1..5) that was completed. The pipeline
            spans five Stages (1 Structural Modeling, 2 DAG Generation,
            3 Operationalization, 4 Environment Provisioning, 5 Campaign
            Execution); this method only persists the completion and does not
            bound the stage number itself — the authoritative 1..5 range is
            enforced by ``StageService``/``VALID_STAGES``.

        Returns
        -------
        StageCompletionResult
            ``saved=True`` on success; ``saved=False`` (with
            :data:`SAVE_FAILED_MESSAGE`) on a persist failure, in which case the
            previously persisted state is preserved unchanged.
        """
        stage_run = self._get_or_create_stage_run(campaign_id, stage=stage)
        stage_run.state = StageState.COMPLETED
        stage_run.progress = 100
        # Clear any prior error markers now that the stage is concluded.
        stage_run.error_message = None
        stage_run.failed_step = None
        now = _utcnow()
        stage_run.updated_at = now

        # Advance the single session-state row's timestamp in the same
        # transaction so the completion and the Session State commit (or roll
        # back) together — no partial mutation (Req. 9.3).
        state = self._get_or_create_state_row()
        state.updated_at = now

        try:
            self._session.commit()
        except SQLAlchemyError:
            # Persist failed: roll back so the pending StageRun/session_state
            # mutations are discarded, leaving the previously persisted state
            # exactly as it was (Req. 9.3 — no partial update).
            self._session.rollback()
            # Re-read the completed stages as they stand *after* the rollback:
            # this is the previously persisted state, unchanged.
            try:
                preserved = self.get_completed_stages()
            except SQLAlchemyError:
                # The DB is unreadable even for the read-back; still report the
                # completion as not saved and surface an empty view rather than
                # raising. The persisted rows are not discarded.
                self._session.rollback()
                preserved = {}
            return StageCompletionResult(
                saved=False,
                campaign_id=campaign_id,
                stage=stage,
                completed_stages_by_campaign=preserved,
                message=SAVE_FAILED_MESSAGE,
            )

        self._session.refresh(stage_run)
        return StageCompletionResult(
            saved=True,
            campaign_id=campaign_id,
            stage=stage,
            completed_stages_by_campaign=self.get_completed_stages(),
        )

    # ---- helpers ----------------------------------------------------------

    def _load_state_row(self) -> Optional[SessionState]:
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

    def _get_or_create_stage_run(self, campaign_id: str, *, stage: int) -> StageRun:
        """Return the ``StageRun`` for ``(campaign_id, stage)``, creating it if absent.

        One row per ``(campaign_id, stage)`` is kept (same upsert contract as
        :class:`~app.services.stage.stage_service.StageService`), so persisting a
        completion updates the existing run rather than piling up rows.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == stage)
            .order_by(StageRun.id.asc())
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(campaign_id=campaign_id, stage=stage)
            self._session.add(stage_run)
        return stage_run


__all__ = [
    "SessionStateService",
    "SessionStateView",
    "OperationResultView",
    "AbilityResultView",
    "StageCompletionResult",
    "RESTORE_FAILED_MESSAGE",
    "SAVE_FAILED_MESSAGE",
]
