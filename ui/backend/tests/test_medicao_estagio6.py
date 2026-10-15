"""Example (non-property) tests for the Stage 6 measurement service (task 8.3).

Exercitam, por exemplos diretos, a medição de suficiência procedural
(:meth:`~app.services.environment.medicao.MedicaoService.medir`), a consolidação
no :class:`~app.services.environment.relatorio.RelatorioExecucao`
(:meth:`~app.services.environment.medicao.MedicaoService.consolidar`) e a ponte
de produção por Caso
(:meth:`~app.services.environment.medicao.MedicaoService.medir_do_caso`) (Req.
10.4–10.7, 15.1–15.7). A medição é **pura**: opera sobre o DAG curado + os
resultados por Ability + o mapa ``ability_id`` → ``technique_id`` em memória, sem
rede, Docker ou IA, então os exemplos não precisam de banco nem de disco.

Cobertura:

1. ``taxa_sucesso = tecnicas_sucesso / total_tecnicas_dag`` (Req. 15.1) e DAG
   vazio sem divisão por zero.
2. ``cobertura_procedural ∈ [0, 1]`` como a proporção de técnicas tentadas (Req.
   15.3).
3. ``nos_falhos`` com a etapa do DAG onde a falha ocorreu (Req. 15.2).
4. Tempo por técnica e tempo total (Req. 15.4) e telemetria por técnica (Req.
   15.5).
5. ``veredito_sucesso`` pelo critério definido (Req. 15.6, 10.5).
6. Consolidação em :class:`RelatorioExecucao` (Req. 10.6, 10.7, 15.7).
7. A ponte Ability → técnica via :meth:`MedicaoService.medir_do_caso` (produção).

_Requisitos: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
"""

from __future__ import annotations

import datetime as dt

from app.models.enums import AbilityResultStatus
from app.services.case_service import AbilityData, CaseData, DagData, DagNode
from app.services.environment.medicao import MedicaoService, ResultadoAbility
from app.services.environment.relatorio import MetricasSuficiencia
from app.services.stage.stage_service import Stage3AbilityResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dag(*nodes: DagNode) -> DagData:
    return DagData(campaign_name="demo", structural_nodes=list(nodes))


def _no(node_id: str, technique_id: str | None, **kwargs: object) -> DagNode:
    return DagNode(node_id=node_id, technique_id=technique_id, **kwargs)  # type: ignore[arg-type]


def _res(ability_id: str | None, status: AbilityResultStatus, **kwargs: object) -> ResultadoAbility:
    return ResultadoAbility(ability_id=ability_id, status=status, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Taxa de sucesso (Req. 15.1)
# ---------------------------------------------------------------------------


def test_taxa_sucesso_e_contagens() -> None:
    dag = _dag(
        _no("n1", "T1", node_type="recon"),
        _no("n2", "T2", node_type="exec"),
        _no("n3", "T3", node_type="exfil"),
        _no("n4", "T4", node_type="impact"),
    )
    mapa = {"a1": "T1", "a2": "T2", "a3": "T3", "a4": "T4"}
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.SUCCESS),
        _res("a3", AbilityResultStatus.SUCCESS),
        _res("a4", AbilityResultStatus.FAILURE),
    ]

    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )

    assert metricas.total_tecnicas_dag == 4
    assert metricas.tecnicas_sucesso == 3
    assert metricas.taxa_sucesso == 0.75


def test_taxa_sucesso_dag_vazio_sem_divisao_por_zero() -> None:
    metricas = MedicaoService().medir(
        resultados=[_res("a1", AbilityResultStatus.SUCCESS)],
        dag=_dag(),
        tecnica_por_ability={"a1": "T1"},
    )
    assert metricas.total_tecnicas_dag == 0
    assert metricas.taxa_sucesso == 0.0
    assert metricas.cobertura_procedural == 0.0
    assert metricas.veredito_sucesso is False


def test_dag_none_zera_as_metricas() -> None:
    metricas = MedicaoService().medir(
        resultados=[], dag=None, tecnica_por_ability={}
    )
    assert metricas == MetricasSuficiencia()


# ---------------------------------------------------------------------------
# Cobertura procedural ∈ [0, 1] (Req. 15.3)
# ---------------------------------------------------------------------------


def test_cobertura_procedural_proporcao_de_tentadas() -> None:
    dag = _dag(
        _no("n1", "T1"),
        _no("n2", "T2"),
        _no("n3", "T3"),
        _no("n4", "T4"),  # nunca tentada → não coberta
    )
    mapa = {"a1": "T1", "a2": "T2", "a3": "T3"}
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.FAILURE),
        _res("a3", AbilityResultStatus.RUNNING),  # tentada mas não concluída
    ]

    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )

    # 3 de 4 técnicas foram tentadas; a 4ª (T4) nunca foi.
    assert metricas.cobertura_procedural == 0.75
    assert 0.0 <= metricas.cobertura_procedural <= 1.0


def test_tecnica_duplicada_no_dag_conta_uma_vez() -> None:
    # T1 aparece em dois nós; conta como UMA técnica distinta.
    dag = _dag(_no("n1", "T1"), _no("n2", "T1"), _no("n3", "T2"))
    metricas = MedicaoService().medir(
        resultados=[_res("a1", AbilityResultStatus.SUCCESS)],
        dag=dag,
        tecnica_por_ability={"a1": "T1"},
    )
    assert metricas.total_tecnicas_dag == 2
    assert metricas.tecnicas_sucesso == 1
    assert metricas.taxa_sucesso == 0.5


# ---------------------------------------------------------------------------
# Nós falhos + etapa do DAG (Req. 15.2)
# ---------------------------------------------------------------------------


def test_nos_falhos_registram_etapa_do_dag() -> None:
    dag = _dag(
        _no("n1", "T1", node_type="recon"),
        _no("n2", "T2", node_type="exec"),
    )
    mapa = {"a1": "T1", "a2": "T2"}
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.FAILURE),
    ]

    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )

    assert metricas.nos_falhos == ["n2"]
    falha = next(m for m in metricas.por_tecnica if m.technique_id == "T2")
    assert falha.status is AbilityResultStatus.FAILURE
    assert falha.etapa_falha == "exec"
    assert falha.node_id == "n2"
    ok = next(m for m in metricas.por_tecnica if m.technique_id == "T1")
    assert ok.etapa_falha is None


def test_etapa_falha_cai_para_tactic_e_depois_indice() -> None:
    dag = _dag(
        _no("n1", "T1", tactic="execution"),  # sem node_type → usa tactic
        _no("n2", "T2", node_index=7),  # sem node_type/tactic → usa índice
    )
    mapa = {"a1": "T1", "a2": "T2"}
    resultados = [
        _res("a1", AbilityResultStatus.FAILURE),
        _res("a2", AbilityResultStatus.FAILURE),
    ]
    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )
    etapas = {m.technique_id: m.etapa_falha for m in metricas.por_tecnica}
    assert etapas["T1"] == "execution"
    assert etapas["T2"] == "no_7"


def test_falha_e_sucesso_na_mesma_tecnica_consolida_sucesso() -> None:
    # Uma técnica com duas Abilities: uma falha, outra sucesso ⇒ sucesso.
    dag = _dag(_no("n1", "T1"))
    mapa = {"a1": "T1", "a1b": "T1"}
    resultados = [
        _res("a1", AbilityResultStatus.FAILURE),
        _res("a1b", AbilityResultStatus.SUCCESS),
    ]
    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )
    assert metricas.tecnicas_sucesso == 1
    assert metricas.nos_falhos == []
    assert metricas.por_tecnica[0].status is AbilityResultStatus.SUCCESS


# ---------------------------------------------------------------------------
# Tempo por técnica / total (Req. 15.4) + telemetria (Req. 15.5)
# ---------------------------------------------------------------------------


def test_tempo_por_tecnica_e_total() -> None:
    dag = _dag(_no("n1", "T1"), _no("n2", "T2"))
    mapa = {"a1": "T1", "a1b": "T1", "a2": "T2"}
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS, tempo_execucao_s=1.5),
        _res("a1b", AbilityResultStatus.SUCCESS, tempo_execucao_s=0.5),  # mesma técnica
        _res("a2", AbilityResultStatus.SUCCESS, tempo_execucao_s=3.0),
    ]

    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )

    tempos = {m.technique_id: m.tempo_execucao_s for m in metricas.por_tecnica}
    assert tempos["T1"] == 2.0  # 1.5 + 0.5 somados na técnica
    assert tempos["T2"] == 3.0
    assert metricas.tempo_total_s == 5.0


def test_telemetria_por_tecnica_deduplicada() -> None:
    dag = _dag(_no("n1", "T1"))
    mapa = {"a1": "T1", "a1b": "T1"}
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS, telemetria=("processo", "rede")),
        _res("a1b", AbilityResultStatus.SUCCESS, telemetria=("rede", "autenticacao")),
    ]

    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )

    tecnica = metricas.por_tecnica[0]
    assert tecnica.telemetria == ["processo", "rede", "autenticacao"]


# ---------------------------------------------------------------------------
# Veredito geral (Req. 15.6, 10.5)
# ---------------------------------------------------------------------------


def test_veredito_sucesso_quando_todas_tecnicas_sucesso() -> None:
    dag = _dag(_no("n1", "T1"), _no("n2", "T2"))
    mapa = {"a1": "T1", "a2": "T2"}
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.SUCCESS),
    ]
    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )
    assert metricas.veredito_sucesso is True


def test_veredito_falha_quando_alguma_tecnica_nao_sucesso() -> None:
    dag = _dag(_no("n1", "T1"), _no("n2", "T2"))
    mapa = {"a1": "T1", "a2": "T2"}
    # T2 nunca executada (não coberta) → não é sucesso total.
    resultados = [_res("a1", AbilityResultStatus.SUCCESS)]
    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )
    assert metricas.tecnicas_sucesso == 1
    assert metricas.veredito_sucesso is False


def test_ability_sem_tecnica_mapeada_e_ignorada() -> None:
    dag = _dag(_no("n1", "T1"))
    mapa = {"a1": "T1", "a_fora": "TX"}  # TX não está no DAG
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a_fora", AbilityResultStatus.FAILURE),  # técnica fora do DAG → ignorada
        _res("desconhecida", AbilityResultStatus.FAILURE),  # ability sem mapa → ignorada
        _res(None, AbilityResultStatus.FAILURE),  # ability_id None → ignorada
    ]
    metricas = MedicaoService().medir(
        resultados=resultados, dag=dag, tecnica_por_ability=mapa
    )
    assert metricas.tecnicas_sucesso == 1
    assert metricas.nos_falhos == []
    assert metricas.veredito_sucesso is True


# ---------------------------------------------------------------------------
# consolidar (Req. 10.6, 10.7, 15.7)
# ---------------------------------------------------------------------------


def test_consolidar_monta_relatorio() -> None:
    metricas = MetricasSuficiencia(total_tecnicas_dag=1, tecnicas_sucesso=1, taxa_sucesso=1.0)
    resultados = [Stage3AbilityResult(ability_id="a1", status=AbilityResultStatus.SUCCESS)]
    gerado = dt.datetime(2024, 5, 6, 7, 8, 9)

    relatorio = MedicaoService().consolidar(
        caso_id="demo",
        metricas=metricas,
        resultados=resultados,
        ambiente_desprovisionado=True,
        gerado_em=gerado,
    )

    assert relatorio.caso_id == "demo"
    assert relatorio.metricas == metricas  # sem recomputar
    assert relatorio.resultados[0].ability_id == "a1"
    assert relatorio.ambiente_desprovisionado is True
    assert relatorio.gerado_em == gerado


def test_consolidar_default_gerado_em_e_sem_resultados() -> None:
    relatorio = MedicaoService().consolidar(
        caso_id="demo", metricas=MetricasSuficiencia()
    )
    assert isinstance(relatorio.gerado_em, dt.datetime)
    assert relatorio.resultados == []


# ---------------------------------------------------------------------------
# medir_do_caso / relatorio_do_caso (caminho de produção)
# ---------------------------------------------------------------------------


def _caso() -> CaseData:
    return CaseData(
        id="demo",
        nome="Demo",
        arquivo_ability="a.yml",
        arquivo_adversary="adv.yml",
        arquivo_dag="d.json",
        abilities=[
            AbilityData(ability_id="a1", technique_id="T1"),
            AbilityData(ability_id="a2", technique_id="T2"),
        ],
        dag=_dag(_no("n1", "T1", node_type="recon"), _no("n2", "T2", node_type="exec")),
    )


def test_medir_do_caso_usa_dag_e_abilities_do_caso() -> None:
    caso = _caso()
    resultados = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.FAILURE),
    ]
    metricas = MedicaoService().medir_do_caso(caso=caso, resultados=resultados)
    assert metricas.total_tecnicas_dag == 2
    assert metricas.tecnicas_sucesso == 1
    assert metricas.taxa_sucesso == 0.5
    assert metricas.nos_falhos == ["n2"]


def test_relatorio_do_caso_mede_e_consolida() -> None:
    caso = _caso()
    resultados_ability = [
        _res("a1", AbilityResultStatus.SUCCESS, tempo_execucao_s=2.0, telemetria=("processo",)),
        _res("a2", AbilityResultStatus.FAILURE, tempo_execucao_s=1.0),
    ]
    resultados_stage3 = [
        Stage3AbilityResult(ability_id="a1", status=AbilityResultStatus.SUCCESS),
        Stage3AbilityResult(ability_id="a2", status=AbilityResultStatus.FAILURE),
    ]

    relatorio = MedicaoService().relatorio_do_caso(
        caso=caso,
        resultados_ability=resultados_ability,
        resultados_stage3=resultados_stage3,
        ambiente_desprovisionado=False,
    )

    assert relatorio.caso_id == "demo"
    assert relatorio.metricas.total_tecnicas_dag == 2
    assert relatorio.metricas.tecnicas_sucesso == 1
    assert relatorio.metricas.tempo_total_s == 3.0
    assert relatorio.metricas.nos_falhos == ["n2"]
    assert relatorio.metricas.veredito_sucesso is False
    assert len(relatorio.resultados) == 2


# ---------------------------------------------------------------------------
# ResultadoAbility normalizadores
# ---------------------------------------------------------------------------


def test_de_execucao_normaliza_resultado_duck_typed() -> None:
    from app.services.environment.execucao import ExecucaoAbilityResult

    ear = ExecucaoAbilityResult(
        ability_id="a1",
        status=AbilityResultStatus.SUCCESS,
        container_destino="nginx (192.168.20.30)",
    )
    normalizado = ResultadoAbility.de_execucao(ear)
    assert normalizado.ability_id == "a1"
    assert normalizado.status is AbilityResultStatus.SUCCESS
    assert normalizado.tempo_execucao_s is None
    assert normalizado.telemetria == ()


def test_de_stage3_normaliza() -> None:
    s3 = Stage3AbilityResult(ability_id="a1", status=AbilityResultStatus.FAILURE)
    normalizado = ResultadoAbility.de_stage3(s3)
    assert normalizado.ability_id == "a1"
    assert normalizado.status is AbilityResultStatus.FAILURE
