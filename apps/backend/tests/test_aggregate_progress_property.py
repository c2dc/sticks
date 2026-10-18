"""Property-based test for the aggregate progress over the four stages (task 7.4).

# Feature: pipeline-cinco-stages, Property 2: campaign totalmente concluído sse os
# cinco Estágios completed. Para qualquer distribuição de states dos Estágios
# across the 8 curated Campaigns, the aggregate replication progress equals the
# count of Campaigns whose five Stages are all in the "concluido" state —
# nem mais, nem menos.

Validates: Requirements 5.5 (adaptado para 1..5), 1.6, 8.2

This is a normal ``feature`` property test (``dag-stage-multi-origin`` is a
feature spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.stage.stage_service.StageService` implementation. It
exercises the aggregate replication progress computed by
:meth:`StageService.aggregate_progress`.

How the property is driven
--------------------------
The aggregate progress is *pure logic over the persisted ``StageRun`` rows*: a
case counts as fully concluded only when **all five** of its stages are in the
``completed`` state, and the aggregate is the number of such cases out of the 8
curated cases. A stage's state is read from its ``StageRun`` row (defaulting to
``nao_iniciado`` when there is no row yet — Req. 9.5).

Everything about the number of stages is derived from
:data:`~app.services.stage.stage_service.VALID_STAGES` (and its length) imported
from ``stage_service`` — there is NO hardcoded ``4`` or ``5`` here. After the
pipeline was generalised to ``VALID_STAGES == (1, 2, 3, 4, 5)``, the generator
produces, for each of the 8 curated cases, a tuple of ``len(VALID_STAGES)``
stage states — one of the four states ``nao_iniciado`` / ``em_andamento`` /
``completed`` / ``erro``, where "no row" is modelled by simply not writing a row
for that stage (which the service must treat as ``nao_iniciado``). Hypothesis
samples across widely varied distributions — from "no case complete" through
"some complete" to "all 8 complete" — with at least 100 iterations.

An INDEPENDENT oracle (``_expected_completed_ids``) recomputes, directly from the
generated states, exactly which cases have all ``len(VALID_STAGES)`` stages
``completed`` — the assertions never trust the implementation to decide what
"fully completed" means. We assert:

* ``completed_campaigns`` equals the oracle count — EXACTLY, so a case with any
  non-``completed`` stage does NOT count, and every all-``completed`` case DOES;
* ``completed_campaign_ids`` is exactly the oracle's set of completed cases;
* ``total_campaigns`` is the whole 8-case universe passed in;
* ``percentage`` equals the independently recomputed ``round(done*100/total)``
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
_CASE_IDS: tuple[str, ...] = tuple(f"campaign-prop2-{i}" for i in range(_TOTAL_CASES))

# The number of stages a case has is the single source of truth, driven from
# ``VALID_STAGES`` — never a hardcoded 4 or 5. After the renumbering this is 5.
_NUM_STAGES = len(VALID_STAGES)


# ---------------------------------------------------------------------------
# Generators for a single case's per-stage state tuple, then 8 of them.
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

# One case = a tuple of ``len(VALID_STAGES)`` stage states (5 after renumbering).
_case_states_strategy = st.tuples(*(_stage_state_strategy for _ in VALID_STAGES))

# A full distribution across the 8 curated cases: 8 tuples, one per case.
_distribution_strategy = st.lists(
    _case_states_strategy, min_size=_TOTAL_CASES, max_size=_TOTAL_CASES
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes the fully-completed cases from the states.
# ---------------------------------------------------------------------------


def _effective_state(state: Optional[StageState]) -> StageState:
    """A missing row (``None``) is ``nao_iniciado`` (Req. 9.5)."""
    return state if state is not None else StageState.NOT_STARTED


def _case_fully_completed(states: tuple[Optional[StageState], ...]) -> bool:
    """A case counts iff ALL ``len(VALID_STAGES)`` stages are ``completed`` (5)."""
    return all(
        _effective_state(state) is StageState.COMPLETED for state in states
    )


def _expected_completed_ids(
    distribution: list[tuple[Optional[StageState], ...]],
) -> list[str]:
    """Recompute the fully-completed case ids WITHOUT calling the implementation."""
    return [
        campaign_id
        for campaign_id, states in zip(_CASE_IDS, distribution)
        if _case_fully_completed(states)
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
    tmp_dir = tempfile.mkdtemp(prefix="dag_multi_agg_progress_")
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
    service persists it. Stages are paired with ``VALID_STAGES`` so the stage
    numbers span the full 1..5 range.
    """
    for campaign_id, states in zip(_CASE_IDS, distribution):
        for stage, state in zip(VALID_STAGES, states):
            if state is None:
                continue
            session.add(
                StageRun(
                    campaign_id=campaign_id,
                    stage=stage,
                    state=state,
                    progress=100 if state is StageState.COMPLETED else 0,
                )
            )
    session.commit()


# ---------------------------------------------------------------------------
# Property 2
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
def test_property2_aggregate_counts_exactly_fully_completed_cases(
    distribution: list[tuple[Optional[StageState], ...]],
) -> None:
    """The aggregate counts EXACTLY the cases whose five stages are completed.

    # Feature: pipeline-cinco-stages, Property 2: campaign totalmente concluído
    # sse os cinco Estágios completed.
    Validates: Requirements 5.5 (adaptado para 1..5), 1.6, 8.2
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_distribution(session, distribution)
        service = StageService(session)

        # Independent oracle: which cases have all five stages completed.
        expected_ids = _expected_completed_ids(distribution)
        expected_done = len(expected_ids)
        expected_percentual = round(expected_done * 100 / _TOTAL_CASES)

        progress = service.aggregate_progress(list(_CASE_IDS))

        # 1) Universe size is exactly the 8 curated cases passed in.
        assert progress.total_campaigns == _TOTAL_CASES

        # 2) The count matches the oracle EXACTLY — no over- or under-counting.
        assert progress.completed_campaigns == expected_done, (
            f"completed_campaigns == {progress.completed_campaigns}, esperado "
            f"{expected_done} para a distribuição {distribution!r}"
        )

        # 3) The completed set is exactly the oracle's set (order-independent).
        assert set(progress.completed_campaign_ids) == set(expected_ids)
        # No case is reported twice.
        assert len(progress.completed_campaign_ids) == len(
            set(progress.completed_campaign_ids)
        )

        # 4) Every reported completed case is genuinely fully completed, and no
        #    other case is — i.e. the count is EXACT, not just the right size.
        completed = set(progress.completed_campaign_ids)
        for campaign_id, states in zip(_CASE_IDS, distribution):
            fully = _case_fully_completed(states)
            assert (campaign_id in completed) is fully, (
                f"{campaign_id} reportado concluído={campaign_id in completed}, "
                f"mas totalmente concluído={fully} (states {states!r})"
            )

        # 5) The percentage is the independently recomputed ratio and is bounded.
        assert progress.percentage == expected_percentual
        assert 0 <= progress.percentage <= 100

        # 6) Extremes: 0 iff no case is complete; total iff every case is.
        if expected_done == 0:
            assert progress.completed_campaigns == 0
            assert progress.percentage == 0
        if expected_done == _TOTAL_CASES:
            assert progress.completed_campaigns == _TOTAL_CASES
            assert progress.percentage == 100
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
    # For the non-complete cases, pick which stage is left non-completed and
    # in which non-completed state, so "one stage short" never counts. The
    # broken stage is drawn from VALID_STAGES, so it spans the full 1..5 range.
    broken_stage=st.sampled_from(VALID_STAGES),
    broken_state=st.sampled_from(
        (None, StageState.NOT_STARTED, StageState.IN_PROGRESS, StageState.ERROR)
    ),
)
def test_property2_case_one_stage_short_never_counts(
    num_complete: int,
    broken_stage: int,
    broken_state: Optional[StageState],
) -> None:
    """A case with any single non-completed stage is never counted.

    Builds a distribution with ``num_complete`` fully-completed cases; every
    remaining case is completed in all but one stage but has ``broken_stage``
    (somewhere in 1..5) left in a non-``completed`` state. The aggregate must
    count EXACTLY ``num_complete`` — a case one stage short of all five
    ``completed`` never counts.

    # Feature: pipeline-cinco-stages, Property 2: campaign totalmente concluído
    # sse os cinco Estágios completed.
    Validates: Requirements 5.5 (adaptado para 1..5), 1.6, 8.2
    """
    assert _effective_state(broken_state) is not StageState.COMPLETED

    distribution: list[tuple[Optional[StageState], ...]] = []
    for idx in range(_TOTAL_CASES):
        if idx < num_complete:
            # All stages completed => this case counts.
            distribution.append(
                tuple(StageState.COMPLETED for _ in VALID_STAGES)
            )
        else:
            # Complete every stage, then break exactly one so it is one stage
            # short of fully completed and must NOT count.
            states: list[Optional[StageState]] = [
                StageState.COMPLETED for _ in VALID_STAGES
            ]
            states[VALID_STAGES.index(broken_stage)] = broken_state
            distribution.append(tuple(states))

    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_distribution(session, distribution)
        service = StageService(session)

        progress = service.aggregate_progress(list(_CASE_IDS))

        assert progress.total_campaigns == _TOTAL_CASES
        assert progress.completed_campaigns == num_complete
        assert set(progress.completed_campaign_ids) == set(_CASE_IDS[:num_complete])
        assert progress.percentage == round(num_complete * 100 / _TOTAL_CASES)
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
