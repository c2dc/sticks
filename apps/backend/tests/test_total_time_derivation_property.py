"""Property-based test for Property 1 of the stage6-screen-redesign design
(task 2.4) — the total execution time derives from the Operation instants.

# Feature: stage6-screen-redesign, Property 1: Tempo total deriva dos instantes da Operação

**Validates: Requirements 4.1, 4.3**

The Stage-6 report presents a **real** total execution time derived from the
Operation lifecycle instants: for a **finished** Operation that carries both
``started_at`` and ``finished_at``, the duration is ``finished_at -
started_at`` in seconds (Req. 4.1). When either instant is absent, the value is
**undefined** (``None``) so the screen shows an absence indicator instead of a
bogus ``0`` (Req. 4.3).

:func:`app.api.campaigns._derive_total_time_s` is the pure, DB-free derivation
``get_report`` applies to the persisted :class:`OperationResultView`s. This
property exercises it directly — building ``OperationResultView`` instances with
generated ``(started_at, finished_at)`` pairs (``finished_at >= started_at``)
plus the absence cases — with **no** database, Caldera or clock involved.

_Requirements: 4.1, 4.3_
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from app.api.campaigns import _derive_total_time_s
from app.models.enums import OperationState
from app.services.session.session_state_service import OperationResultView


# A bounded datetime window keeps the generated instants realistic while still
# spanning a wide range; naive datetimes mirror what the Operation persists.
_START_DATETIMES = st.datetimes(
    min_value=dt.datetime(2000, 1, 1),
    max_value=dt.datetime(2100, 1, 1),
)

# Non-negative durations so the generated pair always satisfies
# ``finished_at >= started_at`` (the real lifecycle invariant). Bounded to a
# century's worth of seconds to avoid overflowing the datetime window.
_DURATIONS = st.timedeltas(
    min_value=dt.timedelta(0),
    max_value=dt.timedelta(days=365 * 100),
)


def _operation(
    *,
    operation_id: int,
    state: OperationState,
    started_at: dt.datetime | None,
    finished_at: dt.datetime | None,
) -> OperationResultView:
    """An ``OperationResultView`` carrying just the fields the derivation reads."""
    return OperationResultView(
        operation_id=operation_id,
        state=state,
        started_at=started_at,
        finished_at=finished_at,
    )


@settings(max_examples=200, deadline=None)
@given(started_at=_START_DATETIMES, duration=_DURATIONS)
def test_property1_finished_with_both_instants_yields_second_difference(
    started_at: dt.datetime,
    duration: dt.timedelta,
) -> None:
    """A finished Operation with both instants -> total = (finished - started) s.

    # Feature: stage6-screen-redesign, Property 1: Tempo total deriva dos instantes da Operação
    **Validates: Requirements 4.1**

    For any ``(started_at, finished_at)`` with ``finished_at >= started_at``, a
    single finished Operation yields ``total_time_s`` equal to the difference in
    seconds — never ``None`` (a measurable duration, including the exact-zero
    boundary when the instants coincide).
    """
    finished_at = started_at + duration
    assert finished_at >= started_at  # the generated pair respects the invariant

    operations = [
        _operation(
            operation_id=1,
            state=OperationState.FINISHED,
            started_at=started_at,
            finished_at=finished_at,
        )
    ]

    total = _derive_total_time_s(operations)
    assert total == duration.total_seconds()
    assert total is not None  # a finished Operation with both instants is measurable


@settings(max_examples=200, deadline=None)
@given(
    started_at=st.one_of(st.none(), _START_DATETIMES),
    duration=_DURATIONS,
    drop_started=st.booleans(),
    drop_finished=st.booleans(),
)
def test_property1_absent_instant_yields_none(
    started_at: dt.datetime | None,
    duration: dt.timedelta,
    drop_started: bool,
    drop_finished: bool,
) -> None:
    """A finished Operation missing an instant -> total is ``None`` (Req. 4.3).

    # Feature: stage6-screen-redesign, Property 1: Tempo total deriva dos instantes da Operação
    **Validates: Requirements 4.3**

    When ``started_at`` or ``finished_at`` is absent on the only finished
    Operation, the derivation leaves the total **undefined** (``None``) rather
    than a bogus ``0.0`` — the screen shows an absence indicator.
    """
    # Build a pair that respects ``finished_at >= started_at`` when both exist,
    # then deliberately drop at least one instant so the absence case holds.
    effective_start = started_at
    effective_finish = (started_at + duration) if started_at is not None else None
    if not drop_started and not drop_finished:
        # Force at least one absence so this example exercises Req. 4.3.
        drop_finished = True
    if drop_started:
        effective_start = None
    if drop_finished:
        effective_finish = None

    operations = [
        _operation(
            operation_id=1,
            state=OperationState.FINISHED,
            started_at=effective_start,
            finished_at=effective_finish,
        )
    ]

    assert _derive_total_time_s(operations) is None


@settings(max_examples=200, deadline=None)
@given(
    started_at=_START_DATETIMES,
    duration=_DURATIONS,
    state=st.sampled_from(
        [
            OperationState.NOT_STARTED,
            OperationState.RUNNING,
            OperationState.ABORTED,
            OperationState.INCOMPLETE_TIMEOUT,
        ]
    ),
)
def test_property1_non_finished_operation_does_not_contribute(
    started_at: dt.datetime,
    duration: dt.timedelta,
    state: OperationState,
) -> None:
    """A non-finished Operation never contributes to the total (Req. 4.1).

    # Feature: stage6-screen-redesign, Property 1: Tempo total deriva dos instantes da Operação
    **Validates: Requirements 4.1**

    Even with both instants present, an Operation not in the ``FINISHED`` state
    is not a completed execution, so it contributes nothing — the total stays
    ``None`` when it is the only Operation.
    """
    operations = [
        _operation(
            operation_id=1,
            state=state,
            started_at=started_at,
            finished_at=started_at + duration,
        )
    ]

    assert _derive_total_time_s(operations) is None
