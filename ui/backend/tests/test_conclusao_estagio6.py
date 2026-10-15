"""Example-based tests for ``ConclusaoEstagio6Service`` — teardown + conclusão (task 8.4).

A costura de conclusão do Estágio 6 amarra as três fases na ordem do design:
executa (conclusão deferida) → mede/gera o relatório → **SEMPRE** desprovisiona
→ conclui o Estágio 6 **somente** quando a execução concluiu, o ambiente foi
desprovisionado e os resultados + relatório estão persistidos. Estes testes
provam:

1. **Caminho feliz**: COMPLETED + FINISHED → teardown chamado, relatório marca
   ``ambiente_desprovisionado=True``, relatório persistido, Estágio 6 concluído.
2. **Falha de técnicas (ABORTED)**: o teardown é **ainda assim** chamado (Req.
   10.8, 12.1); o relatório é gerado; o Estágio 6 **não** conclui (execução não
   concluiu).
3. **Recusa por gate (NOT_CONFIRMED)**: o teardown é chamado mesmo sem execução;
   o Estágio 6 não conclui.
4. **Falha de desprovisionamento**: ``desprovisionado=False`` reporta os recursos
   remanescentes (Req. 12.3), o relatório marca ``ambiente_desprovisionado=False``
   e o Estágio 6 **não** conclui, mesmo com execução COMPLETED.
5. **Exceção do provisionador**: capturada e virada em falha de teardown que
   identifica os recursos remanescentes — o fluxo não é derrubado.
6. **Relatório não persistido**: o Estágio 6 não conclui mesmo com execução
   COMPLETED e teardown bem-sucedido (Req. 10.9).

Tudo externo é *mockado*: um runner falso (projeção/single-call), um
provisionador *fake* que registra o teardown, e um sink de relatório em memória —
sem Caldera, Docker nem a tabela ``relatorios_execucao``. A Base_de_Dados é um
SQLite temporário.

_Requisitos: 10.8, 10.9, 12.1, 12.2, 12.3_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StageRun
from app.models.enums import (
    AbilityResultStatus,
    OperationState,
    StageState,
    TranslationSource,
)
from app.services.case_service import CaseService
from app.services.environment.conclusao import ConclusaoEstagio6Service
from app.services.environment.provisionador import (
    AmbienteProvisionado,
    DesprovisionamentoResult,
)
from app.services.environment.relatorio import RelatorioExecucao
from app.services.operation.runner import (
    AbilityRunResult,
    OperationRunResult,
    RunOutcome,
)
from app.services.stage.stage_service import ESTAGIO_EXECUCAO


_SLUG = "shadowray"  # a registered curated slug (CURATED_CASES)


# ---------------------------------------------------------------------------
# Temp fixtures (curated case + ephemeral SQLite)
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_case(data_dir: Path, slug: str, *, command: str = "cat /etc/passwd") -> None:
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


@pytest.fixture()
def data_dir() -> Iterator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_conclusao6_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def session() -> Iterator[Session]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_conclusao6_db_"))
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
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FixedRunner:
    """A fake ``OperationRunner`` returning a fixed result (one call)."""

    result: OperationRunResult
    calls: int = 0

    def run(self, **kwargs: object) -> OperationRunResult:
        self.calls += 1
        return self.result


@dataclass
class _RecordingProvisioner:
    """A fake ``ProvisionadorDeAmbiente`` recording teardown calls.

    Only exercises ``origem`` + ``desprovisionar`` (the Stage-6 teardown path);
    the other Protocol methods are unused by this seam. ``resultado`` is what
    ``desprovisionar`` returns; ``raise_exc`` makes it blow up instead (to prove
    the seam catches it).
    """

    resultado: DesprovisionamentoResult
    raise_exc: bool = False
    torn_down: list[AmbienteProvisionado] = field(default_factory=list)

    def origem(self) -> TranslationSource:
        return TranslationSource.HUMAN_CURATION

    def desprovisionar(
        self, ambiente: AmbienteProvisionado
    ) -> DesprovisionamentoResult:
        self.torn_down.append(ambiente)
        if self.raise_exc:
            raise RuntimeError("docker daemon unreachable during teardown")
        return self.resultado


class _MemorySink:
    """An in-memory ``RelatorioSink`` recording persisted reports.

    ``ok`` controls whether the sink confirms persistence (``True``) — the
    conclusion gate requires a persisted report (Req. 10.9).
    """

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.saved: list[RelatorioExecucao] = []

    def __call__(self, relatorio: RelatorioExecucao) -> bool:
        self.saved.append(relatorio)
        return self.ok


def _ambiente() -> AmbienteProvisionado:
    return AmbienteProvisionado(
        caso_id=_SLUG,
        containers_alvo=["victim-web", "victim-db"],
        abilities_carregadas=True,
    )


def _ok_teardown() -> DesprovisionamentoResult:
    return DesprovisionamentoResult(desprovisionado=True, mensagem="ok")


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


def _aborted_result() -> OperationRunResult:
    return OperationRunResult(
        outcome=RunOutcome.ABORTED,
        state=OperationState.ABORTED,
        operation_id=1,
        ability_results=(
            AbilityRunResult(
                ability_id=f"{_SLUG}-ab-1",
                status=AbilityResultStatus.FAILURE,
                command="cat /etc/passwd",
                container_destino="host executor (comando local)",
            ),
        ),
        message="abortada: trilha de auditoria",
    )


def _not_confirmed_result() -> OperationRunResult:
    return OperationRunResult(
        outcome=RunOutcome.NOT_CONFIRMED,
        state=OperationState.NOT_STARTED,
        message="não confirmada",
    )


def _service(
    session: Session,
    data_dir: Path,
    *,
    runner_result: OperationRunResult,
    provisioner: _RecordingProvisioner,
    sink: _MemorySink,
) -> ConclusaoEstagio6Service:
    return ConclusaoEstagio6Service(
        session,
        runner_factory=lambda _s: _FixedRunner(result=runner_result),  # type: ignore[arg-type,return-value]
        provisionador=provisioner,  # type: ignore[arg-type]
        relatorio_sink=sink,
        case_service=CaseService(data_dir=data_dir),
    )


def _stage6_state(session: Session, caso: str) -> StageState | None:
    row = (
        session.execute(
            select(StageRun)
            .where(StageRun.caso_id == caso)
            .where(StageRun.estagio == ESTAGIO_EXECUCAO)
        )
        .scalars()
        .first()
    )
    return row.estado if row is not None else None


# ---------------------------------------------------------------------------
# Happy path: execution concludes, teardown runs, Stage 6 concludes
# ---------------------------------------------------------------------------


def test_happy_path_tears_down_and_concludes(
    session: Session, data_dir: Path
) -> None:
    """COMPLETED+FINISHED -> teardown ran, report persisted, Stage 6 concluded.
    _Requisitos: 10.8, 10.9, 12.1, 12.2_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(resultado=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    ambiente = _ambiente()
    result = service.executar_e_concluir(_SLUG, confirmado=True, ambiente=ambiente)

    # Teardown was invoked with the provisioned environment (Req. 12.1, 12.2).
    assert provisioner.torn_down == [ambiente]
    assert result.desprovisionamento.desprovisionado is True
    # The report reflects the teardown and was persisted (Req. 12, 10.9).
    assert result.relatorio.ambiente_desprovisionado is True
    assert result.relatorio_persistido is True
    assert sink.saved and sink.saved[0].caso_id == _SLUG
    # Stage 6 concluded only after execution + teardown + persistence (Req. 10.9).
    assert result.concluido is True
    assert _stage6_state(session, _SLUG) is StageState.COMPLETED


# ---------------------------------------------------------------------------
# ALWAYS deprovision — even on technique failure / refusal (Req. 10.8, 12.1)
# ---------------------------------------------------------------------------


def test_aborted_execution_still_tears_down_but_does_not_conclude(
    session: Session, data_dir: Path
) -> None:
    """ABORTED execution still tears down; Stage 6 does NOT conclude.
    _Requisitos: 10.8, 12.1_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(resultado=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_aborted_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.executar_e_concluir(
        _SLUG, confirmado=True, ambiente=_ambiente()
    )

    # Teardown ran DESPITE the failed techniques (Req. 10.8, 12.1).
    assert len(provisioner.torn_down) == 1
    assert result.desprovisionamento.desprovisionado is True
    # A report is still generated (Req. 10.6, 10.7).
    assert result.relatorio.caso_id == _SLUG
    # Execution did not conclude -> Stage 6 does not conclude.
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


def test_not_confirmed_still_tears_down(session: Session, data_dir: Path) -> None:
    """A refused (not-confirmed) execution still tears the environment down.
    _Requisitos: 10.8, 12.1_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(resultado=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_not_confirmed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.executar_e_concluir(
        _SLUG, confirmado=False, ambiente=_ambiente()
    )

    assert len(provisioner.torn_down) == 1
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


# ---------------------------------------------------------------------------
# Teardown failure reports remaining resources and blocks conclusion
# ---------------------------------------------------------------------------


def test_teardown_failure_reports_remaining_resources_and_blocks_conclusion(
    session: Session, data_dir: Path
) -> None:
    """A failed teardown surfaces remaining resources and blocks conclusion.
    _Requisitos: 12.3, 10.9_"""
    _seed_case(data_dir, _SLUG)
    failed_teardown = DesprovisionamentoResult(
        desprovisionado=False,
        recursos_remanescentes=["victim-web", "victim-db"],
        mensagem="falha ao remover contêineres",
    )
    provisioner = _RecordingProvisioner(resultado=failed_teardown)
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.executar_e_concluir(
        _SLUG, confirmado=True, ambiente=_ambiente()
    )

    # The remaining resources are identified (Req. 12.3).
    assert result.desprovisionamento.desprovisionado is False
    assert result.desprovisionamento.recursos_remanescentes == [
        "victim-web",
        "victim-db",
    ]
    # The report reflects the environment was NOT deprovisioned.
    assert result.relatorio.ambiente_desprovisionado is False
    # Even with a COMPLETED execution, a failed teardown blocks conclusion (10.9).
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


def test_teardown_exception_is_caught_and_reported(
    session: Session, data_dir: Path
) -> None:
    """An exception from the provisioner is caught and reported as a teardown failure.
    _Requisitos: 12.3_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(resultado=_ok_teardown(), raise_exc=True)
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.executar_e_concluir(
        _SLUG, confirmado=True, ambiente=_ambiente()
    )

    # The exception did not propagate; it became a reported failure with the
    # remaining resources identified (the environment's target containers).
    assert result.desprovisionamento.desprovisionado is False
    assert set(result.desprovisionamento.recursos_remanescentes) == {
        "victim-web",
        "victim-db",
    }
    assert result.concluido is False


# ---------------------------------------------------------------------------
# Report persistence is a conclusion pre-condition (Req. 10.9)
# ---------------------------------------------------------------------------


def test_unpersisted_report_blocks_conclusion(
    session: Session, data_dir: Path
) -> None:
    """A report that fails to persist blocks conclusion despite success+teardown.
    _Requisitos: 10.9_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(resultado=_ok_teardown())
    sink = _MemorySink(ok=False)  # the sink refuses to persist
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.executar_e_concluir(
        _SLUG, confirmado=True, ambiente=_ambiente()
    )

    assert result.desprovisionamento.desprovisionado is True
    assert result.relatorio_persistido is False
    assert result.concluido is False
    assert _stage6_state(session, _SLUG) is None


# ---------------------------------------------------------------------------
# Unknown case
# ---------------------------------------------------------------------------


def test_unknown_case_raises_keyerror(session: Session, data_dir: Path) -> None:
    """An unknown slug raises KeyError (mapped to 404 by the endpoint layer)."""
    provisioner = _RecordingProvisioner(resultado=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )
    with pytest.raises(KeyError):
        service.executar_e_concluir(
            "does-not-exist", confirmado=True, ambiente=_ambiente()
        )
