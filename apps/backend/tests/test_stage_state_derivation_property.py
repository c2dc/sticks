"""Property-based test for Property 3 of the dag-stage-multi-origin design (task 7.5).

# Feature: pipeline-cinco-stages, Property 3: Derivação de state por
# StageRun em 1..5. Para todo conjunto de lines ``StageRun`` (``stage`` em
# 1..5, states arbitrários), a derivação de state por Estágio reflete
# exatamente o ``StageRun`` correspondente (e ``nao_iniciado``/0 quando não há
# line), produzindo exatamente um StageStateView por Estágio em 1..5.

Validates: Requirements 1.3, 1.7, 1.8

This is a normal ``feature`` property test (the dag-stage-multi-origin spec is
a feature spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.stage.stage_service.StageService` implementation. It
exercises the per-stage state derivation (Req. 1.3, 1.7, 1.8) that
:meth:`StageService.get_stage_states` performs over the renumbered 1..5 range.

How the property is driven
--------------------------
The per-stage state derivation is *pure logic over the persisted ``StageRun``
rows*: a stage's state (and progress percentage) is read from its ``StageRun``
row, defaulting to ``nao_iniciado`` / ``0`` when there is no row yet (Req. 9.5).
So the generator produces, for each stage in ``VALID_STAGES`` (imported from the
service — no hardcoded 4/5), an arbitrary partial mapping ``stage ->
(StageState, progress)``; a stage may be omitted entirely to model "no
StageRun row". Hypothesis samples across widely varied partial distributions —
from "no row at all" through "some stages present" to "every stage present" —
with at least 100 iterations.

An INDEPENDENT oracle (``_expected_derivation``) recomputes, directly from the
generated mapping, the expected ``(state, progress)`` per stage — the
assertions never trust the implementation to decide what the derived state is.
We assert:

* ``get_stage_states`` returns EXACTLY ``len(VALID_STAGES)`` StageStateView
  entries, one per stage, in ``VALID_STAGES`` order (no missing, no extra, no
  duplicate stage);
* for a stage with a seeded row, its ``state`` / ``progress`` match the row;
* for a stage with NO row, ``state == nao_iniciado`` and ``progress == 0``
  (Req. 9.5);
* the single-stage read :meth:`StageService.get_stage_state` agrees with the
  per-stage view (same derivation).

Each example builds and tears down a fresh temp-file SQLite database (mirroring
``test_stage_blocking_property.py`` / ``test_aggregate_progress_property.py``),
so there is no state leak between examples and every distribution is scored in
isolation. NO real Docker daemon and NO network access are involved —
everything is pure in-memory DB logic, safe on the Windows dev environment.
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
    VALID_STAGES,
    StageService,
)

# ---------------------------------------------------------------------------
# Generators for a partial mapping ``stage -> (StageState, progress)``.
# ---------------------------------------------------------------------------

# The four persisted states a stage's row can be in. "No row" is modelled by
# simply omitting the stage from the mapping — the service must then derive
# ``nao_iniciado`` / 0 (Req. 9.5).
_STATE_VALUES: tuple[StageState, ...] = (
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

# A seeded row = (state, progress). Progress is a free 0..100 integer so the
# property proves the derived ``progress`` mirrors the row verbatim, not just a
# {0, 100} default.
_row_strategy = st.tuples(
    st.sampled_from(_STATE_VALUES),
    st.integers(min_value=0, max_value=100),
)

# A partial mapping over VALID_STAGES: each stage is independently either absent
# (None → no row) or present with a (state, progress) row. Driving the keys
# from VALID_STAGES (not hardcoded 1..5) keeps the test tied to the single
# source of truth for the pipeline range.
_distribution_strategy = st.fixed_dictionaries(
    {
        stage: st.one_of(st.none(), _row_strategy)
        for stage in VALID_STAGES
    }
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes the expected (state, progress) per stage.
# ---------------------------------------------------------------------------


def _expected_derivation(
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> dict[int, tuple[StageState, int]]:
    """Recompute the expected per-stage (state, progress) WITHOUT the impl.

    A seeded stage derives exactly its row's ``(state, progress)``; an absent
    stage derives ``(nao_iniciado, 0)`` (Req. 9.5).
    """
    expected: dict[int, tuple[StageState, int]] = {}
    for stage in VALID_STAGES:
        row = distribution.get(stage)
        if row is None:
            expected[stage] = (StageState.NOT_STARTED, 0)
        else:
            expected[stage] = row
    return expected


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_stage_blocking_property.py``. Returns ``(session, db_path, engine)``
    so the caller can dispose the engine and remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="dag_multi_origem_state_deriv_")
    db_path = Path(tmp_dir) / "state_deriv.db"
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


def _seed_distribution(
    session: Session,
    campaign_id: str,
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> None:
    """Persist one StageRun per stage whose generated row is not ``None``.

    A ``None`` entry means "no row" — the stage is left absent so the service
    exercises its missing-row default (``nao_iniciado`` / 0). The persisted
    ``state`` / ``progress`` are exactly the generated row values, so the
    property can assert the derivation mirrors them verbatim.
    """
    for stage, row in distribution.items():
        if row is None:
            continue
        state, progress = row
        session.add(
            StageRun(
                campaign_id=campaign_id,
                stage=stage,
                state=state,
                progress=progress,
            )
        )
    session.commit()


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


# ``deadline=None``: each example builds and tears down a fresh temp-file SQLite
# database, whose file I/O timing varies on Windows and can occasionally exceed
# Hypothesis's default per-example deadline. The per-example DB is intentional
# (no state leak between distributions), so we disable the deadline rather than
# weaken the isolation.
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(distribution=_distribution_strategy)
def test_property3_stage_state_derived_exactly_from_stage_run(
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> None:
    """Each stage in 1..5 derives exactly its StageRun (or nao_iniciado/0 when absent).

    # Feature: pipeline-cinco-stages, Property 3: Derivação de state por
    # StageRun em 1..5.
    Validates: Requirements 1.3, 1.7, 1.8
    """
    session, db_path, engine = _build_ephemeral_session()
    campaign_id = "campaign-prop3-deriv"
    try:
        _seed_distribution(session, campaign_id, distribution)
        service = StageService(session)

        expected = _expected_derivation(distribution)

        case_states = service.get_stage_states(campaign_id)

        # 1) Exactly one StageStateView per stage, in VALID_STAGES order — no
        #    missing, no extra, no duplicate stage.
        assert [v.stage for v in case_states.stages] == list(VALID_STAGES)
        assert len(case_states.stages) == len(VALID_STAGES)

        views_by_stage = {v.stage: v for v in case_states.stages}

        for stage in VALID_STAGES:
            exp_estado, exp_progresso = expected[stage]
            view = views_by_stage[stage]

            # 2) The derived state/progress mirror the row exactly, or default
            #    to nao_iniciado/0 when there is no row (Req. 9.5).
            assert view.state is exp_estado, (
                f"stage={stage} state derivado {view.state!r}, esperado "
                f"{exp_estado!r} para distribuição {distribution!r}"
            )
            assert view.progress == exp_progresso, (
                f"stage={stage} progress derivado {view.progress}, "
                f"esperado {exp_progresso} para distribuição {distribution!r}"
            )

            # 3) The single-stage read agrees with the per-stage view (same
            #    derivation path).
            single = service.get_stage_state(campaign_id, stage)
            assert single.stage == stage
            assert single.state is exp_estado
            assert single.progress == exp_progresso

        # 4) fully_completed iff every stage derived completed — a direct
        #    corollary of the per-stage derivation over the full 1..5 range.
        expected_total = all(
            exp[0] is StageState.COMPLETED for exp in expected.values()
        )
        assert case_states.fully_completed is expected_total
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
    present_stage=st.sampled_from(VALID_STAGES),
    state=st.sampled_from(_STATE_VALUES),
    progress=st.integers(min_value=0, max_value=100),
)
def test_property3_absent_stages_default_to_not_started_zero(
    present_stage: int,
    state: StageState,
    progress: int,
) -> None:
    """Only the seeded stage reflects its row; every other stage is nao_iniciado/0.

    Seeds a single stage and asserts that exactly that stage derives the seeded
    ``(state, progress)`` while all other stages in 1..5 — which have no row —
    derive ``nao_iniciado`` / 0 (Req. 9.5). This isolates the missing-row
    default from the seeded derivation.

    # Feature: pipeline-cinco-stages, Property 3: Derivação de state por
    # StageRun em 1..5.
    Validates: Requirements 1.3, 1.7, 1.8
    """
    session, db_path, engine = _build_ephemeral_session()
    campaign_id = "campaign-prop3-deriv-single"
    try:
        distribution: dict[int, Optional[tuple[StageState, int]]] = {
            stage: None for stage in VALID_STAGES
        }
        distribution[present_stage] = (state, progress)
        _seed_distribution(session, campaign_id, distribution)
        service = StageService(session)

        case_states = service.get_stage_states(campaign_id)
        assert [v.stage for v in case_states.stages] == list(VALID_STAGES)
        views_by_stage = {v.stage: v for v in case_states.stages}

        for stage in VALID_STAGES:
            view = views_by_stage[stage]
            if stage == present_stage:
                assert view.state is state
                assert view.progress == progress
            else:
                assert view.state is StageState.NOT_STARTED
                assert view.progress == 0
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
