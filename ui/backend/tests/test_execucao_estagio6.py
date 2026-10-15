"""Example-based tests for ``ExecucaoStageService`` — Execução do Estágio 6 (task 8.2).

A costura da Execução do Estágio 6 **reusa** o ``OperationRunner`` com os quatro
gates (confirmação → contenção → isolamento → disponibilidade), execução ÚNICA
(sem retry — Req. 18.3), persistência por comando (``AbilityResult`` +
``AuditLogEntry``) e o **preview** como pré-condição visível. Estes testes provam:

1. ``previsualizar`` apresenta os comandos concretos + container-alvo **sem
   executar** nada (Req. 13.6, 13.7).
2. ``executar`` chama ``OperationRunner.run`` **exatamente uma vez** por
   solicitação — sem laço de retry (Req. 10.3, 18.3).
3. Cada :class:`RunOutcome` do runner é projetado corretamente e **só**
   ``COMPLETED`` + ``FINISHED`` conclui o Estágio 6 (Req. 13.1, 13.2, 13.4).
4. Com um ``OperationRunner`` **real** + *fakes* injetados (Caldera via
   ``MockTransport``, verificador de isolamento falso), os quatro gates de fato
   rodam na ordem do design: não-confirmada / destino externo / não isolado não
   iniciam; o caminho feliz conclui, persiste ``AbilityResult``/``AuditLogEntry``
   por comando e marca o Estágio 6 ``concluido``.

Tudo externo é *mockado*: um runner falso (projeção/single-call), e no teste de
integração um ``OperationRunner`` real com Caldera ``MockTransport`` + inspector
falso — sem Caldera nem Docker reais. A Base_de_Dados é um SQLite temporário.

_Requisitos: 10.1, 10.2, 10.3, 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 14.4,
14.5, 18.3_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AbilityResult, AuditLogEntry, Operation, StageRun
from app.models.enums import AbilityResultStatus, OperationState, StageState
from app.services.audit.audit_logger import AuditLogger
from app.services.caldera import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    HEALTH_PATH,
    OPERATIONS_PATH,
    CalderaClient,
    CalderaUnavailable,
)
from app.services.case_service import CaseService
from app.services.containment.isolation import (
    ContainerIsolationVerdict,
    IsolationResult,
)
from app.services.environment.execucao import ExecucaoStageService
from app.services.operation.runner import (
    AbilityRunResult,
    OperationRunner,
    OperationRunResult,
    RunOutcome,
)
from app.services.stage.stage_service import ESTAGIO_EXECUCAO


_SLUG = "shadowray"  # a registered curated slug (CURATED_CASES)


# ---------------------------------------------------------------------------
# Curated-case temp fixtures (read-only w.r.t. real data)
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(
    data_dir: Path,
    slug: str,
    *,
    command: str = "cat /etc/passwd",
    with_adversary: bool = True,
) -> None:
    """Seed a minimal curated case with a single internal/local ability command."""
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
    if with_adversary:
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


@pytest.fixture()
def data_dir() -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio6_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def session() -> Iterator[Session]:
    """An ephemeral temp-file SQLite session (torn down per test)."""
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_estagio6_db_"))
    db_path = tmp / "test.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    engine = create_db_engine(settings)
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


# ---------------------------------------------------------------------------
# Fakes for the projection/single-call tests (no real runner)
# ---------------------------------------------------------------------------


@dataclass
class RecordingRunner:
    """A fake ``OperationRunner`` that records calls and returns a fixed result."""

    result: OperationRunResult
    calls: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def run(self, **kwargs: object) -> OperationRunResult:
        self.calls.append(dict(kwargs))
        return self.result


def _service_with(session: Session, data_dir: Path, result: OperationRunResult):
    """Build the service wired to a RecordingRunner returning ``result``."""
    runner = RecordingRunner(result=result)
    service = ExecucaoStageService(
        session,
        runner_factory=lambda _s: runner,  # type: ignore[arg-type,return-value]
        case_service=CaseService(data_dir=data_dir),
    )
    return service, runner


def _completed_result() -> OperationRunResult:
    return OperationRunResult(
        outcome=RunOutcome.COMPLETED,
        state=OperationState.FINISHED,
        operation_id=1,
        caldera_operation_id="op-1",
        total_sucesso=1,
        total_falha=0,
        ability_results=(
            AbilityRunResult(
                ability_id=f"{_SLUG}-ab-1",
                status=AbilityResultStatus.SUCCESS,
                command="cat /etc/passwd",
                container_destino="host executor (comando local)",
                output="root:x:0:0",
            ),
        ),
        message="Operação finalizada: 1 sucesso(s), 0 falha(s).",
    )


# ---------------------------------------------------------------------------
# Preview is a pure pre-condition (Req. 13.6, 13.7)
# ---------------------------------------------------------------------------


def test_previsualizar_presents_commands_without_executing(
    session: Session, data_dir: Path
) -> None:
    """``previsualizar`` returns the concrete commands and executes nothing.
    _Requisitos: 13.6, 13.7_"""
    _seed_case(data_dir, _SLUG, command="curl http://192.168.20.30/x")
    service, runner = _service_with(session, data_dir, _completed_result())

    preview = service.previsualizar(_SLUG)

    # The complete command list is presented (one ability, one command).
    assert len(preview.abilities) == 1
    assert preview.abilities[0].commands[0].command == "curl http://192.168.20.30/x"
    # Nothing executed: the runner was never built/called and no Operation row.
    assert runner.calls == []
    assert session.execute(select(func.count()).select_from(Operation)).scalar() == 0


# ---------------------------------------------------------------------------
# Single execution — run() called exactly once, no retry (Req. 10.3, 18.3)
# ---------------------------------------------------------------------------


def test_executar_calls_runner_exactly_once(session: Session, data_dir: Path) -> None:
    """One execution request -> exactly one ``run(...)`` call (no retry).
    _Requisitos: 10.3, 18.3_"""
    _seed_case(data_dir, _SLUG)
    service, runner = _service_with(session, data_dir, _completed_result())

    service.executar(_SLUG, confirmado=True)

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["caso_id"] == _SLUG
    assert call["confirmado"] is True
    assert call["adversary_id"] == f"{_SLUG}-adv"
    # The adversary payload carries the atomic_ordering (DAG order, Req. 10.3).
    assert call["adversary_payload"]["adversary_id"] == f"{_SLUG}-adv"
    assert call["adversary_payload"]["atomic_ordering"] == [f"{_SLUG}-ab-1"]


# ---------------------------------------------------------------------------
# Outcome projection + conclusion gating (Req. 13.1, 13.2, 13.4)
# ---------------------------------------------------------------------------


def _stage6_state(session: Session, caso: str) -> StageState | None:
    row = session.execute(
        select(StageRun)
        .where(StageRun.caso_id == caso)
        .where(StageRun.estagio == ESTAGIO_EXECUCAO)
    ).scalars().first()
    return row.estado if row is not None else None


def test_executar_completed_concludes_stage6(session: Session, data_dir: Path) -> None:
    """COMPLETED + FINISHED concludes Stage 6 via the single completion path.
    _Requisitos: 13.1_"""
    _seed_case(data_dir, _SLUG)
    service, _ = _service_with(session, data_dir, _completed_result())

    result = service.executar(_SLUG, confirmado=True)

    assert result.iniciada is True
    assert result.resultado is RunOutcome.COMPLETED
    assert result.total_sucesso == 1
    assert result.concluido is True
    assert result.conclusao_persistida is True
    assert _stage6_state(session, _SLUG) is StageState.COMPLETED


def test_executar_not_confirmed_does_not_conclude(
    session: Session, data_dir: Path
) -> None:
    """NOT_CONFIRMED runs nothing and does not conclude Stage 6 (Req. 13.2)."""
    _seed_case(data_dir, _SLUG)
    not_confirmed = OperationRunResult(
        outcome=RunOutcome.NOT_CONFIRMED,
        state=OperationState.NOT_STARTED,
        message="não confirmada",
    )
    service, _ = _service_with(session, data_dir, not_confirmed)

    result = service.executar(_SLUG, confirmado=False)

    assert result.iniciada is False
    assert result.resultado is RunOutcome.NOT_CONFIRMED
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


def test_executar_containment_refused_surfaces_violations(
    session: Session, data_dir: Path
) -> None:
    """CONTAINMENT_REFUSED surfaces the violations and does not conclude (Req. 13.4)."""
    _seed_case(data_dir, _SLUG)

    @dataclass
    class _Report:
        refused: bool = True

        def describe_violations(self) -> list[str]:
            return ["ability-x: destino externo 8.8.8.8"]

    refused = OperationRunResult(
        outcome=RunOutcome.CONTAINMENT_REFUSED,
        state=OperationState.NOT_STARTED,
        containment=_Report(),  # type: ignore[arg-type]
        message="recusada por contenção",
    )
    service, _ = _service_with(session, data_dir, refused)

    result = service.executar(_SLUG, confirmado=True)

    assert result.resultado is RunOutcome.CONTAINMENT_REFUSED
    assert result.violacoes == ["ability-x: destino externo 8.8.8.8"]
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


def test_executar_isolation_failed_surfaces_containers(
    session: Session, data_dir: Path
) -> None:
    """ISOLATION_FAILED surfaces the offending containers and does not conclude."""
    _seed_case(data_dir, _SLUG)
    isolation = IsolationResult(
        passed=False,
        verdicts=(
            ContainerIsolationVerdict(
                container="nginx", isolated=False, reasons=("bridge externa",)
            ),
        ),
    )
    failed = OperationRunResult(
        outcome=RunOutcome.ISOLATION_FAILED,
        state=OperationState.NOT_STARTED,
        isolation=isolation,
        message="isolamento falhou",
    )
    service, _ = _service_with(session, data_dir, failed)

    result = service.executar(_SLUG, confirmado=True)

    assert result.resultado is RunOutcome.ISOLATION_FAILED
    assert result.containers_nao_isolados == ["nginx"]
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


def test_executar_aborted_does_not_conclude(session: Session, data_dir: Path) -> None:
    """ABORTED (audit-trail failure) is a run but does not conclude Stage 6 (Req. 10.2)."""
    _seed_case(data_dir, _SLUG)
    aborted = OperationRunResult(
        outcome=RunOutcome.ABORTED,
        state=OperationState.ABORTED,
        operation_id=1,
        message="abortada: trilha de auditoria",
    )
    service, _ = _service_with(session, data_dir, aborted)

    result = service.executar(_SLUG, confirmado=True)

    assert result.iniciada is True  # it started executing
    assert result.resultado is RunOutcome.ABORTED
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


def test_executar_unknown_case_raises_keyerror(
    session: Session, data_dir: Path
) -> None:
    """An unknown slug raises KeyError (mapped to 404 by the endpoint layer)."""
    service, _ = _service_with(session, data_dir, _completed_result())
    with pytest.raises(KeyError):
        service.executar("does-not-exist", confirmado=True)


# ---------------------------------------------------------------------------
# Integration: a REAL OperationRunner + injected fakes proves the four gates
# genuinely run through this seam (no real Caldera / Docker).
# ---------------------------------------------------------------------------


class _AllIsolated:
    """A verifier callable returning an approved IsolationResult."""

    def __call__(self, containers, inspector):  # noqa: ANN001
        return IsolationResult(
            passed=True,
            verdicts=tuple(
                ContainerIsolationVerdict(container=c, isolated=True, reasons=())
                for c in containers
            ),
        )


class _NoneIsolated:
    """A verifier callable returning a failed IsolationResult (not isolated)."""

    def __call__(self, containers, inspector):  # noqa: ANN001
        return IsolationResult(
            passed=False,
            verdicts=tuple(
                ContainerIsolationVerdict(
                    container=c, isolated=False, reasons=("bridge externa",)
                )
                for c in (containers or ["nginx"])
            ),
        )


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
                            "command": "Y2F0IC9ldGMvcGFzc3dk",  # base64("cat /etc/passwd")
                            "output": "root:x:0:0",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={})

    return CalderaClient(transport=httpx.MockTransport(handler))


def _runner_factory(caldera: CalderaClient, verifier):
    def _factory(session: Session) -> OperationRunner:
        return OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
            isolation_verifier=verifier,
        )

    return _factory


def test_real_runner_not_confirmed_executes_nothing(
    session: Session, data_dir: Path
) -> None:
    """Gate 1 via the seam: confirmado=False starts nothing (no Operation row).
    _Requisitos: 13.2_"""
    _seed_case(data_dir, _SLUG, command="cat /etc/passwd")
    caldera = _healthy_caldera()
    service = ExecucaoStageService(
        session,
        runner_factory=_runner_factory(caldera, _AllIsolated()),
        case_service=CaseService(data_dir=data_dir),
    )

    result = service.executar(_SLUG, confirmado=False)

    assert result.resultado is RunOutcome.NOT_CONFIRMED
    assert session.execute(select(func.count()).select_from(Operation)).scalar() == 0


def test_real_runner_external_destination_refused(
    session: Session, data_dir: Path
) -> None:
    """Gate 2 via the seam: an external destination refuses before any run.
    _Requisitos: 13.3, 13.4, 13.5_"""
    _seed_case(data_dir, _SLUG, command="curl http://8.8.8.8/x")
    caldera = _healthy_caldera()
    service = ExecucaoStageService(
        session,
        runner_factory=_runner_factory(caldera, _AllIsolated()),
        case_service=CaseService(data_dir=data_dir),
    )

    result = service.executar(_SLUG, confirmado=True)

    assert result.resultado is RunOutcome.CONTAINMENT_REFUSED
    assert result.violacoes  # the offending ability/command is identified
    assert session.execute(select(func.count()).select_from(Operation)).scalar() == 0
    assert _stage6_state(session, _SLUG) is None


def test_real_runner_not_isolated_aborts(session: Session, data_dir: Path) -> None:
    """Gate 3 via the seam: a non-isolated target container aborts before run.
    _Requisitos: 14.4, 14.5_"""
    _seed_case(data_dir, _SLUG, command="curl http://192.168.20.30/x")
    caldera = _healthy_caldera()
    service = ExecucaoStageService(
        session,
        runner_factory=_runner_factory(caldera, _NoneIsolated()),
        case_service=CaseService(data_dir=data_dir),
    )

    result = service.executar(_SLUG, confirmado=True)

    assert result.resultado is RunOutcome.ISOLATION_FAILED
    assert result.containers_nao_isolados
    assert session.execute(select(func.count()).select_from(Operation)).scalar() == 0
    assert _stage6_state(session, _SLUG) is None


def test_real_runner_happy_path_runs_persists_and_concludes(
    session: Session, data_dir: Path
) -> None:
    """All gates pass -> single execution persists per-command rows + concludes.
    _Requisitos: 10.1, 10.2, 10.3, 13.1_"""
    _seed_case(data_dir, _SLUG, command="cat /etc/passwd")
    caldera = _healthy_caldera()
    service = ExecucaoStageService(
        session,
        runner_factory=_runner_factory(caldera, _AllIsolated()),
        case_service=CaseService(data_dir=data_dir),
    )

    result = service.executar(_SLUG, confirmado=True)

    assert result.resultado is RunOutcome.COMPLETED
    assert result.estado is OperationState.FINISHED
    assert result.total_sucesso == 1
    assert result.concluido is True
    # The runner persisted one Operation, one AbilityResult and one audit row.
    assert session.execute(select(func.count()).select_from(Operation)).scalar() == 1
    assert (
        session.execute(select(func.count()).select_from(AbilityResult)).scalar() == 1
    )
    assert (
        session.execute(select(func.count()).select_from(AuditLogEntry)).scalar() == 1
    )
    assert _stage6_state(session, _SLUG) is StageState.COMPLETED
