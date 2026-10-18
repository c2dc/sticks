"""Example (non-property) tests for the Stage 6 metrics/report DTOs (task 8.1).

Verificam, por exemplos diretos, os DTOs novos da Saída do Estágio 6 (Execução +
medição + relatório) definidos em ``app.services.environment.report`` (Req.
15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8):

1. :class:`TechniqueMetric` — campos e defaults; o ``status`` reusa
   :class:`~app.models.enums.AbilityResultStatus` e serializa em pt-BR.
2. :class:`SufficiencyMetrics` — defaults e serialização, incluindo a lista
   ``per_technique`` aninhada.
3. :class:`ExecutionReport` — consolida as métricas, os
   :class:`~app.services.stage.stage_service.Stage3AbilityResult` reusados e o
   flag de desprovisionamento (Req. 12), serializando em pt-BR.

_Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8_
"""

from __future__ import annotations

import datetime as dt

from app.models.enums import AbilityResultStatus
from app.services.environment import (
    SufficiencyMetrics,
    TechniqueMetric,
    ExecutionReport,
)
from app.services.stage.stage_service import Stage3AbilityResult


def test_metrica_tecnica_defaults() -> None:
    metric = TechniqueMetric()
    assert metric.technique_id is None
    assert metric.node_id is None
    assert metric.status is AbilityResultStatus.PENDING
    assert metric.failed_step is None
    assert metric.execution_time_s is None
    assert metric.telemetry == []


def test_metrica_tecnica_full_and_serialization() -> None:
    metric = TechniqueMetric(
        technique_id="T1059",
        node_id="n1",
        status=AbilityResultStatus.FAILURE,
        failed_step="execution",
        execution_time_s=1.5,
        telemetry=["processo", "rede"],
    )
    dumped = metric.model_dump(mode="json")
    assert dumped["technique_id"] == "T1059"
    assert dumped["node_id"] == "n1"
    assert dumped["status"] == "falha"
    assert dumped["failed_step"] == "execution"
    assert dumped["execution_time_s"] == 1.5
    assert dumped["telemetry"] == ["processo", "rede"]


def test_metricas_suficiencia_defaults() -> None:
    metrics = SufficiencyMetrics()
    assert metrics.total_dag_techniques == 0
    assert metrics.successful_techniques == 0
    assert metrics.success_rate == 0.0
    assert metrics.procedural_coverage == 0.0
    assert metrics.total_time_s is None
    assert metrics.per_technique == []
    assert metrics.failed_nodes == []
    assert metrics.success_verdict is False


def test_metricas_suficiencia_full_and_nested() -> None:
    metrics = SufficiencyMetrics(
        total_dag_techniques=4,
        successful_techniques=3,
        success_rate=0.75,
        procedural_coverage=0.9,
        total_time_s=12.0,
        per_technique=[
            TechniqueMetric(technique_id="T1059", status=AbilityResultStatus.SUCCESS),
            TechniqueMetric(technique_id="T1105", status=AbilityResultStatus.FAILURE),
        ],
        failed_nodes=["n2"],
        success_verdict=True,
    )
    dumped = metrics.model_dump(mode="json")
    assert dumped["total_dag_techniques"] == 4
    assert dumped["successful_techniques"] == 3
    assert dumped["success_rate"] == 0.75
    assert dumped["procedural_coverage"] == 0.9
    assert dumped["total_time_s"] == 12.0
    assert dumped["per_technique"][0]["status"] == "sucesso"
    assert dumped["per_technique"][1]["status"] == "falha"
    assert dumped["failed_nodes"] == ["n2"]
    assert dumped["success_verdict"] is True


def test_relatorio_execucao_requires_caso_metricas_and_gerado_em() -> None:
    generated_at = dt.datetime(2024, 1, 2, 3, 4, 5)
    report = ExecutionReport(
        campaign_id="apt-x",
        metrics=SufficiencyMetrics(total_dag_techniques=1, successful_techniques=1, success_rate=1.0),
        generated_at=generated_at,
    )
    assert report.campaign_id == "apt-x"
    assert report.metrics.success_rate == 1.0
    assert report.results == []
    assert report.environment_deprovisioned is False
    assert report.generated_at == generated_at


def test_relatorio_execucao_full_and_serialization() -> None:
    report = ExecutionReport(
        campaign_id="apt-x",
        metrics=SufficiencyMetrics(
            total_dag_techniques=2,
            successful_techniques=1,
            success_rate=0.5,
            success_verdict=False,
        ),
        results=[
            Stage3AbilityResult(
                ability_id="a1",
                name="Discovery",
                status=AbilityResultStatus.SUCCESS,
                command_output="ok",
            ),
            Stage3AbilityResult(
                ability_id="a2",
                status=AbilityResultStatus.FAILURE,
            ),
        ],
        environment_deprovisioned=True,
        generated_at=dt.datetime(2024, 1, 2, 3, 4, 5),
    )
    dumped = report.model_dump(mode="json")
    assert dumped["campaign_id"] == "apt-x"
    assert dumped["metrics"]["success_rate"] == 0.5
    assert dumped["results"][0]["ability_id"] == "a1"
    assert dumped["results"][0]["status"] == "sucesso"
    assert dumped["results"][1]["status"] == "falha"
    assert dumped["environment_deprovisioned"] is True
    assert dumped["generated_at"].startswith("2024-01-02T03:04:05")
