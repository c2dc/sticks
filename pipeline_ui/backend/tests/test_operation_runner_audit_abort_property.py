"""Property-based test for Property 7 of the pipeline-ui design (task 7.6) —
security core (audit-failure aborts the Operation).

# Feature: pipeline-ui, Property 7: Falha de auditoria aborta a Operação. Se, ao
# executar uma Operação com múltiplos comandos, a persistência de um
# ``AuditLogEntry`` falha no passo K, THE Backend SHALL abortar a Operação a
# partir de K — nenhum comando posterior a K é executado — e a UI/resultado
# SHALL sinalizar que a trilha de auditoria não pôde ser registrada.

Validates: Requisitos 6.9

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.operation.runner.OperationRunner` implementation, whose
audit-failure abort was implemented in task 7.5. It complements the
confirmation-gate property test (Property 6) and the 1:1 audit-trail property
test (Property 5) by exercising the *abort-on-audit-failure* seam across many
generated Operations, injecting the persistence failure at a Hypothesis-chosen
step ``K``.

The invariant, stated operationally over the runner's public ``run`` API
--------------------------------------------------------------------------
For an Operation whose mocked Caldera chain has ``N`` commands (``N >= 1``) and a
failure injected at the ``K``-th audit write (``1 <= K <= N``):

* The runner returns :data:`RunOutcome.ABORTED` with persisted state
  :data:`OperationState.ABORTED`, and its ``message`` signals the audit trail
  could not be recorded (UI signalling — Req. 6.9).
* **No command after K runs.** The per-command loop breaks at K, so:
    - exactly ``K - 1`` commands were fully processed (each with its persisted
      ``AbilityResult`` *and* audit row);
    - the ``K``-th command's ``AbilityResult`` is persisted (the runner persists
      it *before* the audit write that fails) but its audit row is **not**;
    - **no** command with index > K is touched: there are exactly ``K``
      ``AbilityResult`` rows and exactly ``K - 1`` ``AuditLogEntry`` rows, the
      audit logger's ``log_command`` was called exactly ``K`` times (the K-th
      raised), and the runner's returned ``ability_results`` has length ``K - 1``
      (only the commands whose audit succeeded are reported).
* The aggregate (``total_sucesso`` / ``total_falha``) reflects exactly the
  ``K - 1`` commands that ran and were audited before the failing write.

Everything external is mocked (no real Caldera, no real Docker daemon):
- Caldera is a :class:`httpx.MockTransport`-backed real :class:`CalderaClient`
  serving a healthy chain of ``N`` finished links (so, absent the injected audit
  failure, the Operation would run to COMPLETED).
- Isolation is a fake verifier that reports the target containers isolated.
- The audit failure is injected by a :class:`_FailingAuditLogger`, a real
  :class:`AuditLogger` subclass that raises :class:`AuditPersistenceError` on its
  ``K``-th ``log_command`` call and delegates to the real implementation
  otherwise — so the abort path under test is the production one, driven only by
  a realistic persistence failure at an arbitrary step.
- The database is an **ephemeral temp-file SQLite** built fresh *per example*
  (Hypothesis re-runs the body per example) and torn down in a ``finally`` block,
  so row counts are exact and no ``.db`` state leaks between examples.

_Requisitos: 6.9_
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
from app.services.audit.audit_logger import AuditLogger, AuditPersistenceError
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
# containment gate (Property 1) always PASSES — isolating Property 7's variable
# (the audit-write failure) from the destination gate.
_INTERNAL_COMMAND_TEMPLATES: tuple[str, ...] = (
    "curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec",
    "wget http://172.21.0.20/payload.sh",
    "sshpass -p Passw0rd ssh attacker@172.20.0.20 'whoami'",
    "ssh attacker@172.22.0.20 'id'",
    "mysql -h 172.22.0.20 -u root -e 'show databases;'",
    "cat /etc/passwd",  # purely local — no destination
    "uname -a",  # purely local — no destination
)


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """A fake isolation verifier: every requested container is isolated.

    Mirrors :func:`inspect_isolation`'s signature ``(containers, inspector)`` so
    it drops straight into the runner via ``isolation_verifier=``. Reporting all
    containers isolated keeps Property 2's gate open, isolating Property 7's
    variable (the audit-write failure) from the isolation gate too.
    """
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Mocked Caldera — a healthy chain of N finished (SUCCESS) links.
# ---------------------------------------------------------------------------


@dataclass
class ChainCaldera:
    """A MockTransport handler serving a healthy Caldera whose Operation chain
    has ``num_links`` finished (``status == 0`` -> SUCCESS) links.

    ``num_links`` == the number of commands the runner's per-command loop will
    iterate. Absent the injected audit failure the Operation would run to
    COMPLETED with ``num_links`` successes; injecting the failure at step K makes
    the runner abort so no link after K is processed — exactly Property 7.
    """

    num_links: int
    requests: list[httpx.Request] = field(default_factory=list)

    def _chain(self) -> list[dict]:
        return [
            {
                "id": f"link-{i}",
                "ability": {"ability_id": f"ability-{i}"},
                "command": "",
                "status": 0,
            }
            for i in range(self.num_links)
        ]

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
            return httpx.Response(
                200,
                json={"id": "op-test", "state": "finished", "chain": self._chain()},
            )
        raise AssertionError(f"unexpected request: {method} {path}")


# ---------------------------------------------------------------------------
# Audit failure injection at an arbitrary step K.
# ---------------------------------------------------------------------------


class _FailingAuditLogger(AuditLogger):
    """A real :class:`AuditLogger` that fails to persist on its K-th call.

    Every ``log_command`` call is counted; the ``fail_at``-th call raises
    :class:`AuditPersistenceError` (exactly what the real logger raises when a
    persistence error occurs), and every other call delegates to the real
    implementation (which persists a genuine ``AuditLogEntry`` row). This drives
    the production abort seam in :class:`OperationRunner` with a realistic audit
    failure at a Hypothesis-chosen step — without touching production code.
    """

    def __init__(self, session: Session, *, fail_at: int) -> None:
        super().__init__(session)
        self._fail_at = fail_at
        self.calls = 0

    def log_command(self, **kwargs: object) -> AuditLogEntry:  # type: ignore[override]
        self.calls += 1
        if self.calls == self._fail_at:
            comando = str(kwargs.get("comando") or "(comando não informado)")
            container = str(kwargs.get("container_destino") or "(desconhecido)")
            raise AuditPersistenceError(
                "Falha simulada ao persistir o registro de auditoria "
                f"(passo {self._fail_at}).",
                comando=comando,
                container_destino=container,
            )
        return super().log_command(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Ephemeral per-example SQLite (exact counts, no state leak).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create a fresh temp-file SQLite engine + schema and return a session.

    Mirrors the temp-file SQLite + ``Base.create_all`` pattern from
    ``test_operation_runner_property.py``. Returns ``(session, db_path, engine)``
    so the caller can dispose the engine and remove the file afterward. A temp
    file (not ``:memory:``) is used so ``create_all`` and NOT NULL behavior match
    the dev SQLite default exactly.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_audit_abort_")
    db_path = Path(tmp_dir) / "audit_abort.db"
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


def _build_abilities(num: int) -> list[FakeAbility]:
    """Build ``num`` internal-only Abilities so the containment gate stays open.

    The runner's per-command loop iterates the mocked Caldera *chain*, not these
    Abilities directly, so the exact commands here only matter for the
    (always-passing) containment/isolation pre-flight; each carries one
    internal-only executor command.
    """
    return [
        FakeAbility(
            ability_id=f"ability-{i}",
            name=f"T{1000 + i} - generated",
            executors=(
                {
                    "name": "sh",
                    "platform": "linux",
                    "command": _INTERNAL_COMMAND_TEMPLATES[
                        i % len(_INTERNAL_COMMAND_TEMPLATES)
                    ],
                },
            ),
        )
        for i in range(num)
    ]


# ---------------------------------------------------------------------------
# Property 7 — audit failure at step K aborts the Operation from K onward.
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    data=st.data(),
    num_commands=st.integers(min_value=1, max_value=12),
    adversary_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
    caso_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property7_audit_failure_aborts_operation_from_step_k(
    data: st.DataObject,
    num_commands: int,
    adversary_id: str,
    caso_id: str | None,
) -> None:
    """Property 7: injeção de falha de auditoria no passo K aborta a partir de K.

    # Feature: pipeline-ui, Property 7: Falha de auditoria aborta a Operação.
    Validates: Requisitos 6.9

    For an Operation whose mocked Caldera chain has ``N`` commands and an audit
    persistence failure injected at step ``K`` (``1 <= K <= N``), the runner
    aborts: it returns ABORTED / OperationState.ABORTED with a message signalling
    the audit trail could not be recorded, NO command after ``K`` is processed
    (exactly ``K`` AbilityResult rows, exactly ``K - 1`` audit rows, exactly
    ``K`` audit-logger calls, and ``K - 1`` reported ability results), and the
    aggregate reflects only the ``K - 1`` commands audited before the failure.
    """
    # K is chosen within [1, N] so the failing step is always a real command.
    fail_at = data.draw(st.integers(min_value=1, max_value=num_commands), label="K")

    session, db_path, engine = _build_ephemeral_session()
    recorder = ChainCaldera(num_links=num_commands)
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    audit = _FailingAuditLogger(session, fail_at=fail_at)
    try:
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=audit,
            isolation_verifier=_isolated_verifier,
        )

        result = runner.run(
            caso_id=caso_id,
            abilities=_build_abilities(num_commands),
            adversary_id=adversary_id,
            confirmado=True,
            # Explicit isolated containers so isolation derivation never matters.
            target_containers=["nginx", "db"],
        )

        # --- The Operation aborted, and the UI/result signals it (Req. 6.9). ---
        assert result.outcome is RunOutcome.ABORTED
        assert result.state is OperationState.ABORTED
        assert result.started is True
        assert result.operation_id is not None
        # The message signals the audit trail could not be recorded.
        assert "auditoria" in result.message.lower()
        assert "abort" in result.message.lower()

        # --- The persisted Operation row mirrors the abort. ---
        assert _count(session, Operation) == 1
        operation = session.execute(select(Operation)).scalar_one()
        assert operation.estado is OperationState.ABORTED
        assert operation.finalizada_em is not None

        # --- No command after K ran. ---
        # The audit logger was called exactly K times (the K-th raised); every
        # call after K would mean a later command was processed.
        assert audit.calls == fail_at

        # The runner persists the AbilityResult BEFORE the audit write, so the
        # failing K-th command leaves its result row behind: exactly K result
        # rows (K - 1 successful + the K-th whose audit failed), and no more.
        assert _count(session, AbilityResult) == fail_at

        # Exactly K - 1 audit rows persisted (the writes before the failing one);
        # the K-th audit row was never written and nothing after it ran.
        assert _count(session, AuditLogEntry) == fail_at - 1

        # The runner reports only the commands whose audit succeeded (K - 1);
        # the aggregate reflects exactly those (all SUCCESS in this healthy chain).
        assert len(result.ability_results) == fail_at - 1
        assert result.total_sucesso == fail_at - 1
        assert result.total_falha == 0
        assert operation.total_sucesso == fail_at - 1
        assert operation.total_falha == 0
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)
