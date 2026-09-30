"""Property-based test for Property 3 of the pipeline-ui design (task 8.4).

# Feature: pipeline-ui, Property 3: A UI nunca permite iniciar um estágio
# bloqueado. Para qualquer combinação de estados dos três Estágios de um
# Caso_Curado, um Estágio sinalizado como bloqueado NUNCA pode ser iniciado, e a
# regra de iniciabilidade é exatamente: o Estágio 1 é sempre iniciável; um
# Estágio n > 1 é iniciável se e somente se o Estágio n-1 está "concluído".

Validates: Requisitos 5.4

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.stage.stage_service.StageService` implementation. It
exercises the sequential-blocking gate (Req. 5.4) that governs whether a stage
of a curated case can be started.

How the property is driven
--------------------------
The blocking gate is *pure logic over the persisted ``StageRun`` rows*: a
stage's state is read from its ``StageRun`` row (defaulting to ``nao_iniciado``
when there is no row yet). So the generator produces, for each of the three
stages, one of the four states — ``nao_iniciado`` / ``em_andamento`` /
``concluido`` / ``erro`` — where "no row" is modelled by simply not writing a
row for that stage (which the service must treat as ``nao_iniciado``). Every
combination of the three stages' states is reachable (4 × 4 × 4 = 64 base
combinations, plus the missing-row variants), and Hypothesis samples across all
of them with at least 100 iterations.

An INDEPENDENT oracle (``_expected_startable``) recomputes iniciabilidade
directly from the generated states — the assertions never trust the
implementation to decide what "startable" means. We assert, for every stage:

* ``is_stage_startable`` agrees with the oracle;
* the ``StageStateView`` exposes ``iniciavel`` == oracle and ``bloqueado`` ==
  ``not iniciavel`` (the two are exact complements — a blocked stage is never
  startable);
* Stage 1 is ALWAYS startable / never blocked, regardless of the other stages;
* a stage n > 1 is startable IFF the immediately preceding stage n-1 is
  ``concluido`` — and it depends only on n-1, never on the stage's own state.

Each example builds and tears down a fresh temp-file SQLite database (mirroring
``test_audit_logger_property.py``), so there is no state leak between examples
and every combination is scored in isolation. NO real Docker daemon and NO
network access are involved — everything is pure in-memory DB logic, safe on the
Windows dev environment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterator, Optional

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StageRun
from app.models.enums import StageState
from app.services.stage.stage_service import (
    FIRST_STAGE,
    VALID_STAGES,
    StageService,
)

# ---------------------------------------------------------------------------
# Generators for the three stages' states.
# ---------------------------------------------------------------------------

# The four persisted states a stage can be in, PLUS ``None`` to model "no
# StageRun row yet" — which the service must treat as ``nao_iniciado`` (Req.
# 9.5). Including ``None`` proves the missing-row default participates in the
# blocking decision exactly like an explicit ``nao_iniciado``.
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

_stage_state_strategy = st.sampled_from(_STATE_CHOICES)

# A full combination of the three stages: (stage1_state, stage2_state,
# stage3_state). ``st.tuples`` samples every combination across the choice sets.
_stage_states_strategy = st.tuples(
    _stage_state_strategy, _stage_state_strategy, _stage_state_strategy
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes iniciabilidade from the generated states.
# ---------------------------------------------------------------------------


def _effective_state(state: Optional[StageState]) -> StageState:
    """A missing row (``None``) is ``nao_iniciado`` (Req. 9.5)."""
    return state if state is not None else StageState.NOT_STARTED


def _expected_startable(
    estagio: int, states: tuple[Optional[StageState], ...]
) -> bool:
    """Compute the expected startability WITHOUT calling the implementation.

    Mirrors the Property 3 / Req. 5.4 rule: Stage 1 is always startable; any
    later stage n is startable iff the immediately preceding stage n-1 is
    ``concluido``. Depends only on stage n-1's effective state.
    """
    if estagio == FIRST_STAGE:
        return True
    previous = _effective_state(states[estagio - 2])  # 1-indexed stage -> 0-index
    return previous is StageState.COMPLETED


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_audit_logger_property.py``. Returns ``(session, db_path, engine)`` so
    the caller can dispose the engine and remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage_block_")
    db_path = Path(tmp_dir) / "stage_block.db"
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


def _seed_stage_runs(
    session: Session,
    caso_id: str,
    states: tuple[Optional[StageState], ...],
) -> None:
    """Persist one StageRun per stage whose generated state is not ``None``.

    A ``None`` state means "no row" — the stage is left absent so the service
    exercises its missing-row default (``nao_iniciado``). Progress is set to 100
    for a completed stage and 0 otherwise, matching how the service persists it.
    """
    for estagio, state in zip(VALID_STAGES, states):
        if state is None:
            continue
        session.add(
            StageRun(
                caso_id=caso_id,
                estagio=estagio,
                estado=state,
                progresso=100 if state is StageState.COMPLETED else 0,
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database, whose file I/O timing varies on Windows and can occasionally exceed
# Hypothesis's default per-example deadline. The per-example DB is intentional
# (no state leak between combinations), so we disable the deadline rather than
# weaken the isolation.
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(states=_stage_states_strategy)
def test_property3_blocked_stage_is_never_startable(
    states: tuple[Optional[StageState], ...],
) -> None:
    """A blocked stage is never startable; startability is exactly the Req. 5.4 rule.

    # Feature: pipeline-ui, Property 3: A UI nunca permite iniciar um estágio
    # bloqueado.
    Validates: Requisitos 5.4
    """
    session, db_path, engine = _build_ephemeral_session()
    caso_id = "caso-prop3"
    try:
        _seed_stage_runs(session, caso_id, states)
        service = StageService(session)

        # Per-stage snapshot (single read) so the view for stage n uses the same
        # states as the individual is_stage_startable(n) call.
        case_states = service.get_stage_states(caso_id)
        views_by_stage = {v.estagio: v for v in case_states.estagios}

        for estagio in VALID_STAGES:
            expected = _expected_startable(estagio, states)

            # 1) The public startability decision matches the independent oracle.
            startable = service.is_stage_startable(caso_id, estagio)
            assert startable is expected, (
                f"is_stage_startable(estagio={estagio}) == {startable}, "
                f"esperado {expected} para estados {states!r}"
            )

            # 2) The per-stage view agrees, and — crucially — a blocked stage is
            #    the exact complement of a startable one: bloqueado == not iniciavel.
            view = views_by_stage[estagio]
            assert view.iniciavel is expected
            assert view.bloqueado is (not expected)
            # A blocked stage NEVER reports itself as startable (Property 3 core).
            if view.bloqueado:
                assert view.iniciavel is False
                assert service.is_stage_startable(caso_id, estagio) is False

            # 3) get_stage_state (single-stage read) matches get_stage_states.
            single = service.get_stage_state(caso_id, estagio)
            assert single.iniciavel is expected
            assert single.bloqueado is (not expected)

        # Stage 1 is ALWAYS startable / never blocked, regardless of the others.
        first_view = views_by_stage[FIRST_STAGE]
        assert first_view.iniciavel is True
        assert first_view.bloqueado is False
        assert service.is_stage_startable(caso_id, FIRST_STAGE) is True

        # A stage n > 1 is startable IFF the previous stage is concluido — and
        # this depends ONLY on n-1, never on the stage's own state.
        for estagio in (2, 3):
            previous_completed = (
                _effective_state(states[estagio - 2]) is StageState.COMPLETED
            )
            assert (
                service.is_stage_startable(caso_id, estagio) is previous_completed
            )
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    stage1=st.sampled_from(_STATE_CHOICES),
    stage3=st.sampled_from(_STATE_CHOICES),
    prev_not_completed=st.sampled_from(
        (
            None,
            StageState.NOT_STARTED,
            StageState.IN_PROGRESS,
            StageState.ERROR,
        )
    ),
)
def test_property3_later_stage_blocked_when_previous_not_completed(
    stage1: Optional[StageState],
    stage3: Optional[StageState],
    prev_not_completed: Optional[StageState],
) -> None:
    """Targeted direction: stage 2 is blocked whenever stage 1 is not concluido.

    Builds combinations where the immediately-preceding stage is in any state
    OTHER than ``concluido`` and asserts stage 2 is signalled blocked and cannot
    be started, no matter what stages 1 (its own irrelevance is covered by the
    broad test) and 3 look like.

    # Feature: pipeline-ui, Property 3: A UI nunca permite iniciar um estágio
    # bloqueado.
    Validates: Requisitos 5.4
    """
    # By construction the preceding stage (stage 1 slot) is NOT completed.
    assert _effective_state(prev_not_completed) is not StageState.COMPLETED

    session, db_path, engine = _build_ephemeral_session()
    caso_id = "caso-prop3-neg"
    try:
        _seed_stage_runs(session, caso_id, (prev_not_completed, stage1, stage3))
        service = StageService(session)

        # Stage 2's predecessor (slot 0) is not completed => stage 2 is blocked.
        assert service.is_stage_startable(caso_id, 2) is False
        view2 = service.get_stage_state(caso_id, 2)
        assert view2.bloqueado is True
        assert view2.iniciavel is False
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
