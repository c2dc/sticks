"""Example-based tests for ``CompletionStage6Service`` — teardown + completion (task 8.4).

The Stage 6 completion seam ties the three phases in the design's order:
executes (deferred completion) → measures/generates the report → **ALWAYS**
deprovisions → concludes Stage 6 **only** when the execution concluded, the
environment was deprovisioned and the results + report are persisted. These
tests prove:

1. **Happy path**: COMPLETED + FINISHED → teardown called, the report marks
   ``environment_deprovisioned=True``, report persisted, Stage 6 completed.
2. **Technique failure (ABORTED)**: the teardown is **still** called (Req.
   10.8, 12.1); the report is generated; Stage 6 does **not** complete (execution
   did not conclude).
3. **Gate refusal (NOT_CONFIRMED)**: the teardown is called even with no execution;
   Stage 6 does not complete.
4. **Deprovisioning failure**: ``deprovisioned=False`` reports the remaining
   resources (Req. 12.3), the report marks ``environment_deprovisioned=False``
   and Stage 6 does **not** complete, even with a COMPLETED execution.
5. **Provisioner exception**: caught and turned into a teardown failure that
   identifies the remaining resources — the stream is not torn down.
6. **Report not persisted**: Stage 6 does not complete even with a COMPLETED
   execution and a successful teardown (Req. 10.9).

Everything external is *mocked*: a fake runner (projection/single-call), a *fake*
provisioner that records the teardown, and an in-memory report sink — no Caldera,
Docker nor the ``execution_reports`` table. The database is a temporary SQLite.

_Requirements: 10.8, 10.9, 12.1, 12.2, 12.3_
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
from app.services.campaign_service import CaseService
from app.services.environment.completion import CompletionStage6Service
from app.services.environment.provisioner import (
    ProvisionedEnvironment,
    DeprovisioningResult,
)
from app.services.environment.report import ExecutionReport
from app.services.operation.runner import (
    AbilityRunResult,
    OperationRunResult,
    RunOutcome,
)
from app.services.stage.stage_service import STAGE_EXECUTION


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
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_completion6_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture()
def session() -> Iterator[Session]:
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_completion6_db_"))
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
    """A fake ``EnvironmentProvisioner`` recording teardown calls.

    Only exercises ``origin`` + ``deprovision`` (the Stage-6 teardown path);
    the other Protocol methods are unused by this seam. ``result`` is what
    ``deprovision`` returns; ``raise_exc`` makes it blow up instead (to prove
    the seam catches it).
    """

    result: DeprovisioningResult
    raise_exc: bool = False
    torn_down: list[ProvisionedEnvironment] = field(default_factory=list)

    def origin(self) -> TranslationSource:
        return TranslationSource.HUMAN_CURATION

    def deprovision(
        self, environment: ProvisionedEnvironment
    ) -> DeprovisioningResult:
        self.torn_down.append(environment)
        if self.raise_exc:
            raise RuntimeError("docker daemon unreachable during teardown")
        return self.result


class _MemorySink:
    """An in-memory ``ReportSink`` recording persisted reports.

    ``ok`` controls whether the sink confirms persistence (``True``) — the
    conclusion gate requires a persisted report (Req. 10.9).
    """

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.saved: list[ExecutionReport] = []

    def __call__(self, report: ExecutionReport) -> bool:
        self.saved.append(report)
        return self.ok


def _environment() -> ProvisionedEnvironment:
    return ProvisionedEnvironment(
        campaign_id=_SLUG,
        target_containers=["victim-web", "victim-db"],
        loaded_abilities=True,
    )


def _ok_teardown() -> DeprovisioningResult:
    return DeprovisioningResult(deprovisioned=True, mensagem="ok")


def _completed_result() -> OperationRunResult:
    return OperationRunResult(
        outcome=RunOutcome.COMPLETED,
        state=OperationState.FINISHED,
        operation_id=1,
        caldera_operation_id="op-1",
        total_success=1,
        total_failure=0,
        ability_results=(
            AbilityRunResult(
                ability_id=f"{_SLUG}-ab-1",
                status=AbilityResultStatus.SUCCESS,
                command="cat /etc/passwd",
                target_container="host executor (command local)",
                output="root:x:0:0",
            ),
        ),
        message="Operação finalizada: 1 success(s), 0 failure(s).",
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
                target_container="host executor (command local)",
            ),
        ),
        message="abortada: trilha de audit",
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
) -> CompletionStage6Service:
    return CompletionStage6Service(
        session,
        runner_factory=lambda _s: _FixedRunner(result=runner_result),  # type: ignore[arg-type,return-value]
        provisioner=provisioner,  # type: ignore[arg-type]
        report_sink=sink,
        case_service=CaseService(data_dir=data_dir),
    )


def _stage6_state(session: Session, campaign: str) -> StageState | None:
    row = (
        session.execute(
            select(StageRun)
            .where(StageRun.campaign_id == campaign)
            .where(StageRun.stage == STAGE_EXECUTION)
        )
        .scalars()
        .first()
    )
    return row.state if row is not None else None


# ---------------------------------------------------------------------------
# Happy path: execution concludes, teardown runs, Stage 6 concludes
# ---------------------------------------------------------------------------


def test_happy_path_tears_down_and_concludes(
    session: Session, data_dir: Path
) -> None:
    """COMPLETED+FINISHED -> teardown ran, report persisted, Stage 6 concluded.
    _Requirements: 10.8, 10.9, 12.1, 12.2_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(result=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    environment = _environment()
    result = service.execute_and_complete(_SLUG, confirmed=True, environment=environment)

    # Teardown was invoked with the provisioned environment (Req. 12.1, 12.2).
    assert provisioner.torn_down == [environment]
    assert result.deprovisioning.deprovisioned is True
    # The report reflects the teardown and was persisted (Req. 12, 10.9).
    assert result.report.environment_deprovisioned is True
    assert result.report_persisted is True
    assert sink.saved and sink.saved[0].campaign_id == _SLUG
    # Stage 6 concluded only after execution + teardown + persistence (Req. 10.9).
    assert result.completed is True
    assert _stage6_state(session, _SLUG) is StageState.COMPLETED


# ---------------------------------------------------------------------------
# ALWAYS deprovision — even on technique failure / refusal (Req. 10.8, 12.1)
# ---------------------------------------------------------------------------


def test_aborted_execution_still_tears_down_but_does_not_conclude(
    session: Session, data_dir: Path
) -> None:
    """ABORTED execution still tears down; Stage 6 does NOT conclude.
    _Requirements: 10.8, 12.1_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(result=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_aborted_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.execute_and_complete(
        _SLUG, confirmed=True, environment=_environment()
    )

    # Teardown ran DESPITE the failed techniques (Req. 10.8, 12.1).
    assert len(provisioner.torn_down) == 1
    assert result.deprovisioning.deprovisioned is True
    # A report is still generated (Req. 10.6, 10.7).
    assert result.report.campaign_id == _SLUG
    # Execution did not conclude -> Stage 6 does not conclude.
    assert result.completed is False
    assert _stage6_state(session, _SLUG) is None


def test_not_confirmed_still_tears_down(session: Session, data_dir: Path) -> None:
    """A refused (not-confirmed) execution still tears the environment down.
    _Requirements: 10.8, 12.1_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(result=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_not_confirmed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.execute_and_complete(
        _SLUG, confirmed=False, environment=_environment()
    )

    assert len(provisioner.torn_down) == 1
    assert result.completed is False
    assert _stage6_state(session, _SLUG) is None


# ---------------------------------------------------------------------------
# Teardown failure reports remaining resources and blocks conclusion
# ---------------------------------------------------------------------------


def test_teardown_failure_reports_remaining_resources_and_blocks_conclusion(
    session: Session, data_dir: Path
) -> None:
    """A failed teardown surfaces remaining resources and blocks conclusion.
    _Requirements: 12.3, 10.9_"""
    _seed_case(data_dir, _SLUG)
    failed_teardown = DeprovisioningResult(
        deprovisioned=False,
        remaining_resources=["victim-web", "victim-db"],
        mensagem="failure ao remover contêineres",
    )
    provisioner = _RecordingProvisioner(result=failed_teardown)
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.execute_and_complete(
        _SLUG, confirmed=True, environment=_environment()
    )

    # The remaining resources are identified (Req. 12.3).
    assert result.deprovisioning.deprovisioned is False
    assert result.deprovisioning.remaining_resources == [
        "victim-web",
        "victim-db",
    ]
    # The report reflects the environment was NOT deprovisioned.
    assert result.report.environment_deprovisioned is False
    # Even with a COMPLETED execution, a failed teardown blocks conclusion (10.9).
    assert result.completed is False
    assert _stage6_state(session, _SLUG) is None


def test_teardown_exception_is_caught_and_reported(
    session: Session, data_dir: Path
) -> None:
    """An exception from the provisioner is caught and reported as a teardown failure.
    _Requirements: 12.3_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(result=_ok_teardown(), raise_exc=True)
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.execute_and_complete(
        _SLUG, confirmed=True, environment=_environment()
    )

    # The exception did not propagate; it became a reported failure with the
    # remaining resources identified (the environment's target containers).
    assert result.deprovisioning.deprovisioned is False
    assert set(result.deprovisioning.remaining_resources) == {
        "victim-web",
        "victim-db",
    }
    assert result.completed is False


# ---------------------------------------------------------------------------
# Report persistence is a conclusion pre-condition (Req. 10.9)
# ---------------------------------------------------------------------------


def test_unpersisted_report_blocks_conclusion(
    session: Session, data_dir: Path
) -> None:
    """A report that fails to persist blocks conclusion despite success+teardown.
    _Requirements: 10.9_"""
    _seed_case(data_dir, _SLUG)
    provisioner = _RecordingProvisioner(result=_ok_teardown())
    sink = _MemorySink(ok=False)  # the sink refuses to persist
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )

    result = service.execute_and_complete(
        _SLUG, confirmed=True, environment=_environment()
    )

    assert result.deprovisioning.deprovisioned is True
    assert result.report_persisted is False
    assert result.completed is False
    assert _stage6_state(session, _SLUG) is None


# ---------------------------------------------------------------------------
# Unknown case
# ---------------------------------------------------------------------------


def test_unknown_case_raises_keyerror(session: Session, data_dir: Path) -> None:
    """An unknown slug raises KeyError (mapped to 404 by the endpoint layer)."""
    provisioner = _RecordingProvisioner(result=_ok_teardown())
    sink = _MemorySink(ok=True)
    service = _service(
        session,
        data_dir,
        runner_result=_completed_result(),
        provisioner=provisioner,
        sink=sink,
    )
    with pytest.raises(KeyError):
        service.execute_and_complete(
            "does-not-exist", confirmed=True, environment=_environment()
        )
