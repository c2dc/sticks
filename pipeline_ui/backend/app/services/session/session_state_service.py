"""SessionStateService — read/write the Estado_de_Sessão (task 9.1).

The Estado_de_Sessão gives the Pesquisador continuity across machines (Req. 9):
it records the **current case** (``caso_atual``), the **completed stages per
case** and the **per-Ability operation results per case**. Only the current
case is stored directly on the ``estado_sessao`` row (``SessionState``); the
completed stages and the results are **derived** from the ``StageRun`` and
``Operation``/``AbilityResult`` rows already persisted by the Stage/Operation
services, associated with the case (Req. 9.1, 9.7). This avoids duplicating —
and risking divergence from — the authoritative stage/operation tables.

What this service does (task 9.1)
---------------------------------
* :meth:`SessionStateService.get_session_state` — retrieve the persisted state
  on open (Req. 9.4): the current case plus the completed-stages-per-case map.
  When **no** session state exists (Req. 9.5) it returns a clean *default*
  state (``caso_atual is None`` and an empty completed-stages map) that the UI
  renders as every case with all stages "não iniciado". If the persisted state
  cannot be read (Req. 9.6) it returns a typed result flagged
  ``restore_failed=True`` carrying a "could not restore progress" message
  **without discarding** the persisted row.
* :meth:`SessionStateService.set_current_case` — persist the current case
  (``caso_atual``) so re-opening on another machine restores it (Req. 9.4).
* :meth:`SessionStateService.get_completed_stages` — completed stages per case
  derived from ``StageRun`` (Req. 9.1).
* :meth:`SessionStateService.get_operation_results` — per-Ability operation
  results for a case, derived from ``Operation`` (``caso_id``) →
  ``AbilityResult`` (Req. 9.1, 9.7).

Injectable Session, naive UTC timestamps
-----------------------------------------
Like :class:`~app.services.audit.AuditLogger` and
:class:`~app.services.stage.StageService`, this service takes an injected
SQLAlchemy :class:`~sqlalchemy.orm.Session` so the same code works with the
app's ``SessionLocal`` and an ephemeral test SQLite session — no global state.
``atualizado_em`` uses **naive UTC** (``datetime.utcnow``), consistent with the
rest of the backend, so it round-trips identically on SQLite (dev) and
PostgreSQL (production).

Scope boundaries
----------------
* This module implements reading/writing the current case + deriving the
  completed-stages/results views (task 9.1) **and** the "preserve the previously
  persisted state on a stage-completion persist failure" guard (task 9.2, via
  :meth:`SessionStateService.persist_stage_completion`). It does not run any
  stage/operation logic itself.
* Preferences (tema/idioma) persistence is handled at the API level in
  tasks 13/14; it is intentionally **not** part of this service.

Stage-completion persistence with previous-state preservation (task 9.2)
------------------------------------------------------------------------
When the Pesquisador concludes a Stage of a case, that conclusion must be
persisted **before** it is reported as done (Req. 9.2), and the completed
stages per case are the ``StageRun`` rows in the ``concluido`` state (Req. 9.1).
:meth:`SessionStateService.persist_stage_completion` upserts the
``StageRun`` for ``(caso_id, estagio)`` to ``concluido`` and bumps the
session-state ``atualizado_em`` in a **single transaction**. If that commit
fails, the service rolls back — leaving the previously persisted state exactly
as it was, with **no partial mutation** — and returns a typed
:class:`StageCompletionResult` flagged ``saved=False`` carrying the "conclusão
não foi salva" message (Req. 9.3). Either the completion is fully persisted or
nothing changes (atomicity).

_Requisitos: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_
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
# Typed views (Entrada/Saída for the API / UI later — task 11.1)
# ---------------------------------------------------------------------------


class AbilityResultView(BaseModel):
    """One per-Ability operation result (status + saída do comando) — Req. 9.1.

    Mirrors :class:`~app.models.domain.AbilityResult`, carrying the status and
    the command output so the UI can render Stage 3 output per Ability, plus the
    owning ``operacao_id`` for grouping.
    """

    operacao_id: Optional[int] = None
    ability_id: Optional[str] = None
    status: AbilityResultStatus
    saida_comando: Optional[str] = None


class OperationResultView(BaseModel):
    """A case's Operation with its per-Ability results and aggregate — Req. 9.7.

    Groups the ``AbilityResult`` rows under their owning ``Operation`` so the
    result set stays associated with the case (Req. 9.7). ``total_sucesso`` /
    ``total_falha`` are the persisted aggregate on the Operation (Req. 4.5).
    """

    operacao_id: int
    caso_id: Optional[str] = None
    estado: OperationState
    total_sucesso: int = 0
    total_falha: int = 0
    resultados: list[AbilityResultView] = Field(default_factory=list)


class SessionStateView(BaseModel):
    """The retrieved Estado_de_Sessão on open (Req. 9.4/9.5/9.6).

    Attributes
    ----------
    caso_atual:
        The persisted current case, or ``None`` when there is no session state
        yet (default state — Req. 9.5) or none was ever set.
    estagios_concluidos_por_caso:
        Map of ``caso_id -> [completed stage numbers]`` derived from
        ``StageRun`` (Req. 9.1). A case absent from the map (or with an empty
        list) has no completed stage; the UI renders its stages as "não
        iniciado" (Req. 9.5).
    atualizado_em:
        When the session-state row was last updated, or ``None`` in the default
        state.
    exists:
        ``True`` when a persisted ``estado_sessao`` row was found. ``False`` is
        the default all-not-started state (Req. 9.5).
    restore_failed:
        ``True`` when the persisted state could not be read (Req. 9.6). The
        persisted row is **not** discarded; ``mensagem`` carries the
        "could not restore progress" message for the API to surface.
    mensagem:
        Optional human-readable message. Set to the Req. 9.6 message when
        ``restore_failed`` is ``True``.
    """

    caso_atual: Optional[str] = None
    estagios_concluidos_por_caso: dict[str, list[int]] = Field(default_factory=dict)
    atualizado_em: Optional[dt.datetime] = None
    exists: bool = False
    restore_failed: bool = False
    mensagem: Optional[str] = None


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
        Pesquisador when this is ``False`` (Req. 9.2).
    caso_id / estagio:
        The case and Stage number the completion was for.
    estagios_concluidos_por_caso:
        The completed-stages-per-case map (derived from ``StageRun``) as it
        stands after the attempt: it *includes* the new completion on success,
        and is *identical to the previously persisted state* on failure.
    mensagem:
        ``None`` on success; the "conclusão não foi salva" message
        (:data:`SAVE_FAILED_MESSAGE`) on failure, for the API/UI to surface
        (Req. 9.3).
    """

    saved: bool
    caso_id: str
    estagio: int
    estagios_concluidos_por_caso: dict[str, list[int]] = Field(default_factory=dict)
    mensagem: Optional[str] = None


# Message surfaced when the persisted state cannot be read (Req. 9.6). The API
# turns this into the user-facing "progress could not be restored" notice.
RESTORE_FAILED_MESSAGE = (
    "Não foi possível restaurar o progresso: o Estado_de_Sessão persistido "
    "está indisponível ou não pôde ser lido."
)

# Message surfaced when persisting a Stage completion fails (Req. 9.3). The
# previously persisted Estado_de_Sessão is preserved unchanged; the UI turns
# this into the user-facing "the completion was not saved" notice.
SAVE_FAILED_MESSAGE = (
    "A conclusão do Estágio não foi salva: falha ao persistir na Base_de_Dados. "
    "O progresso anteriormente registrado foi preservado sem alteração."
)


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across SQLite dev / PostgreSQL prod)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class SessionStateService:
    """Read and write the Estado_de_Sessão (task 9.1).

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
        """Retrieve the Estado_de_Sessão on open.

        Behavior:

        * **State present** (Req. 9.4): returns the persisted ``caso_atual`` and
          the completed-stages-per-case map derived from ``StageRun``.
        * **No state** (Req. 9.5): returns a default view (``caso_atual`` is
          ``None``, empty completed-stages map, ``exists=False``) that the UI
          renders as every case with all stages "não iniciado".
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
                mensagem=RESTORE_FAILED_MESSAGE,
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
                caso_atual=state.caso_atual,
                atualizado_em=state.atualizado_em,
                exists=True,
                restore_failed=True,
                mensagem=RESTORE_FAILED_MESSAGE,
            )

        return SessionStateView(
            caso_atual=state.caso_atual,
            estagios_concluidos_por_caso=completed,
            atualizado_em=state.atualizado_em,
            exists=True,
        )

    def get_completed_stages(self) -> dict[str, list[int]]:
        """Completed stages per case, derived from ``StageRun`` (Req. 9.1).

        Returns a map ``caso_id -> [stage numbers in ``concluido`` state]`` with
        the stage numbers sorted ascending. Cases with no completed stage are
        omitted (the UI treats them as all "não iniciado" — Req. 9.5). Rows with
        a ``NULL`` ``caso_id`` are ignored.
        """
        stmt = (
            select(StageRun.caso_id, StageRun.estagio)
            .where(StageRun.estado == StageState.COMPLETED)
            .where(StageRun.caso_id.is_not(None))
        )
        completed: dict[str, list[int]] = {}
        for caso_id, estagio in self._session.execute(stmt).all():
            completed.setdefault(caso_id, []).append(estagio)
        for caso_id in completed:
            completed[caso_id] = sorted(set(completed[caso_id]))
        return completed

    def get_operation_results(self, caso_id: str) -> list[OperationResultView]:
        """Per-Ability operation results for a case (Req. 9.1, 9.7).

        Walks ``Operation`` rows for the case (``Operation.caso_id == caso_id``)
        and, for each, its ``AbilityResult`` rows (``AbilityResult.operacao_id``),
        so the results stay associated with the case (Req. 9.7). Operations are
        ordered by ``id`` (creation order); results within an operation by
        ``id`` too, for a stable view.
        """
        op_stmt = (
            select(Operation)
            .where(Operation.caso_id == caso_id)
            .order_by(Operation.id.asc())
        )
        operations = list(self._session.execute(op_stmt).scalars().all())

        views: list[OperationResultView] = []
        for op in operations:
            res_stmt = (
                select(AbilityResult)
                .where(AbilityResult.operacao_id == op.id)
                .order_by(AbilityResult.id.asc())
            )
            results = list(self._session.execute(res_stmt).scalars().all())
            views.append(
                OperationResultView(
                    operacao_id=op.id,
                    caso_id=op.caso_id,
                    estado=op.estado,
                    total_sucesso=op.total_sucesso,
                    total_falha=op.total_falha,
                    resultados=[
                        AbilityResultView(
                            operacao_id=r.operacao_id,
                            ability_id=r.ability_id,
                            status=r.status,
                            saida_comando=r.saida_comando,
                        )
                        for r in results
                    ],
                )
            )
        return views

    # ---- writes (Req. 9.4) -----------------------------------------------

    def set_current_case(self, slug: Optional[str]) -> SessionState:
        """Persist the current case (``caso_atual``) — Req. 9.4.

        Upserts the single ``estado_sessao`` row (one session-state row is kept)
        so re-opening on any machine restores the current case. Updates
        ``atualizado_em`` to the current naive UTC instant.

        Passing ``slug=None`` clears the current case (leaves the row present
        with ``caso_atual`` NULL), which the UI renders as "no case selected".

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
        state.caso_atual = slug
        state.atualizado_em = _utcnow()
        try:
            self._session.commit()
        except SQLAlchemyError:
            self._session.rollback()
            raise
        self._session.refresh(state)
        return state

    def persist_stage_completion(
        self, caso_id: str, estagio: int
    ) -> StageCompletionResult:
        """Persist the completion of a Stage, preserving prior state on failure.

        When the Pesquisador concludes a Stage of a case, that conclusion is the
        ``StageRun`` for ``(caso_id, estagio)`` transitioning to ``concluido``
        (Req. 9.1). This method upserts that ``StageRun`` to ``concluido`` and
        bumps the session-state ``atualizado_em`` in a **single transaction**,
        so the persisted Estado_de_Sessão advances atomically before the
        conclusion is reported to the Pesquisador (Req. 9.2).

        Atomicity / previous-state preservation (Req. 9.3)
        --------------------------------------------------
        Either the completion is **fully** persisted, or **nothing** changes:

        * **Success** — returns :class:`StageCompletionResult` with
          ``saved=True`` and the completed-stages map *including* the new
          completion.
        * **Persist failure** — the session is rolled back, which discards the
          pending ``StageRun``/``estado_sessao`` mutations, leaving the
          previously persisted state **exactly as it was** (no partial update).
          Returns :class:`StageCompletionResult` with ``saved=False``, the
          completed-stages map *as previously persisted* (re-read after
          rollback), and :data:`SAVE_FAILED_MESSAGE` so the UI can report that
          the conclusion was not saved. It does **not** raise — the failure is a
          typed signal in the returned result, matching the read-side contract.

        Parameters
        ----------
        caso_id:
            The case whose Stage was completed.
        estagio:
            The Stage number (1, 2 or 3) that was completed.

        Returns
        -------
        StageCompletionResult
            ``saved=True`` on success; ``saved=False`` (with
            :data:`SAVE_FAILED_MESSAGE`) on a persist failure, in which case the
            previously persisted state is preserved unchanged.
        """
        stage_run = self._get_or_create_stage_run(caso_id, estagio=estagio)
        stage_run.estado = StageState.COMPLETED
        stage_run.progresso = 100
        # Clear any prior error markers now that the stage is concluded.
        stage_run.mensagem_erro = None
        stage_run.etapa_falha = None
        now = _utcnow()
        stage_run.atualizado_em = now

        # Advance the single session-state row's timestamp in the same
        # transaction so the completion and the Estado_de_Sessão commit (or roll
        # back) together — no partial mutation (Req. 9.3).
        state = self._get_or_create_state_row()
        state.atualizado_em = now

        try:
            self._session.commit()
        except SQLAlchemyError:
            # Persist failed: roll back so the pending StageRun/estado_sessao
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
                caso_id=caso_id,
                estagio=estagio,
                estagios_concluidos_por_caso=preserved,
                mensagem=SAVE_FAILED_MESSAGE,
            )

        self._session.refresh(stage_run)
        return StageCompletionResult(
            saved=True,
            caso_id=caso_id,
            estagio=estagio,
            estagios_concluidos_por_caso=self.get_completed_stages(),
        )

    # ---- helpers ----------------------------------------------------------

    def _load_state_row(self) -> Optional[SessionState]:
        """Return the single persisted ``estado_sessao`` row, or ``None``.

        Reads the lowest-``id`` row so behavior is deterministic even if more
        than one row were ever written. Never creates a row.
        """
        stmt = select(SessionState).order_by(SessionState.id.asc())
        return self._session.execute(stmt).scalars().first()

    def _get_or_create_state_row(self) -> SessionState:
        """Return the single ``estado_sessao`` row, creating it if absent."""
        state = self._load_state_row()
        if state is None:
            state = SessionState()
            self._session.add(state)
        return state

    def _get_or_create_stage_run(self, caso_id: str, *, estagio: int) -> StageRun:
        """Return the ``StageRun`` for ``(caso_id, estagio)``, creating it if absent.

        One row per ``(caso_id, estagio)`` is kept (same upsert contract as
        :class:`~app.services.stage.stage_service.StageService`), so persisting a
        completion updates the existing run rather than piling up rows.
        """
        stmt = (
            select(StageRun)
            .where(StageRun.caso_id == caso_id)
            .where(StageRun.estagio == estagio)
            .order_by(StageRun.id.asc())
        )
        stage_run = self._session.execute(stmt).scalars().first()
        if stage_run is None:
            stage_run = StageRun(caso_id=caso_id, estagio=estagio)
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
