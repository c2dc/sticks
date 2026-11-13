"""Example (non-property) tests for the Stage 6 measurement service (task 8.3).

Exercitam, por exemplos diretos, a medição de suficiência procedural
(:meth:`~app.services.environment.measurement.MeasurementService.measure`), a consolidação
no :class:`~app.services.environment.report.ExecutionReport`
(:meth:`~app.services.environment.measurement.MeasurementService.consolidate`) e a ponte
de produção por campaign
(:meth:`~app.services.environment.measurement.MeasurementService.measure_from_campaign`) (Req.
10.4-10.7, 15.1-15.7). A medição é **pura**: opera sobre o attack flow curated + os
results por Ability + o mapa ``ability_id`` → ``technique_id`` em memória, sem
rede, Docker ou IA, então os exemplos não precisam de banco nem de disco.

Cobertura:

1. ``success_rate = successful_techniques / total_flow_techniques`` (Req. 15.1) e attack flow
   vazio sem divisão por zero.
2. ``procedural_coverage ∈ [0, 1]`` como a proporção de técnicas tentadas (Req.
   15.3).
3. ``failed_nodes`` com a step do attack flow onde a failure ocorreu (Req. 15.2).
4. Tempo por técnica e tempo total (Req. 15.4) e telemetry por técnica (Req.
   15.5).
5. ``success_verdict`` pelo critério definido (Req. 15.6, 10.5).
6. Consolidação em :class:`ExecutionReport` (Req. 10.6, 10.7, 15.7).
7. A ponte Ability → técnica via :meth:`MeasurementService.measure_from_campaign` (produção).

_Requirements: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
"""

from __future__ import annotations

import datetime as dt

from app.models.enums import AbilityResultStatus
from app.services.curated_case_models import AbilityData, CaseData, FlowData, FlowNode
from app.services.emulation.views import Stage3AbilityResult
from app.services.environment.measurement import AbilityMeasurementResult, MeasurementService
from app.services.environment.report import SufficiencyMetrics

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flow(*nodes: FlowNode) -> FlowData:
    return FlowData(campaign_name="demo", structural_nodes=list(nodes))


def _no(node_id: str, technique_id: str | None, **kwargs: object) -> FlowNode:
    return FlowNode(node_id=node_id, technique_id=technique_id, **kwargs)  # type: ignore[arg-type] -- test factory forwards loosely typed keyword arguments


def _res(
    ability_id: str | None, status: AbilityResultStatus, **kwargs: object
) -> AbilityMeasurementResult:
    return AbilityMeasurementResult(ability_id=ability_id, status=status, **kwargs)  # type: ignore[arg-type] -- test factory forwards loosely typed keyword arguments


# ---------------------------------------------------------------------------
# Taxa de success (Req. 15.1)
# ---------------------------------------------------------------------------


def test_success_rate_and_counts() -> None:
    flow = _flow(
        _no("n1", "T1", node_type="recon"),
        _no("n2", "T2", node_type="exec"),
        _no("n3", "T3", node_type="exfil"),
        _no("n4", "T4", node_type="impact"),
    )
    mapa = {"a1": "T1", "a2": "T2", "a3": "T3", "a4": "T4"}
    results = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.SUCCESS),
        _res("a3", AbilityResultStatus.SUCCESS),
        _res("a4", AbilityResultStatus.FAILURE),
    ]

    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)

    assert metrics.total_flow_techniques == 4
    assert metrics.successful_techniques == 3
    assert metrics.success_rate == 0.75


def test_success_rate_empty_flow_no_division_by_zero() -> None:
    metrics = MeasurementService().measure(
        results=[_res("a1", AbilityResultStatus.SUCCESS)],
        flow=_flow(),
        technique_by_ability={"a1": "T1"},
    )
    assert metrics.total_flow_techniques == 0
    assert metrics.success_rate == 0.0
    assert metrics.procedural_coverage == 0.0
    assert metrics.success_verdict is False


def test_flow_none_zeroes_the_metrics() -> None:
    metrics = MeasurementService().measure(results=[], flow=None, technique_by_ability={})
    assert metrics == SufficiencyMetrics()


# ---------------------------------------------------------------------------
# Cobertura procedural ∈ [0, 1] (Req. 15.3)
# ---------------------------------------------------------------------------


def test_cobertura_procedural_proporcao_de_tentadas() -> None:
    flow = _flow(
        _no("n1", "T1"),
        _no("n2", "T2"),
        _no("n3", "T3"),
        _no("n4", "T4"),  # nunca tentada → não coberta
    )
    mapa = {"a1": "T1", "a2": "T2", "a3": "T3"}
    results = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.FAILURE),
        _res("a3", AbilityResultStatus.RUNNING),  # tentada mas não concluída
    ]

    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)

    # 3 de 4 técnicas foram tentadas; a 4ª (T4) nunca foi.
    assert metrics.procedural_coverage == 0.75
    assert 0.0 <= metrics.procedural_coverage <= 1.0


def test_duplicate_technique_in_flow_counts_once() -> None:
    # T1 aparece em dois nós; conta como UMA técnica distinta.
    flow = _flow(_no("n1", "T1"), _no("n2", "T1"), _no("n3", "T2"))
    metrics = MeasurementService().measure(
        results=[_res("a1", AbilityResultStatus.SUCCESS)],
        flow=flow,
        technique_by_ability={"a1": "T1"},
    )
    assert metrics.total_flow_techniques == 2
    assert metrics.successful_techniques == 1
    assert metrics.success_rate == 0.5


# ---------------------------------------------------------------------------
# Nós falhos + step do attack flow (Req. 15.2)
# ---------------------------------------------------------------------------


def test_nos_falhos_registram_etapa_do_flow() -> None:
    flow = _flow(
        _no("n1", "T1", node_type="recon"),
        _no("n2", "T2", node_type="exec"),
    )
    mapa = {"a1": "T1", "a2": "T2"}
    results = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.FAILURE),
    ]

    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)

    assert metrics.failed_nodes == ["n2"]
    failure = next(m for m in metrics.per_technique if m.technique_id == "T2")
    assert failure.status is AbilityResultStatus.FAILURE
    assert failure.failed_step == "exec"
    assert failure.node_id == "n2"
    ok = next(m for m in metrics.per_technique if m.technique_id == "T1")
    assert ok.failed_step is None


def test_failed_step_falls_back_to_tactic_then_none() -> None:
    flow = _flow(
        _no("n1", "T1", tactic="execution"),  # sem node_type → usa tactic
        _no("n2", "T2"),  # sem node_type/tactic → sem etapa
    )
    mapa = {"a1": "T1", "a2": "T2"}
    results = [
        _res("a1", AbilityResultStatus.FAILURE),
        _res("a2", AbilityResultStatus.FAILURE),
    ]
    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)
    etapas = {m.technique_id: m.failed_step for m in metrics.per_technique}
    assert etapas["T1"] == "execution"
    assert etapas["T2"] is None


def test_failure_and_success_in_same_technique_consolidates_success() -> None:
    # Uma técnica com duas Abilities: uma failure, outra success ⇒ success.
    flow = _flow(_no("n1", "T1"))
    mapa = {"a1": "T1", "a1b": "T1"}
    results = [
        _res("a1", AbilityResultStatus.FAILURE),
        _res("a1b", AbilityResultStatus.SUCCESS),
    ]
    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)
    assert metrics.successful_techniques == 1
    assert metrics.failed_nodes == []
    assert metrics.per_technique[0].status is AbilityResultStatus.SUCCESS


# ---------------------------------------------------------------------------
# Tempo por técnica / total (Req. 15.4) + telemetry (Req. 15.5)
# ---------------------------------------------------------------------------


def test_tempo_por_tecnica_e_total() -> None:
    flow = _flow(_no("n1", "T1"), _no("n2", "T2"))
    mapa = {"a1": "T1", "a1b": "T1", "a2": "T2"}
    results = [
        _res("a1", AbilityResultStatus.SUCCESS, execution_time_s=1.5),
        _res("a1b", AbilityResultStatus.SUCCESS, execution_time_s=0.5),  # mesma técnica
        _res("a2", AbilityResultStatus.SUCCESS, execution_time_s=3.0),
    ]

    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)

    tempos = {m.technique_id: m.execution_time_s for m in metrics.per_technique}
    assert tempos["T1"] == 2.0  # 1.5 + 0.5 somados na técnica
    assert tempos["T2"] == 3.0
    assert metrics.total_time_s == 5.0


def test_telemetry_per_technique_deduplicated() -> None:
    flow = _flow(_no("n1", "T1"))
    mapa = {"a1": "T1", "a1b": "T1"}
    results = [
        _res("a1", AbilityResultStatus.SUCCESS, telemetry=("processo", "rede")),
        _res("a1b", AbilityResultStatus.SUCCESS, telemetry=("rede", "autenticacao")),
    ]

    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)

    technique = metrics.per_technique[0]
    assert technique.telemetry == ["processo", "rede", "autenticacao"]


# ---------------------------------------------------------------------------
# Veredito geral (Req. 15.6, 10.5)
# ---------------------------------------------------------------------------


def test_verdict_success_when_all_techniques_succeed() -> None:
    flow = _flow(_no("n1", "T1"), _no("n2", "T2"))
    mapa = {"a1": "T1", "a2": "T2"}
    results = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.SUCCESS),
    ]
    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)
    assert metrics.success_verdict is True


def test_verdict_failure_when_some_technique_not_success() -> None:
    flow = _flow(_no("n1", "T1"), _no("n2", "T2"))
    mapa = {"a1": "T1", "a2": "T2"}
    # T2 nunca executada (não coberta) → não é success total.
    results = [_res("a1", AbilityResultStatus.SUCCESS)]
    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)
    assert metrics.successful_techniques == 1
    assert metrics.success_verdict is False


def test_ability_without_mapped_technique_is_ignored() -> None:
    flow = _flow(_no("n1", "T1"))
    mapa = {"a1": "T1", "a_fora": "TX"}  # TX não está no attack flow
    results = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a_fora", AbilityResultStatus.FAILURE),  # técnica fora do attack flow → ignorada
        _res("desconhecida", AbilityResultStatus.FAILURE),  # ability sem mapa → ignorada
        _res(None, AbilityResultStatus.FAILURE),  # ability_id None → ignorada
    ]
    metrics = MeasurementService().measure(results=results, flow=flow, technique_by_ability=mapa)
    assert metrics.successful_techniques == 1
    assert metrics.failed_nodes == []
    assert metrics.success_verdict is True


# ---------------------------------------------------------------------------
# consolidate (Req. 10.6, 10.7, 15.7)
# ---------------------------------------------------------------------------


def test_consolidate_builds_report() -> None:
    metrics = SufficiencyMetrics(total_flow_techniques=1, successful_techniques=1, success_rate=1.0)
    results = [Stage3AbilityResult(ability_id="a1", status=AbilityResultStatus.SUCCESS)]
    gerado = dt.datetime(2024, 5, 6, 7, 8, 9)

    report = MeasurementService().consolidate(
        campaign_id="demo",
        metrics=metrics,
        results=results,
        environment_deprovisioned=True,
        generated_at=gerado,
    )

    assert report.campaign_id == "demo"
    assert report.metrics == metrics  # sem recomputar
    assert report.results[0].ability_id == "a1"
    assert report.environment_deprovisioned is True
    assert report.generated_at == gerado


def test_consolidate_default_generated_at_and_no_results() -> None:
    report = MeasurementService().consolidate(campaign_id="demo", metrics=SufficiencyMetrics())
    assert isinstance(report.generated_at, dt.datetime)
    assert report.results == []


# ---------------------------------------------------------------------------
# measure_from_campaign / report_from_campaign (caminho de produção)
# ---------------------------------------------------------------------------


def _campaign() -> CaseData:
    return CaseData(
        id="demo",
        name="Demo",
        flow_file="d.json",
        abilities=[
            AbilityData(ability_id="a1", technique_id="T1"),
            AbilityData(ability_id="a2", technique_id="T2"),
        ],
        flow=_flow(_no("n1", "T1", node_type="recon"), _no("n2", "T2", node_type="exec")),
    )


def test_measure_from_campaign_uses_campaign_flow_and_abilities() -> None:
    campaign = _campaign()
    results = [
        _res("a1", AbilityResultStatus.SUCCESS),
        _res("a2", AbilityResultStatus.FAILURE),
    ]
    metrics = MeasurementService().measure_from_campaign(campaign=campaign, results=results)
    assert metrics.total_flow_techniques == 2
    assert metrics.successful_techniques == 1
    assert metrics.success_rate == 0.5
    assert metrics.failed_nodes == ["n2"]


def test_report_from_campaign_measures_and_consolidates() -> None:
    campaign = _campaign()
    ability_results = [
        _res("a1", AbilityResultStatus.SUCCESS, execution_time_s=2.0, telemetry=("processo",)),
        _res("a2", AbilityResultStatus.FAILURE, execution_time_s=1.0),
    ]
    stage3_results = [
        Stage3AbilityResult(ability_id="a1", status=AbilityResultStatus.SUCCESS),
        Stage3AbilityResult(ability_id="a2", status=AbilityResultStatus.FAILURE),
    ]

    report = MeasurementService().report_from_campaign(
        campaign=campaign,
        ability_results=ability_results,
        stage3_results=stage3_results,
        environment_deprovisioned=False,
    )

    assert report.campaign_id == "demo"
    assert report.metrics.total_flow_techniques == 2
    assert report.metrics.successful_techniques == 1
    assert report.metrics.total_time_s == 3.0
    assert report.metrics.failed_nodes == ["n2"]
    assert report.metrics.success_verdict is False
    assert len(report.results) == 2


# ---------------------------------------------------------------------------
# AbilityMeasurementResult normalizadores
# ---------------------------------------------------------------------------


def test_from_execution_normalizes_duck_typed_result() -> None:
    from app.services.environment.execution import ExecutionAbilityResult

    ear = ExecutionAbilityResult(
        ability_id="a1",
        status=AbilityResultStatus.SUCCESS,
        target_container="nginx (192.168.20.30)",
    )
    normalizado = AbilityMeasurementResult.from_execution(ear)
    assert normalizado.ability_id == "a1"
    assert normalizado.status is AbilityResultStatus.SUCCESS
    assert normalizado.execution_time_s is None
    assert normalizado.telemetry == ()


def test_de_stage3_normaliza() -> None:
    s3 = Stage3AbilityResult(ability_id="a1", status=AbilityResultStatus.FAILURE)
    normalizado = AbilityMeasurementResult.from_stage3(s3)
    assert normalizado.ability_id == "a1"
    assert normalizado.status is AbilityResultStatus.FAILURE
