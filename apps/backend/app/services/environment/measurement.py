"""MeasurementService — Stage 6 procedural-sufficiency measurement (task 8.3).

This module is the **measurement** layer of the Stage 6 output (Execution +
measurement + report). The execution itself **reuses** the
:class:`~app.services.operation.runner.OperationRunner` (stitched by task 8.2 in
:class:`~app.services.environment.execution.ExecutionStageService`), which
persists one :class:`~app.models.domain.AbilityResult` per Ability; this task
**derives** the :class:`~app.services.environment.report.SufficiencyMetrics` from
those per-Ability results **plus the curated DAG** and consolidates everything
into an :class:`~app.services.environment.report.ExecutionReport` (Req. 10.4–10.7,
15.1–15.7).

What is derived, and how
------------------------
The measurement unit is the **DAG technique** (a structural node with
``technique_id``; Req. 15.1, 15.3). The execution results come per **Ability**;
each curated Ability carries its ``technique_id``
(:class:`~app.services.campaign_service.AbilityData`), so the bridge is
``ability_id`` → ``technique_id`` → DAG node:

* **total_dag_techniques** — the number of **distinct** DAG techniques (nodes with
  ``technique_id``, de-duplicated by ``technique_id`` in node order), the
  denominator of ``success_rate`` and ``procedural_coverage`` (Req. 15.1, 15.3).
* **successful_techniques** — how many DAG techniques had at least one Ability
  result in ``success`` (a technique may map to more than one Ability).
* **success_rate** — ``successful_techniques / total_dag_techniques`` ∈ ``[0, 1]``
  (``0.0`` when the DAG has no techniques, to avoid division by zero — Req. 15.1).
* **procedural_coverage** — the **effectively executable** proportion: the DAG
  techniques that were **attempted** (some result that left ``pending``), over the
  total — ∈ ``[0, 1]`` (Req. 15.3).
* **failed_nodes** — the techniques whose consolidated status is ``failure``
  (failed and had no success), in DAG order, each identified by the
  representative node's ``node_id`` and annotated with the **DAG step** where the
  failure occurred (Req. 15.2).
* **time per technique / total** (Req. 15.4) and **telemetry per technique** (Req.
  15.5), consolidated from the per-Ability results when available.
* **success_verdict** — the overall verdict: ``True`` iff the DAG is non-empty,
  **all** its techniques had success and no node failed (Req. 15.6, 10.5).

The measurement is **pure and deterministic** — no network, Docker or AI. It
operates on in-memory structures (the per-Ability results + the DAG + the
``ability_id`` → technique map), so the tests exercise it without a database. For
the production path, :meth:`MeasurementService.measure_from_campaign` resolves the
curated DAG and the technique map from a
:class:`~app.services.campaign_service.CaseData`.

_Requirements: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from app.models.enums import AbilityResultStatus
from app.services.campaign_service import CaseData, DagData, DagNode
from app.services.environment.report import (
    SufficiencyMetrics,
    TechniqueMetric,
    ExecutionReport,
)
from app.services.stage.stage_service import Stage3AbilityResult


# ---------------------------------------------------------------------------
# Normalized measurement input (one result per executed Ability)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AbilityMeasurementResult:
    """The normalized measurement input — one result per executed Ability.

    A thin, neutral layer over the system's various per-Ability result sources
    (the historical :class:`~app.services.stage.stage_service.Stage3AbilityResult`
    or the :class:`~app.services.environment.execution.ExecutionAbilityResult` of
    the task 8.2 seam). The measurement consumes **only** what it needs, staying
    decoupled from the concrete result shape.

    Attributes:
        ability_id: The executed Ability (the bridge to the technique via the
            ``ability_id`` → ``technique_id`` map).
        status: The final :class:`~app.models.enums.AbilityResultStatus`.
        execution_time_s: Ability execution time in seconds, when measured
            (Req. 15.4); ``None`` when unavailable.
        telemetry: Telemetry signals observed (process/network/auth/file) during
            the execution (Req. 15.5).
    """

    ability_id: Optional[str] = None
    status: AbilityResultStatus = AbilityResultStatus.PENDING
    execution_time_s: Optional[float] = None
    telemetry: tuple[str, ...] = ()

    @classmethod
    def from_stage3(cls, result: Stage3AbilityResult) -> "AbilityMeasurementResult":
        """Normalize a :class:`Stage3AbilityResult` (no time/telemetry)."""
        return cls(ability_id=result.ability_id, status=result.status)

    @classmethod
    def from_execution(cls, result: object) -> "AbilityMeasurementResult":
        """Normalize a *duck-typed* result (``ability_id``/``status``).

        Accepts any object that exposes ``ability_id`` and ``status`` — for
        example the
        :class:`~app.services.environment.execution.ExecutionAbilityResult` of the
        task 8.2 seam — and the optional ``execution_time_s`` and ``telemetry``
        fields when present.
        """
        telemetry = getattr(result, "telemetry", ()) or ()
        return cls(
            ability_id=getattr(result, "ability_id", None),
            status=getattr(result, "status", AbilityResultStatus.PENDING),
            execution_time_s=getattr(result, "execution_time_s", None),
            telemetry=tuple(telemetry),
        )


# ---------------------------------------------------------------------------
# DAG technique (measurement unit) — structural node with technique_id
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DagTechnique:
    """A distinct DAG technique — the measurement unit (Req. 15.1, 15.3).

    Attributes:
        technique_id: The technique's ``technique_id``.
        node_id: The representative node's ``node_id`` (the 1st node of that
            technique), to annotate the :class:`TechniqueMetric` and the
            ``failed_nodes``.
        step: The DAG step (``node_type`` → ``tactic`` → ``node_index``) used to
            annotate where a failure occurred (Req. 15.2).
    """

    technique_id: str
    node_id: Optional[str] = None
    step: Optional[str] = None


def _step_of_node(node: DagNode) -> Optional[str]:
    """Derive the 'DAG step' of a node, to annotate the failure (Req. 15.2).

    Prefers ``node_type`` (the structural phase), falls back to ``tactic`` (the
    ATT&CK tactic) and finally to the node index. ``None`` when none is present.
    """
    if node.node_type:
        return node.node_type
    if node.tactic:
        return node.tactic
    if node.node_index is not None:
        return f"node_{node.node_index}"
    return None


def _dag_techniques(dag: Optional[DagData]) -> list[_DagTechnique]:
    """Extract the **distinct** DAG techniques, preserving node order.

    A technique is a structural node with ``technique_id`` (the same definition as
    :func:`app.services.stage.stage_service._iter_technique_nodes`),
    de-duplicated by ``technique_id`` — the 1st node of each technique is the
    representative (provides ``node_id`` and the step). Preserves node order for a
    deterministic output.
    """
    if dag is None:
        return []
    techniques: list[_DagTechnique] = []
    seen: set[str] = set()
    for node in dag.structural_nodes:
        tid = node.technique_id
        if not tid or tid in seen:
            continue
        seen.add(tid)
        techniques.append(
            _DagTechnique(technique_id=tid, node_id=node.node_id, step=_step_of_node(node))
        )
    return techniques


# ---------------------------------------------------------------------------
# Internal per-technique result aggregate
# ---------------------------------------------------------------------------


@dataclass
class _TechniqueAggregate:
    """Mutable accumulator of per-technique results during the measurement."""

    attempted: bool = False
    success: bool = False
    failure: bool = False
    time_s: Optional[float] = None
    telemetry: list[str] = field(default_factory=list)

    def accumulate(self, result: AbilityMeasurementResult) -> None:
        """Accumulate one per-Ability result into this technique."""
        if result.status is not AbilityResultStatus.PENDING:
            self.attempted = True
        if result.status is AbilityResultStatus.SUCCESS:
            self.success = True
        if result.status is AbilityResultStatus.FAILURE:
            self.failure = True
        if result.execution_time_s is not None:
            self.time_s = (self.time_s or 0.0) + result.execution_time_s
        for signal in result.telemetry:
            if signal not in self.telemetry:
                self.telemetry.append(signal)

    @property
    def status(self) -> AbilityResultStatus:
        """The consolidated technique status (success > failure > attempted > pending).

        A technique in ``success`` has at least one Ability in success; it is only
        ``failure`` when it failed and had no success; ``running`` when it was
        attempted but did not complete; ``pending`` when it was never attempted.
        """
        if self.success:
            return AbilityResultStatus.SUCCESS
        if self.failure:
            return AbilityResultStatus.FAILURE
        if self.attempted:
            return AbilityResultStatus.RUNNING
        return AbilityResultStatus.PENDING


# ---------------------------------------------------------------------------
# The measurement service (pure, no network/Docker/AI)
# ---------------------------------------------------------------------------


class MeasurementService:
    """Derive :class:`SufficiencyMetrics` and consolidate into :class:`ExecutionReport`.

    The measurement is **pure and deterministic**: given the list of per-Ability
    results, the curated DAG and the ``ability_id`` → ``technique_id`` map, it
    produces the procedural-sufficiency metrics (Req. 15) without touching the
    network, Docker or AI. The entry points:

    * :meth:`measure` — the pure derivation, over in-memory structures (results +
      DAG + map); the target of the tests and of the composition.
    * :meth:`consolidate` — packs the metrics (without recomputing) into an
      :class:`ExecutionReport` with the reused results.
    * :meth:`measure_from_campaign` / :meth:`report_from_campaign` — the production
      convenience that extracts the curated DAG and the technique map from a
      :class:`~app.services.campaign_service.CaseData` and delegates to the above.

    No ``Session`` dependency nor external construction — the service is a grouper
    of pure functions; instantiating it is optional (the methods are instance
    methods only to keep the package's *service* style).

    _Requirements: 10.4, 10.5, 10.6, 10.7, 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7_
    """

    def measure(
        self,
        *,
        results: Sequence[AbilityMeasurementResult],
        dag: Optional[DagData],
        technique_by_ability: Mapping[str, Optional[str]],
    ) -> SufficiencyMetrics:
        """Derive the :class:`SufficiencyMetrics` from the results + the DAG (Req. 15).

        Args:
            results: The execution's per-Ability results (normalized into
                :class:`AbilityMeasurementResult`), in execution order.
            dag: The Campaign's curated DAG (Stage 3 output); defines the universe
                of techniques (the denominator). ``None``/empty ⇒ zeroed metrics.
            technique_by_ability: The ``ability_id`` → ``technique_id`` map of the
                curated Abilities (the result → technique bridge). An Ability with
                no mapped technique, or mapped to a technique OUTSIDE the DAG, does
                not contribute to any DAG technique. A missing ``ability_id`` is
                ignored.

        Returns:
            The consolidated :class:`SufficiencyMetrics` — total/success/rate,
            procedural coverage ∈ ``[0, 1]``, total time, the per-technique list
            (one per distinct DAG technique, in order), the failed nodes (with
            step) and the overall verdict.
        """
        techniques = _dag_techniques(dag)
        # Aggregator per technique_id, initialized with all DAG techniques
        # (techniques not attempted stay 'pending'/not covered).
        aggregates: dict[str, _TechniqueAggregate] = {
            t.technique_id: _TechniqueAggregate() for t in techniques
        }
        dag_technique_ids: set[str] = set(aggregates)

        for result in results:
            ability_id = result.ability_id
            if ability_id is None:
                continue
            technique = technique_by_ability.get(ability_id)
            # Only results whose Abilities map to a DAG technique count.
            if technique is None or technique not in dag_technique_ids:
                continue
            aggregates[technique].accumulate(result)

        per_technique: list[TechniqueMetric] = []
        failed_nodes: list[str] = []
        successful_techniques = 0
        attempted_techniques = 0
        total_time: Optional[float] = None

        for technique in techniques:
            aggregate = aggregates[technique.technique_id]
            status = aggregate.status
            if aggregate.success:
                successful_techniques += 1
            if aggregate.attempted:
                attempted_techniques += 1
            if aggregate.time_s is not None:
                total_time = (total_time or 0.0) + aggregate.time_s

            failed = status is AbilityResultStatus.FAILURE
            failed_step = technique.step if failed else None
            if failed:
                failed_nodes.append(technique.node_id or technique.technique_id)

            per_technique.append(
                TechniqueMetric(
                    technique_id=technique.technique_id,
                    node_id=technique.node_id,
                    status=status,
                    failed_step=failed_step,
                    execution_time_s=aggregate.time_s,
                    telemetry=list(aggregate.telemetry),
                )
            )

        total = len(techniques)
        success_rate = (successful_techniques / total) if total else 0.0
        coverage = (attempted_techniques / total) if total else 0.0
        # Overall verdict: non-empty DAG, ALL techniques in success and no failed
        # node (Req. 15.6, 10.5). An empty DAG is never a success.
        verdict = total > 0 and successful_techniques == total and not failed_nodes

        return SufficiencyMetrics(
            total_dag_techniques=total,
            successful_techniques=successful_techniques,
            success_rate=success_rate,
            procedural_coverage=coverage,
            total_time_s=total_time,
            per_technique=per_technique,
            failed_nodes=failed_nodes,
            success_verdict=verdict,
        )

    def consolidate(
        self,
        *,
        campaign_id: str,
        metrics: SufficiencyMetrics,
        results: Optional[Sequence[Stage3AbilityResult]] = None,
        environment_deprovisioned: bool = False,
        generated_at: Optional[dt.datetime] = None,
    ) -> ExecutionReport:
        """Consolidate the metrics + the results into an :class:`ExecutionReport`.

        The Stage 6 output (Req. 10.6, 10.7, 15.7): joins the measured
        :class:`SufficiencyMetrics` (preserved in full, **without** recomputing),
        the :class:`~app.services.stage.stage_service.Stage3AbilityResult` reused
        from the execution, the environment deprovisioning flag (Req. 12, filled
        by task 8.4) and the generation timestamp.

        Args:
            campaign_id: The *slug* of the Campaign whose execution originated the
                report.
            metrics: The metrics derived by :meth:`measure`.
            results: The per-Ability results reused from the historical execution
                stage (``Stage3*``); default: empty.
            environment_deprovisioned: ``True`` iff the environment has already
                been deprovisioned (filled at the end of Stage 6 — Req. 12).
            generated_at: The generation timestamp; default: now (UTC).
        """
        return ExecutionReport(
            campaign_id=campaign_id,
            metrics=metrics,
            results=list(results or []),
            environment_deprovisioned=environment_deprovisioned,
            generated_at=generated_at or dt.datetime.now(dt.timezone.utc),
        )

    def measure_from_campaign(
        self,
        *,
        campaign: CaseData,
        results: Sequence[AbilityMeasurementResult],
    ) -> SufficiencyMetrics:
        """Production convenience: measure from a :class:`CaseData`.

        Extracts the curated DAG (``campaign.dag``) and the ``ability_id`` →
        ``technique_id`` map of the curated Abilities (``campaign.abilities``) and
        delegates to :meth:`measure`. Keeps the derivation in one place — the API
        layer (task 8.5) only needs to resolve the Campaign and pass the execution
        results.
        """
        technique_by_ability: dict[str, Optional[str]] = {
            a.ability_id: a.technique_id for a in campaign.abilities
        }
        return self.measure(
            results=results,
            dag=campaign.dag,
            technique_by_ability=technique_by_ability,
        )

    def report_from_campaign(
        self,
        *,
        campaign: CaseData,
        ability_results: Sequence[AbilityMeasurementResult],
        stage3_results: Sequence[Stage3AbilityResult],
        environment_deprovisioned: bool = False,
        generated_at: Optional[dt.datetime] = None,
    ) -> ExecutionReport:
        """Measure and consolidate in a single step — the complete Stage 6 output.

        Combines :meth:`measure_from_campaign` and :meth:`consolidate`: derives the
        metrics from the Campaign + the per-Ability results and packs the report
        with the reused ``Stage3AbilityResult``. It is the composition point the
        API/seam layer uses to produce the final :class:`ExecutionReport`.
        """
        metrics = self.measure_from_campaign(campaign=campaign, results=ability_results)
        return self.consolidate(
            campaign_id=campaign.id,
            metrics=metrics,
            results=stage3_results,
            environment_deprovisioned=environment_deprovisioned,
            generated_at=generated_at,
        )


__all__ = [
    "AbilityMeasurementResult",
    "MeasurementService",
]
