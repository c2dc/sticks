"""Property-based test for the aggregate progress over the SIX stages (task 1.3).

# Feature: pipeline-seis-estagios, Property 2: Campanha totalmente concluída sse
# os seis Estágios concluido. Uma campanha conta como totalmente concluída no
# progresso agregado se e somente se os seis Estágios estão "concluido".

Validates: Requirements 1.5

This is a normal ``feature`` property test (``pipeline-seis-estagios`` is a
feature spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.stage.stage_service.StageService` implementation. It
exercises the aggregate replication progress computed by
:meth:`StageService.aggregate_progress` and the ``concluido_total`` flag of
:meth:`StageService.get_stage_states`, now that the pipeline has SIX Estágios
(``VALID_STAGES == (1, 2, 3, 4, 5, 6)`` after task 1.1 widened the range).

How the property is driven
--------------------------
The aggregate progress is *pure logic over the persisted ``StageRun`` rows*: a
campaign counts as fully concluded **if and only if all six** of its stages are
in the ``concluido`` state, and the aggregate is the number of such campaigns
out of the campaign universe passed in. A stage's state is read from its
``StageRun`` row (defaulting to ``nao_iniciado`` when there is no row yet).

Everything about the number of stages is derived from
:data:`~app.services.stage.stage_service.VALID_STAGES` (and its length) imported
from ``stage_service`` — there is NO hardcoded ``6`` in the generators or the
oracle. Because task 1.1 widened ``VALID_STAGES`` to ``(1, 2, 3, 4, 5, 6)``, the
generator produces, for each campaign, a tuple of ``len(VALID_STAGES)`` stage
states — one of the four states ``nao_iniciado`` / ``em_andamento`` /
``concluido`` / ``erro``, where "no row" is modelled by simply not writing a row
for that stage (which the service must treat as ``nao_iniciado``). Hypothesis
samples across widely varied distributions — from "no campaign complete" through
"some complete" to "all complete" — with at least 100 iterations.

An INDEPENDENT oracle (``_expected_completed_ids``) recomputes, directly from the
generated states, exactly which campaigns have all ``len(VALID_STAGES)`` stages
``concluido`` — the assertions never trust the implementation to decide what
"fully completed" means. We assert the biconditional (sse / iff) from Req. 1.5:

* ``casos_concluidos`` equals the oracle count — EXACTLY, so a campaign with any
  non-``concluido`` stage does NOT count, and every all-``concluido`` campaign
  DOES;
* ``casos_concluidos_ids`` is exactly the oracle's set of completed campaigns;
* ``get_stage_states(...).concluido_total`` is ``True`` for a campaign IFF the
  oracle says all six stages are ``concluido`` — the per-campaign side of the
  same biconditional;
* ``total_casos`` is the whole universe passed in;
* ``percentual`` equals the independently recomputed ``round(done*100/total)``
  and always lies in ``0..100``;
* the count is monotone at the extremes: it is ``0`` iff no campaign is fully
  complete and ``total`` iff every campaign is fully complete.

Each example builds and tears down a fresh temp-file SQLite database (mirroring
``test_aggregate_progress_property.py`` / ``test_stage_blocking_property.py``),
so there is no state leak between examples and every distribution is scored in
isolation. NO real Docker daemon and NO network access are involved — everything
is pure in-memory DB logic, safe on the Windows dev environment.
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
# The campaign universe the aggregate is scored against (8 curated campaigns).
# ---------------------------------------------------------------------------

_TOTAL_CASES = 8
_CASE_IDS: tuple[str, ...] = tuple(f"caso-seis-prop2-{i}" for i in range(_TOTAL_CASES))

# The number of stages a campaign has is the single source of truth, driven from
# ``VALID_STAGES`` — never a hardcoded 6. After task 1.1 this is 6.
_NUM_STAGES = len(VALID_STAGES)


# ---------------------------------------------------------------------------
# Generators for a single campaign's per-stage state tuple, then the universe.
# ---------------------------------------------------------------------------

# The four persisted states a stage can be in, PLUS ``None`` to model "no
# StageRun row yet" — which the service must treat as ``nao_iniciado``. Including
# ``None`` proves the missing-row default participates in the aggregate exactly
# like an explicit ``nao_iniciado``.
_STATE_CHOICES: tuple[Optional[StageState], ...] = (
    None,
    StageState.NOT_STARTED,
    StageState.IN_PROGRESS,
    StageState.COMPLETED,
    StageState.ERROR,
)

_stage_state_strategy = st.sampled_from(_STATE_CHOICES)

# One campaign = a tuple of ``len(VALID_STAGES)`` stage states (6 after task 1.1).
_case_states_strategy = st.tuples(*(_stage_state_strategy for _ in VALID_STAGES))

# A full distribution across the campaign universe: one tuple per campaign.
_distribution_strategy = st.lists(
    _case_states_strategy, min_size=_TOTAL_CASES, max_size=_TOTAL_CASES
)


# ---------------------------------------------------------------------------
# Independent oracle — recomputes the fully-completed campaigns from the states.
# ---------------------------------------------------------------------------


def _effective_state(state: Optional[StageState]) -> StageState:
    """A missing row (``None``) is ``nao_iniciado``."""
    return state if state is not None else StageState.NOT_STARTED


def _case_fully_completed(states: tuple[Optional[StageState], ...]) -> bool:
    """A campaign counts iff ALL ``len(VALID_STAGES)`` stages are ``concluido`` (6)."""
    return all(
        _effective_state(state) is StageState.COMPLETED for state in states
    )


def _expected_completed_ids(
    distribution: list[tuple[Optional[StageState], ...]],
) -> list[str]:
    """Recompute the fully-completed campaign ids WITHOUT calling the implementation."""
    return [
        caso_id
        for caso_id, states in zip(_CASE_IDS, distribution)
        if _case_fully_completed(states)
    ]


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
    tmp_dir = tempfile.mkdtemp(prefix="seis_estagios_agg_progress_")
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
    """Persist the StageRun rows for the whole campaign-universe distribution.

    For each campaign, one ``StageRun`` per stage whose generated state is not
    ``None`` is written; a ``None`` state means "no row" — the stage is left
    absent so the service exercises its missing-row default (``nao_iniciado``).
    Progress is 100 for a completed stage and 0 otherwise, matching how the
    service persists it. Stages are paired with ``VALID_STAGES`` so the stage
    numbers span the full 1..6 range.
    """
    for caso_id, states in zip(_CASE_IDS, distribution):
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
# Property 2 — a campaign is fully concluded sse the six stages are concluido.
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
def test_property2_aggregate_counts_exactly_fully_completed_campaigns(
    distribution: list[tuple[Optional[StageState], ...]],
) -> None:
    """The aggregate counts EXACTLY the campaigns whose six stages are concluido.

    # Feature: pipeline-seis-estagios, Property 2: Campanha totalmente concluída
    # sse os seis Estágios concluido.
    Validates: Requirements 1.5
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_distribution(session, distribution)
        service = StageService(session)

        # Independent oracle: which campaigns have all six stages concluido.
        expected_ids = _expected_completed_ids(distribution)
        expected_done = len(expected_ids)
        expected_percentual = round(expected_done * 100 / _TOTAL_CASES)

        progress = service.aggregate_progress(list(_CASE_IDS))

        # 1) Universe size is exactly the campaign set passed in.
        assert progress.total_casos == _TOTAL_CASES

        # 2) The count matches the oracle EXACTLY — no over- or under-counting.
        assert progress.casos_concluidos == expected_done, (
            f"casos_concluidos == {progress.casos_concluidos}, esperado "
            f"{expected_done} para a distribuição {distribution!r}"
        )

        # 3) The completed set is exactly the oracle's set (order-independent).
        assert set(progress.casos_concluidos_ids) == set(expected_ids)
        # No campaign is reported twice.
        assert len(progress.casos_concluidos_ids) == len(
            set(progress.casos_concluidos_ids)
        )

        # 4) The biconditional (Req. 1.5), per campaign and both directions:
        #    a campaign is reported complete IFF all six stages are concluido,
        #    AND the per-campaign ``concluido_total`` flag agrees. This is the
        #    "sse os seis Estágios concluido" claim made concrete.
        completed = set(progress.casos_concluidos_ids)
        for caso_id, states in zip(_CASE_IDS, distribution):
            fully = _case_fully_completed(states)
            # (=>) and (<=) over the aggregate membership.
            assert (caso_id in completed) is fully, (
                f"{caso_id} reportado concluído={caso_id in completed}, "
                f"mas totalmente concluído={fully} (estados {states!r})"
            )
            # The per-campaign flag mirrors the same biconditional.
            case_states = service.get_stage_states(caso_id)
            assert case_states.concluido_total is fully
            # And the view set is exactly the six stages, never more nor fewer.
            assert {v.estagio for v in case_states.estagios} == set(VALID_STAGES)

        # 5) The percentage is the independently recomputed ratio and is bounded.
        assert progress.percentual == expected_percentual
        assert 0 <= progress.percentual <= 100

        # 6) Extremes: 0 iff no campaign is complete; total iff every one is.
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
    # For the non-complete campaigns, pick which stage is left non-concluido and
    # in which non-completed state, so "one stage short" never counts. The
    # broken stage is drawn from VALID_STAGES, so it spans the full 1..6 range —
    # including the new Estágio 6 "Execução + medição + relatório".
    broken_stage=st.sampled_from(VALID_STAGES),
    broken_state=st.sampled_from(
        (None, StageState.NOT_STARTED, StageState.IN_PROGRESS, StageState.ERROR)
    ),
)
def test_property2_campaign_one_stage_short_never_counts(
    num_complete: int,
    broken_stage: int,
    broken_state: Optional[StageState],
) -> None:
    """A campaign with any single non-concluido stage is never counted.

    Builds a distribution with ``num_complete`` fully-completed campaigns; every
    remaining campaign is completed in all but one stage but has ``broken_stage``
    (somewhere in 1..6) left in a non-``concluido`` state. The aggregate must
    count EXACTLY ``num_complete`` — a campaign one stage short of all six
    ``concluido`` never counts — proving the "only if" direction of Req. 1.5
    even for the sixth stage.

    # Feature: pipeline-seis-estagios, Property 2: Campanha totalmente concluída
    # sse os seis Estágios concluido.
    Validates: Requirements 1.5
    """
    assert _effective_state(broken_state) is not StageState.COMPLETED

    distribution: list[tuple[Optional[StageState], ...]] = []
    for idx in range(_TOTAL_CASES):
        if idx < num_complete:
            # All six stages concluido => this campaign counts.
            distribution.append(
                tuple(StageState.COMPLETED for _ in VALID_STAGES)
            )
        else:
            # Complete every stage, then break exactly one so it is one stage
            # short of fully concluido and must NOT count.
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

        assert progress.total_casos == _TOTAL_CASES
        assert progress.casos_concluidos == num_complete
        assert set(progress.casos_concluidos_ids) == set(_CASE_IDS[:num_complete])
        assert progress.percentual == round(num_complete * 100 / _TOTAL_CASES)

        # The per-campaign flag mirrors the aggregate: broken campaigns are not
        # ``concluido_total`` even though five of their six stages are concluido.
        for idx, caso_id in enumerate(_CASE_IDS):
            case_states = service.get_stage_states(caso_id)
            assert case_states.concluido_total is (idx < num_complete)
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def test_valid_stages_span_six_stages() -> None:
    """Guard: the covered range is exactly ``FIRST_STAGE..LAST_STAGE`` with 6 stages.

    This anchors the property's claim ("os seis Estágios") to the imported
    constants, so if the pipeline range ever changes the generators above
    (driven by ``VALID_STAGES``) and this guard move together instead of
    silently covering the wrong span.

    # Feature: pipeline-seis-estagios, Property 2: Campanha totalmente concluída
    # sse os seis Estágios concluido.
    Validates: Requirements 1.5
    """
    assert VALID_STAGES[0] == FIRST_STAGE
    assert VALID_STAGES[-1] == LAST_STAGE
    assert VALID_STAGES == tuple(range(FIRST_STAGE, LAST_STAGE + 1))
    assert len(VALID_STAGES) == 6
