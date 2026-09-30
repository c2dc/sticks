"""Property-based test for Property 4 of the pipeline-ui design (task 8.5).

# Feature: pipeline-ui, Property 4: O progresso agregado conta exatamente os
# casos totalmente concluídos. Para qualquer distribuição de estados dos
# estágios entre os 8 casos curados, o progresso agregado de replicação é igual
# à quantidade de casos cujos três estágios estão no estado "concluído" — nem
# mais, nem menos.

Validates: Requisitos 5.5

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.stage.stage_service.StageService` implementation. It
exercises the aggregate replication progress (Req. 5.5) computed by
:meth:`StageService.aggregate_progress`.

How the property is driven
--------------------------
The aggregate progress is *pure logic over the persisted ``StageRun`` rows*: a
case counts as fully concluded only when **all three** of its stages are in the
``concluido`` state, and the aggregate is the number of such cases out of the 8
curated cases. A stage's state is read from its ``StageRun`` row (defaulting to
``nao_iniciado`` when there is no row yet — Req. 9.5).

So the generator produces, for each of the 8 curated cases, a triple of the
three stages' states — one of the four states ``nao_iniciado`` /
``em_andamento`` / ``concluido`` / ``erro``, where "no row" is modelled by
simply not writing a row for that stage (which the service must treat as
``nao_iniciado``). Hypothesis samples across widely varied distributions — from
"no case complete" through "some complete" to "all 8 complete" — with at least
100 iterations.

An INDEPENDENT oracle (``_expected_completed_ids``) recomputes, directly from the
generated states, exactly which cases have all three stages ``concluido`` — the
assertions never trust the implementation to decide what "fully completed"
means. We assert:

* ``casos_concluidos`` equals the oracle count — EXACTLY, so a case with any
  non-``concluido`` stage does NOT count, and every all-``concluido`` case DOES;
* ``casos_concluidos_ids`` is exactly the oracle's set of completed cases;
* ``total_casos`` is the whole 8-case universe passed in;
* ``percentual`` equals the independently recomputed ``round(done*100/total)``
  and always lies in ``0..100``;
* the count is monotone at the extremes: it is ``0`` iff no case is fully
  complete and ``total`` iff every case is fully complete.

Each example builds and tears down a fresh temp-file SQLite database (mirroring
``test_stage_blocking_property.py``), so there is no state leak between examples
and every distribution is scored in isolation. NO real Docker daemon and NO
network access are involved — everything is pure in-memory DB logic, safe on the
Windows dev environment.
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
# The 8 curated cases (the universe the aggregate is scored against — Req. 5.5).
# ---------------------------------------------------------------------------

_TOTAL_CASES = 8
_CASE_IDS: tuple[str, ...] = tuple(f"caso-prop4-{i}" for i in range(_TOTAL_CASES))


# ---------------------------------------------------------------------------
# Generators for a single case's three-stage state triple, then 8 of them.
# ---------------------------------------------------------------------------

# The four persisted states a stage can be in, PLUS ``None`` to model "no
# StageRun row yet" — which the service must treat as ``nao_iniciado`` (Req.
# 9.5). Including ``None`` proves the missing-row default participates in the
# aggregate exactly like an explicit ``nao_iniciado``.
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

_stage_state_strategy = st.sampled_from(_STATE_CHOICES)

# One case = (stage1_state, stage2_state, stage3_state).
_case_triple_strategy = st.tuples(
    _stage_state_strategy, _stage_state_strategy, _stage_state_strategy
)

# A full distribution across the 8 curated cases: 8 triples, one per case.
_distribution_strategy = st.lists(
    _case_triple_strategy, min_size=_TOTAL_CASES, max_size=_TOTAL_CASES
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes the fully-completed cases from the states.
# ---------------------------------------------------------------------------


def _effective_state(state: Optional[StageState]) -> StageState:
    """A missing row (``None``) is ``nao_iniciado`` (Req. 9.5)."""
    return state if state is not None else StageState.NOT_STARTED


def _case_fully_completed(triple: tuple[Optional[StageState], ...]) -> bool:
    """A case counts iff ALL THREE stages are ``concluido`` (Req. 5.5)."""
    return all(
        _effective_state(state) is StageState.COMPLETED for state in triple
    )


def _expected_completed_ids(
    distribution: list[tuple[Optional[StageState], ...]],
) -> list[str]:
    """Recompute the fully-completed case ids WITHOUT calling the implementation."""
    return [
        caso_id
        for caso_id, triple in zip(_CASE_IDS, distribution)
        if _case_fully_completed(triple)
    ]


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_stage_blocking_property.py``. Returns ``(session, db_path, engine)``
    so the caller can dispose the engine and remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_agg_progress_")
    db_path = Path(tmp_dir) / "agg_progress.db"
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
    distribution: list[tuple[Optional[StageState], ...]],
) -> None:
    """Persist the StageRun rows for the whole 8-case distribution.

    For each case, one ``StageRun`` per stage whose generated state is not
    ``None`` is written; a ``None`` state means "no row" — the stage is left
    absent so the service exercises its missing-row default (``nao_iniciado``).
    Progress is 100 for a completed stage and 0 otherwise, matching how the
    service persists it.
    """
    for caso_id, triple in zip(_CASE_IDS, distribution):
        for estagio, state in zip(VALID_STAGES, triple):
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
# Property 4
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
def test_property4_aggregate_counts_exactly_fully_completed_cases(
    distribution: list[tuple[Optional[StageState], ...]],
) -> None:
    """The aggregate counts EXACTLY the cases whose three stages are concluido.

    # Feature: pipeline-ui, Property 4: O progresso agregado conta exatamente os
    # casos totalmente concluídos.
    Validates: Requisitos 5.5
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_distribution(session, distribution)
        service = StageService(session)

        # Independent oracle: which cases have all three stages concluido.
        expected_ids = _expected_completed_ids(distribution)
        expected_done = len(expected_ids)
        expected_percentual = round(expected_done * 100 / _TOTAL_CASES)

        progress = service.aggregate_progress(list(_CASE_IDS))

        # 1) Universe size is exactly the 8 curated cases passed in.
        assert progress.total_casos == _TOTAL_CASES

        # 2) The count matches the oracle EXACTLY — no over- or under-counting.
        assert progress.casos_concluidos == expected_done, (
            f"casos_concluidos == {progress.casos_concluidos}, esperado "
            f"{expected_done} para a distribuição {distribution!r}"
        )

        # 3) The completed set is exactly the oracle's set (order-independent).
        assert set(progress.casos_concluidos_ids) == set(expected_ids)
        # No case is reported twice.
        assert len(progress.casos_concluidos_ids) == len(
            set(progress.casos_concluidos_ids)
        )

        # 4) Every reported completed case is genuinely fully completed, and no
        #    other case is — i.e. the count is EXACT, not just the right size.
        completed = set(progress.casos_concluidos_ids)
        for caso_id, triple in zip(_CASE_IDS, distribution):
            fully = _case_fully_completed(triple)
            assert (caso_id in completed) is fully, (
                f"{caso_id} reportado concluído={caso_id in completed}, "
                f"mas totalmente concluído={fully} (estados {triple!r})"
            )

        # 5) The percentage is the independently recomputed ratio and is bounded.
        assert progress.percentual == expected_percentual
        assert 0 <= progress.percentual <= 100

        # 6) Extremes: 0 iff no case is complete; total iff every case is.
        if expected_done == 0:
            assert progress.casos_concluidos == 0
            assert progress.percentual == 0
        if expected_done == _TOTAL_CASES:
            assert progress.casos_concluidos == _TOTAL_CASES
            assert progress.percentual == 100
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
    num_complete=st.integers(min_value=0, max_value=_TOTAL_CASES),
    # For the non-complete cases, pick which stage is left non-concluido and
    # in which non-completed state, so "one stage short" never counts.
    broken_stage=st.sampled_from(VALID_STAGES),
    broken_state=st.sampled_from(
        (None, StageState.NOT_STARTED, StageState.IN_PROGRESS, StageState.ERROR)
    ),
)
def test_property4_case_one_stage_short_never_counts(
    num_complete: int,
    broken_stage: int,
    broken_state: Optional[StageState],
) -> None:
    """A case with any single non-concluido stage is never counted (Req. 5.5).

    Builds a distribution with ``num_complete`` fully-completed cases; every
    remaining case is completed in two stages but has ``broken_stage`` left in a
    non-``concluido`` state. The aggregate must count EXACTLY ``num_complete``.

    # Feature: pipeline-ui, Property 4: O progresso agregado conta exatamente os
    # casos totalmente concluídos.
    Validates: Requisitos 5.5
    """
    assert _effective_state(broken_state) is not StageState.COMPLETED

    distribution: list[tuple[Optional[StageState], ...]] = []
    for idx in range(_TOTAL_CASES):
        if idx < num_complete:
            distribution.append(
                (StageState.COMPLETED, StageState.COMPLETED, StageState.COMPLETED)
            )
        else:
            triple = [
                StageState.COMPLETED,
                StageState.COMPLETED,
                StageState.COMPLETED,
            ]
            triple[broken_stage - 1] = broken_state  # type: ignore[assignment]
            distribution.append(tuple(triple))

    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_distribution(session, distribution)
        service = StageService(session)

        progress = service.aggregate_progress(list(_CASE_IDS))

        assert progress.total_casos == _TOTAL_CASES
        assert progress.casos_concluidos == num_complete
        assert set(progress.casos_concluidos_ids) == set(_CASE_IDS[:num_complete])
        assert progress.percentual == round(num_complete * 100 / _TOTAL_CASES)
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
