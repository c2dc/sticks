"""Property-based test for Property 6 of the pipeline-ui design (task 7.4) —
security core (explicit-confirmation gate).

# Feature: pipeline-ui, Property 6: Execução exige confirmação explícita. Para
# qualquer solicitação de emulação, o Backend inicia a Operação se e somente se a
# confirmação explícita do Pesquisador é fornecida (confirmado verdadeiro); na
# ausência de confirmação ou em cancelamento, nenhum comando de adversário é
# executado e o estado retorna ao anterior à solicitação.

Validates: Requisitos 6.6, 6.7

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.operation.runner.OperationRunner` implementation. It
complements the containment/isolation property tests (Properties 1 and 2) by
exercising the *first* enforcement gate of the runner — the explicit
confirmation flag — across many generated emulation requests.

The invariant, stated operationally over the runner's public ``run`` API
--------------------------------------------------------------------------
For any emulation request (varied Ability sets, adversary id, containers):

* ``confirmado is not True`` (absent / ``False`` / any non-True falsy value the
  boolean flag could carry, e.g. ``None``/``0``/``""``) MUST:
    - return :data:`RunOutcome.NOT_CONFIRMED` with state
      :data:`OperationState.NOT_STARTED` (the prior state — nothing started);
    - and — crucially — leave **nothing** observable behind: no ``Operation``
      row, no ``AbilityResult`` row and no ``AuditLogEntry`` row is persisted,
      and the injected Caldera client is **never touched** (no HTTP request of
      any kind reaches the MockTransport, so no adversary command runs).
* ``confirmado is True`` (with a healthy mocked Caldera, internal-only Ability
  commands so containment passes, and isolated target containers) MUST start and
  run the Operation to :data:`RunOutcome.COMPLETED`, creating the expected rows —
  proving the gate is *confirmation*, not some unrelated pre-flight refusal.

Everything external is mocked (no real Caldera, no real Docker daemon):
- Caldera is a :class:`httpx.MockTransport`-backed real :class:`CalderaClient`;
  a recording router lets us assert the client was never called on the negative
  path and drives a healthy chain on the positive path.
- Isolation is a fake verifier that reports the target containers isolated.
- The database is an **ephemeral temp-file SQLite** built fresh *per example*
  (Hypothesis re-runs the body per example) and torn down in a ``finally`` block,
  so row counts are exact and no ``.db`` state leaks between examples.

_Requisitos: 6.6, 6.7_
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
    ``executors`` (a list of ``{name, platform, command}`` dicts); the Caldera
    payload builder reads ``ability_id``/``name``. Keeping this a plain object
    (no DB row) means the run path never depends on Case/Ability rows existing.
    """

    ability_id: str
    name: str
    executors: tuple[dict[str, str], ...]


# Internal-only concrete commands (all destinations ⊆ 172.20/21/22.0.0/24) so the
# containment gate (Property 1) always PASSES — isolating Property 6's variable
# (confirmation) from the destination gate.
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


# ``confirmado`` values that are NOT the literal ``True`` — the runner requires
# exactly ``True`` (``confirmado is not True`` gates), so every one of these must
# be treated as "not confirmed": absent-intent (None), explicit False, and the
# non-True falsy variants the flag could carry from a lax caller.
_NON_TRUE_CONFIRMS: tuple[object, ...] = (False, None, 0, "", 0.0, [], {})


# ---------------------------------------------------------------------------
# Mocked Caldera — recording MockTransport router.
# ---------------------------------------------------------------------------


@dataclass
class RecordingCaldera:
    """A MockTransport handler that records every request and serves a healthy
    Caldera on the happy path.

    Recording every request is exactly how we prove Property 6's negative case:
    on an unconfirmed request the runner must not touch Caldera at all, so
    ``requests`` stays empty. On the positive path it drives a chain whose single
    link finished with ``status == 0`` (SUCCESS), so the Operation completes.
    """

    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.method.upper()
        path = request.url.path

        if method == "GET" and path == HEALTH_PATH:
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == ABILITIES_PATH:
            # Echo the posted ability back (ids round-trip).
            return httpx.Response(200, json=json.loads(request.content.decode("utf-8")))
        if method == "POST" and path == OPERATIONS_PATH:
            return httpx.Response(
                200, json={"id": "op-test", "name": "run", "state": "running"}
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
            # A single finished (status 0 -> SUCCESS) link so the Operation runs
            # to completion and persists rows on the positive path.
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


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """A fake isolation verifier: every requested container is isolated.

    Mirrors :func:`inspect_isolation`'s signature ``(containers, inspector)`` so
    it drops straight into the runner via ``isolation_verifier=``. Reporting all
    containers isolated keeps Property 2's gate open, isolating Property 6's
    variable (confirmation) from the isolation gate too.
    """
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_models.py`` / ``test_audit_logger_property.py``. Returns
    ``(session, db_path, engine)`` so the caller can dispose the engine and
    remove the file afterward. A temp file (not ``:memory:``) is used so
    ``create_all`` and NOT NULL behavior match the dev SQLite default exactly.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_runner_")
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


def _make_runner(session: Session, caldera: CalderaClient) -> OperationRunner:
    """Build a runner with mocked Caldera + fake isolation over ``session``."""
    return OperationRunner(
        session=session,
        caldera=caldera,
        audit_logger=AuditLogger(session),
        isolation_verifier=_isolated_verifier,
    )


# ---------------------------------------------------------------------------
# Property 6 — negative direction: no confirmation => nothing happens.
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    abilities=st.lists(_ability_strategy(), min_size=1, max_size=4),
    confirmado=st.sampled_from(_NON_TRUE_CONFIRMS),
    adversary_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
    caso_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property6_unconfirmed_request_changes_nothing(
    abilities: list[FakeAbility],
    confirmado: object,
    adversary_id: str,
    caso_id: str | None,
) -> None:
    """Property 6 (⇐): confirmação ausente/False/cancelada não inicia a Operação.

    # Feature: pipeline-ui, Property 6: Execução exige confirmação explícita.
    Validates: Requisitos 6.6, 6.7

    For any emulation request whose ``confirmado`` is not exactly ``True``, the
    runner returns NOT_CONFIRMED / NOT_STARTED and NOTHING is persisted (no
    Operation, AbilityResult or AuditLogEntry row) — and the injected Caldera
    client is never called (no HTTP request reaches the transport), so no
    adversary command executes.
    """
    session, db_path, engine = _build_ephemeral_session()
    recorder = RecordingCaldera()
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    try:
        runner = _make_runner(session, caldera)

        result = runner.run(
            caso_id=caso_id,
            abilities=abilities,
            adversary_id=adversary_id,
            confirmado=confirmado,  # type: ignore[arg-type]
        )

        # Outcome: not confirmed, state returns to the prior (NOT_STARTED) state.
        assert result.outcome is RunOutcome.NOT_CONFIRMED
        assert result.state is OperationState.NOT_STARTED
        assert result.started is False
        assert result.operation_id is None
        assert result.caldera_operation_id is None
        assert result.total_sucesso == 0
        assert result.total_falha == 0
        assert result.ability_results == ()

        # Nothing observable happened: no rows in any of the three tables.
        assert _count(session, Operation) == 0
        assert _count(session, AbilityResult) == 0
        assert _count(session, AuditLogEntry) == 0

        # Caldera was NEVER touched — the confirmation gate runs before any
        # availability check, ability load, operation create or poll.
        assert recorder.requests == []
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


# ---------------------------------------------------------------------------
# Property 6 — positive direction: confirmation => Operation runs.
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    abilities=st.lists(_ability_strategy(), min_size=1, max_size=4),
    adversary_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
    caso_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property6_confirmed_request_runs_the_operation(
    abilities: list[FakeAbility],
    adversary_id: str,
    caso_id: str | None,
) -> None:
    """Property 6 (⇒): confirmação explícita (True) inicia e conduz a Operação.

    # Feature: pipeline-ui, Property 6: Execução exige confirmação explícita.
    Validates: Requisitos 6.6, 6.7

    With ``confirmado is True`` and every other gate open (healthy mocked
    Caldera, internal-only commands so containment passes, isolated target
    containers), the runner starts the Operation and runs it to COMPLETED,
    creating the expected rows. This proves the gate that blocked the negative
    case is *confirmation itself*, not an unrelated pre-flight refusal.
    """
    session, db_path, engine = _build_ephemeral_session()
    recorder = RecordingCaldera()
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    try:
        runner = _make_runner(session, caldera)

        result = runner.run(
            caso_id=caso_id,
            abilities=abilities,
            adversary_id=adversary_id,
            confirmado=True,
            # Explicit isolated containers so isolation derivation never matters.
            target_containers=["nginx", "db"],
        )

        # The Operation started and finished (the gate opened on True).
        assert result.outcome is RunOutcome.COMPLETED
        assert result.state is OperationState.FINISHED
        assert result.started is True
        assert result.operation_id is not None

        # Exactly one Operation row was created and finalized.
        assert _count(session, Operation) == 1
        operation = session.execute(select(Operation)).scalar_one()
        assert operation.estado is OperationState.FINISHED

        # The mocked chain had one SUCCESS link -> one AbilityResult, one audit
        # row, and the aggregate reflects the single success.
        assert _count(session, AbilityResult) == 1
        assert _count(session, AuditLogEntry) == 1
        assert result.total_sucesso == 1
        assert result.total_falha == 0

        # Caldera WAS exercised on this path (contrast with the negative case).
        assert recorder.requests != []
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
