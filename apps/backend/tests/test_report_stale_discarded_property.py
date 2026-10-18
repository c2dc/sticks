"""Property-based test for Property 5 of the stage6-execution-redesign design
(task 6.3) — stale persisted state is discarded and recomputed.

# Feature: stage6-execution-redesign, Property 5: Estado obsoleto é descartado e recomputado (nunca mistura execuções)

**Validates: Requirements 3.5**

Context (the bug this guards against)
-------------------------------------
A report may already be persisted in :class:`~app.models.domain.ExecutionReport`
for a campaign, left over from a **previous** execution (a different
``Operation``) — or carrying a ``metrics`` payload written under a divergent /
unreadable schema. Req. 3.5 forbids ever presenting a report that *combines*
data from distinct executions: when the persisted state references an execution
whose ``operation_id`` (or schema) diverges from the current one,
:meth:`ReportBuilder.build_report` MUST **discard** that stale state and
recompute the report **exclusively** from the current ``Operation``'s results.

The invariant, stated over the builder's public ``build_report`` API
--------------------------------------------------------------------
*For any* pair (persisted prior-execution state, current execution) whose
``operation_id`` or ``metrics`` schema diverges, after ``build_report`` of the
**current** Operation:

* the returned view derives **exclusively** from the current Operation's
  per-Ability results — never a blend of the stale metrics and the current ones
  (``operation_id`` of the view is the current one; counts/failed-nodes match the
  current results, independent of the stale payload);
* the stale row is **discarded**: the campaign ends with exactly **one**
  ``ExecutionReport`` row, and it references the current ``operation_id`` and
  carries the freshly recomputed ``metrics`` / ``success_verdict``;
* the Property 4 invariants still hold on the recomputed report —
  (b) ``total_success + total_failure == total_techniques``,
  (c) ``len(failed_nodes) == total_failure`` and
  (d) no technique remains in a non-terminal state.

Everything is exercised against an **ephemeral per-example temp-file SQLite**
built fresh and torn down in a ``finally`` block, so no ``.db`` state leaks
between examples. No network, no real Caldera — ``build_report`` is pure DB.

_Requirements: 3.5_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, Case, ExecutionReport, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.operation.report import ReportBuilder

# ---------------------------------------------------------------------------
# Generators — the current execution's per-Ability results + a stale payload.
# ---------------------------------------------------------------------------

# The four per-Ability statuses the current execution may persist. The builder
# normalizes anything that is not ``sucesso`` to ``falha`` for the final report
# (Req. 3.4d), so the recomputed counts must track that normalization regardless
# of what the stale state said.
_ALL_STATUSES: tuple[AbilityResultStatus, ...] = (
    AbilityResultStatus.PENDING,
    AbilityResultStatus.RUNNING,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
)


@dataclass(frozen=True)
class _ResultSpec:
    """One per-Ability result of the CURRENT execution."""

    ability_id: str
    status: AbilityResultStatus


def _result_spec_strategy() -> st.SearchStrategy[_ResultSpec]:
    return st.builds(
        lambda uid, status: _ResultSpec(
            ability_id=f"ab-{uid.hex[:10]}", status=status
        ),
        st.uuids(),
        st.sampled_from(_ALL_STATUSES),
    )


def _current_results_strategy() -> st.SearchStrategy[tuple[_ResultSpec, ...]]:
    """1..11 current-execution results with DISTINCT ability ids.

    Distinct ids keep the recomputed counts exact and unambiguous. 11 is the
    ShadowRay chain length from the bug report, kept as the upper bound.
    """
    return st.lists(
        _result_spec_strategy(),
        min_size=1,
        max_size=11,
        unique_by=lambda spec: spec.ability_id,
    ).map(tuple)


def _expected_terminal(spec: _ResultSpec) -> AbilityResultStatus:
    """How the builder normalizes a current result for the final report.

    ``sucesso`` stays ``sucesso``; everything else (including the non-terminal
    ``pendente`` / ``em_execucao``) becomes ``falha`` (Req. 3.4d).
    """
    return (
        AbilityResultStatus.SUCCESS
        if spec.status is AbilityResultStatus.SUCCESS
        else AbilityResultStatus.FAILURE
    )


def _stale_metrics_strategy() -> st.SearchStrategy[object]:
    """A stale ``metrics`` payload from a PRIOR execution.

    Deliberately divergent from the current execution: either a plausible-looking
    (but belonging-to-another-run) metrics dict with inflated counts and
    non-terminal leftovers, OR a structurally divergent/unreadable schema. Either
    way the builder must ignore it entirely and recompute from the current rows.
    """
    plausible = st.fixed_dictionaries(
        {
            "operation_id": st.integers(min_value=10_000, max_value=99_999),
            "total_techniques": st.integers(min_value=1, max_value=999),
            "total_success": st.integers(min_value=0, max_value=999),
            "total_failure": st.integers(min_value=0, max_value=999),
            "success_rate": st.floats(min_value=0.0, max_value=1.0),
            "success_verdict": st.booleans(),
            "failed_nodes": st.lists(st.text(max_size=8), max_size=5),
            "per_ability": st.lists(
                st.fixed_dictionaries(
                    {
                        "ability_id": st.text(max_size=8),
                        # non-terminal leftovers that must NOT survive recompute
                        "status": st.sampled_from(["pendente", "em_execucao"]),
                    }
                ),
                max_size=5,
            ),
        }
    )
    divergent_schema = st.one_of(
        st.dictionaries(st.text(min_size=1, max_size=6), st.integers(), max_size=4),
        st.just({"legacy": True, "shape": "unknown"}),
        st.just([1, 2, 3]),
        st.just({}),
    )
    return st.one_of(plausible, divergent_schema)


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="stage6_report_stale_")
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


def _count(session: Session, model: type) -> int:
    """Count all rows of ``model`` in the (per-example) database."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Property 5 — stale state discarded; report derives exclusively from current.
# ---------------------------------------------------------------------------


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    current_results=_current_results_strategy(),
    stale_metrics=_stale_metrics_strategy(),
    campaign_id=st.sampled_from(["shadowray", "apt41_dust", "c0010"]),
)
def test_property5_stale_state_discarded_and_recomputed(
    current_results: tuple[_ResultSpec, ...],
    stale_metrics: object,
    campaign_id: str,
) -> None:
    """Property 5: divergent persisted state is discarded & recomputed.

    # Feature: stage6-execution-redesign, Property 5: Estado obsoleto é descartado e recomputado (nunca mistura execuções)
    **Validates: Requirements 3.5**

    Pre-seed an ``ExecutionReport`` for the campaign referencing a DIFFERENT
    (prior) ``operation_id`` and carrying a divergent ``metrics`` schema. Create
    the CURRENT ``Operation`` with its own ``AbilityResult`` rows. Then
    ``build_report`` of the current Operation must: (1) return a view derived
    exclusively from the current results (never a blend of the stale payload),
    (2) discard the stale row — exactly one ``ExecutionReport`` remains for the
    campaign, referencing the current ``operation_id`` with freshly recomputed
    ``metrics`` — and (3) keep the Property 4 invariants (b/c/d).
    """
    session, db_path, engine = _build_ephemeral_session()
    try:
        # -- Campaign (FK target for the report + operation). ----------------
        session.add(Case(id=campaign_id, name=campaign_id.title()))
        session.flush()

        # -- A PRIOR Operation (the stale report points at it). --------------
        prior_operation = Operation(
            campaign_id=campaign_id,
            caldera_operation_id="op-prior",
            state=OperationState.FINISHED,
            total_success=7,
            total_failure=4,
        )
        session.add(prior_operation)
        session.flush()

        # -- The CURRENT Operation + its own per-Ability results. ------------
        current_operation = Operation(
            campaign_id=campaign_id,
            caldera_operation_id="op-current",
            state=OperationState.FINISHED,
        )
        session.add(current_operation)
        session.flush()

        for spec in current_results:
            session.add(
                AbilityResult(
                    operation_id=current_operation.id,
                    ability_id=spec.ability_id,
                    status=spec.status,
                )
            )
        session.flush()

        # The two operations are genuinely distinct executions.
        assert prior_operation.id != current_operation.id

        # -- Pre-seed the STALE ExecutionReport (prior op + divergent schema).
        stale_row = ExecutionReport(
            campaign_id=campaign_id,
            operation_id=prior_operation.id,
            metrics=stale_metrics,
            success_verdict=True,  # a divergent verdict from the prior run
            environment_deprovisioned=False,
            generated_at=_utcnow(),
        )
        session.add(stale_row)
        session.commit()
        stale_report_id = stale_row.id

        # -- Expected recompute, derived ONLY from the current results. ------
        expected_terminals = [_expected_terminal(spec) for spec in current_results]
        expected_total = len(current_results)
        expected_success = sum(
            1 for s in expected_terminals if s is AbilityResultStatus.SUCCESS
        )
        expected_failure = sum(
            1 for s in expected_terminals if s is AbilityResultStatus.FAILURE
        )
        expected_failed_nodes = [
            spec.ability_id
            for spec in current_results
            if _expected_terminal(spec) is AbilityResultStatus.FAILURE
        ]

        # -- Act: build the report for the CURRENT execution. ----------------
        builder = ReportBuilder(session=session)
        view = builder.build_report(current_operation)

        # (1) The view derives EXCLUSIVELY from the current execution — not a
        # blend with the stale metrics.
        assert view.operation_id == current_operation.id
        assert view.total_techniques == expected_total
        assert view.total_success == expected_success
        assert view.total_failure == expected_failure
        assert sorted(view.failed_nodes) == sorted(expected_failed_nodes)
        expected_rate = (
            (expected_success / expected_total) if expected_total else 0.0
        )
        assert view.success_rate == expected_rate
        assert view.success_verdict == (
            expected_total > 0 and expected_rate >= 1.0
        )

        # Property 4 invariants still hold on the recomputed report.
        # (b) sum of terminal buckets == total techniques
        assert view.total_success + view.total_failure == view.total_techniques
        # (c) failed-nodes count == #failures
        assert len(view.failed_nodes) == view.total_failure
        # (d) no technique remains non-terminal in the final report
        assert all(
            entry.status in (AbilityResultStatus.SUCCESS, AbilityResultStatus.FAILURE)
            for entry in view.per_ability
        )
        assert len(view.per_ability) == expected_total

        # (2) The stale row is DISCARDED: exactly one ExecutionReport remains for
        # the campaign, and it references the CURRENT operation_id with the fresh
        # metrics — never the prior execution.
        assert _count(session, ExecutionReport) == 1
        remaining = session.execute(
            select(ExecutionReport).where(
                ExecutionReport.campaign_id == campaign_id
            )
        ).scalar_one()
        assert remaining.operation_id == current_operation.id
        # The stale row (prior operation) no longer exists — it was deleted, not
        # merged (a brand-new row may have been created, so the id may differ).
        assert remaining.operation_id != prior_operation.id
        if remaining.id == stale_report_id:
            # Reused the row only after repointing it at the current execution.
            assert remaining.operation_id == current_operation.id

        # The persisted metrics are the freshly recomputed ones (not the stale
        # payload): they round-trip the view exactly.
        assert remaining.metrics == view.model_dump(mode="json")
        assert remaining.success_verdict == view.success_verdict
        # No non-terminal status leaked into the persisted per-Ability metrics.
        for entry in remaining.metrics["per_ability"]:
            assert entry["status"] in (
                AbilityResultStatus.SUCCESS.value,
                AbilityResultStatus.FAILURE.value,
            )
    finally:
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
