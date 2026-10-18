"""Example (non-property) test for the Stage 6 ReportBuilder (task 6.4).

Reproduces the real **ShadowRay** bug scenario described in
``requirements.md`` and in ``app.services.operation.report``: the Operation
reached ``state=finished`` with ``chain=11`` in Caldera (all 11 Abilities
executed), but the backend had persisted it ``FINISHED`` early with only a
partial slice of results — producing an **inconsistent** report (verdict
"Falha", "0 nós falharam", yet techniques stuck "em execução"/"pendente").

With the collection fix (task 3.5), a terminal Operation has **all 11 links
collected**; this test verifies the *report* side: given an Operation with 11
``AbilityResult`` rows in terminal states (a mix of ``sucesso`` / ``falha``),
:meth:`ReportBuilder.build_report` yields a **consistent** final report
satisfying the four invariants of Req. 3.4:

* (a) the verdict is a deterministic function of the computed success rate;
* (b) ``total_success + total_failure == 11`` (the campaign's total techniques);
* (c) the failed-nodes count equals the number of ``falha`` techniques;
* (d) no technique remains in a non-terminal state.

The test uses an **ephemeral temp-file SQLite** built fresh and torn down in a
``finally`` block, following the convention of the other Stage 6 tests (e.g.
``test_collection_terminal_count_property.py``).

_Requirements: 3.3, 3.4_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, ExecutionReport, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.operation.report import ReportBuilder

# The real ShadowRay chain length from the bug report: 11 Abilities executed.
_SHADOWRAY_CHAIN = 11

# A representative terminal mix: 11 techniques, some sucesso and some falha.
# (Reproduces the "all links collected" terminal state the fix guarantees.)
_SHADOWRAY_STATUSES: tuple[AbilityResultStatus, ...] = (
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
)


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="stage6_report_shadowray_")
    db_path = Path(tmp_dir) / "report.db"
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


def _seed_terminal_operation(
    session: Session,
    statuses: tuple[AbilityResultStatus, ...],
) -> Operation:
    """Persist a terminal ShadowRay Operation with all its links collected.

    Mirrors the post-fix state: the Operation is ``FINISHED`` and carries one
    ``AbilityResult`` per link, each already in a terminal status.
    """
    campaign = _ensure_campaign(session, "shadowray")
    operation = Operation(
        campaign_id=campaign,
        caldera_operation_id="op-shadowray",
        state=OperationState.FINISHED,
        total_success=sum(
            1 for s in statuses if s is AbilityResultStatus.SUCCESS
        ),
        total_failure=sum(
            1 for s in statuses if s is AbilityResultStatus.FAILURE
        ),
        started_at=dt.datetime(2024, 1, 2, 3, 4, 5),
        finished_at=dt.datetime(2024, 1, 2, 3, 14, 5),
    )
    session.add(operation)
    session.flush()  # assign operation.id

    for index, status in enumerate(statuses):
        session.add(
            AbilityResult(
                operation_id=operation.id,
                ability_id=f"shadowray-ability-{index:02d}",
                status=status,
                command=f"curl http://192.168.20.30/step-{index:02d}.sh",
                target_container="nginx (192.168.20.30)",
                attacker_host="kali",
            )
        )
    session.commit()
    return operation


def _ensure_campaign(session: Session, campaign_id: str) -> str:
    """Insert the ShadowRay campaign row (FK target of Operation/report)."""
    from app.models.domain import Case

    existing = session.get(Case, campaign_id)
    if existing is None:
        session.add(Case(id=campaign_id, name="ShadowRay"))
        session.commit()
    return campaign_id


def test_shadowray_terminal_report_is_consistent() -> None:
    """A terminal ShadowRay Operation (11 collected links) yields a consistent report.

    _Requirements: 3.3, 3.4_

    Reproduces the bug scenario with the fix in place: 11 ``AbilityResult`` rows
    in terminal states (mix of sucesso/falha). ``build_report`` must produce a
    report where every Req. 3.4 invariant holds and the verdict is deterministic.
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        operation = _seed_terminal_operation(session, _SHADOWRAY_STATUSES)
        expected_failures = sum(
            1 for s in _SHADOWRAY_STATUSES if s is AbilityResultStatus.FAILURE
        )
        expected_successes = _SHADOWRAY_CHAIN - expected_failures

        builder = ReportBuilder(session=session)
        view = builder.build_report(operation)

        # Reproduces the bug fix: all 11 links are accounted for in the report.
        assert view.total_techniques == _SHADOWRAY_CHAIN
        assert len(view.per_ability) == _SHADOWRAY_CHAIN

        # Invariant (b): the per-state sums equal the total techniques.
        assert view.total_success + view.total_failure == _SHADOWRAY_CHAIN
        assert view.total_success == expected_successes
        assert view.total_failure == expected_failures

        # Invariant (c): failed-nodes count equals the number of falha techniques.
        assert len(view.failed_nodes) == expected_failures
        assert len(view.failed_nodes) == view.total_failure

        # Invariant (d): no technique remains in a non-terminal state.
        terminal = {AbilityResultStatus.SUCCESS, AbilityResultStatus.FAILURE}
        assert all(entry.status in terminal for entry in view.per_ability)

        # Invariant (a): the verdict is a deterministic function of success rate.
        assert view.success_rate == expected_successes / _SHADOWRAY_CHAIN
        # ShadowRay had failures, so the rate is < 1.0 ⇒ verdict is False.
        assert view.success_verdict is False
        assert view.success_verdict == (
            view.total_techniques > 0 and view.success_rate >= 1.0
        )

        # Determinism: building the report again yields the same consistent view.
        again = builder.build_report(operation)
        assert again.model_dump(mode="json") == view.model_dump(mode="json")

        # The report was persisted for the campaign (single row, this execution).
        assert (
            session.execute(select(func.count()).select_from(ExecutionReport))
            .scalar_one()
            == 1
        )
        persisted = session.execute(select(ExecutionReport)).scalar_one()
        assert persisted.operation_id == operation.id
        assert persisted.success_verdict is False
        assert persisted.metrics["total_techniques"] == _SHADOWRAY_CHAIN
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def test_shadowray_all_success_terminal_report_verdict_true() -> None:
    """An all-success terminal ShadowRay run yields verdict True (determinism).

    _Requirements: 3.4_

    Complements the mixed case: with all 11 techniques in ``sucesso`` the
    success rate is exactly 1.0, so the deterministic verdict is ``True`` and the
    invariants still hold (0 failures, 0 failed nodes, all terminal).
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        all_success = tuple(
            AbilityResultStatus.SUCCESS for _ in range(_SHADOWRAY_CHAIN)
        )
        operation = _seed_terminal_operation(session, all_success)

        builder = ReportBuilder(session=session)
        view = builder.build_report(operation)

        assert view.total_techniques == _SHADOWRAY_CHAIN
        assert view.total_success == _SHADOWRAY_CHAIN
        assert view.total_failure == 0
        assert view.failed_nodes == []
        assert view.success_rate == 1.0
        assert view.success_verdict is True
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
