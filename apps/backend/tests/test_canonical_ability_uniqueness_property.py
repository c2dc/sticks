"""Property-based test for Property 11 of the stage6-execution-redesign design
(task 9.4) — canonical uniqueness of Abilities (no duplication).

# Feature: stage6-execution-redesign, Property 11: Unicidade canônica das Abilities (sem duplicação)

**Validates: Requirements 2.7**

The WS frame / snapshot presented to the UI must not duplicate an Ability's
target/state data across multiple blocks: *for any* set of per-Ability results,
the canonical presented projection contains each ``ability_id`` **at most once**
(the ``ability_id`` of the projection are distinct).

Where the dedup actually happens (the canonical point under test)
-----------------------------------------------------------------
The runner's :meth:`OperationRunner._reconcile` *upserts* ``AbilityResult`` rows
keyed by ``(operation_id, ability_id)``: a repeated poll (or a repeated link)
that carries an already-seen ``ability_id`` **updates the existing row in place**
rather than inserting a second one. The canonical projection the WS channel /
snapshot reads back is
:meth:`SessionStateService.get_operation_results` →
:attr:`OperationResultView.results`. So even when the same ``ability_id`` is
observed across many polls (the realistic case — Caldera's chain is re-polled on
an interval and the same links reappear every poll), the projected result set
carries each ``ability_id`` exactly once.

How this test drives the real path (ephemeral SQLite, no network / no Docker)
-----------------------------------------------------------------------------
Each example:

* builds a **fresh temp-file SQLite** database (so row counts are exact and no
  state leaks between Hypothesis examples), seeds a single ``Operation`` row;
* generates an arbitrary **sequence of polls**, each a list of
  :class:`OperationLink` whose ``ability_id`` are drawn from a **small shared
  pool** — so the same ``ability_id`` is reconciled repeatedly across polls
  (the duplication-prone scenario), interleaved with links that advance state;
* feeds every poll through the **real** :meth:`OperationRunner._reconcile` (the
  canonical dedup point), with a real :class:`AuditLogger` on the same session;
* reads back the **canonical projection**
  (:meth:`SessionStateService.get_operation_results`) and asserts the projected
  ``ability_id`` are **distinct** (each Ability appears at most once).

Independent oracle: the set of distinct ``ability_id`` ever observed across all
polls equals the set of ``ability_id`` in the projection — nothing is dropped
and nothing is duplicated.

_Requirements: 2.7_
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import CalderaClient, OperationLink
from app.services.operation.runner import OperationRunner
from app.services.session.session_state_service import SessionStateService

# ---------------------------------------------------------------------------
# Status pool: a link may be observed in any of the four derived states. A later
# poll of the same ability_id can advance it (pendente -> em_execucao ->
# sucesso/falha); the reconciler never regresses a terminal status.
# ---------------------------------------------------------------------------
_STATUSES: tuple[AbilityResultStatus, ...] = (
    AbilityResultStatus.PENDING,
    AbilityResultStatus.RUNNING,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
)

# A SMALL shared pool of ability ids so the same ability_id is reconciled many
# times across polls — the exact scenario that would duplicate rows if the
# upsert keyed anything other than (operation_id, ability_id). Includes ``None``
# so ability-less links (reconciled positionally) are exercised too.
_ABILITY_POOL: tuple[str | None, ...] = (
    "ab-alpha",
    "ab-beta",
    "ab-gamma",
    "ab-delta",
    None,
)


def _link_strategy() -> st.SearchStrategy[OperationLink]:
    """A single :class:`OperationLink` with an ability_id drawn from the pool."""
    return st.builds(
        lambda ability_id, status, idx, out: OperationLink(
            link_id=f"link-{ability_id}-{idx}",
            ability_id=ability_id,
            command=f"echo {ability_id}",
            paw="kali",
            status_code={
                AbilityResultStatus.PENDING: None,
                AbilityResultStatus.RUNNING: -3,
                AbilityResultStatus.SUCCESS: 0,
                AbilityResultStatus.FAILURE: 1,
            }[status],
            status=status,
            output=out,
        ),
        ability_id=st.sampled_from(_ABILITY_POOL),
        status=st.sampled_from(_STATUSES),
        idx=st.integers(min_value=0, max_value=3),
        out=st.one_of(st.none(), st.text(max_size=12)),
    )


def _poll_strategy() -> st.SearchStrategy[list[OperationLink]]:
    """A single poll: 0..5 links (some abilities may repeat within a poll)."""
    return st.lists(_link_strategy(), min_size=0, max_size=5)


def _polls_strategy() -> st.SearchStrategy[list[list[OperationLink]]]:
    """A sequence of 1..6 polls — the same ability_id recurs across polls."""
    return st.lists(_poll_strategy(), min_size=1, max_size=6)


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="stage6_canonical_unique_")
    db_path = Path(tmp_dir) / "runner.db"
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


def _seed_operation(session: Session, campaign_id: str) -> Operation:
    """Insert a single running Operation to own the reconciled AbilityResults."""
    op = Operation(
        campaign_id=campaign_id,
        caldera_operation_id="op-canonical",
        state=OperationState.RUNNING,
        total_success=0,
        total_failure=0,
    )
    session.add(op)
    session.commit()
    session.refresh(op)
    return op


# ---------------------------------------------------------------------------
# Property 11 — the canonical projection carries each ability_id at most once.
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(polls=_polls_strategy())
def test_property11_canonical_projection_abilities_are_unique(
    polls: list[list[OperationLink]],
) -> None:
    """Property 11: the canonical projection has DISTINCT ability_ids.

    # Feature: stage6-execution-redesign, Property 11: Unicidade canônica das Abilities (sem duplicação)
    **Validates: Requirements 2.7**

    For any sequence of per-Ability polls — repeatedly reconciling the same
    ``ability_id`` across polls — the canonical presented projection
    (``get_operation_results(...).results``) contains each non-null
    ``ability_id`` at most once, and the set of distinct projected ability_ids
    equals exactly the set of distinct ability_ids ever observed (nothing
    dropped, nothing duplicated).
    """
    campaign_id = "shadowray"
    session, db_path, engine = _build_ephemeral_session()
    caldera = CalderaClient()  # never contacted; _reconcile touches only the DB
    try:
        operation = _seed_operation(session, campaign_id)
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
        )

        # Drive the real canonical dedup point: reconcile every poll in order.
        observed_ability_ids: set[str] = set()
        for poll in polls:
            runner._reconcile(operation_id=operation.id, links=poll)
            observed_ability_ids.update(
                link.ability_id for link in poll if link.ability_id is not None
            )

        # Read back the CANONICAL projection the WS frame / snapshot presents.
        views = SessionStateService(session).get_operation_results(campaign_id)
        assert len(views) == 1, "exactly one Operation projected for the case"
        results = views[0].results

        projected_ids = [r.ability_id for r in results if r.ability_id is not None]

        # Core invariant (Req. 2.7): no ability_id appears more than once.
        assert len(projected_ids) == len(set(projected_ids)), (
            "canonical projection duplicated an ability_id: "
            f"{projected_ids}"
        )

        # Independent oracle: the distinct projected ability_ids are EXACTLY the
        # distinct ability_ids ever observed across every poll — nothing dropped,
        # nothing duplicated.
        assert set(projected_ids) == observed_ability_ids
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
