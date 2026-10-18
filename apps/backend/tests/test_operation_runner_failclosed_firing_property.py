"""Property-based test for Property 6 of the stage6-execution-redesign design
(task 4.2) — the non-negotiable fail-closed command-firing gate.

# Feature: stage6-execution-redesign, Property 6: Fail-closed de disparo de
# comando (gate inegociável). Para qualquer combinação de confirmação explícita
# e veredito de isolamento, se algum comando de ataque é disparado, então
# necessariamente a confirmação é exatamente True E o veredito de isolamento é
# isolado (passed). Equivalentemente (contrapositiva): se a confirmação não é
# True OU o isolamento não é isolado/não pôde ser verificado, então nenhum
# comando é disparado e o fluxo é interrompido antes de qualquer execução.

Validates: Requirements 1.2, 1.6, 6.2, 6.5

Why a *new* test (vs. the pipeline-ui Property 6 test)
------------------------------------------------------
``tests/test_operation_runner_property.py`` already covers the pipeline-ui
spec's Property 6, which isolates a *single* variable — the explicit
confirmation flag — holding every other gate open. THIS spec's Property 6 is a
strictly stronger, two-variable biconditional over the pair
``(confirmed, isolation verdict)``: the design's gate is "inegociável", so the
automatic start (Req. 1.1) and the inline convenience confirmation (Req. 1.6)
MUST NOT bypass or weaken the isolation gate, and no command SHALL fire while
the isolation verdict is not "isolado" — including the fail-closed case where
isolation could not be verified at all (empty / unverifiable → not isolated,
Req. 6.2). This test varies BOTH axes together and asserts the biconditional.

The invariant, stated operationally over the runner's public ``run`` API
-------------------------------------------------------------------------
For any emulation request and any ``(confirmed, isolation verdict)`` pair:

    a command is fired  ⟺  (confirmed is True  AND  isolation.passed is True)

"A command is fired" is observed structurally: the runner only ever reaches the
Caldera Operation surface — ``create_operation`` (``POST /api/v2/operations``)
and ``get_operation`` (``GET /api/v2/operations/{id}``) — when both gates open.
We inject a Caldera client over a recording ``httpx.MockTransport`` that flags
whether ANY request hit the operations endpoint, and an isolation verifier that
returns the Hypothesis-generated verdict (passed / not passed / empty /
unverifiable). The biconditional is asserted directly against that flag, plus a
cross-check that NO ``Operation``/``AbilityResult``/``AuditLogEntry`` row is
persisted whenever the gate stays shut.

Everything external is mocked (no real Caldera, no real Docker daemon), and the
database is an ephemeral per-example temp-file SQLite torn down in ``finally``,
so row counts are exact and no state leaks between examples.

_Requirements: 1.2, 1.6, 6.2, 6.5_
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
from app.models.domain import AbilityResult, AuditLogEntry, Operation
from app.models.enums import OperationState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.containment.isolation import ContainerIsolationVerdict, IsolationResult
from app.services.operation.runner import OperationRunner, RunOutcome

# ---------------------------------------------------------------------------
# Duck-typed Ability the runner / containment / preview all accept.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeAbility:
    """A minimal Ability exposing the attributes the runner consumes.

    ``validate_abilities`` (containment) and ``preview_abilities`` read
    ``executors`` (``{name, platform, command}`` dicts); the Caldera payload
    builder reads ``ability_id``/``name``. A plain object (no DB row) keeps the
    run path independent of Case/Ability rows existing.
    """

    ability_id: str
    name: str
    executors: tuple[dict[str, str], ...]


# Internal-only concrete commands (all destinations ⊆ the known internal lab
# subnets) so the containment gate (destinations) always PASSES — isolating the
# two variables under test (confirmation + isolation) from the destination gate.
_INTERNAL_COMMAND_TEMPLATES: tuple[str, ...] = (
    "curl -X POST -F 'cmd=whoami' http://192.168.20.30:5055/exec",
    "wget http://192.168.20.30/payload.sh",
    "sshpass -p Passw0rd ssh attacker@192.168.10.20 'whoami'",
    "ssh attacker@192.168.30.40 'id'",
    "mysql -h 192.168.30.40 -u root -e 'show databases;'",
    "cat /etc/passwd",  # purely local — no destination
    "uname -a",  # purely local — no destination
)


def _ability_strategy() -> st.SearchStrategy[FakeAbility]:
    """Generate an Ability with 1..3 internal-only executor commands."""
    return st.builds(
        lambda uid, cmds: FakeAbility(
            ability_id=f"ability-{uid.hex[:8]}",
            name=f"T{1000 + (uid.int % 600)} - generated",
            executors=tuple(
                {"name": "sh", "platform": "linux", "command": cmd} for cmd in cmds
            ),
        ),
        st.uuids(),
        st.lists(st.sampled_from(_INTERNAL_COMMAND_TEMPLATES), min_size=1, max_size=3),
    )


# ``confirmed`` values. Exactly ``True`` is the only confirming value; the rest
# are the non-True / falsy variants a lax caller could pass. The runner gates on
# ``confirmed is not True``, so each non-True value must keep the gate shut.
_CONFIRM_VALUES: tuple[object, ...] = (True, False, None, 0, "", 0.0, [], {})


# ---------------------------------------------------------------------------
# Isolation verdict axis — passed / not passed / empty / unverifiable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IsolationScenario:
    """A generated isolation outcome plus whether it should pass the gate.

    ``kind`` labels the scenario for readable counterexamples; ``result`` is the
    exact :class:`IsolationResult` the injected verifier returns; ``passes`` is
    the ground truth of whether the gate should open on this verdict (True only
    when ``result.passed`` is True — i.e. the aggregate verdict is "isolado").
    """

    kind: str
    result: IsolationResult
    passes: bool


_TARGET_CONTAINERS: tuple[str, ...] = ("nginx", "db")


def _isolation_scenarios() -> st.SearchStrategy[IsolationScenario]:
    """Generate the four fail-closed-relevant isolation verdicts.

    * ``isolated`` — every target container isolated → ``passed=True`` (the ONLY
      verdict that may open the gate, Req. 6.1/6.5).
    * ``not_isolated`` — at least one container not isolated → ``passed=False``
      (Req. 6.2: environment not isolated).
    * ``empty`` — no verdicts recorded with ``passed=False`` → the fail-closed
      "nothing proven isolated" case (``verify_isolation([...])`` yields this for
      an empty container list; here we model the aggregate directly).
    * ``unverifiable`` — isolation could NOT be verified (e.g. Docker daemon
      down): verdict carries a reason and ``passed=False`` (Req. 6.2).
    """
    isolated = IsolationScenario(
        kind="isolated",
        result=IsolationResult(
            passed=True,
            verdicts=tuple(
                ContainerIsolationVerdict(container=c, isolated=True, reasons=())
                for c in _TARGET_CONTAINERS
            ),
        ),
        passes=True,
    )
    not_isolated = IsolationScenario(
        kind="not_isolated",
        result=IsolationResult(
            passed=False,
            verdicts=(
                ContainerIsolationVerdict(container="nginx", isolated=True, reasons=()),
                ContainerIsolationVerdict(
                    container="db",
                    isolated=False,
                    reasons=("conectado à rede bridge externa 'local-network'",),
                    external_networks=("local-network",),
                ),
            ),
        ),
        passes=False,
    )
    empty = IsolationScenario(
        kind="empty",
        result=IsolationResult(passed=False, verdicts=()),
        passes=False,
    )
    unverifiable = IsolationScenario(
        kind="unverifiable",
        result=IsolationResult(
            passed=False,
            verdicts=(
                ContainerIsolationVerdict(
                    container="nginx",
                    isolated=False,
                    reasons=("Docker Engine API indisponível; isolamento não verificável",),
                ),
            ),
        ),
        passes=False,
    )
    return st.sampled_from((isolated, not_isolated, empty, unverifiable))


def _make_isolation_verifier(scenario: IsolationScenario):
    """Build an injectable verifier returning ``scenario.result`` verbatim.

    Mirrors :func:`inspect_isolation`'s signature ``(containers, inspector)`` so
    it drops straight into the runner via ``isolation_verifier=``. Returning the
    generated verdict unchanged is what lets us vary the isolation axis freely.
    """

    def _verifier(_containers: list[str], _inspector: object) -> IsolationResult:
        return scenario.result

    return _verifier


# ---------------------------------------------------------------------------
# Mocked Caldera — recording MockTransport that flags command firing.
# ---------------------------------------------------------------------------


@dataclass
class RecordingCaldera:
    """A MockTransport handler recording every request and serving a healthy
    Caldera on the happy path.

    ``operation_fired`` is set the moment any request hits the operations
    endpoint (``create_operation`` POST or ``get_operation`` GET), which is the
    structural signal that a command was dispatched to the attack surface. On the
    gated-shut paths that endpoint is never reached, so the flag stays False.
    """

    requests: list[httpx.Request] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.operation_fired = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path

        # Any touch of the operations resource == a command was fired.
        if path == OPERATIONS_PATH or path.startswith(f"{OPERATIONS_PATH}/"):
            self.operation_fired = True

        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200, json={"id": "op-test", "name": "run", "state": "running"}
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
            return httpx.Response(
                200,
                json={
                    "id": "op-test",
                    "state": "finished",
                    "chain": [
                        {
                            "id": "link-1",
                            "ability": {"ability_id": "ability-run"},
                            "command": "",
                            "status": 0,
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Returns ``(session, db_path, engine)`` so the caller can dispose the engine
    and remove the file afterward. A temp file (not ``:memory:``) matches the dev
    SQLite default exactly for ``create_all``/NOT NULL behavior.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stage6_failclosed_firing_")
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


def _make_runner(
    session: Session, caldera: CalderaClient, scenario: IsolationScenario
) -> OperationRunner:
    """Build a runner with mocked Caldera + the scenario's isolation verifier."""
    return OperationRunner(
        session=session,
        caldera=caldera,
        audit_logger=AuditLogger(session),
        isolation_verifier=_make_isolation_verifier(scenario),
    )


# ---------------------------------------------------------------------------
# Property 6 — the biconditional over (confirmed, isolation).
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    abilities=st.lists(_ability_strategy(), min_size=1, max_size=4),
    confirmed=st.sampled_from(_CONFIRM_VALUES),
    scenario=_isolation_scenarios(),
    adversary_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
    campaign_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property6_command_fires_iff_confirmed_and_isolated(
    abilities: list[FakeAbility],
    confirmed: object,
    scenario: IsolationScenario,
    adversary_id: str,
    campaign_id: str | None,
) -> None:
    """Property 6: a command fires iff (confirmed is True AND isolation passed).

    # Feature: stage6-execution-redesign, Property 6: Fail-closed de disparo de
    # comando (gate inegociável).
    Validates: Requirements 1.2, 1.6, 6.2, 6.5

    For every ``(confirmed, isolation verdict)`` pair the runner MUST dispatch an
    Operation command (reach the operations endpoint) **iff** the confirmation is
    exactly ``True`` and the isolation verdict is "isolado". In every other case
    — unconfirmed, not isolated, isolation empty, or isolation unverifiable — the
    flow stops before any execution: no request hits the operations endpoint and
    no row is persisted.
    """
    expected_fire = (confirmed is True) and scenario.passes

    session, db_path, engine = _build_ephemeral_session()
    recorder = RecordingCaldera()
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    try:
        runner = _make_runner(session, caldera, scenario)

        result = runner.run(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id=adversary_id,
            confirmed=confirmed,  # type: ignore[arg-type]
            target_containers=list(_TARGET_CONTAINERS),
        )

        # --- The biconditional: command fired iff the gate opened. ----------
        assert recorder.operation_fired is expected_fire, (
            f"operation_fired={recorder.operation_fired} but expected "
            f"{expected_fire} for confirmed={confirmed!r}, "
            f"isolation={scenario.kind}"
        )

        if expected_fire:
            # Gate open: the Operation ran — a command was fired, rows persisted.
            assert result.started is True
            assert result.outcome is RunOutcome.COMPLETED
            assert result.state is OperationState.FINISHED
            assert result.operation_id is not None
            assert _count(session, Operation) == 1
            # The operations endpoint was reached (create + poll).
            op_requests = [
                r
                for r in recorder.requests
                if r.url.path == OPERATIONS_PATH
                or r.url.path.startswith(f"{OPERATIONS_PATH}/")
            ]
            assert op_requests != []
        else:
            # Gate shut: fail-closed. No command fired, nothing persisted, and the
            # outcome names the gate that blocked (confirmation first, else
            # isolation — confirmation is checked before the isolation gate).
            assert result.started is False
            assert result.operation_id is None
            assert result.caldera_operation_id is None
            assert result.total_success == 0
            assert result.total_failure == 0
            assert result.ability_results == ()
            assert result.state is OperationState.NOT_STARTED

            if confirmed is not True:
                assert result.outcome is RunOutcome.NOT_CONFIRMED
                # Confirmation gate runs before Caldera is touched at all.
                assert recorder.requests == []
            else:
                # Confirmed but not isolated/unverifiable: isolation gate refused.
                assert result.outcome is RunOutcome.ISOLATION_FAILED

            # No side effects persisted on any gated-shut path.
            assert _count(session, Operation) == 0
            assert _count(session, AbilityResult) == 0
            assert _count(session, AuditLogEntry) == 0
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
