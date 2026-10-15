"""Example (non-property) tests for the Stage 6 metrics/report DTOs (task 8.1).

Verificam, por exemplos diretos, os DTOs novos da Saída do Estágio 6 (Execução +
medição + relatório) definidos em ``app.services.environment.relatorio`` (Req.
15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8):

1. :class:`MetricaTecnica` — campos e defaults; o ``status`` reusa
   :class:`~app.models.enums.AbilityResultStatus` e serializa em pt-BR.
2. :class:`MetricasSuficiencia` — defaults e serialização, incluindo a lista
   ``por_tecnica`` aninhada.
3. :class:`RelatorioExecucao` — consolida as métricas, os
   :class:`~app.services.stage.stage_service.Stage3AbilityResult` reusados e o
   flag de desprovisionamento (Req. 12), serializando em pt-BR.

_Requisitos: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8_
"""

from __future__ import annotations

import datetime as dt

from app.models.enums import AbilityResultStatus
from app.services.environment import (
    MetricasSuficiencia,
    MetricaTecnica,
    RelatorioExecucao,
)
from app.services.stage.stage_service import Stage3AbilityResult


def test_metrica_tecnica_defaults() -> None:
    metrica = MetricaTecnica()
    assert metrica.technique_id is None
    assert metrica.node_id is None
    assert metrica.status is AbilityResultStatus.PENDING
    assert metrica.etapa_falha is None
    assert metrica.tempo_execucao_s is None
    assert metrica.telemetria == []


def test_metrica_tecnica_full_and_serialization() -> None:
    metrica = MetricaTecnica(
        technique_id="T1059",
        node_id="n1",
        status=AbilityResultStatus.FAILURE,
        etapa_falha="execucao",
        tempo_execucao_s=1.5,
        telemetria=["processo", "rede"],
    )
    dumped = metrica.model_dump(mode="json")
    assert dumped["technique_id"] == "T1059"
    assert dumped["node_id"] == "n1"
    assert dumped["status"] == "falha"
    assert dumped["etapa_falha"] == "execucao"
    assert dumped["tempo_execucao_s"] == 1.5
    assert dumped["telemetria"] == ["processo", "rede"]


def test_metricas_suficiencia_defaults() -> None:
    metricas = MetricasSuficiencia()
    assert metricas.total_tecnicas_dag == 0
    assert metricas.tecnicas_sucesso == 0
    assert metricas.taxa_sucesso == 0.0
    assert metricas.cobertura_procedural == 0.0
    assert metricas.tempo_total_s is None
    assert metricas.por_tecnica == []
    assert metricas.nos_falhos == []
    assert metricas.veredito_sucesso is False


def test_metricas_suficiencia_full_and_nested() -> None:
    metricas = MetricasSuficiencia(
        total_tecnicas_dag=4,
        tecnicas_sucesso=3,
        taxa_sucesso=0.75,
        cobertura_procedural=0.9,
        tempo_total_s=12.0,
        por_tecnica=[
            MetricaTecnica(technique_id="T1059", status=AbilityResultStatus.SUCCESS),
            MetricaTecnica(technique_id="T1105", status=AbilityResultStatus.FAILURE),
        ],
        nos_falhos=["n2"],
        veredito_sucesso=True,
    )
    dumped = metricas.model_dump(mode="json")
    assert dumped["total_tecnicas_dag"] == 4
    assert dumped["tecnicas_sucesso"] == 3
    assert dumped["taxa_sucesso"] == 0.75
    assert dumped["cobertura_procedural"] == 0.9
    assert dumped["tempo_total_s"] == 12.0
    assert dumped["por_tecnica"][0]["status"] == "sucesso"
    assert dumped["por_tecnica"][1]["status"] == "falha"
    assert dumped["nos_falhos"] == ["n2"]
    assert dumped["veredito_sucesso"] is True


def test_relatorio_execucao_requires_caso_metricas_and_gerado_em() -> None:
    gerado_em = dt.datetime(2024, 1, 2, 3, 4, 5)
    relatorio = RelatorioExecucao(
        caso_id="apt-x",
        metricas=MetricasSuficiencia(total_tecnicas_dag=1, tecnicas_sucesso=1, taxa_sucesso=1.0),
        gerado_em=gerado_em,
    )
    assert relatorio.caso_id == "apt-x"
    assert relatorio.metricas.taxa_sucesso == 1.0
    assert relatorio.resultados == []
    assert relatorio.ambiente_desprovisionado is False
    assert relatorio.gerado_em == gerado_em


def test_relatorio_execucao_full_and_serialization() -> None:
    relatorio = RelatorioExecucao(
        caso_id="apt-x",
        metricas=MetricasSuficiencia(
            total_tecnicas_dag=2,
            tecnicas_sucesso=1,
            taxa_sucesso=0.5,
            veredito_sucesso=False,
        ),
        resultados=[
            Stage3AbilityResult(
                ability_id="a1",
                name="Discovery",
                status=AbilityResultStatus.SUCCESS,
                saida_comando="ok",
            ),
            Stage3AbilityResult(
                ability_id="a2",
                status=AbilityResultStatus.FAILURE,
            ),
        ],
        ambiente_desprovisionado=True,
        gerado_em=dt.datetime(2024, 1, 2, 3, 4, 5),
    )
    dumped = relatorio.model_dump(mode="json")
    assert dumped["caso_id"] == "apt-x"
    assert dumped["metricas"]["taxa_sucesso"] == 0.5
    assert dumped["resultados"][0]["ability_id"] == "a1"
    assert dumped["resultados"][0]["status"] == "sucesso"
    assert dumped["resultados"][1]["status"] == "falha"
    assert dumped["ambiente_desprovisionado"] is True
    assert dumped["gerado_em"].startswith("2024-01-02T03:04:05")
