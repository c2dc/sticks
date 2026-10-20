"""CompletionStage6Service — deprovisioning + Stage 6 completion (task 8.4).

Stage 6 (execution + measurement + report) has three phases already built by
earlier tasks: the **execution** reuses ``OperationRunner`` with the four gates
(task 8.2, :class:`~app.services.environment.execution.ExecutionStageService`);
the **measurement + report** derives the procedural-sufficiency metrics and
consolidates the :class:`~app.services.environment.report.ExecutionReport` (task
8.3, :class:`~app.services.environment.measurement.MeasurementService`); and Stage 5
(task 7.x) defined the :class:`~app.services.environment.provisioner.EnvironmentProvisioner`
seam with the ``deprovision`` method.

This task is the **completion seam** that ties the three phases in the order
required by the design (Req. 10.8, 10.9, 12):

1. **Runs** Stage 6 reusing ``ExecutionStageService`` with **deferred
   completion** (``complete=False``) — the stage completion does **not** happen
   during execution; this seam decides when to complete (Req. 10.9).
2. **Measures and generates the report** from the execution results + the curated
   DAG (via ``MeasurementService``), on technique success **or** failure.
3. **ALWAYS deprovisions** the campaign environment via
   :meth:`EnvironmentProvisioner.deprovision` — on technique success **or**
   failure, even when execution was refused by a gate or aborted (Req. 10.8,
   12.1). The teardown removes the created containers (Req. 12.2); a teardown
   failure is reported identifying the **remaining resources** (Req. 12.3),
   without bringing down the flow.
4. **Completes Stage 6** through the single completion path
   (``persist_stage_completion``) **only** when (a) execution actually completed
   (``COMPLETED`` + ``FINISHED``), (b) the environment was deprovisioned and
   (c) the results + the report are persisted (Req. 10.9). If any condition
   fails, the stage does **not** complete.

The report stamps ``environment_deprovisioned`` with the real teardown result
(Req. 12), so the Stage 6 output reflects whether the environment was actually
cleaned up. Persisting the :class:`ExecutionReport` is delegated to an
**injectable persister** (``report_sink``) — the ``execution_reports`` table is
task 10.1/10.2; here the seam only requires that *some* sink confirms the
persistence before completion, and the tests inject an in-memory sink.

As in the rest of the package, the external dependencies (the provisioner, the
report sink, the execution/measurement service) are **injected**, so the tests
replace the provisioner with a *fake* (a mock that records the teardown) without
real Caldera or Docker.

_Requirements: 10.8, 10.9, 12.1, 12.2, 12.3_
"""

from __future__ import annotations

import datetime as dt
import inspect
from typing import Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models.domain import ExecutionReport as ExecutionReportRow
from app.services.campaign_service import CaseData, CaseService
from app.services.environment.execution import (
    ExecutionResult,
    ExecutionStageService,
    RunnerFactory,
)
from app.services.environment.measurement import MeasurementService, AbilityMeasurementResult
from app.services.environment.operationalization import EnvironmentParameters
from app.services.environment.provisioner import (
    ProvisionedEnvironment,
    DeprovisioningResult,
    EnvironmentProvisioner,
)
from app.services.environment.report import ExecutionReport
from app.services.stage.stage_service import Stage3AbilityResult


#: A persister for the execution's :class:`ExecutionReport`. It confirms the
#: persistence (``True``) before completing Stage 6 (Req. 10.9). Injectable so
#: the tests use an in-memory sink without the table.
#:
#: The sink may optionally accept the :class:`DeprovisioningResult` of the
#: teardown as a second argument so the persistence records the structured
#: teardown outcome — ``deprovisioned`` and the ``remaining_resources`` that the
#: read path recovers (Req. 7.3). The completion seam calls the sink defensively:
#: a legacy single-argument sink (``(report) -> bool``) keeps working unchanged,
#: while a two-argument sink (``(report, deprovisioning) -> bool``), such as
#: :class:`DbReportSink`, also receives the teardown.
ReportSink = Callable[..., bool]


def _sink_accepts_deprovisioning(sink: ReportSink) -> bool:
    """Whether ``sink`` accepts the :class:`DeprovisioningResult` second argument.

    Inspects the callable's signature: a sink declared with two positional
    parameters (``(report, deprovisioning)``) — or a ``*args``/``**kwargs``
    catch-all — is handed the teardown; a legacy single-parameter sink
    (``(report)``) is not. When the signature cannot be introspected (a C builtin,
    for instance), it falls back to the conservative single-argument call.
    """
    try:
        signature = inspect.signature(sink)
    except (TypeError, ValueError):
        return False
    positional = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            return True
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
    return positional >= 2


class DbReportSink:
    """Teardown-aware :class:`ReportSink` writing the ``execution_reports`` row.

    Persists the consolidated :class:`ExecutionReport` into the
    :class:`~app.models.domain.ExecutionReport` table (``execution_reports``) and
    records the **structured teardown** of the end of Stage 6 (Req. 7.3): the
    boolean ``deprovisioned`` and the ``remaining_resources`` that remained when
    the teardown failed, recovered later by the report read path.

    It is a ``__call__``-able so it satisfies :data:`ReportSink`; the completion
    seam detects its two-argument signature and hands it the
    :class:`DeprovisioningResult` alongside the report. The write is delegated to
    :meth:`_persist`, which maps the DTO + teardown onto the ORM row.
    """

    def __init__(self, session: Session) -> None:
        """Bind the sink to an open :class:`~sqlalchemy.orm.Session`.

        The session is injected by the caller (the API layer) and is **not**
        opened or closed here; :meth:`_persist` flushes the row within it.
        """
        self._session = session

    def __call__(
        self,
        report: ExecutionReport,
        deprovisioning: Optional[DeprovisioningResult] = None,
    ) -> bool:
        """Persist ``report`` + the ``deprovisioning`` teardown; confirm on success.

        Returns ``True`` when the row was written (the completion precondition of
        Req. 10.9). A persistence failure is turned into ``False`` (the stage does
        not complete) without propagating, consistent with the seam treating the
        persistence as a confirmable precondition.
        """
        try:
            self._persist(report, deprovisioning)
            return True
        except Exception:  # defensive: a failed write blocks completion, not the flow
            self._session.rollback()
            return False

    def _persist(
        self,
        report: ExecutionReport,
        deprovisioning: Optional[DeprovisioningResult],
    ) -> None:
        """Write the ``execution_reports`` row from the report + the teardown.

        Serializes the :class:`SufficiencyMetrics` into the portable ``metrics``
        JSON column (as the ORM model documents) and stamps the teardown fields
        from the :class:`DeprovisioningResult` produced at completion: the boolean
        ``deprovisioned`` and the JSON ``remaining_resources`` (the containers/
        networks that remained on a failed teardown — Req. 7.3). When no teardown
        is supplied, both stay ``None`` (nullable columns), preserving the legacy
        booleano ``environment_deprovisioned`` already carried by the report.
        """
        row = ExecutionReportRow(
            campaign_id=report.campaign_id,
            metrics=report.metrics.model_dump(mode="json"),
            success_verdict=report.metrics.success_verdict,
            environment_deprovisioned=report.environment_deprovisioned,
            deprovisioned=(
                deprovisioning.deprovisioned if deprovisioning is not None else None
            ),
            remaining_resources=(
                list(deprovisioning.remaining_resources)
                if deprovisioning is not None
                else None
            ),
            generated_at=report.generated_at,
        )
        self._session.add(row)
        self._session.flush()


class CompletionStage6Result(BaseModel):
    """Typed result of the Stage 6 completion — execution + report + teardown.

    Serializable projection of the full Stage 6 flow, for the API layer
    (task 8.5) and for the tests. Carries the execution result, the consolidated
    report, the deprovisioning result and whether the stage was actually
    completed.

    Attributes:
        campaign_id: The *slug* of the Campaign whose Stage 6 was executed.
        execution: The :class:`~app.services.environment.execution.ExecutionResult`
            of the execution (with deferred completion — ``execution.completed`` is
            always ``False`` here; the real completion is ``completed`` below).
        report: The consolidated :class:`ExecutionReport` (metrics + results
            + deprovisioning flag), generated on technique success **or** failure
            (Req. 10.6, 10.7).
        deprovisioning: The :class:`DeprovisioningResult` of the teardown —
            **always** executed (Req. 10.8, 12.1); on failure it identifies the
            remaining resources (Req. 12.3).
        report_persisted: ``True`` iff the report was persisted by the sink
            (precondition of completion, Req. 10.9).
        completed: ``True`` iff Stage 6 was marked ``completed`` by this call —
            that is, execution completed, the environment was deprovisioned and
            the results + report are persisted (Req. 10.9).
    """

    campaign_id: str
    execution: ExecutionResult
    report: ExecutionReport
    deprovisioning: DeprovisioningResult
    report_persisted: bool = False
    completed: bool = False


class CompletionStage6Service:
    """Stage 6 completion seam — always teardown + gated completion (task 8.4).

    Orchestrates the three Stage 6 phases in the design order, **without**
    reimplementing any of them: it delegates execution to
    :class:`~app.services.environment.execution.ExecutionStageService` (deferred
    completion), measurement/report to
    :class:`~app.services.environment.measurement.MeasurementService`, and the
    teardown to the :class:`~app.services.environment.provisioner.EnvironmentProvisioner`
    seam (injectable). The only logic of its own is the **order** and the
    **completion gate** (Req. 10.8, 10.9, 12).

    _Requirements: 10.8, 10.9, 12.1, 12.2, 12.3_
    """

    def __init__(
        self,
        session: Session,
        *,
        runner_factory: RunnerFactory,
        provisioner: EnvironmentProvisioner,
        report_sink: ReportSink,
        case_service: Optional[CaseService] = None,
        measurement: Optional[MeasurementService] = None,
    ) -> None:
        """Assemble the Stage 6 completion seam.

        Args:
            session: Open :class:`~sqlalchemy.orm.Session`, injected by the
                caller — passed through to ``ExecutionStageService`` and the
                completion path. The service does not open or close it.
            runner_factory: ``(session) -> OperationRunner`` factory passed through
                to ``ExecutionStageService`` (the tests inject a fake runner or a
                real one with *fakes*).
            provisioner: The
                :class:`~app.services.environment.provisioner.EnvironmentProvisioner`
                seam whose ``deprovision`` is **always** called at the end (Req.
                10.8, 12.1). The tests inject a *fake* that records the teardown.
            report_sink: Persister of the :class:`ExecutionReport`
                (``True`` when it persisted). Precondition of completion (Req.
                10.9).
            case_service: :class:`CaseService` to resolve/read the curated Campaign
                (DAG + Abilities for the measurement). Default: production.
            measurement: The (pure) :class:`MeasurementService` used to measure/consolidate.
                Default: a new instance.
        """
        self._session = session
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )
        self._execution = ExecutionStageService(
            session,
            runner_factory=runner_factory,
            case_service=self._case_service,
        )
        self._provisioner = provisioner
        self._report_sink = report_sink
        self._measurement = measurement if measurement is not None else MeasurementService()

    def execute_and_complete(
        self,
        campaign: str,
        *,
        confirmed: bool,
        environment: ProvisionedEnvironment,
        generated_at: Optional[dt.datetime] = None,
    ) -> CompletionStage6Result:
        """Run, measure, generate the report, **deprovision** and complete (Req. 10.8–10.9, 12).

        The flow, in the design order:

        1. Runs Stage 6 with **deferred completion** (``complete=False``):
           ``ExecutionStageService`` applies the four gates and, on success,
           persists the ``AbilityResult``/``AuditLogEntry`` per command — but
           does **not** complete the stage yet (Req. 10.9).
        2. Measures procedural sufficiency and consolidates the
           :class:`ExecutionReport` from the execution results + the curated DAG,
           on technique success **or** failure (Req. 10.4–10.7).
        3. **ALWAYS** deprovisions the ``environment`` via
           :meth:`EnvironmentProvisioner.deprovision` — even when execution was
           refused/aborted or failed (Req. 10.8, 12.1); it stamps the result in
           ``report.environment_deprovisioned`` (Req. 12). A teardown failure is
           reported with the remaining resources (Req. 12.3) and does **not**
           bring down the flow.
        4. Persists the report (via the ``report_sink``) and completes Stage 6
           **only** when execution completed, the environment was deprovisioned
           and the report was persisted (Req. 10.9). Otherwise the stage does
           **not** complete.

        Args:
            campaign: The *slug* of the curated Campaign to execute.
            confirmed: Explicit confirmation of the execution (runner gate 1).
            environment: The :class:`ProvisionedEnvironment` from Stage 5 — the
                target of the deprovisioning at the end (Req. 12.1, 12.2).
            generated_at: Report generation timestamp; default: now (UTC).

        Returns:
            The :class:`CompletionStage6Result` with the execution, the report
            (with the deprovisioning flag), the teardown result and whether Stage
            6 was completed.
        """
        case = self._load_case(campaign)

        # 1) Execution with DEFERRED completion (completion is decided here, after
        #    the deprovisioning — Req. 10.9).
        execution = self._execution.execute(
            campaign, confirmed=confirmed, complete=False
        )

        # 2) Measurement + report, on technique success OR failure (Req. 10.6, 10.7).
        ability_results = [
            AbilityMeasurementResult.from_execution(r) for r in execution.results
        ]
        stage3_results = [
            Stage3AbilityResult(
                ability_id=r.ability_id or "",
                status=r.status,
                command_output=r.output,
            )
            for r in execution.results
        ]

        # 3) ALWAYS deprovision — on success or failure (Req. 10.8, 12.1). The
        #    teardown never brings down the flow; a failure is reported in the result.
        deprovisioning = self._deprovision(environment)

        report = self._measurement.report_from_campaign(
            campaign=case,
            ability_results=ability_results,
            stage3_results=stage3_results,
            environment_deprovisioned=deprovisioning.deprovisioned,
            generated_at=generated_at,
        )

        # 4) Persist the report (precondition of completion, Req. 10.9). The
        #    structured teardown (deprovisioned + remaining_resources) is handed to
        #    the sink so the persistence records it for the read path (Req. 7.3) —
        #    passed only when the sink accepts it, keeping legacy single-argument
        #    sinks working unchanged.
        report_persisted = bool(self._persist_report(report, deprovisioning))

        # Completion ONLY when: execution actually completed AND environment
        # deprovisioned AND results + report persisted (Req. 10.9).
        completed = False
        if (
            self._execution.executed_successfully(execution)
            and deprovisioning.deprovisioned
            and report_persisted
        ):
            completed = self._execution.complete_stage6(campaign)

        return CompletionStage6Result(
            campaign_id=campaign,
            execution=execution,
            report=report,
            deprovisioning=deprovisioning,
            report_persisted=report_persisted,
            completed=completed,
        )

    # -- helpers -------------------------------------------------------------

    def _persist_report(
        self, report: ExecutionReport, deprovisioning: DeprovisioningResult
    ) -> bool:
        """Invoke the report sink, handing it the teardown when it accepts it.

        The structured :class:`DeprovisioningResult` (``deprovisioned`` +
        ``remaining_resources``) is passed to the sink only when its signature
        accepts a second positional argument, so a legacy single-argument sink
        (``(report) -> bool``) keeps working unchanged, while a teardown-aware sink
        (``(report, deprovisioning) -> bool``, e.g. :class:`DbReportSink`) records
        the teardown for the read path (Req. 7.3).
        """
        if _sink_accepts_deprovisioning(self._report_sink):
            return bool(self._report_sink(report, deprovisioning))
        return bool(self._report_sink(report))

    def _deprovision(
        self, environment: ProvisionedEnvironment
    ) -> DeprovisioningResult:
        """Call ``deprovision`` defensively — never brings down the flow.

        The deprovisioning is **always** attempted (Req. 10.8, 12.1). An
        unexpected exception from the provisioner is caught and turned into a
        failure result that identifies the remaining resources (the environment's
        target containers, Req. 12.3) — the failure is reported, not propagated,
        so it does not mask the execution already performed nor block the report.
        """
        try:
            return self._provisioner.deprovision(environment)
        except Exception as exc:  # defensive: teardown never brings down Stage 6
            return DeprovisioningResult(
                deprovisioned=False,
                remaining_resources=list(environment.target_containers),
                origin=environment.origin,
                message=(
                    "Failed to deprovision the campaign environment; resources "
                    f"remained provisioned: {exc}"
                ),
            )

    def _load_case(self, campaign: str) -> CaseData:
        """Resolve and read the curated Campaign ``campaign`` (DAG + Abilities for the measurement).

        Same distinction as the execution seam: a *slug* outside the catalog is a
        :class:`KeyError`; a missing/malformed Campaign is a descriptive
        :class:`ValueError` (the API maps it to 404/422).
        """
        if campaign not in self._case_service.list_cases():
            raise KeyError(f"Unknown campaign: {campaign!r}.")
        result = self._case_service.try_load_case(campaign)
        if not result.ok or result.case is None:
            detail = (
                result.error.detail
                if result.error
                else f"Failed to read Campaign {campaign!r}."
            )
            raise ValueError(detail)
        return result.case


__all__ = [
    "ReportSink",
    "DbReportSink",
    "CompletionStage6Result",
    "CompletionStage6Service",
]
