"""Property-based test for Property 9 of the pipeline-ui design (task 9.4).

# Feature: pipeline-ui, Property 9: Falha de persistência preserva o estado
# anterior. Para qualquer Estado_de_Sessão previamente persistido, se a
# persistência da conclusão de um estágio falha, então o estado persistido
# permanece idêntico ao anterior à tentativa, sem alteração parcial.

Validates: Requisitos 9.3

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.session.session_state_service.SessionStateService`
implementation. Task 9.2 implemented the "preserve the previously persisted
state on a stage-completion persist failure" guard inside
:meth:`SessionStateService.persist_stage_completion`; this test exercises that
guard across arbitrary previously-persisted states.

What Req. 9.3 requires
----------------------
IF persisting the conclusion of a Stage to the Base_de_Dados fails, THEN the
Backend SHALL preserve the previously persisted Estado_de_Sessão **without
change** and the UI SHALL surface a message that the conclusion was not saved.
So a failed persist must be *atomic*: either the completion is fully written or
nothing at all changes — no partial mutation of the previously persisted state.

How the property is driven
--------------------------
For each example Hypothesis generates an arbitrary but *valid* previously
persisted Estado_de_Sessão:

* a ``caso_atual`` chosen among a small universe of case slugs, or ``None``;
* for each of several cases, an arbitrary subset of the three stages already
  marked completed (``{}`` … ``{1, 2, 3}``) — the previously persisted progress;
* the ``(caso_id, estagio)`` of the *new* stage completion whose persist will be
  made to fail (chosen from the pool, and — importantly — allowed to be a stage
  that is NOT already completed, so a successful write *would* have mutated the
  state; that is exactly the case where "no partial mutation" has teeth).

The prior state is persisted THROUGH the service's own write path
(``set_current_case`` for the current case, ``persist_stage_completion`` for
each already-completed stage), each verified to have succeeded on the healthy
DB. Then an INDEPENDENT snapshot of the *persisted* state is taken by reading
the ``SessionState`` and ``StageRun`` rows directly from the database (not
through the service), capturing every field that a partial write could touch:
the current case, the session-state ``atualizado_em``, and for every StageRun
its ``(caso_id, estagio, estado, progresso, mensagem_erro, etapa_falha,
atualizado_em)``.

A persistence failure is then injected by wrapping the session's ``commit`` so
the very next call — the completion commit inside ``persist_stage_completion``
— raises :class:`~sqlalchemy.exc.SQLAlchemyError`, while the service's own
``rollback`` and read-back run normally (mirroring a real commit-time DB
failure). ``persist_stage_completion`` is invoked and we assert:

* it reports the completion was NOT saved (``saved is False``) and carries the
  :data:`SAVE_FAILED_MESSAGE` (Req. 9.3 — "conclusão não foi salva");
* the completed-stages map it returns equals the previously persisted one
  (independently recomputed from the generated input — the new completion is
  absent);
* a fresh INDEPENDENT snapshot of the persisted rows, read directly from the DB
  after the failed attempt, is **byte-for-byte identical** to the snapshot taken
  before it — every StageRun field and the session-state row unchanged, and no
  new StageRun row created. This is the core of Property 9: no partial mutation.

At least 100 iterations run (``max_examples=150``). Each example builds and
tears down a fresh temp-file SQLite database (mirroring
``test_session_state_roundtrip_property.py``), so there is no state leak between
examples. Everything is mocked/in-process — NO real Docker daemon and NO network
access — safe on the Windows dev environment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import SessionState, StageRun
from app.services.session.session_state_service import (
    SAVE_FAILED_MESSAGE,
    SessionStateService,
)

# ---------------------------------------------------------------------------
# Universe the arbitrary previously-persisted Estado_de_Sessão is generated over.
# ---------------------------------------------------------------------------

# A small, fixed pool of case slugs, matching the roundtrip property test's
# style. A bounded pool keeps the current-case and completed-stages components
# comparable while letting Hypothesis vary which cases appear.
_CASE_IDS: tuple[str, ...] = tuple(f"caso-prop9-{i}" for i in range(4))

# The three stages of a curated case.
_STAGES: tuple[int, ...] = (1, 2, 3)


# ---------------------------------------------------------------------------
# Generators for an arbitrary previously-persisted state + the failing attempt.
# ---------------------------------------------------------------------------

# The current case: one of the pool slugs, or None (no case selected). Only
# slugs from the pool are used so the generated state stays internally coherent.
_caso_atual_strategy = st.one_of(st.none(), st.sampled_from(_CASE_IDS))

# Already-completed stages of a single case: an arbitrary subset of {1, 2, 3}.
_completed_stages_strategy = st.sets(st.sampled_from(_STAGES))

# Completed-stages-per-case for the previously persisted state: a map from a
# subset of the case pool to its already-completed stage set. Spans "nothing
# done" … "everything done".
_completed_by_case_strategy = st.dictionaries(
    keys=st.sampled_from(_CASE_IDS),
    values=_completed_stages_strategy,
    max_size=len(_CASE_IDS),
)

# The whole scenario: the prior persisted state, plus the (caso_id, estagio) of
# the new completion whose persist will be forced to fail. The failing target is
# drawn independently from the pool so it may or may not already be completed —
# both are valid Req. 9.3 scenarios (a would-be no-op and a would-be mutation).
_scenario_strategy = st.fixed_dictionaries(
    {
        "caso_atual": _caso_atual_strategy,
        "completed_by_case": _completed_by_case_strategy,
        "fail_caso_id": st.sampled_from(_CASE_IDS),
        "fail_estagio": st.sampled_from(_STAGES),
    }
)


# ---------------------------------------------------------------------------
# Independent oracle — the previously persisted completed-stages map.
# ---------------------------------------------------------------------------


def _expected_completed_map(
    completed_by_case: dict[str, set[int]],
) -> dict[str, list[int]]:
    """Previously persisted completed-stages-per-case map, from the input alone.

    Mirrors the service contract (stages sorted + de-duplicated, cases with no
    completed stage omitted — Req. 9.1). Never calls the implementation. This is
    what a *failed* persist must leave the map at, unchanged (Req. 9.3).
    """
    expected: dict[str, list[int]] = {}
    for caso_id, stages in completed_by_case.items():
        if stages:
            expected[caso_id] = sorted(set(stages))
    return expected


# ---------------------------------------------------------------------------
# Independent DB snapshots (read the persisted rows directly, not the service).
# ---------------------------------------------------------------------------


def _snapshot_stage_runs(session: Session) -> list[tuple]:
    """Snapshot every StageRun row's mutable fields, ordered deterministically.

    Reads the rows directly (not through the service) so the comparison does not
    trust the implementation. Captures every field ``persist_stage_completion``
    could touch on a StageRun, so a partial write would change this snapshot.
    """
    rows = session.execute(select(StageRun).order_by(StageRun.id.asc())).scalars().all()
    return [
        (
            r.id,
            r.caso_id,
            r.estagio,
            r.estado,
            r.progresso,
            r.mensagem_erro,
            r.etapa_falha,
            r.atualizado_em,
        )
        for r in rows
    ]


def _snapshot_session_state(session: Session) -> Optional[tuple]:
    """Snapshot the single ``estado_sessao`` row's mutable fields (or ``None``).

    Captures ``caso_atual`` and ``atualizado_em`` — the session-state fields
    ``persist_stage_completion`` bumps — so a partial write would change this.
    """
    state = (
        session.execute(select(SessionState).order_by(SessionState.id.asc()))
        .scalars()
        .first()
    )
    if state is None:
        return None
    return (state.id, state.caso_atual, state.atualizado_em)


# ---------------------------------------------------------------------------
# A commit wrapper that fails exactly once, then behaves normally.
# ---------------------------------------------------------------------------


class _FailOnceCommit:
    """Make the session's next ``commit`` raise, restoring normal commits after.

    This simulates a real commit-time Base_de_Dados failure on the completion
    write inside ``persist_stage_completion``: the service's subsequent
    ``rollback`` and read-back run against the real DB, so the "preserve the
    previously persisted state" path is exercised end-to-end. Only the *first*
    commit after arming fails; any later commit (there are none in the guard's
    failure path, but defensively) goes through untouched.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._original_commit = session.commit
        self._armed = True

    def __call__(self) -> None:
        if self._armed:
            self._armed = False
            raise OperationalError("injected persist failure", None, Exception("boom"))
        self._original_commit()


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_session_state_roundtrip_property.py``. Returns ``(session, db_path,
    engine)`` so the caller can dispose the engine and remove the file.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_session_failure_")
    db_path = Path(tmp_dir) / "session_failure.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database, whose file I/O timing varies on Windows and can occasionally exceed
# Hypothesis's default per-example deadline. The per-example DB is intentional
# (no state leak between generated states), so we disable the deadline rather
# than weaken the isolation.
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scenario=_scenario_strategy)
def test_property9_persist_failure_preserves_previous_state(
    scenario: dict,
) -> None:
    """A failed Stage-completion persist leaves the previously persisted state intact.

    # Feature: pipeline-ui, Property 9: Falha de persistência preserva o estado anterior.
    Validates: Requisitos 9.3
    """
    caso_atual: Optional[str] = scenario["caso_atual"]
    completed_by_case: dict[str, set[int]] = scenario["completed_by_case"]
    fail_caso_id: str = scenario["fail_caso_id"]
    fail_estagio: int = scenario["fail_estagio"]

    session, db_path, engine = _build_ephemeral_session()
    try:
        service = SessionStateService(session)

        # --- Persist the PRIOR state THROUGH the service's own write path ----
        # Current case (Req. 9.4). ``None`` is a valid "no case selected" state.
        service.set_current_case(caso_atual)

        # Already-completed stages per case: persist each via the service's own
        # write method (Req. 9.2), the healthy way the Backend records them.
        for caso_id, stages in completed_by_case.items():
            for estagio in sorted(stages):
                prior = service.persist_stage_completion(caso_id, estagio)
                # The write path must report success on a healthy DB.
                assert prior.saved is True

        # --- Snapshot the PERSISTED state (read rows directly, not service) --
        state_before = _snapshot_session_state(session)
        stage_runs_before = _snapshot_stage_runs(session)
        expected_completed = _expected_completed_map(completed_by_case)

        # Sanity: the prior state we snapshotted matches the independent oracle,
        # so the "unchanged" assertions below are comparing against a known-good
        # baseline rather than whatever the DB happened to hold.
        assert service.get_completed_stages() == expected_completed

        # --- Inject a persist failure on the NEXT completion commit ----------
        session.commit = _FailOnceCommit(session)  # type: ignore[method-assign]

        # --- Attempt to persist a new Stage completion (this must fail) ------
        result = service.persist_stage_completion(fail_caso_id, fail_estagio)

        # 1) The completion was NOT saved and the failure is signalled (Req. 9.3).
        assert result.saved is False
        assert result.mensagem == SAVE_FAILED_MESSAGE
        assert result.caso_id == fail_caso_id
        assert result.estagio == fail_estagio

        # 2) The returned completed map is the PREVIOUSLY persisted one — the new
        #    completion is absent (no partial mutation surfaced to the caller).
        assert result.estagios_concluidos_por_caso == expected_completed
        # The failing (caso_id, estagio) must not have been recorded as completed
        # unless it was ALREADY completed before the attempt.
        already_completed = fail_estagio in completed_by_case.get(fail_caso_id, set())
        got_stages = result.estagios_concluidos_por_caso.get(fail_caso_id, [])
        assert (fail_estagio in got_stages) == already_completed

        # 3) The persisted rows are byte-for-byte identical to before the
        #    attempt — the core of Property 9 (no partial mutation).
        #    Re-read directly from the DB after the failed attempt.
        state_after = _snapshot_session_state(session)
        stage_runs_after = _snapshot_stage_runs(session)
        assert state_after == state_before
        assert stage_runs_after == stage_runs_before
        # No new StageRun row was created for the failed completion.
        assert len(stage_runs_after) == len(stage_runs_before)

        # 4) A fresh read through the service also reflects the unchanged state,
        #    confirming the rollback left the session usable (Req. 9.6 seam).
        recovered = service.get_session_state()
        assert recovered.restore_failed is False
        assert recovered.caso_atual == caso_atual
        assert recovered.estagios_concluidos_por_caso == expected_completed
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
