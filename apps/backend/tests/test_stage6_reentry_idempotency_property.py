"""Property-based test for Property 7 of the stage6-execution-redesign design
(task 7.2) — idempotency of re-entering Stage 6.

# Feature: stage6-execution-redesign, Property 7: Idempotência da reentrada no Estágio 6

**Validates: Requirements 1.5**

Context (the behaviour this guards)
-----------------------------------
Stage 6 (*Execução, medição e relatório*) starts the campaign execution
automatically on entry (Req. 1.1). But if an :class:`~app.models.domain.Operation`
is **already** persisted in a terminal state — ``FINISHED`` / ``ABORTED`` /
``INCOMPLETE_TIMEOUT`` — that run is finished, and re-opening Stage 6 must
**re-display** the persisted snapshot WITHOUT starting a new run (Req. 1.5,
idempotency). Firing the runner again would duplicate the Operation, re-dispatch
attack commands and corrupt the report.

The invariant, stated over ``ExecutionStageService.autostart``
--------------------------------------------------------------
*For any* campaign whose Stage 5 is concluded and that already has an Operation
persisted in a terminal state (``finalizada`` / ``abortada`` /
``incompleta_timeout``) with some ``AbilityResult`` rows, calling
``autostart(campaign_id, confirmed=...)``:

* re-displays the persisted snapshot (``reused=True``, ``executed=False``,
  ``blocked=False``), with ``output`` reflecting exactly the persisted Operation
  (its ``operation_id``, aggregate counts/state and per-Ability results in
  insertion order);
* does **not** create a new Operation row (the Operation count is unchanged);
* never fires any command — the injected mock ``OperationRunner.run`` is called
  **zero** times (its run-count stays at 0), independent of the ``confirmed``
  flag.

Everything runs against an **ephemeral per-example temp-file SQLite** built fresh
and torn down in a ``finally`` block, so no ``.db`` state leaks between examples.
No network, no real Caldera, no real runner — a mock runner that COUNTS ``run``
calls is injected via :class:`OperationRunnerProtocol`.

_Requirements: 1.5_
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, Case, Operation, StageRun
from app.models.enums import AbilityResultStatus, OperationState, StageState
from app.services.campaign_service import CaseData
from app.services.stage.execution_stage_service import (
    TERMINAL_OPERATION_STATES,
    ExecutionStageService,
)
from app.services.stage.stage_service import (
    STAGE_ENVIRONMENT,
    OperationRunResult,
)

# ---------------------------------------------------------------------------
# A mock OperationRunner that COUNTS run() calls (OperationRunnerProtocol).
# ---------------------------------------------------------------------------


@dataclass
class CountingRunner:
    """A mock implementing :class:`OperationRunnerProtocol` that counts runs.

    Property 7 forbids any run on the idempotent re-entry path, so the invariant
    is simply ``run_count == 0`` after ``autostart``. If the service ever
    (wrongly) delegated to the runner, this would surface it immediately. The
    returned :class:`OperationRunResult` is irrelevant for this property because
    it must never be produced.
    """

    run_count: int = 0
    calls: list[dict] = field(default_factory=list)

    def run(self, case: CaseData, *, confirmed: bool) -> OperationRunResult:
        self.run_count += 1
        self.calls.append({"case": case, "confirmed": confirmed})
        # Should never be reached on the idempotent path; return a benign value.
        return OperationRunResult(executed=False, message="unexpected run")


# ---------------------------------------------------------------------------
# Generators — terminal state, aggregate counts and per-Ability results.
# ---------------------------------------------------------------------------

# The three terminal states that make a Stage-6 re-entry idempotent (Req. 1.5).
_TERMINAL_STATES: tuple[OperationState, ...] = tuple(TERMINAL_OPERATION_STATES)

# The four per-Ability statuses a persisted result may carry (the snapshot is a
# pure read of whatever was persisted — it does not normalise).
_ALL_STATUSES: tuple[AbilityResultStatus, ...] = (
    AbilityResultStatus.PENDING,
    AbilityResultStatus.RUNNING,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
)


@dataclass(frozen=True)
class _ResultSpec:
    """One persisted per-Ability result of the terminal Operation."""

    ability_id: str
    status: AbilityResultStatus
    command_output: str | None


def _result_spec_strategy() -> st.SearchStrategy[_ResultSpec]:
    return st.builds(
        lambda uid, status, out: _ResultSpec(
            ability_id=f"ab-{uid.hex[:10]}",
            status=status,
            command_output=out,
        ),
        st.uuids(),
        st.sampled_from(_ALL_STATUSES),
        st.one_of(st.none(), st.text(max_size=12)),
    )


def _results_strategy() -> st.SearchStrategy[tuple[_ResultSpec, ...]]:
    """0..11 persisted results with DISTINCT ability ids.

    0 covers a terminal Operation with no collected results (still idempotent);
    11 is the ShadowRay chain length from the bug report, kept as the upper
    bound. Distinct ids keep the per-Ability snapshot unambiguous.
    """
    return st.lists(
        _result_spec_strategy(),
        min_size=0,
        max_size=11,
        unique_by=lambda spec: spec.ability_id,
    ).map(tuple)


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="stage6_reentry_idem_")
    db_path = Path(tmp_dir) / "reentry.db"
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


def _count(session: Session, model: type) -> int:
    """Count all rows of ``model`` in the (per-example) database."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# Property 7 — re-entry re-displays the snapshot without a new run.
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    terminal_state=st.sampled_from(_TERMINAL_STATES),
    results=_results_strategy(),
    total_success=st.integers(min_value=0, max_value=11),
    total_failure=st.integers(min_value=0, max_value=11),
    confirmed=st.booleans(),
    campaign_id=st.sampled_from(["shadowray", "apt41_dust", "c0010"]),
)
def test_property7_stage6_reentry_is_idempotent(
    terminal_state: OperationState,
    results: tuple[_ResultSpec, ...],
    total_success: int,
    total_failure: int,
    confirmed: bool,
    campaign_id: str,
) -> None:
    """Property 7: a persisted terminal Operation re-displays, never re-runs.

    # Feature: stage6-execution-redesign, Property 7: Idempotência da reentrada no Estágio 6
    **Validates: Requirements 1.5**

    Seed a concluded Stage 5 (so the prerequisite passes) and a terminal
    Operation (varying across FINISHED / ABORTED / INCOMPLETE_TIMEOUT) with some
    AbilityResults. Then ``autostart`` must: (1) report a reused snapshot
    (``reused=True``, no new run), (2) leave the Operation count unchanged — no
    new Operation created, (3) never call the injected runner (run-count stays
    0), and (4) return a snapshot reflecting exactly the persisted Operation.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        # -- Campaign (FK target for the stage run + operation). -------------
        session.add(Case(id=campaign_id, name=campaign_id.title()))
        session.flush()

        # -- A concluded Stage 5 so the prerequisite passes (Req. 1.4). ------
        session.add(
            StageRun(
                campaign_id=campaign_id,
                stage=STAGE_ENVIRONMENT,
                state=StageState.COMPLETED,
                progress=100,
            )
        )
        session.flush()

        # -- A persisted TERMINAL Operation + its per-Ability results. -------
        operation = Operation(
            campaign_id=campaign_id,
            caldera_operation_id="op-terminal",
            state=terminal_state,
            total_success=total_success,
            total_failure=total_failure,
        )
        session.add(operation)
        session.flush()

        for spec in results:
            session.add(
                AbilityResult(
                    operation_id=operation.id,
                    ability_id=spec.ability_id,
                    status=spec.status,
                    command_output=spec.command_output,
                )
            )
        session.commit()

        operation_id = operation.id
        operations_before = _count(session, Operation)
        results_before = _count(session, AbilityResult)
        assert operations_before == 1
        assert results_before == len(results)
        # Sanity: the seeded state really is terminal (idempotency key).
        assert terminal_state in TERMINAL_OPERATION_STATES

        # -- Act: re-enter Stage 6. ------------------------------------------
        runner = CountingRunner()
        service = ExecutionStageService(session, operation_runner=runner)
        start = service.autostart(campaign_id, confirmed=confirmed)

        # (1) Re-displayed, not re-run.
        assert start.reused is True
        assert start.executed is False
        assert start.blocked is False
        assert start.operation_id == operation_id

        # (3) The runner was NEVER called — no command fired, regardless of
        # the confirmed flag.
        assert runner.run_count == 0
        assert runner.calls == []

        # (2) No new Operation (nor AbilityResult) rows were created.
        assert _count(session, Operation) == operations_before
        assert _count(session, AbilityResult) == results_before

        # (4) The returned snapshot reflects exactly the persisted Operation.
        assert start.output is not None
        snapshot = start.output
        assert snapshot.operation_id == operation_id
        assert snapshot.aggregate.state is terminal_state
        assert snapshot.aggregate.total_success == total_success
        assert snapshot.aggregate.total_failure == total_failure
        # Per-Ability results mirror the persisted rows in insertion order.
        assert len(snapshot.results) == len(results)
        for got, spec in zip(snapshot.results, results):
            assert got.ability_id == spec.ability_id
            assert got.status is spec.status
            assert got.command_output == spec.command_output
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
