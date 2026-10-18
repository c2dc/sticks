"""Property-based test for Property 1 of the stage6-execution-redesign design
(task 3.6) — the core of the collection bug fix.

# Feature: stage6-execution-redesign, Property 1: Contagem persistida iguala links terminais (correção do bug)

**Validates: Requirements 3.3**

The bug this fixes (observed on the real ShadowRay run): the Operation reached
``state=finished`` with ``chain=11`` in Caldera, but the backend persisted the
Operation as ``FINISHED`` early with only 6 ``AbilityResult`` rows collected —
the old ``poll_until_complete`` + single-iteration loop didn't track the chain
until the real terminal state. The redesign replaces that with
:meth:`OperationRunner._collect_until_terminal`, which polls
``CalderaClient.get_operation`` on an interval, reconciles (upserts) the
``AbilityResult`` rows on every poll, and performs a final reconcile on the
first terminal state.

The invariant, stated operationally over the runner's public ``run`` API
--------------------------------------------------------------------------
*For any* chain of links that reaches a terminal Caldera state
(``finished`` / ``out_of_time`` / ``cleanup``), after the collection loop the
number of persisted ``AbilityResult`` rows for the Operation is **equal** to the
number of terminal links observed in Caldera for that Operation — and **never**
remains below the observed count (the exact failure mode of the bug).

Everything external is mocked (no real Caldera, no real Docker daemon, no real
clock):

* Caldera is a :class:`httpx.MockTransport`-backed **real** :class:`CalderaClient`.
  A stateful recorder serves a healthy ``POST /operations`` and then a sequence
  of ``GET /operations/{id}`` snapshots — zero or more non-terminal
  (``state=running``) snapshots carrying a partial/growing chain, followed by a
  **terminal** snapshot carrying the full chain of ``N`` links. This reproduces
  the real timing: Caldera is still advancing the chain across polls, and the
  loop must keep collecting until the terminal snapshot.
* Isolation is a fake verifier that reports every target container isolated, so
  the pre-flight gates never interfere with the collection invariant.
* The clock/sleeper are injected and deterministic (no real ``time``); the
  collection timeout is set generously so the terminal state is always observed
  before the budget elapses — isolating the invariant under test (terminal
  reached ⇒ persisted count == terminal links).
* The database is an **ephemeral temp-file SQLite** built fresh *per example* and
  torn down in a ``finally`` block, so the ``AbilityResult`` row count is exact
  and no ``.db`` state leaks between examples.

_Requirements: 3.3_
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
from app.models.enums import OperationState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.caldera.client import TERMINAL_OPERATION_STATES
from app.services.containment.isolation import ContainerIsolationVerdict, IsolationResult
from app.services.operation.runner import OperationRunner, RunOutcome

# ---------------------------------------------------------------------------
# Duck-typed Ability the runner / containment / preview all accept.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeAbility:
    """Minimal Ability exposing the attributes the runner consumes.

    A single internal-only executor command keeps the containment gate open so
    the run always reaches the collection loop (the subject of this property).
    """

    ability_id: str
    name: str
    executors: tuple[dict[str, str], ...]


# A single internal-only command so the destination-containment gate always
# passes — the collection invariant must not be masked by a pre-flight refusal.
_INTERNAL_COMMAND = "curl http://192.168.20.30/payload.sh"


def _abilities_strategy() -> st.SearchStrategy[list[FakeAbility]]:
    """Generate 1..6 Abilities with internal-only commands (ids need not match
    the generated chain — the runner loads abilities but the terminal count is
    driven by the Caldera chain, not the loaded abilities)."""
    return st.lists(
        st.builds(
            lambda uid: FakeAbility(
                ability_id=f"loaded-{uid.hex[:8]}",
                name=f"T{1000 + (uid.int % 600)} - generated",
                executors=(
                    {"name": "sh", "platform": "linux", "command": _INTERNAL_COMMAND},
                ),
            ),
            st.uuids(),
        ),
        min_size=1,
        max_size=6,
    )


# Terminal Caldera states that end the collection loop (Req. 3.3). Any one of
# these in the final snapshot must freeze the persisted count at the terminal
# link count.
_TERMINAL_STATES: tuple[str, ...] = tuple(sorted(TERMINAL_OPERATION_STATES))

# Link status codes Caldera uses. In the TERMINAL snapshot every link is
# *collected* (never ``None``/``-3``), so each terminal link maps to a terminal
# per-Ability status (``sucesso``/``falha``) — a faithful terminal chain.
_TERMINAL_STATUS_CODES: tuple[int, ...] = (0, 1, -1, 2, 42, 255)


@dataclass(frozen=True)
class _LinkSpec:
    """A generated terminal link: its ability id and collected status code."""

    ability_id: str
    status_code: int


def _link_spec_strategy() -> st.SearchStrategy[_LinkSpec]:
    return st.builds(
        lambda uid, code: _LinkSpec(ability_id=f"ab-{uid.hex[:10]}", status_code=code),
        st.uuids(),
        st.sampled_from(_TERMINAL_STATUS_CODES),
    )


def _terminal_chain_strategy() -> st.SearchStrategy[tuple[_LinkSpec, ...]]:
    """Generate a terminal chain of 1..11 links with DISTINCT ability ids.

    Distinct ability ids make the persisted-row count exact and unambiguous:
    the reconciler keys rows by ``(operation_id, ability_id)``, so N distinct
    terminal links ⇒ exactly N ``AbilityResult`` rows. 11 is the ShadowRay chain
    length from the bug report, kept as the upper bound to cover that case.
    """
    return st.lists(
        _link_spec_strategy(),
        min_size=1,
        max_size=11,
        unique_by=lambda spec: spec.ability_id,
    ).map(tuple)


# ---------------------------------------------------------------------------
# Stateful MockTransport recorder: non-terminal snapshots then a terminal one.
# ---------------------------------------------------------------------------


@dataclass
class SequencedCaldera:
    """A MockTransport handler driving a non-terminal → terminal poll sequence.

    ``POST /operations`` creates ``op-test``. Each ``GET /operations/op-test``
    returns the next snapshot in the configured sequence: ``num_non_terminal``
    non-terminal (``state=running``) snapshots carrying a *partial* chain,
    followed by the terminal snapshot carrying the FULL chain of ``N`` links.
    Once the terminal snapshot is reached it is served for any further polls (the
    loop stops at the first terminal, so in practice it is served once).
    """

    terminal_state: str
    terminal_chain: tuple[_LinkSpec, ...]
    num_non_terminal: int
    _polls: int = 0
    requests: list[httpx.Request] = field(default_factory=list)

    def _snapshot(self, state: str, chain: tuple[_LinkSpec, ...]) -> dict:
        return {
            "id": "op-test",
            "state": state,
            "chain": [
                {
                    "id": f"link-{i}",
                    "ability": {"ability_id": spec.ability_id},
                    "command": "",
                    "status": spec.status_code,
                    "paw": "kali",
                }
                for i, spec in enumerate(chain)
            ],
        }

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
                200, json={"id": "op-test", "name": "run", "state": "running"}
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
            index = self._polls
            self._polls += 1
            if index < self.num_non_terminal:
                # Non-terminal snapshot with a partial chain (a prefix of the
                # full terminal chain, with the still-running tail marked -3).
                partial_len = (index * len(self.terminal_chain)) // max(
                    1, self.num_non_terminal
                )
                partial = tuple(
                    _LinkSpec(ability_id=spec.ability_id, status_code=-3)
                    for spec in self.terminal_chain[:partial_len]
                )
                return httpx.Response(
                    200, json=self._snapshot("running", partial)
                )
            # Terminal snapshot — the full chain of N collected links.
            return httpx.Response(
                200, json=self._snapshot(self.terminal_state, self.terminal_chain)
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """Fake isolation verifier: every requested container is isolated."""
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Deterministic injected clock / sleeper (no real time).
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A monotonic clock that advances 1s on every ``sleep`` and never on read.

    Reads return the accumulated time; ``sleep(n)`` advances it by ``n``. Started
    at 0, so with a generous collection timeout the loop always observes the
    terminal snapshot before the budget elapses — isolating the terminal-count
    invariant under test from the timeout path (covered by Property 3).
    """

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session."""
    tmp_dir = tempfile.mkdtemp(prefix="stage6_collect_count_")
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
# Property 1 — terminal reached ⇒ persisted count == terminal links.
# ---------------------------------------------------------------------------


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    abilities=_abilities_strategy(),
    terminal_chain=_terminal_chain_strategy(),
    terminal_state=st.sampled_from(_TERMINAL_STATES),
    num_non_terminal=st.integers(min_value=0, max_value=5),
    adversary_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
    campaign_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property1_persisted_count_equals_terminal_links(
    abilities: list[FakeAbility],
    terminal_chain: tuple[_LinkSpec, ...],
    terminal_state: str,
    num_non_terminal: int,
    adversary_id: str,
    campaign_id: str | None,
) -> None:
    """Property 1: terminal reached ⇒ persisted count == terminal links.

    # Feature: stage6-execution-redesign, Property 1: Contagem persistida iguala links terminais (correção do bug)
    **Validates: Requirements 3.3**

    For any chain of ``N`` distinct terminal links reached after zero or more
    non-terminal polls, the collection loop persists **exactly** ``N``
    ``AbilityResult`` rows for the Operation (never fewer — the bug), the
    Operation ends ``FINISHED`` / ``COMPLETED``, and the runner's aggregate
    ``total_success + total_failure`` equals ``N`` (every terminal link is a
    terminal per-Ability status).
    """
    session, db_path, engine = _build_ephemeral_session()
    recorder = SequencedCaldera(
        terminal_state=terminal_state,
        terminal_chain=terminal_chain,
        num_non_terminal=num_non_terminal,
    )
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    clock = FakeClock()
    try:
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=_isolated_verifier,
            # Interval 2s between polls; a generous budget so the terminal
            # snapshot is always reached before the budget elapses (the timeout
            # path is Property 3, not this property). Re-check disabled (0 would
            # fire a re-check every poll — harmless since isolation always
            # passes — but a large value keeps the loop focused on collection).
            collection_interval=2.0,
            collection_timeout=10_000.0,
            isolation_recheck_interval=30.0,
            clock=clock,
            sleeper=clock.sleep,
        )

        result = runner.run(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id=adversary_id,
            confirmed=True,
            target_containers=["nginx", "db"],
        )

        expected = len(terminal_chain)

        # The loop stopped at the terminal state and finalized the Operation.
        assert result.outcome is RunOutcome.COMPLETED
        assert result.state is OperationState.FINISHED

        # The invariant (Req. 3.3): the number of persisted AbilityResult rows
        # EQUALS the number of terminal links observed in Caldera — never fewer.
        persisted = _count(session, AbilityResult)
        assert persisted == expected, (
            f"persisted {persisted} AbilityResult rows but Caldera reported "
            f"{expected} terminal links"
        )
        assert persisted >= expected  # never remains below the observed count

        # Exactly one Operation row, finalized.
        assert _count(session, Operation) == 1
        operation = session.execute(select(Operation)).scalar_one()
        assert operation.state is OperationState.FINISHED

        # Every terminal link maps to a terminal per-Ability status, so the
        # aggregate accounts for all N links (none left pending/running).
        assert result.total_success + result.total_failure == expected
        assert operation.total_success + operation.total_failure == expected
        assert len(result.ability_results) == expected
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
