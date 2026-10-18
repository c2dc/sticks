"""Property test da segurança da Execução do Estágio 6 (task 8.6).

# Feature: pipeline-seis-stages, Property 7: Nenhuma execução com destino
# external ou contêiner não isolated (segurança). Para qualquer conjunto de
# Abilities, se ao menos um command tem destino external OU ao menos um
# contêiner-target não está isolated, então a execução do Estágio 6 é
# recusada/abortada e NENHUMA Operação é started; só executa quando TODOS os
# destinations são internos E TODOS os contêineres-target estão isolados (fail-closed).

Validates: Requirements 10.2, 13.4, 13.5, 14.2, 14.4, 14.5, 9.6, 9.7

Este teste dirige :meth:`ExecutionStageService.execute` através de um
``OperationRunner`` **real** com *fakes* injetados — exatamente o seam de
integração de ``test_execucao_estagio6.py`` — para que os quatro gates (ordem do
design: confirmação → contenção → isolation → disponibilidade) rodem de fato:

* **Caldera** é um :class:`CalderaClient` real sobre ``httpx.MockTransport`` que
  responde ``finished`` imediatamente (nenhuma rede, nenhum Caldera real);
* o **verificador de isolation** é um *fake* parametrizável que reprova
  exatamente os contêineres que a geração marcou como não isolados;
* a Base_de_Dados é um SQLite **efêmero** em file temporário;
* o campaign curated é semeado em um ``data_dir`` temporário com UM command cujo
  destino (interno/external) é escolhido pela geração.

Para cada exemplo o Hypothesis escolhe um **destino de command** (um IP interno
de laboratório, um IP external, ou um hostname — ambos externos por definição) e
um **state de isolation** (todos isolados vs. ao menos um não isolated). Um
**oráculo independente** (``_deve_executar``) recomputa o verdict de segurança
diretamente dessas escolhas — as asserções nunca confiam na implementação para
decidir o que é "seguro".

Invariante central (fail-closed): se o destino é external OU o isolation reprova,
então ``result`` é ``CONTAINMENT_REFUSED`` ou ``ISOLATION_FAILED``,
``started is False`` e **nenhuma line ``Operation`` existe** no banco. campaign
contrário (destino interno E tudo isolated), a execução conclui, cria exatamente
uma ``Operation`` e marca o Estágio 6 ``completed``.

_Requirements: 10.2, 13.4, 13.5, 14.2, 14.4, 14.5, 9.6, 9.7_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Operation, StageRun
from app.models.enums import OperationState, StageState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
)
from app.services.campaign_service import CaseService
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    IsolationResult,
)
from app.services.environment.execution import ExecutionStageService
from app.services.operation.runner import OperationRunner, RunOutcome
from app.services.stage.stage_service import ESTAGIO_EXECUCAO


_SLUG = "shadowray"  # a registered curated slug (CURATED_CASES)

# Internal lab IPs (resolve to lab containers; see subnets.STATIC_IP_CONTAINERS).
_INTERNAL_IPS = ("192.168.10.20", "192.168.20.30", "192.168.30.40")
# External destinations: public IPs and bare hostnames (external by definition).
_EXTERNAL_IPS = ("8.8.8.8", "1.1.1.1", "203.0.113.7")
_EXTERNAL_HOSTS = ("nmap.org", "evil.example.com", "attacker.net")


# ---------------------------------------------------------------------------
# Ephemeral fixtures (no network, no Docker daemon)
# ---------------------------------------------------------------------------


@contextmanager
def _ephemeral_session() -> Iterator[Session]:
    """A fresh ephemeral temp-file SQLite session, isolated **per example**.

    Hypothesis replays many examples inside a single test invocation, so a
    function-scoped fixture would share one database across every generated
    example and let ``Operation`` rows accumulate — defeating the "no Operation
    created" assertion. This context manager builds and tears down a brand-new
    SQLite database for each example, so every example starts from a clean slate.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio6_seg_db_"))
    db_path = tmp / "test.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path}")
    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Local()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str, *, command: str) -> None:
    """Seed a minimal curated case with a single ability command (one destination)."""
    _write_json(
        data_dir / "api" / f"{slug}_dag-ability.json",
        [
            {
                "ability_id": f"{slug}-ab-1",
                "name": "first",
                "technique_id": "T1059",
                "executors": [
                    {"name": "sh", "platform": "linux", "command": command}
                ],
            }
        ],
    )
    _write_json(
        data_dir / "api" / f"{slug}_dag-adversary.json",
        {
            "id": f"{slug}-adv",
            "name": "Seed Adversary",
            "description": "seed",
            "atomic_ordering": [f"{slug}-ab-1"],
        },
    )
    _write_json(
        data_dir / "dag" / f"{slug}_dag.json",
        {
            "campaign_name": "Seed",
            "structural_nodes": [
                {"node_id": "n0", "node_index": 0, "technique_id": "T1059"}
            ],
            "metadata": {"total_techniques": 1},
        },
    )


# ---------------------------------------------------------------------------
# Fakes: a real CalderaClient over MockTransport + a parametrizable verifier
# ---------------------------------------------------------------------------


def _healthy_caldera() -> CalderaClient:
    """A real CalderaClient backed by MockTransport returning a finished chain."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == HEALTH_PATH:
            return httpx.Response(200, json={"access": "red"})
        if path == ABILITIES_PATH:
            return httpx.Response(200, json=[])
        if path == ADVERSARIES_PATH:
            return httpx.Response(200, json={"adversary_id": "a"})
        if path == OPERATIONS_PATH and request.method == "POST":
            return httpx.Response(
                200, json={"id": "op-1", "name": "n", "state": "running"}
            )
        if path.startswith(OPERATIONS_PATH):  # GET poll
            return httpx.Response(
                200,
                json={
                    "id": "op-1",
                    "state": "finished",
                    "chain": [
                        {
                            "ability": {"ability_id": f"{_SLUG}-ab-1"},
                            "status": 0,
                            "command": "ZWNobyAx",  # base64("echo 1")
                            "output": "ok",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})

    return CalderaClient(transport=httpx.MockTransport(handler))


class _ConfigurableVerifier:
    """A verifier callable approving IFF ``isolated_all`` is True.

    When ``isolated_all`` is False, EVERY inspected target container is reported
    as not isolated with a human-readable reason (so the offending containers are
    always identified, Req. 9.7 / 14.3). When there are no derived containers to
    inspect (a local-only command), a fallback container keeps the failed verdict
    observable so the gate still fails closed.
    """

    def __init__(self, *, isolated_all: bool) -> None:
        self._isolated_all = isolated_all

    def __call__(self, containers, inspector):  # noqa: ANN001
        names = list(containers) if containers else []
        if self._isolated_all:
            return IsolationResult(
                passed=True,
                verdicts=tuple(
                    ContainerIsolationVerdict(container=c, isolated=True, reasons=())
                    for c in names
                ),
            )
        offenders = names or ["nginx"]
        return IsolationResult(
            passed=False,
            verdicts=tuple(
                ContainerIsolationVerdict(
                    container=c, isolated=False, reasons=("bridge externa",)
                )
                for c in offenders
            ),
        )


def _runner_factory(caldera: CalderaClient, verifier):
    def _factory(session: Session) -> OperationRunner:
        return OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=verifier,
        )

    return _factory


# ---------------------------------------------------------------------------
# Generated inputs: a destination choice + an isolation state
# ---------------------------------------------------------------------------

# ``kind`` selects how the single command's destination is classified:
#   "internal"      -> an internal lab IP (contained → allowed by containment)
#   "external_ip"   -> a public IP (external → refused by containment)
#   "external_host" -> a bare hostname (external by definition → refused)
_DESTINATION_KINDS = ("internal", "external_ip", "external_host")


def _destination_strategy() -> st.SearchStrategy[tuple[str, str]]:
    """Yield ``(kind, command)`` with exactly one destination of that kind."""
    internal = st.sampled_from(_INTERNAL_IPS).map(
        lambda ip: ("internal", f"curl http://{ip}/x")
    )
    external_ip = st.sampled_from(_EXTERNAL_IPS).map(
        lambda ip: ("external_ip", f"curl http://{ip}/x")
    )
    # A bare hostname token (``nmap nmap.org``) is NOT extracted by the parser;
    # only URL / user@host forms carry a hostname destination. Use a URL so the
    # hostname is a real, extracted EXTERNAL destination (HOST kind).
    external_host = st.sampled_from(_EXTERNAL_HOSTS).map(
        lambda h: ("external_host", f"curl http://{h}/x")
    )
    return st.one_of(internal, external_ip, external_host)


def _deve_executar(dest_kind: str, isolated_all: bool) -> bool:
    """Independent oracle: execution is allowed IFF internal destination AND isolated."""
    return dest_kind == "internal" and isolated_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage6_state(session: Session, campaign: str) -> StageState | None:
    row = (
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == campaign)
            .where(StageRun.stage == ESTAGIO_EXECUCAO)
        )
        .scalars()
        .first()
    )
    return row.state if row is not None else None


def _operation_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(Operation)).scalar() or 0


def _run_once(
    session: Session, data_dir: Path, *, command: str, isolated_all: bool
):
    """Seed the case, wire a REAL runner with fakes, and execute Stage 6 once."""
    _seed_case(data_dir, _SLUG, command=command)
    service = ExecutionStageService(
        session,
        runner_factory=_runner_factory(
            _healthy_caldera(), _ConfigurableVerifier(isolated_all=isolated_all)
        ),
        case_service=CaseService(data_dir=data_dir),
    )
    return service.execute(_SLUG, confirmed=True)


# ---------------------------------------------------------------------------
# Property 7 (main): fail-closed over any mix of destination + isolation
# ---------------------------------------------------------------------------


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(destination=_destination_strategy(), isolated_all=st.booleans())
def test_property7_no_execution_with_external_or_unisolated(
    destination: tuple[str, str],
    isolated_all: bool,
) -> None:
    """No Operation starts if ANY destination is external OR any target not isolated.

    # Feature: pipeline-seis-stages, Property 7: Nenhuma execução com destino
    # external ou contêiner não isolated (segurança).
    Validates: Requirements 10.2, 13.4, 13.5, 14.2, 14.4, 14.5, 9.6, 9.7
    """
    dest_kind, command = destination
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio6_seg_"))
    try:
        with _ephemeral_session() as session:
            result = _run_once(
                session, tmp, command=command, isolated_all=isolated_all
            )

            # Independent oracle: only internal destination AND full isolation runs.
            expected_run = _deve_executar(dest_kind, isolated_all)

            assert _operation_count(session) == (1 if expected_run else 0)
            assert result.started is expected_run

            if expected_run:
                # Happy path: the single execution completed and concluded Stage 6.
                assert result.result is RunOutcome.COMPLETED
                assert result.state is OperationState.FINISHED
                assert result.completed is True
                assert _stage6_state(session, _SLUG) is StageState.COMPLETED
            else:
                # Fail-closed: refused/aborted pre-flight, nothing started/concluded.
                assert result.completed is False
                assert _stage6_state(session, _SLUG) is None
                # Containment runs BEFORE isolation: an external destination
                # refuses first regardless of the isolation state (gate order of
                # the design). An internal-but-not-isolated destination aborts at
                # the isolation gate instead.
                if dest_kind != "internal":
                    assert result.result is RunOutcome.CONTAINMENT_REFUSED
                    assert result.violations  # offending ability/command surfaced
                else:
                    assert result.result is RunOutcome.ISOLATION_FAILED
                    assert result.non_isolated_containers
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 7 (targeted): the ONLY safe combination always executes
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(internal_ip=st.sampled_from(_INTERNAL_IPS))
def test_property7_internal_and_isolated_always_executes(
    internal_ip: str,
) -> None:
    """Internal destination AND fully isolated is the only combination that runs.

    # Feature: pipeline-seis-stages, Property 7: Nenhuma execução com destino
    # external ou contêiner não isolated (segurança).
    Validates: Requirements 10.2, 14.4, 14.5
    """
    # Oracle sanity: this combination is exactly the one allowed to execute.
    assert _deve_executar("internal", True) is True

    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio6_seg_"))
    try:
        with _ephemeral_session() as session:
            result = _run_once(
                session,
                tmp,
                command=f"curl http://{internal_ip}/x",
                isolated_all=True,
            )

            assert result.result is RunOutcome.COMPLETED
            assert result.started is True
            assert result.completed is True
            assert _operation_count(session) == 1
            assert _stage6_state(session, _SLUG) is StageState.COMPLETED
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
