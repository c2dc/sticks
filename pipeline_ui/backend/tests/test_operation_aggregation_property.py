"""Property-based test for Property 10 of the pipeline-ui design (task 7.7) —
Operation aggregate is consistent with the per-Ability results.

# Feature: pipeline-ui, Property 10: Agregação da Operação é consistente com os
# resultados por Ability. WHEN uma Operação atinge seu estado final, o resultado
# agregado (``Operation.total_sucesso`` / ``Operation.total_falha``) SHALL refletir
# exatamente a quantidade de Abilities com status "sucesso" e a quantidade com
# status "falha" apurada a partir dos resultados individuais por Ability.

Validates: Requisitos 4.5

This is a normal ``feature`` property test (the pipeline-ui spec is a feature
spec, not a bugfix): the property is expected to HOLD on the current
:class:`~app.services.operation.runner.OperationRunner` implementation, whose
per-Ability aggregation was implemented in task 7.3. It complements the
confirmation-gate (Property 6) and audit-abort (Property 7) property tests by
exercising the *aggregation* seam across many generated Operations whose mocked
Caldera chain mixes SUCCESS and FAILURE links (and, to strengthen the invariant,
occasional not-yet-final PENDING/RUNNING links).

The invariant, stated operationally over the runner's public ``run`` API
--------------------------------------------------------------------------
For an Operation whose mocked Caldera chain has a Hypothesis-generated set of
per-Ability link statuses (each mapping to SUCCESS / FAILURE / PENDING /
RUNNING), after the Operation reaches its final state:

* One :class:`AbilityResult` row is persisted **per link** — the runner persists
  every command's result regardless of its status. So ``count(AbilityResult)``
  equals the number of links (the number of per-Ability results).
* ``Operation.total_sucesso`` equals the number of links whose derived status is
  SUCCESS, and ``Operation.total_falha`` equals the number whose derived status
  is FAILURE — counted directly from the individual results.
* ``total_sucesso + total_falha`` equals the number of **finished** links
  (SUCCESS or FAILURE). Not-yet-final links (PENDING / RUNNING) are persisted as
  ``AbilityResult`` rows but contribute to neither aggregate, so the aggregate is
  *exactly* consistent with — never exceeds — the finished per-Ability results.
* The runner's returned ``OperationRunResult`` mirrors the persisted Operation:
  its ``total_sucesso`` / ``total_falha`` match the row, and its
  ``ability_results`` has one entry per link.

Everything external is mocked (no real Caldera, no real Docker daemon):
- Caldera is a :class:`httpx.MockTransport`-backed real :class:`CalderaClient`
  serving a chain whose link statuses are exactly the generated ones.
- Isolation is a fake verifier that reports the target containers isolated, and
  the commands are internal-only, so the confirmation/containment/isolation gates
  all pass and the Operation runs to its final state — isolating Property 10's
  variable (the aggregation) from the four pre-flight gates.
- The database is an **ephemeral temp-file SQLite** built fresh *per example*
  (Hypothesis re-runs the body per example) and torn down in a ``finally`` block,
  so row counts are exact and no ``.db`` state leaks between examples.

_Requisitos: 4.5_
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
# containment gate (Property 1) always PASSES — isolating Property 10's variable
# (the aggregation) from the destination gate.
_INTERNAL_COMMAND_TEMPLATES: tuple[str, ...] = (
    "curl -X POST -F 'cmd=whoami' http://172.21.0.20:5055/exec",
    "wget http://172.21.0.20/payload.sh",
    "sshpass -p Passw0rd ssh attacker@172.20.0.20 'whoami'",
    "ssh attacker@172.22.0.20 'id'",
    "mysql -h 172.22.0.20 -u root -e 'show databases;'",
    "cat /etc/passwd",  # purely local — no destination
    "uname -a",  # purely local — no destination
)


# ---------------------------------------------------------------------------
# Caldera link status codes and their derived AbilityResultStatus.
# ---------------------------------------------------------------------------
#
# The CalderaClient maps a raw link ``status`` to an AbilityResultStatus:
#   None            -> PENDING   ("pendente")     — not counted in the aggregate
#   -3              -> RUNNING   ("em_execucao")  — not counted in the aggregate
#   0               -> SUCCESS   ("sucesso")      — counted in total_sucesso
#   any other int   -> FAILURE   ("falha")        — counted in total_falha
#
# We draw statuses from this labelled set so each example knows, per link, the
# EXACT status the runner will derive — letting the property compute the expected
# aggregate directly from the generated inputs (no re-derivation guesswork).
_STATUS_SUCCESS = 0
_STATUS_RUNNING = -3
# A handful of concrete non-zero "finished failure" codes Caldera uses/permits.
_FAILURE_CODES: tuple[int, ...] = (1, 124, 255, -1, -2, -4, -5)


def _link_status_strategy() -> st.SearchStrategy[object]:
    """Draw one raw Caldera link status covering every derived category.

    Weighted toward finished outcomes (SUCCESS / FAILURE) — the core of the
    aggregate — while still sampling the not-yet-final categories (PENDING via
    ``None``, RUNNING via ``-3``) so the property also pins that those are
    persisted as results yet excluded from the totals.
    """
    return st.one_of(
        st.just(_STATUS_SUCCESS),  # -> SUCCESS
        st.sampled_from(_FAILURE_CODES),  # -> FAILURE
        st.just(None),  # -> PENDING (not counted)
        st.just(_STATUS_RUNNING),  # -> RUNNING (not counted)
    )


def _isolated_verifier(containers: list[str], _inspector: object) -> IsolationResult:
    """A fake isolation verifier: every requested container is isolated.

    Mirrors :func:`inspect_isolation`'s signature ``(containers, inspector)`` so
    it drops straight into the runner via ``isolation_verifier=``. Reporting all
    containers isolated keeps Property 2's gate open, isolating Property 10's
    variable (the aggregation) from the isolation gate too.
    """
    verdicts = tuple(
        ContainerIsolationVerdict(container=name, isolated=True, reasons=())
        for name in containers
    )
    return IsolationResult(passed=True, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Mocked Caldera — a chain whose link statuses are exactly the generated ones.
# ---------------------------------------------------------------------------


@dataclass
class MixedChainCaldera:
    """A MockTransport handler serving a healthy Caldera whose Operation chain
    has one finished/pending link per entry in ``statuses``.

    Each generated status becomes one link, so the runner's per-command loop
    produces one :class:`AbilityResult` per status and aggregates SUCCESS /
    FAILURE across them — exactly the surface Property 10 pins down.
    """

    statuses: tuple[object, ...]
    requests: list[httpx.Request] = field(default_factory=list)

    def _chain(self) -> list[dict]:
        chain: list[dict] = []
        for i, status in enumerate(self.statuses):
            link: dict = {
                "id": f"link-{i}",
                "ability": {"ability_id": f"ability-{i}"},
                "command": "",
            }
            # Omitting ``status`` entirely yields ``payload.get("status") is None``
            # -> PENDING; a present value drives SUCCESS / FAILURE / RUNNING.
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
                200, json={"id": "op-test", "name": "run", "state": "running"}
            )
        if method == "GET" and path == f"{OPERATIONS_PATH}/op-test":
            return httpx.Response(
                200,
                json={"id": "op-test", "state": "finished", "chain": self._chain()},
            )
        raise AssertionError(f"unexpected request: {method} {path}")


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
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_aggregation_")
    db_path = Path(tmp_dir) / "aggregation.db"
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


def _expected_status(raw: object) -> AbilityResultStatus:
    """Map a raw generated link status to the AbilityResultStatus the runner derives.

    Kept independent of production so the property computes the expected aggregate
    from the *inputs* rather than trusting the code under test.
    """
    if raw is None:
        return AbilityResultStatus.PENDING
    if raw == _STATUS_RUNNING:
        return AbilityResultStatus.RUNNING
    if raw == _STATUS_SUCCESS:
        return AbilityResultStatus.SUCCESS
    return AbilityResultStatus.FAILURE


def _build_abilities(num: int) -> list[FakeAbility]:
    """Build ``num`` internal-only Abilities so the containment gate stays open.

    The runner's per-command loop iterates the mocked Caldera *chain*, not these
    Abilities directly, so the exact commands here only matter for the
    (always-passing) containment/isolation pre-flight; each carries one
    internal-only executor command. At least one Ability is always built so the
    pre-flight has something to validate even when the chain is short.
    """
    count = max(1, num)
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
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Property 10 — the Operation aggregate is exactly consistent with the results.
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    statuses=st.lists(_link_status_strategy(), min_size=1, max_size=15),
    adversary_id=st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
    caso_id=st.sampled_from(["apt41_dust", "c0010", "shadowray", None]),
)
def test_property10_operation_aggregate_matches_per_ability_results(
    statuses: list[object],
    adversary_id: str,
    caso_id: str | None,
) -> None:
    """Property 10: agregação da Operação é consistente com os resultados por Ability.

    # Feature: pipeline-ui, Property 10: Agregação da Operação é consistente com
    # os resultados por Ability.
    Validates: Requisitos 4.5

    For an Operation whose mocked Caldera chain mixes SUCCESS / FAILURE (and
    occasional PENDING / RUNNING) links, after it reaches its final state:
    ``Operation.total_sucesso`` equals the number of SUCCESS per-Ability results,
    ``Operation.total_falha`` equals the number of FAILURE results, their sum
    equals the number of finished (SUCCESS or FAILURE) results, and one
    ``AbilityResult`` row is persisted per link — so the aggregate is exactly
    consistent with the individual results.
    """
    # Expected aggregate computed directly from the generated inputs.
    derived = [_expected_status(raw) for raw in statuses]
    expected_sucesso = sum(1 for s in derived if s is AbilityResultStatus.SUCCESS)
    expected_falha = sum(1 for s in derived if s is AbilityResultStatus.FAILURE)
    total_links = len(statuses)

    session, db_path, engine = _build_ephemeral_session()
    recorder = MixedChainCaldera(statuses=tuple(statuses))
    caldera = CalderaClient(transport=httpx.MockTransport(recorder))
    audit = AuditLogger(session)
    try:
        runner = OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=audit,
            isolation_verifier=_isolated_verifier,
        )

        result = runner.run(
            caso_id=caso_id,
            abilities=_build_abilities(total_links),
            adversary_id=adversary_id,
            confirmado=True,
            # Explicit isolated containers so isolation derivation never matters.
            target_containers=["nginx", "db"],
        )

        # --- The Operation ran to its final state (no gate/abort interfered). ---
        assert result.outcome is RunOutcome.COMPLETED
        assert result.state is OperationState.FINISHED
        assert result.operation_id is not None

        # --- Exactly one Operation row, finalized. ---
        assert _count(session, Operation) == 1
        operation = session.execute(select(Operation)).scalar_one()
        assert operation.estado is OperationState.FINISHED
        assert operation.finalizada_em is not None

        # --- One AbilityResult row per link (every command's result persisted). ---
        assert _count(session, AbilityResult) == total_links
        assert len(result.ability_results) == total_links

        # --- The per-Ability result rows carry exactly the derived statuses. ---
        persisted_statuses = (
            session.execute(select(AbilityResult.status)).scalars().all()
        )
        assert sorted(s.value for s in persisted_statuses) == sorted(
            s.value for s in derived
        )

        # --- The persisted aggregate matches the counts from the results. ---
        assert operation.total_sucesso == expected_sucesso
        assert operation.total_falha == expected_falha

        # --- sum(sucesso, falha) == number of FINISHED results (not all links). ---
        finished = expected_sucesso + expected_falha
        assert operation.total_sucesso + operation.total_falha == finished
        # The aggregate never exceeds the total number of per-Ability results.
        assert operation.total_sucesso + operation.total_falha <= total_links

        # --- The returned result mirrors the persisted aggregate. ---
        assert result.total_sucesso == expected_sucesso
        assert result.total_falha == expected_falha

        # --- Cross-check against a direct count of the persisted result rows. ---
        persisted_sucesso = _count_by_status(session, AbilityResultStatus.SUCCESS)
        persisted_falha = _count_by_status(session, AbilityResultStatus.FAILURE)
        assert operation.total_sucesso == persisted_sucesso
        assert operation.total_falha == persisted_falha
    finally:
        caldera.close()
        session.close()
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def _count_by_status(session: Session, status: AbilityResultStatus) -> int:
    """Count persisted AbilityResult rows whose ``status`` equals ``status``."""
    return session.execute(
        select(func.count())
        .select_from(AbilityResult)
        .where(AbilityResult.status == status)
    ).scalar_one()
