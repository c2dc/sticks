"""Property-based test for Property 3 of the pipeline-seis-stages design (task 1.4).

# Feature: pipeline-seis-stages, Property 3: Per-StageRun state derivation in
# 1..6. For any set of ``StageRun`` rows (``stage`` in 1..6, arbitrary states),
# the per-Stage state derivation reflects exactly the corresponding
# ``StageRun`` (and ``nao_iniciado``/0 when there is no row), producing exactly
# one StageStateView per Stage in 1..6 — one per Stage number, in
# ``VALID_STAGES`` order (Req. 1.2), with each Stage's Input/Output (state +
# progress) recorded explicitly from its persisted row (Req. 1.4).

Validates: Requirements 1.2, 1.4

This spec restructures the pipeline from five to SIX Stages: the lever is to
widen ``VALID_STAGES`` to ``(1, 2, 3, 4, 5, 6)`` in ``stage_service.py``,
keeping the derivation functions (``get_stage_states`` /
``_build_stage_state_view``) *range-agnostic*. This is a ``feature`` test
(``pipeline-seis-stages`` is a feature spec, not a bugfix): the property MUST
hold on the current implementation of
:class:`~app.services.stage.stage_service.StageService`.

The property exercises per-Stage state derivation over the 1..6 range:
``get_stage_states`` must return exactly one ``StageStateView`` per Stage number
(1..6, Req. 1.2), and each one's state/progress must mirror exactly its
``StageRun`` row — the Stage's persisted Input/Output (Req. 1.4) — or fall back
to ``nao_iniciado`` / 0 when there is no row.

How the property is driven
--------------------------
State derivation is *pure logic over the persisted ``StageRun`` rows*: a Stage's
state (and progress) is read from its ``StageRun`` row, defaulting to
``nao_iniciado`` / 0 when there is no row yet. The generator produces, for each
Stage in ``VALID_STAGES`` (imported from the service — no hardcoded 5/6
literals), an arbitrary partial mapping ``stage -> (StageState, progress)``; a
Stage may be omitted entirely to model "no StageRun row". Hypothesis samples
varied partial distributions — from "no row" to "all Stages present" — with at
least 100 iterations.

An INDEPENDENT oracle (``_expected_derivation``) recomputes, straight from the
generated mapping, the expected ``(state, progress)`` per Stage — the assertions
never trust the implementation to decide the derived state. We ensure:

* ``get_stage_states`` returns EXACTLY ``len(VALID_STAGES)`` ``StageStateView``,
  one per Stage, in ``VALID_STAGES`` order (none missing, extra nor duplicate
  Stage number) — the 1..6 numbering identity (Req. 1.2);
* for a Stage with a seeded row, its ``state`` / ``progress`` mirror the
  persisted row (the Stage's Input/Output — Req. 1.4);
* for a Stage with NO row, ``state == nao_iniciado`` and ``progress == 0``;
* the per-Stage read :meth:`StageService.get_stage_state` agrees with the
  aggregate view (same derivation) for each Stage in 1..6.

Each example creates and destroys its own temporary SQLite database (mirroring
``test_stage_state_derivation_property.py`` / ``test_stage_blocking_property.py``),
with no state leak between examples. NO real Docker daemon and NO network access
are involved — everything is pure DB logic, safe in a Windows dev environment.
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

# The four persistable states of a Stage row. "No row" is modeled simply by
# omitting the Stage from the mapping — the service must then derive
# ``nao_iniciado`` / 0.
_STATE_VALUES: tuple[StageState, ...] = (
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

# A seeded row = (state, progress). The progress is a free integer 0..100 so the
# property can prove the derived ``progress`` mirrors the row verbatim, not just
# a {0, 100} default.
_row_strategy = st.tuples(
    st.sampled_from(_STATE_VALUES),
    st.integers(min_value=0, max_value=100),
)

# A partial mapping over VALID_STAGES: each Stage is independently either absent
# (None → no row) or present with a row (state, progress). Driving the keys by
# VALID_STAGES (not by hardcoded 1..6) keeps the test tied to the single source
# of the pipeline range (widened to 1..6 in this spec).
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
    stage derives ``(nao_iniciado, 0)``.
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

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern of
    ``test_stage_state_derivation_property.py``. Returns ``(session, db_path,
    engine)`` so the caller can dispose the engine and remove the file after.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_seis_state_deriv_")
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

    A ``None`` input means "no row" — the Stage is left absent so the service
    exercises the missing-row default (``nao_iniciado`` / 0). The persisted
    ``state`` / ``progress`` are exactly the generated values, so the property
    can assert the derivation mirrors them verbatim.
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


# ``deadline=None``: each example creates and destroys a temporary SQLite
# database whose I/O timing varies on Windows and may exceed Hypothesis's
# per-example deadline. The per-example database is intentional (no leak between
# distributions), so we turn off the deadline rather than weaken the isolation.
@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(distribution=_distribution_strategy)
def test_property3_stage_state_derived_exactly_from_stage_run_seis(
    distribution: dict[int, Optional[tuple[StageState, int]]],
) -> None:
    """Cada Estágio em 1..6 deriva exatamente a sua StageRun (ou nao_iniciado/0 quando ausente).

    # Feature: pipeline-seis-stages, Property 3: Derivação de state por
    # StageRun em 1..6.
    Validates: Requirements 1.2, 1.4
    """
    session, db_path, engine = _build_ephemeral_session()
    campaign_id = "campaign-seis-prop3-deriv"
    try:
        _seed_distribution(session, campaign_id, distribution)
        service = StageService(session)

        expected = _expected_derivation(distribution)

        case_states = service.get_stage_states(campaign_id)

        # 1) Exatamente um StageStateView por Estágio, na ordem de VALID_STAGES
        #    (1..6) — sem faltar, sobrar nem duplicar número de Estágio (Req. 1.2).
        assert [v.stage for v in case_states.stages] == list(VALID_STAGES)
        assert len(case_states.stages) == len(VALID_STAGES)

        views_by_stage = {v.stage: v for v in case_states.stages}

        for stage in VALID_STAGES:
            exp_estado, exp_progresso = expected[stage]
            view = views_by_stage[stage]

            # 2) O state/progress derivados espelham a line exatamente — a
            #    Entrada/Saída persistida do Estágio (Req. 1.4) — ou caem para
            #    nao_iniciado/0 quando não há line.
            assert view.state is exp_estado, (
                f"stage={stage} state derivado {view.state!r}, esperado "
                f"{exp_estado!r} para distribuição {distribution!r}"
            )
            assert view.progress == exp_progresso, (
                f"stage={stage} progress derivado {view.progress}, "
                f"esperado {exp_progresso} para distribuição {distribution!r}"
            )

            # 3) A leitura por Estágio concorda com a visão agregada (mesma
            #    derivação) para cada Estágio em 1..6.
            single = service.get_stage_state(campaign_id, stage)
            assert single.stage == stage
            assert single.state is exp_estado
            assert single.progress == exp_progresso

        # 4) fully_completed sse todo Estágio derivou completed — corolário
        #    direto da derivação por Estágio sobre todo o range 1..6.
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
def test_property3_absent_stages_default_to_nao_iniciado_zero_seis(
    present_stage: int,
    state: StageState,
    progress: int,
) -> None:
    """Só o Estágio semeado reflete a sua line; todo outro Estágio é nao_iniciado/0.

    Semeia um único Estágio e afirma que exatamente esse Estágio deriva o
    ``(state, progress)`` semeado, enquanto todos os outros Estágios em 1..6 —
    que não têm line — derivam ``nao_iniciado`` / 0. Isola o default de line
    faltante da derivação semeada, cobrindo os seis números de Estágio (Req. 1.2).

    # Feature: pipeline-seis-stages, Property 3: Derivação de state por
    # StageRun em 1..6.
    Validates: Requirements 1.2, 1.4
    """
    session, db_path, engine = _build_ephemeral_session()
    campaign_id = "campaign-seis-prop3-deriv-single"
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
