"""Metric and report DTOs — Stage 6 Output (Execution + measurement + report).

This module defines the typed (Pydantic) models of the procedural-sufficiency
measurement and of the consolidated report produced at the end of Stage 6. The
execution itself **reuses** the ``OperationRunner``/``Stage3*`` (which persists
the ``AbilityResult`` per Ability); these DTOs are the **measurement** layer over
those results plus the DAG, consolidated into an :class:`ExecutionReport`.

- :class:`TechniqueMetric` — the per-technique/DAG-node measurement: final
  status, DAG step where it failed, execution time and observed telemetry
  (Req. 15.2, 15.4, 15.5).
- :class:`SufficiencyMetrics` — the procedural-sufficiency metrics (INITIAL
  PROPOSAL, Req. 15.8): total DAG techniques, successful techniques, success
  rate, procedural coverage, total time, the per-technique list, the failed
  nodes and the overall verdict (Req. 15.1, 15.2, 15.3, 15.4, 15.6).
- :class:`DeprovisioningView` — the structured teardown outcome exposed on the
  report (``deprovisioned`` + ``remaining_resources``), recovered from the
  persisted ``execution_reports`` row at read time (Req. 7.2, 7.3).
- :class:`ExecutionReport` — the Stage 6 Output: the consolidated metrics, the
  :class:`~app.services.stage.stage_service.Stage3AbilityResult` reused from the
  historical execution stage, the environment deprovisioning flag (Req. 12), the
  structured teardown ``deprovisioning`` (Req. 7.2, 7.3) and the generation
  timestamp (Req. 10.6, 10.7, 15.7).

The technique status reuses :class:`~app.models.enums.AbilityResultStatus`
(``pendente``/``em_execucao``/``sucesso``/``falha``), the same source of truth as
``Stage3AbilityResult``.

_Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7, 15.8_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field

from app.models.enums import AbilityResultStatus
from app.services.stage.stage_service import Stage3AbilityResult


class TechniqueMetric(BaseModel):
    """Per-technique/DAG-node measurement (Req. 15.2, 15.4, 15.5).

    Attributes:
        technique_id: ``technique_id`` of the measured technique, when available.
        node_id: Identifier of the matching DAG node, when available.
        status: Final status of the technique — one of
            :class:`~app.models.enums.AbilityResultStatus`
            (``pendente``/``em_execucao``/``sucesso``/``falha``).
        failed_step: DAG step where the technique failed, when applicable
            (Req. 15.2); ``None`` when it did not fail.
        execution_time_s: Execution time of the technique in seconds (Req. 15.4);
            ``None`` when not measured.
        telemetry: Telemetry signals observed (process/network/auth/file) during
            the technique's execution (Req. 15.5).
    """

    technique_id: Optional[str] = None
    node_id: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    failed_step: Optional[str] = None
    execution_time_s: Optional[float] = None
    telemetry: list[str] = Field(default_factory=list)


class SufficiencyMetrics(BaseModel):
    """Procedural-sufficiency metrics — INITIAL PROPOSAL (Req. 15.8).

    Attributes:
        total_dag_techniques: Total number of techniques in the Campaign's DAG.
        successful_techniques: Number of techniques that ended in ``sucesso``.
        success_rate: ``successful_techniques / total_dag_techniques`` (Req. 15.1).
        procedural_coverage: Proportion of the DAG effectively executable, in
            ``[0, 1]`` (Req. 15.3).
        total_time_s: Total execution time in seconds (Req. 15.4); ``None`` when
            not measured.
        per_technique: The :class:`TechniqueMetric` of each DAG technique/node.
        failed_nodes: Nodes/techniques that failed (Req. 15.2).
        success_verdict: Overall execution verdict (Req. 15.6, 10.5).
    """

    total_dag_techniques: int = 0
    successful_techniques: int = 0
    success_rate: float = 0.0
    procedural_coverage: float = 0.0
    total_time_s: Optional[float] = None
    per_technique: list[TechniqueMetric] = Field(default_factory=list)
    failed_nodes: list[str] = Field(default_factory=list)
    success_verdict: bool = False


class DeprovisioningView(BaseModel):
    """Structured teardown outcome exposed on the report (Req. 7.2, 7.3).

    Mirrors the persisted fields of the
    :class:`~app.services.environment.provisioner.DeprovisioningResult` produced
    at the end of Stage 6 and written to the ``execution_reports`` row: the
    boolean ``deprovisioned`` and the ``remaining_resources`` that remained when
    the teardown failed. The read path (``GET .../report``) recovers them from the
    persisted row so the report screen can indicate teardown success/failure and,
    on failure, list the resources that remained provisioned (Req. 7.2, 7.3).

    Only the two persisted fields are modelled here; the ``origin``/``message`` of
    the full :class:`DeprovisioningResult` are not persisted, so they are not
    reconstructed on the read path.

    Attributes:
        deprovisioned: ``True`` iff the environment was removed successfully.
        remaining_resources: Resources (containers/networks) that remained
            provisioned when the teardown failed; empty on success.
    """

    deprovisioned: bool = False
    remaining_resources: list[str] = Field(default_factory=list)


class ExecutionReport(BaseModel):
    """Stage 6 Output — consolidated report (Req. 10.6, 10.7, 15.7).

    Attributes:
        campaign_id: *Slug* of the Campaign whose execution produced the report.
        metrics: The execution's :class:`SufficiencyMetrics`.
        results: The
            :class:`~app.services.stage.stage_service.Stage3AbilityResult`
            per Ability, reused from the historical execution stage (``Stage3*``).
        environment_deprovisioned: ``True`` iff the campaign environment was
            deprovisioned at the end (Req. 12). Legacy boolean preserved for
            backward compatibility.
        deprovisioning: The structured teardown outcome (``deprovisioned`` +
            ``remaining_resources``) recovered from the persisted
            ``execution_reports`` row (Req. 7.2, 7.3). ``None`` when no teardown
            has been registered yet (e.g. the execution has not finished).
        generated_at: Instant at which the report was generated.
    """

    campaign_id: str
    metrics: SufficiencyMetrics
    results: list[Stage3AbilityResult] = Field(default_factory=list)
    environment_deprovisioned: bool = False
    deprovisioning: Optional[DeprovisioningView] = None
    generated_at: dt.datetime


__all__ = [
    "TechniqueMetric",
    "SufficiencyMetrics",
    "DeprovisioningView",
    "ExecutionReport",
]
