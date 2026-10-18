"""Property-based test for Property 8 of the pipeline-ui design (task 9.3).

# Feature: pipeline-ui, Property 8: Round-trip do Estado_de_Sessão. Para
# qualquer Estado_de_Sessão válido (campaign atual, estágios concluídos por campaign e
# results de operações por Ability), persistir e em seguida recuperar produz
# um state estruturalmente igual ao persistido.

Validates: Requirements 9.1, 9.4

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.session.session_state_service.SessionStateService`
implementation. It exercises the persist-then-retrieve round-trip (Req. 9.1 for
what the Estado_de_Sessão is composed of; Req. 9.4 for retrieving it on open).

What the Estado_de_Sessão is (Req. 9.1)
---------------------------------------
Per Req. 9.1 / the design's Data Models, the persisted Estado_de_Sessão is the
combination of three things:

* the **current case** (``current_campaign``) — stored directly on the single
  ``session_state`` row and written via ``set_current_case``;
* the **completed stages per case** — derived from the ``StageRun`` rows in the
  ``completed`` state (a stage is "concluido" exactly when its ``StageRun`` is
  ``completed``), written here via ``persist_stage_completion`` (the service's
  own write path — Req. 9.2);
* the **per-Ability operation results per case** — for each Operation of a case,
  the status and command output of every executed Ability, derived from the
  ``Operation`` → ``AbilityResult`` rows (Req. 9.1, 9.7). These are persisted by
  the Operation services, so the test seeds those rows directly and verifies the
  service reads them back structurally unchanged.

How the property is driven
--------------------------
Hypothesis generates an arbitrary but *valid* Estado_de_Sessão:

* a ``current_campaign`` chosen among a small universe of case slugs, or ``None`` (no
  case selected);
* for each of several cases, an arbitrary subset of the three stages marked
  completed (``{}`` … ``{1, 2, 3}``), so the completed-stages-per-case map spans
  from "nothing done" to "everything done";
* for each case, an arbitrary list of Operations, each with an arbitrary list of
  per-Ability results (status + command output), plus the persisted
  success/failure aggregate.

The state is then persisted **through the service's own write path** where one
exists (``set_current_case`` for the current case, ``persist_stage_completion``
for each completed stage) and by seeding the Operation/AbilityResult rows for
the results view. It is recovered with ``get_session_state`` (Req. 9.4) plus
``get_operation_results`` and compared to an INDEPENDENT expectation recomputed
from the generated input — the assertions never trust the implementation to
decide what the round-tripped state should be. We assert structural equality of
every relevant field:

* the recovered ``current_campaign`` equals the persisted one;
* the recovered completed-stages-per-case map equals the generated one, with
  each case's stages as a sorted, de-duplicated list and cases with no completed
  stage omitted (a case with no completed stage renders as all "não iniciado" —
  Req. 9.5);
* for every case, the recovered per-Ability operation results (operation
  ordering, per-result ordering, each result's ability id / status / command
  output, and the operation's success/failure aggregate) equal what was
  persisted, and stay associated with the correct case (Req. 9.7).

At least 100 iterations run. Each example builds and tears down a fresh
temp-file SQLite database (mirroring ``test_stage_blocking_property.py`` and
``test_aggregate_progress_property.py``), so there is no state leak between
examples and every generated state is round-tripped in isolation. NO real
Docker daemon and NO network access are involved — everything is pure in-memory
DB logic, safe on the Windows dev environment.
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
from app.models.domain import AbilityResult, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.session.session_state_service import SessionStateService

# ---------------------------------------------------------------------------
# Universe the arbitrary Estado_de_Sessão is generated over.
# ---------------------------------------------------------------------------

# A small, fixed pool of case slugs. Using a bounded pool keeps generated cases
# comparable across the current-case, completed-stages and results components
# while still letting Hypothesis vary which cases appear.
_CASE_IDS: tuple[str, ...] = tuple(f"campaign-prop8-{i}" for i in range(4))

# The three stages of a curated case.
_STAGES: tuple[int, ...] = (1, 2, 3)

# The four per-Ability result statuses (Req. 4.3).
_STATUS_CHOICES: tuple[AbilityResultStatus, ...] = tuple(AbilityResultStatus)

# The Operation states.
_OP_STATE_CHOICES: tuple[OperationState, ...] = tuple(OperationState)


# ---------------------------------------------------------------------------
# Generators for an arbitrary, valid Estado_de_Sessão.
# ---------------------------------------------------------------------------

# The current case: one of the pool slugs, or None (no case selected — Req. 9.5
# renders that as nothing selected). Only slugs from the pool are used so the
# generated state stays internally consistent.
_current_campaign_strategy = st.one_of(st.none(), st.sampled_from(_CASE_IDS))

# Completed stages of a single case: an arbitrary subset of {1, 2, 3}. ``{}``
# means no stage completed for that case (omitted from the derived map).
_completed_stages_strategy = st.sets(st.sampled_from(_STAGES))

# Completed-stages-per-case: a map from a subset of the case pool to its
# completed-stage set. ``dictionaries`` naturally varies which cases appear.
_completed_by_case_strategy = st.dictionaries(
    keys=st.sampled_from(_CASE_IDS),
    values=_completed_stages_strategy,
    max_size=len(_CASE_IDS),
)

# One per-Ability result: an ability id, a status and an optional command output.
_ability_result_strategy = st.fixed_dictionaries(
    {
        "ability_id": st.text(
            alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=12
        ),
        "status": st.sampled_from(_STATUS_CHOICES),
        "command_output": st.one_of(st.none(), st.text(max_size=40)),
    }
)

# One Operation of a case: an operation state, its persisted aggregate and an
# arbitrary (possibly empty) list of per-Ability results.
_operation_strategy = st.fixed_dictionaries(
    {
        "state": st.sampled_from(_OP_STATE_CHOICES),
        "total_success": st.integers(min_value=0, max_value=20),
        "total_failure": st.integers(min_value=0, max_value=20),
        "results": st.lists(_ability_result_strategy, max_size=4),
    }
)

# Operations-per-case: a map from a subset of the case pool to its (possibly
# empty) list of Operations.
_operations_by_case_strategy = st.dictionaries(
    keys=st.sampled_from(_CASE_IDS),
    values=st.lists(_operation_strategy, max_size=3),
    max_size=len(_CASE_IDS),
)

# The whole arbitrary Estado_de_Sessão.
_session_state_strategy = st.fixed_dictionaries(
    {
        "current_campaign": _current_campaign_strategy,
        "completed_by_case": _completed_by_case_strategy,
        "operations_by_case": _operations_by_case_strategy,
    }
)


# ---------------------------------------------------------------------------
# Independent oracle — what the round-trip must reproduce, computed from input.
# ---------------------------------------------------------------------------


def _expected_completed_map(
    completed_by_case: dict[str, set[int]],
) -> dict[str, list[int]]:
    """Expected completed-stages-per-case map, computed from the input alone.

    Mirrors the service contract: stages sorted ascending and de-duplicated,
    and cases with no completed stage omitted (Req. 9.1 / 9.5). Never calls the
    implementation.
    """
    expected: dict[str, list[int]] = {}
    for campaign_id, stages in completed_by_case.items():
        if stages:
            expected[campaign_id] = sorted(set(stages))
    return expected


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + schema per example).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_stage_blocking_property.py`` / ``test_aggregate_progress_property.py``.
    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_session_roundtrip_")
    db_path = Path(tmp_dir) / "session_roundtrip.db"
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


def _seed_operations(
    session: Session,
    operations_by_case: dict[str, list[dict]],
) -> dict[str, list[dict]]:
    """Persist the Operation/AbilityResult rows for the results component.

    Operations are persisted by the Operation services in production, so the
    round-trip seeds those rows directly (there is no SessionStateService write
    method for them) and later verifies ``get_operation_results`` reads them
    back unchanged. Rows are inserted per case in list order; ``AbilityResult``
    rows in each Operation's ``results`` order — matching the id-ascending
    ordering the service reads with.

    Returns a per-case list of the expected result views (with the assigned
    ``operation_id``) so the assertions compare against exactly what was written.
    """
    expected: dict[str, list[dict]] = {}
    for campaign_id in _CASE_IDS:
        ops = operations_by_case.get(campaign_id, [])
        expected_ops: list[dict] = []
        for op_spec in ops:
            op = Operation(
                campaign_id=campaign_id,
                state=op_spec["state"],
                total_success=op_spec["total_success"],
                total_failure=op_spec["total_failure"],
            )
            session.add(op)
            # Flush so the autoincrement id is assigned before we attach results
            # and record it in the expectation.
            session.flush()

            expected_results: list[dict] = []
            for res_spec in op_spec["results"]:
                session.add(
                    AbilityResult(
                        operation_id=op.id,
                        ability_id=res_spec["ability_id"],
                        status=res_spec["status"],
                        command_output=res_spec["command_output"],
                    )
                )
                expected_results.append(
                    {
                        "operation_id": op.id,
                        "ability_id": res_spec["ability_id"],
                        "status": res_spec["status"],
                        "command_output": res_spec["command_output"],
                    }
                )
            expected_ops.append(
                {
                    "operation_id": op.id,
                    "campaign_id": campaign_id,
                    "state": op_spec["state"],
                    "total_success": op_spec["total_success"],
                    "total_failure": op_spec["total_failure"],
                    "results": expected_results,
                }
            )
        if expected_ops:
            expected[campaign_id] = expected_ops
    session.commit()
    return expected


# ---------------------------------------------------------------------------
# Property 8
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
@given(state=_session_state_strategy)
def test_property8_session_state_roundtrips_structurally(
    state: dict,
) -> None:
    """Persisting then retrieving an arbitrary Estado_de_Sessão is structurally identity.

    # Feature: pipeline-ui, Property 8: Round-trip do Estado_de_Sessão.
    Validates: Requirements 9.1, 9.4
    """
    current_campaign: Optional[str] = state["current_campaign"]
    completed_by_case: dict[str, set[int]] = state["completed_by_case"]
    operations_by_case: dict[str, list[dict]] = state["operations_by_case"]

    session, db_path, engine = _build_ephemeral_session()
    try:
        service = SessionStateService(session)

        # --- Persist the state THROUGH the service's own write path ----------
        # Current case (Req. 9.4). ``None`` is a valid "no case selected" state.
        service.set_current_case(current_campaign)

        # Completed stages per case: persist each completion via the service's
        # own write method (Req. 9.2), the way the Backend records a conclusion.
        for campaign_id, stages in completed_by_case.items():
            for stage in sorted(stages):
                result = service.persist_stage_completion(campaign_id, stage)
                # The write path must report success on a healthy DB.
                assert result.saved is True

        # Per-Ability operation results per case (Req. 9.1, 9.7): persisted by
        # the Operation services in production; seeded directly here.
        expected_ops_by_case = _seed_operations(session, operations_by_case)

        # --- Recover the state (Req. 9.4) ------------------------------------
        recovered = service.get_session_state()

        # A healthy round-trip: the state exists and did not fail to restore.
        assert recovered.exists is True
        assert recovered.restore_failed is False

        # 1) Current case round-trips exactly (including the None case).
        assert recovered.current_campaign == current_campaign

        # 2) Completed-stages-per-case map equals the independently computed one.
        expected_completed = _expected_completed_map(completed_by_case)
        assert recovered.completed_stages_by_campaign == expected_completed

        # A case with no completed stage must NOT appear in the map (Req. 9.5).
        for campaign_id, stages in completed_by_case.items():
            if not stages:
                assert campaign_id not in recovered.completed_stages_by_campaign

        # 3) Per-Ability operation results round-trip per case (Req. 9.1, 9.7).
        for campaign_id in _CASE_IDS:
            recovered_ops = service.get_operation_results(campaign_id)
            expected_ops = expected_ops_by_case.get(campaign_id, [])

            # Same number of Operations, in the same (creation) order, all bound
            # to this case (association preserved — Req. 9.7).
            assert len(recovered_ops) == len(expected_ops)
            for got_op, exp_op in zip(recovered_ops, expected_ops):
                assert got_op.operation_id == exp_op["operation_id"]
                assert got_op.campaign_id == campaign_id
                assert got_op.state == exp_op["state"]
                assert got_op.total_success == exp_op["total_success"]
                assert got_op.total_failure == exp_op["total_failure"]

                # Same per-Ability results, in the same order, each field intact.
                assert len(got_op.results) == len(exp_op["results"])
                for got_res, exp_res in zip(got_op.results, exp_op["results"]):
                    assert got_res.operation_id == exp_op["operation_id"]
                    assert got_res.ability_id == exp_res["ability_id"]
                    assert got_res.status == exp_res["status"]
                    assert got_res.command_output == exp_res["command_output"]

        # A case never given an Operation has no results (association is exact).
        for campaign_id in _CASE_IDS:
            if campaign_id not in expected_ops_by_case:
                assert service.get_operation_results(campaign_id) == []
    finally:
        session.close()
        engine.dispose()
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
