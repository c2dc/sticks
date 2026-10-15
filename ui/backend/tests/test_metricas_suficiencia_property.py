"""Property test das métricas de suficiência consistentes — Estágio 6 (task 8.8).

# Feature: pipeline-seis-estagios, Property 10: Métricas de suficiência
# consistentes com os resultados. Para qualquer conjunto de resultados por
# Ability sobre um DAG curado, ``MedicaoService.medir`` produz métricas
# internamente consistentes: ``tecnicas_sucesso`` nunca excede
# ``total_tecnicas_dag``; ``taxa_sucesso == tecnicas_sucesso / total_tecnicas_dag``
# (guardando a divisão por zero); ``cobertura_procedural ∈ [0, 1]``;
# ``nos_falhos`` contém EXATAMENTE as técnicas/nós com status consolidado
# ``falha``; e ``veredito_sucesso`` segue o critério definido (todas as técnicas
# do DAG em sucesso e nenhum nó falho) — e o relatório consolida todas essas
# medições.

Validates: Requirements 15.1, 15.2, 15.3, 15.6, 15.7, 10.4, 10.5, 10.6

Esta é uma property test de *feature* (``pipeline-seis-estagios`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual de
:meth:`~app.services.environment.medicao.MedicaoService.medir` /
:meth:`~app.services.environment.medicao.MedicaoService.consolidar`.

Estratégia / isolamento
-----------------------
Cada exemplo gera, via Hypothesis:

* um DAG curado (:class:`~app.services.case_service.DagData`) com um conjunto
  arbitrário de nós, alguns com ``technique_id`` (as técnicas, possivelmente
  repetidas entre nós — de-duplicadas pela medição) e alguns sem (ignorados);
* um mapa ``ability_id`` → ``technique_id`` ligando Abilities às técnicas (ou a
  técnicas FORA do DAG, que devem ser ignoradas);
* uma lista de :class:`~app.services.environment.medicao.ResultadoAbility`
  arbitrários (status/tempo/telemetria), incluindo Abilities sem técnica mapeada
  e Abilities com ``ability_id`` ausente — ambos devem ser ignorados.

O **oráculo independente** recomputa cada métrica por um caminho distinto do da
implementação (agregando por técnica num dicionário à parte) e as invariantes
são verificadas contra esse oráculo. A medição é pura: sem rede, sem Docker, sem
IA. Hypothesis roda ≥100 iterações.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import AbilityResultStatus
from app.services.case_service import DagData, DagNode
from app.services.environment.medicao import MedicaoService, ResultadoAbility
from app.services.environment.relatorio import (
    MetricasSuficiencia,
    RelatorioExecucao,
)

_SERVICE = MedicaoService()
_STATUSES = list(AbilityResultStatus)

# Universo pequeno e fechado de identificadores — favorece colisões (técnicas
# repetidas entre nós, Abilities mapeando a mesma técnica) e assim exercita a
# de-duplicação e a agregação por técnica.
_TECH_IDS = ["T1", "T2", "T3", "T4"]
_FOREIGN_TECH_IDS = ["TX", "TY"]  # técnicas que NÃO aparecem no DAG
_ABILITY_IDS = ["a1", "a2", "a3", "a4", "a5"]


@st.composite
def _cenario(draw: st.DrawFn):
    """Gera (dag, tecnica_por_ability, resultados) para um exemplo."""

    # --- DAG: nós com/sem technique_id, técnicas possivelmente repetidas. ---
    n_nos = draw(st.integers(min_value=0, max_value=8))
    nos: list[DagNode] = []
    for i in range(n_nos):
        tid = draw(st.one_of(st.none(), st.sampled_from(_TECH_IDS)))
        nos.append(
            DagNode(
                node_id=f"n{i}",
                node_index=i,
                node_type=draw(st.one_of(st.none(), st.sampled_from(["recon", "exec", "exfil"]))),
                technique_id=tid,
                tactic=draw(st.one_of(st.none(), st.sampled_from(["ta1", "ta2"]))),
            )
        )
    dag = DagData(campaign_name="camp", structural_nodes=nos)

    # --- Mapa ability_id -> technique_id (pode apontar p/ técnica fora do DAG). ---
    tecnica_por_ability: dict[str, str | None] = {}
    for aid in _ABILITY_IDS:
        tecnica_por_ability[aid] = draw(
            st.one_of(st.none(), st.sampled_from(_TECH_IDS + _FOREIGN_TECH_IDS))
        )

    # --- Resultados por Ability (inclui ruído: ability_id None/desconhecido). ---
    def _resultado() -> ResultadoAbility:
        aid = draw(st.one_of(st.none(), st.sampled_from(_ABILITY_IDS + ["desconhecida"])))
        tempo = draw(
            st.one_of(
                st.none(),
                st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False),
            )
        )
        telemetria = tuple(
            draw(st.lists(st.sampled_from(["processo", "rede", "autenticacao", "arquivo"]), max_size=3))
        )
        return ResultadoAbility(
            ability_id=aid,
            status=draw(st.sampled_from(_STATUSES)),
            tempo_execucao_s=tempo,
            telemetria=telemetria,
        )

    resultados = [_resultado() for _ in range(draw(st.integers(min_value=0, max_value=10)))]
    return dag, tecnica_por_ability, resultados


def _oraculo(dag, tecnica_por_ability, resultados):
    """Oráculo independente: recomputa as métricas por um caminho distinto.

    Devolve ``(total, sucesso, taxa, cobertura, nos_falhos, veredito,
    ordem_tecnicas, node_id_por_tecnica)``.
    """

    # Técnicas distintas na ordem dos nós (1º nó representa a técnica).
    ordem: list[str] = []
    node_id_por_tecnica: dict[str, str | None] = {}
    for no in dag.structural_nodes:
        tid = no.technique_id
        if tid and tid not in node_id_por_tecnica:
            ordem.append(tid)
            node_id_por_tecnica[tid] = no.node_id
    dag_set = set(ordem)

    # Agrega por técnica (flags independentes do acumulador da implementação).
    tem_sucesso: dict[str, bool] = {t: False for t in ordem}
    tem_falha: dict[str, bool] = {t: False for t in ordem}
    tem_tentada: dict[str, bool] = {t: False for t in ordem}

    for r in resultados:
        if r.ability_id is None:
            continue
        tid = tecnica_por_ability.get(r.ability_id)
        if tid is None or tid not in dag_set:
            continue
        if r.status != AbilityResultStatus.PENDING:
            tem_tentada[tid] = True
        if r.status == AbilityResultStatus.SUCCESS:
            tem_sucesso[tid] = True
        if r.status == AbilityResultStatus.FAILURE:
            tem_falha[tid] = True

    total = len(ordem)
    sucesso = sum(1 for t in ordem if tem_sucesso[t])
    tentadas = sum(1 for t in ordem if tem_tentada[t])
    # Status consolidado: sucesso > falha. nos_falhos = técnicas cujo status final
    # é falha (falhou e não teve nenhum sucesso).
    nos_falhos = [
        (node_id_por_tecnica[t] or t)
        for t in ordem
        if tem_falha[t] and not tem_sucesso[t]
    ]

    taxa = (sucesso / total) if total else 0.0
    cobertura = (tentadas / total) if total else 0.0
    veredito = total > 0 and sucesso == total and not nos_falhos
    return total, sucesso, taxa, cobertura, nos_falhos, veredito, ordem, node_id_por_tecnica


# Feature: pipeline-seis-estagios, Property 10: Métricas de suficiência
# consistentes com os resultados.
@settings(max_examples=200)
@given(_cenario())
def test_metricas_suficiencia_sao_consistentes(cenario) -> None:
    """As métricas derivadas honram cada invariante contra um oráculo independente.

    # Feature: pipeline-seis-estagios, Property 10: Métricas de suficiência
    # consistentes com os resultados.
    Validates: Requirements 15.1, 15.2, 15.3, 15.6, 10.4, 10.5
    """

    dag, tecnica_por_ability, resultados = cenario

    metricas = _SERVICE.medir(
        resultados=resultados,
        dag=dag,
        tecnica_por_ability=tecnica_por_ability,
    )

    (
        total_o,
        sucesso_o,
        taxa_o,
        cobertura_o,
        nos_falhos_o,
        veredito_o,
        ordem_o,
        _node_por_tec,
    ) = _oraculo(dag, tecnica_por_ability, resultados)

    # total_tecnicas_dag == nº de técnicas DISTINTAS do DAG (o denominador).
    assert metricas.total_tecnicas_dag == total_o

    # tecnicas_sucesso nunca excede o total (Req. 15.1).
    assert metricas.tecnicas_sucesso == sucesso_o
    assert 0 <= metricas.tecnicas_sucesso <= metricas.total_tecnicas_dag

    # taxa_sucesso == tecnicas_sucesso / total, guardando a divisão por zero
    # (Req. 15.1). DAG sem técnicas => 0.0.
    assert metricas.taxa_sucesso == taxa_o
    if metricas.total_tecnicas_dag == 0:
        assert metricas.taxa_sucesso == 0.0
    else:
        assert metricas.taxa_sucesso == (
            metricas.tecnicas_sucesso / metricas.total_tecnicas_dag
        )

    # cobertura_procedural ∈ [0, 1] (Req. 15.3).
    assert metricas.cobertura_procedural == cobertura_o
    assert 0.0 <= metricas.cobertura_procedural <= 1.0

    # nos_falhos contém EXATAMENTE as técnicas/nós falhos, na ordem do DAG, sem
    # duplicatas (Req. 15.2).
    assert metricas.nos_falhos == nos_falhos_o
    assert len(metricas.nos_falhos) == len(set(metricas.nos_falhos))

    # veredito_sucesso segue o critério definido: todas as técnicas do DAG em
    # sucesso e nenhum nó falho (DAG não vazio) — Req. 15.6, 10.5.
    assert metricas.veredito_sucesso == veredito_o

    # por_tecnica cobre EXATAMENTE as técnicas distintas do DAG, na ordem do DAG.
    assert [m.technique_id for m in metricas.por_tecnica] == ordem_o

    # Consistência interna da lista por_tecnica com os agregados:
    sucesso_na_lista = sum(
        1 for m in metricas.por_tecnica if m.status == AbilityResultStatus.SUCCESS
    )
    falha_na_lista = [
        (m.node_id or m.technique_id)
        for m in metricas.por_tecnica
        if m.status == AbilityResultStatus.FAILURE
    ]
    assert sucesso_na_lista == metricas.tecnicas_sucesso
    assert falha_na_lista == metricas.nos_falhos
    # Toda técnica falha é anotada com o nó do DAG onde falhou (Req. 15.2).
    for m in metricas.por_tecnica:
        if m.status == AbilityResultStatus.FAILURE:
            assert m.node_id is not None


# Feature: pipeline-seis-estagios, Property 10: Métricas de suficiência
# consistentes com os resultados (parte consolidação do relatório).
@settings(max_examples=200)
@given(_cenario())
def test_relatorio_consolida_as_mesmas_metricas(cenario) -> None:
    """O relatório consolida EXATAMENTE as mesmas métricas medidas (Req. 15.7, 10.6).

    # Feature: pipeline-seis-estagios, Property 10: Métricas de suficiência
    # consistentes com os resultados.
    Validates: Requirements 15.7, 10.6
    """

    dag, tecnica_por_ability, resultados = cenario

    metricas = _SERVICE.medir(
        resultados=resultados,
        dag=dag,
        tecnica_por_ability=tecnica_por_ability,
    )
    gerado_em = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    relatorio = _SERVICE.consolidar(
        caso_id="apt-x",
        metricas=metricas,
        resultados=[],
        ambiente_desprovisionado=True,
        gerado_em=gerado_em,
    )

    assert isinstance(relatorio, RelatorioExecucao)
    assert isinstance(relatorio.metricas, MetricasSuficiencia)
    # Consolida sem recomputar: as métricas são preservadas na íntegra.
    assert relatorio.metricas == metricas
    assert relatorio.caso_id == "apt-x"
    assert relatorio.ambiente_desprovisionado is True
    assert relatorio.gerado_em == gerado_em
