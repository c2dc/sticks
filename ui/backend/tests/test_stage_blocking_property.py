"""Property-based test for the sequential-blocking gate across Estágios 1..6.

# Feature: pipeline-seis-estagios, Property 1: Bloqueio sequencial em 1..6. Para
# qualquer distribuição de estados dos seis Estágios de um Caso, o Estágio N
# (2..6) é iniciável se e somente se o Estágio N-1 está "concluído"; o Estágio 1
# (``FIRST_STAGE``) nunca é bloqueado.

Validates: Requirements 1.3, 3.1, 3.2, 3.3

This property originates in the pipeline-ui spec (Property 3) and is carried
forward through ``pipeline-cinco-estagios``; it now spans the widened range of
``pipeline-seis-estagios`` (``VALID_STAGES == (1, 2, 3, 4, 5, 6)``). The rule is
unchanged — a stage is startable iff it is the first stage or its immediately
preceding stage is ``concluido`` — but it must now hold across the whole 1..6
range, including the new Estágio 6 "Execução + medição + relatório" gated on the
Estágio 5 "Criação do ambiente de simulação" (Req. 3.1, 3.2, 3.3).

This is a normal ``feature`` property test (``pipeline-seis-estagios`` is a
feature spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.stage.stage_service.StageService` implementation, whose
range was widened to 1..6 in task 1.1. It exercises the sequential-blocking gate
(Req. 1.3, 3.1, 3.2, 3.3) that governs whether a stage of a case can be started.

Driven entirely by the imported constants
------------------------------------------
Every stage number in this test is derived from ``VALID_STAGES`` / ``FIRST_STAGE``
/ ``LAST_STAGE`` imported from ``stage_service`` — there are NO hardcoded ``5``
or ``6`` literals. The generator produces one state per stage in ``VALID_STAGES``
(so it automatically widened to six states when the pipeline gained Estágio 6),
and the oracle/assertions iterate ``VALID_STAGES``. Were a seventh stage ever
added, this test would cover it with no edit.

How the property is driven
--------------------------
The blocking gate is *pure logic over the persisted ``StageRun`` rows*: a
stage's state is read from its ``StageRun`` row (defaulting to ``nao_iniciado``
when there is no row yet). So the generator produces, for each stage in
``VALID_STAGES``, one of the four states — ``nao_iniciado`` / ``em_andamento`` /
``concluido`` / ``erro`` — where "no row" is modelled by simply not writing a
row for that stage (which the service must treat as ``nao_iniciado``). Every
combination of the six stages' states is reachable and Hypothesis samples across
them with at least 100 iterations.

An INDEPENDENT oracle (``_expected_startable``) recomputes iniciabilidade
directly from the generated states — the assertions never trust the
implementation to decide what "startable" means. We assert, for every stage:

* ``is_stage_startable`` agrees with the oracle;
* the ``StageStateView`` exposes ``iniciavel`` == oracle and ``bloqueado`` ==
  ``not iniciavel`` (the two are exact complements — a blocked stage is never
  startable);
* Stage 1 (``FIRST_STAGE``) is ALWAYS startable / never blocked, regardless of
  the other stages;
* a stage N > 1 is startable IFF the immediately preceding stage N-1 is
  ``concluido`` — and it depends only on N-1, never on the stage's own state —
  for EVERY adjacent pair in 1..6, including the pair ``(5, 6)``.

Each example builds and tears down a fresh temp-file SQLite database (mirroring
``test_aggregate_progress_property.py``), so there is no state leak between
examples and every combination is scored in isolation. NO real Docker daemon
and NO network access are involved — everything is pure in-memory DB logic, safe
on the Windows dev environment.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

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
    LAST_STAGE,
    VALID_STAGES,
    StageService,
)

# ---------------------------------------------------------------------------
# Generators for every stage's state, sized by VALID_STAGES (1..6).
# ---------------------------------------------------------------------------

# The four persisted states a stage can be in, PLUS ``None`` to model "no
# StageRun row yet" — which the service must treat as ``nao_iniciado``
# (Req. 3.1). Including ``None`` proves the missing-row default participates in
# the blocking decision exactly like an explicit ``nao_iniciado``.
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

_stage_state_strategy = st.sampled_from(_STATE_CHOICES)

# A full combination of ALL stages: one state per stage in ``VALID_STAGES``
# (currently six). ``st.tuples`` samples every combination across the choice
# sets. The tuple length is driven by ``VALID_STAGES`` so it tracks the pipeline
# range automatically (no hardcoded count).
_stage_states_strategy = st.tuples(
    *(_stage_state_strategy for _ in VALID_STAGES)
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes iniciabilidade from the generated states.
# ---------------------------------------------------------------------------


def _effective_state(state: Optional[StageState]) -> StageState:
    """A missing row (``None``) is ``nao_iniciado`` (Req. 3.1)."""
    return state if state is not None else StageState.NOT_STARTED


def _state_for_stage(
    estagio: int, states: tuple[Optional[StageState], ...]
) -> Optional[StageState]:
    """The generated state for ``estagio``, indexed via ``VALID_STAGES``.

    Mapping stage number -> position through ``VALID_STAGES`` (rather than an
    arithmetic ``estagio - 1``) keeps the test free of literal stage offsets and
    correct for any ``VALID_STAGES`` ordering.
    """
    return states[VALID_STAGES.index(estagio)]


def _expected_startable(
    estagio: int, states: tuple[Optional[StageState], ...]
) -> bool:
    """Compute the expected startability WITHOUT calling the implementation.

    Mirrors the Req. 3.1/3.2/3.3 rule: ``FIRST_STAGE`` is always startable; any
    later stage N is startable iff the immediately preceding stage N-1 is
    ``concluido``. Depends only on stage N-1's effective state.
    """
    if estagio == FIRST_STAGE:
        return True
    previous = _effective_state(_state_for_stage(estagio - 1, states))
    return previous is StageState.COMPLETED


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_aggregate_progress_property.py``. Returns ``(session, db_path,
    engine)`` so the caller can dispose the engine and remove the file
    afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_seis_stage_block_")
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
    Stages are paired with ``VALID_STAGES`` so all 1..6 rows are seeded.
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
# Property 1 — sequential blocking across every adjacent pair of 1..6.
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
def test_property1_blocked_stage_is_never_startable(
    states: tuple[Optional[StageState], ...],
) -> None:
    """A blocked stage is never startable; startability is exactly the gate rule.

    # Feature: pipeline-seis-estagios, Property 1: Bloqueio sequencial em 1..6.
    Validates: Requirements 1.3, 3.1, 3.2, 3.3
    """
    session, db_path, engine = _build_ephemeral_session()
    caso_id = "caso-prop1"
    try:
        _seed_stage_runs(session, caso_id, states)
        service = StageService(session)

        # Per-stage snapshot (single read) so the view for stage N uses the same
        # states as the individual is_stage_startable(N) call.
        case_states = service.get_stage_states(caso_id)
        views_by_stage = {v.estagio: v for v in case_states.estagios}

        # The service must expose exactly the VALID_STAGES views.
        assert set(views_by_stage) == set(VALID_STAGES)

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
            # A blocked stage NEVER reports itself as startable (Property core).
            if view.bloqueado:
                assert view.iniciavel is False
                assert service.is_stage_startable(caso_id, estagio) is False

            # 3) get_stage_state (single-stage read) matches get_stage_states.
            single = service.get_stage_state(caso_id, estagio)
            assert single.iniciavel is expected
            assert single.bloqueado is (not expected)

        # The FIRST_STAGE is ALWAYS startable / never blocked, regardless of the
        # others.
        first_view = views_by_stage[FIRST_STAGE]
        assert first_view.iniciavel is True
        assert first_view.bloqueado is False
        assert service.is_stage_startable(caso_id, FIRST_STAGE) is True

        # Every stage N > FIRST_STAGE is startable IFF the previous stage is
        # concluido — and this depends ONLY on N-1, never on the stage's own
        # state. Iterating VALID_STAGES[1:] covers all adjacent pairs up to the
        # new (LAST_STAGE-1, LAST_STAGE) pair, i.e. including Estágio 6.
        for estagio in VALID_STAGES[1:]:
            previous_completed = (
                _effective_state(_state_for_stage(estagio - 1, states))
                is StageState.COMPLETED
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


# A later stage sampled from every non-first stage of 1..6 (so Estágio 6 is
# directly exercised), paired with the four non-``concluido`` predecessor states.
_LATER_STAGES: tuple[int, ...] = VALID_STAGES[1:]
_NON_COMPLETED_CHOICES: tuple[Optional[StageState], ...] = tuple(
    s for s in _STATE_CHOICES if _effective_state(s) is not StageState.COMPLETED
)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    later_stage=st.sampled_from(_LATER_STAGES),
    prev_not_completed=st.sampled_from(_NON_COMPLETED_CHOICES),
    other_states=st.lists(
        st.sampled_from(_STATE_CHOICES),
        min_size=len(VALID_STAGES),
        max_size=len(VALID_STAGES),
    ),
)
def test_property1_later_stage_blocked_when_previous_not_completed(
    later_stage: int,
    prev_not_completed: Optional[StageState],
    other_states: list[Optional[StageState]],
) -> None:
    """Targeted direction: a stage is blocked whenever its predecessor isn't concluido.

    For a stage ``later_stage`` sampled from every non-first stage of 1..6
    (including the new Estágio 6), forces the immediately-preceding stage into a
    non-``concluido`` state and asserts ``later_stage`` is signalled blocked and
    cannot be started — no matter what the remaining stages look like.

    # Feature: pipeline-seis-estagios, Property 1: Bloqueio sequencial em 1..6.
    Validates: Requirements 1.3, 3.1, 3.2, 3.3
    """
    # By construction the preceding stage is NOT completed.
    assert _effective_state(prev_not_completed) is not StageState.COMPLETED

    # Start from an arbitrary distribution, then pin the predecessor slot of
    # ``later_stage`` to the non-completed state. Indices are resolved through
    # VALID_STAGES, so there are no literal offsets.
    states = list(other_states)
    prev_index = VALID_STAGES.index(later_stage - 1)
    states[prev_index] = prev_not_completed
    states_tuple = tuple(states)

    session, db_path, engine = _build_ephemeral_session()
    caso_id = "caso-prop1-neg"
    try:
        _seed_stage_runs(session, caso_id, states_tuple)
        service = StageService(session)

        # The predecessor is not completed => later_stage is blocked.
        assert service.is_stage_startable(caso_id, later_stage) is False
        view = service.get_stage_state(caso_id, later_stage)
        assert view.bloqueado is True
        assert view.iniciavel is False
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_valid_stages_span_first_to_last() -> None:
    """Guard: the covered range is exactly ``FIRST_STAGE..LAST_STAGE`` with 6 stages.

    This anchors the property's claim ("1..6") to the imported constants, so if
    the pipeline range changes, the generators above (driven by ``VALID_STAGES``)
    and this guard move together instead of silently covering the wrong span.

    # Feature: pipeline-seis-estagios, Property 1: Bloqueio sequencial em 1..6.
    Validates: Requirements 1.3
    """
    assert VALID_STAGES[0] == FIRST_STAGE
    assert VALID_STAGES[-1] == LAST_STAGE
    assert VALID_STAGES == tuple(range(FIRST_STAGE, LAST_STAGE + 1))
    assert len(VALID_STAGES) == 6
