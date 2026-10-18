"""Property test das métricas de suficiência consistentes — Estágio 6 (task 8.8).

# Feature: pipeline-seis-stages, Property 10: Métricas de suficiência
# consistentes com os results. Para qualquer conjunto de results por
# Ability sobre um DAG curated, ``MeasurementService.measure`` produz métricas
# internamente consistentes: ``successful_techniques`` nunca excede
# ``total_dag_techniques``; ``success_rate == successful_techniques / total_dag_techniques``
# (guardando a divisão por zero); ``procedural_coverage ∈ [0, 1]``;
# ``failed_nodes`` contém EXATAMENTE as técnicas/nós com status consolidado
# ``failure``; e ``success_verdict`` segue o critério definido (todas as técnicas
# do DAG em success e nenhum nó falho) — e o relatório consolida todas essas
# medições.

Validates: Requirements 15.1, 15.2, 15.3, 15.6, 15.7, 10.4, 10.5, 10.6

Esta é uma property test de *feature* (``pipeline-seis-stages`` é uma feature,
não um bugfix): a propriedade é esperada **valer** sobre a implementação atual de
:meth:`~app.services.environment.measurement.MeasurementService.measure` /
:meth:`~app.services.environment.measurement.MeasurementService.consolidate`.

Estratégia / isolation
-----------------------
Cada exemplo gera, via Hypothesis:

* um DAG curated (:class:`~app.services.case_service.DagData`) com um conjunto
  arbitrário de nós, alguns com ``technique_id`` (as técnicas, possivelmente
  repetidas entre nós — de-duplicadas pela medição) e alguns sem (ignorados);
* um mapa ``ability_id`` → ``technique_id`` ligando Abilities às técnicas (ou a
  técnicas FORA do DAG, que devem ser ignoradas);
* uma lista de :class:`~app.services.environment.measurement.AbilityMeasurementResult`
  arbitrários (status/tempo/telemetry), incluindo Abilities sem técnica mapeada
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
from app.services.campaign_service import DagData, DagNode
from app.services.environment.measurement import MeasurementService, AbilityMeasurementResult
from app.services.environment.report import (
    SufficiencyMetrics,
    ExecutionReport,
)

_SERVICE = MeasurementService()
_STATUSES = list(AbilityResultStatus)

# Universo pequeno e fechado de identificadores — favorece colisões (técnicas
# repetidas entre nós, Abilities mapeando a mesma técnica) e assim exercita a
# de-duplicação e a agregação por técnica.
_TECH_IDS = ["T1", "T2", "T3", "T4"]
_FOREIGN_TECH_IDS = ["TX", "TY"]  # técnicas que NÃO aparecem no DAG
_ABILITY_IDS = ["a1", "a2", "a3", "a4", "a5"]


@st.composite
def _cenario(draw: st.DrawFn):
    """Gera (dag, technique_by_ability, results) para um exemplo."""

    # --- DAG: nós com/sem technique_id, técnicas possivelmente repetidas. ---
    n_nos = draw(st.integers(min_value=0, max_value=8))
    nodes: list[DagNode] = []
    for i in range(n_nos):
        tid = draw(st.one_of(st.none(), st.sampled_from(_TECH_IDS)))
        nodes.append(
            DagNode(
                node_id=f"n{i}",
                node_index=i,
                node_type=draw(st.one_of(st.none(), st.sampled_from(["recon", "exec", "exfil"]))),
                technique_id=tid,
                tactic=draw(st.one_of(st.none(), st.sampled_from(["ta1", "ta2"]))),
            )
        )
    dag = DagData(campaign_name="camp", structural_nodes=nodes)

    # --- Mapa ability_id -> technique_id (pode apontar p/ técnica fora do DAG). ---
    technique_by_ability: dict[str, str | None] = {}
    for aid in _ABILITY_IDS:
        technique_by_ability[aid] = draw(
            st.one_of(st.none(), st.sampled_from(_TECH_IDS + _FOREIGN_TECH_IDS))
        )

    # --- Per-Ability results (includes noise: ability_id None/unknown). ---
    def _result() -> AbilityMeasurementResult:
        aid = draw(st.one_of(st.none(), st.sampled_from(_ABILITY_IDS + ["desconhecida"])))
        tempo = draw(
            st.one_of(
                st.none(),
                st.floats(min_value=0.0, max_value=300.0, allow_nan=False, allow_infinity=False),
            )
        )
        telemetry = tuple(
            draw(st.lists(st.sampled_from(["processo", "rede", "autenticacao", "file"]), max_size=3))
        )
        return AbilityMeasurementResult(
            ability_id=aid,
            status=draw(st.sampled_from(_STATUSES)),
            execution_time_s=tempo,
            telemetry=telemetry,
        )

    results = [_result() for _ in range(draw(st.integers(min_value=0, max_value=10)))]
    return dag, technique_by_ability, results


def _oraculo(dag, technique_by_ability, results):
    """Oráculo independente: recomputa as métricas por um caminho distinto.

    Devolve ``(total, success, taxa, cobertura, failed_nodes, verdict,
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

    for r in results:
        if r.ability_id is None:
            continue
        tid = technique_by_ability.get(r.ability_id)
        if tid is None or tid not in dag_set:
            continue
        if r.status != AbilityResultStatus.PENDING:
            tem_tentada[tid] = True
        if r.status == AbilityResultStatus.SUCCESS:
            tem_sucesso[tid] = True
        if r.status == AbilityResultStatus.FAILURE:
            tem_falha[tid] = True

    total = len(ordem)
    success = sum(1 for t in ordem if tem_sucesso[t])
    tentadas = sum(1 for t in ordem if tem_tentada[t])
    # Status consolidado: success > failure. failed_nodes = técnicas cujo status final
    # é failure (falhou e não teve nenhum success).
    failed_nodes = [
        (node_id_por_tecnica[t] or t)
        for t in ordem
        if tem_falha[t] and not tem_sucesso[t]
    ]

    taxa = (success / total) if total else 0.0
    cobertura = (tentadas / total) if total else 0.0
    verdict = total > 0 and success == total and not failed_nodes
    return total, success, taxa, cobertura, failed_nodes, verdict, ordem, node_id_por_tecnica


# Feature: pipeline-seis-stages, Property 10: Métricas de suficiência
# consistentes com os results.
@settings(max_examples=200)
@given(_cenario())
def test_sufficiency_metrics_are_consistent(cenario) -> None:
    """As métricas derivadas honram cada invariante contra um oráculo independente.

    # Feature: pipeline-seis-stages, Property 10: Métricas de suficiência
    # consistentes com os results.
    Validates: Requirements 15.1, 15.2, 15.3, 15.6, 10.4, 10.5
    """

    dag, technique_by_ability, results = cenario

    metrics = _SERVICE.measure(
        results=results,
        dag=dag,
        technique_by_ability=technique_by_ability,
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
    ) = _oraculo(dag, technique_by_ability, results)

    # total_dag_techniques == nº de técnicas DISTINTAS do DAG (o denominador).
    assert metrics.total_dag_techniques == total_o

    # successful_techniques nunca excede o total (Req. 15.1).
    assert metrics.successful_techniques == sucesso_o
    assert 0 <= metrics.successful_techniques <= metrics.total_dag_techniques

    # success_rate == successful_techniques / total, guardando a divisão por zero
    # (Req. 15.1). DAG sem técnicas => 0.0.
    assert metrics.success_rate == taxa_o
    if metrics.total_dag_techniques == 0:
        assert metrics.success_rate == 0.0
    else:
        assert metrics.success_rate == (
            metrics.successful_techniques / metrics.total_dag_techniques
        )

    # procedural_coverage ∈ [0, 1] (Req. 15.3).
    assert metrics.procedural_coverage == cobertura_o
    assert 0.0 <= metrics.procedural_coverage <= 1.0

    # failed_nodes contém EXATAMENTE as técnicas/nós falhos, na ordem do DAG, sem
    # duplicatas (Req. 15.2).
    assert metrics.failed_nodes == nos_falhos_o
    assert len(metrics.failed_nodes) == len(set(metrics.failed_nodes))

    # success_verdict segue o critério definido: todas as técnicas do DAG em
    # success e nenhum nó falho (DAG não vazio) — Req. 15.6, 10.5.
    assert metrics.success_verdict == veredito_o

    # per_technique cobre EXATAMENTE as técnicas distintas do DAG, na ordem do DAG.
    assert [m.technique_id for m in metrics.per_technique] == ordem_o

    # Consistência interna da lista per_technique com os agregados:
    sucesso_na_lista = sum(
        1 for m in metrics.per_technique if m.status == AbilityResultStatus.SUCCESS
    )
    falha_na_lista = [
        (m.node_id or m.technique_id)
        for m in metrics.per_technique
        if m.status == AbilityResultStatus.FAILURE
    ]
    assert sucesso_na_lista == metrics.successful_techniques
    assert falha_na_lista == metrics.failed_nodes
    # Toda técnica failure é anotada com o nó do DAG onde falhou (Req. 15.2).
    for m in metrics.per_technique:
        if m.status == AbilityResultStatus.FAILURE:
            assert m.node_id is not None


# Feature: pipeline-seis-stages, Property 10: Métricas de suficiência
# consistentes com os results (parte consolidação do relatório).
@settings(max_examples=200)
@given(_cenario())
def test_report_consolidates_the_same_metrics(cenario) -> None:
    """O relatório consolida EXATAMENTE as mesmas métricas medidas (Req. 15.7, 10.6).

    # Feature: pipeline-seis-stages, Property 10: Métricas de suficiência
    # consistentes com os results.
    Validates: Requirements 15.7, 10.6
    """

    dag, technique_by_ability, results = cenario

    metrics = _SERVICE.measure(
        results=results,
        dag=dag,
        technique_by_ability=technique_by_ability,
    )
    generated_at = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    report = _SERVICE.consolidate(
        campaign_id="apt-x",
        metrics=metrics,
        results=[],
        environment_deprovisioned=True,
        generated_at=generated_at,
    )

    assert isinstance(report, ExecutionReport)
    assert isinstance(report.metrics, SufficiencyMetrics)
    # Consolida sem recomputar: as métricas são preservadas na íntegra.
    assert report.metrics == metrics
    assert report.campaign_id == "apt-x"
    assert report.environment_deprovisioned is True
    assert report.generated_at == generated_at
