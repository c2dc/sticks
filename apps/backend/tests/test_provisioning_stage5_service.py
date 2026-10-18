"""Service tests for the active Stage 5 orchestration (task 5.5).

Exercises :meth:`~app.services.environment.stage_service.EnvironmentStageService.provision_stage5`
— the **active, automatic provisioning** of Stage 5 (Simulation environment
creation), triggered with no manual action when the Stage starts (Req. 1.1) —
end-to-end over an **ephemeral** SQLite engine (same seam as the
``test_provisioning_auto_property.py`` fixtures), seeding ``StageRun`` rows
per campaign.

Instead of touching Docker, the disk or the real operationalization seam, it
injects:

* a controllable :class:`_FakeProvisioner` — structurally satisfies the
  :class:`~app.services.environment.provisioner.EnvironmentProvisioner`,
  emits :class:`~app.services.environment.compose_executor.LogLine` via ``on_log``
  (in order) and returns a configurable
  :class:`~app.services.environment.provisioner.ProvisionedEnvironment` (ready or
  with ``failed_step``), **counting** the calls to ``provision`` (to assert
  idempotency);
* a ``params_provider`` *lambda* that resolves the
  :class:`~app.services.environment.operationalization.EnvironmentParameters`
  without touching operationalization nor the disk;
* a **real** instance of
  :class:`~app.services.environment.log_buffer.ProvisioningLogBuffer`, so the
  assertions inspect the buffer the WebSocket channel reads (Req. 2.5, 2.7).

No path touches the Docker daemon, the network or AI — everything is pure DB
logic + in-memory *fakes*, safe in a dev environment.

Coverage (each test cites the requirements it satisfies):

1. Automatic provisioning (Req. 1.1): Stage 4 completed + environment ready ⇒
   the pipeline runs with no manual action, completes Stage 5 and returns
   ``ready``.
2. Gate (Req. 1.1): Stage 4 not completed ⇒ ``blocked_by_gate``, nothing brought
   up nor completed.
3. Completion only with the three prerequisites (Req. 5.5): environment with
   ``failed_step`` ⇒ Stage 5 does **not** complete (``ERROR``), with
   ``failed_step``/``error_message``.
4. Failure blocks Stage 6 (Req. 6.4): after a failure Stage 6 is not startable;
   after success it is released.
5. Buffer and recovery (Req. 7.1, 7.3): the emitted lines reach the buffer in
   ``seq`` order (monotonic from 1), a fresh run clears the previous buffer
   (``seq`` restarts at 1), and ``new_since`` returns only the delta.
6. Idempotency (Req. 1.6): after success + persisted Output, a second call
   **reuses** without calling ``provision`` again (``reused``), with the
   ``ProvisionedEnvironment`` rebuilt from the persisted JSON.
7. Screen ↔ console consistency (Req. 7.5, supports 7.3): the ``StageRun``'s
   ``error_message`` is the ``text`` of the last error line of the failed step
   in the buffer.

_Requirements: 1.1, 1.6, 5.5, 6.4, 7.1, 7.3_
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Optional

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import ProvisionedEnvironmentRow, StageRun
from app.models.enums import StageState, TranslationSource
from app.services.environment.compose_executor import LogLine
from app.services.environment.log_buffer import ProvisioningLogBuffer
from app.services.environment.operationalization import (
    TargetHost,
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.provisioner import (
    ProvisionedEnvironment,
    IsolationSummary,
    EnvironmentProvisioner,
    ContainerVerdict,
)
from app.services.environment.health import HealthSummary, HealthVerdict
from app.services.environment.stage_service import EnvironmentStageService
from app.services.stage.stage_service import (
    STAGE_ENVIRONMENT,
    STAGE_EXECUTION,
    STAGE_OPERATIONALIZATION,
    StageService,
)

_CAMPAIGN = "shadowray"
_C1 = "sticks-nginx-target"
_C2 = "sticks-mariadb-target"


# ---------------------------------------------------------------------------
# Fake EnvironmentProvisioner — controllable, counts calls, emits LogLine.
# ---------------------------------------------------------------------------


class _FakeProvisioner:
    """Test double of :class:`EnvironmentProvisioner` for the Stage 5 service.

    Structurally satisfies the ``@runtime_checkable`` ``EnvironmentProvisioner``
    (exposes ``origin``/``provision``/``deprovision``/``prepare``), never
    touching Docker, the network or AI. In ``provision``:

    * emits the configured :class:`LogLine` (``lines``) via ``on_log``, in order
      — exactly as the real provisioner pushes each step's lines to the
      buffer/console (Req. 2.2); and
    * returns the configured :class:`ProvisionedEnvironment` (``environment``) —
      ready or with ``failed_step`` —, without repeating the real provisioner's
      logic.

    Counts ``provision_calls`` to assert that idempotency (Req. 1.6) does
    **not** re-invoke provisioning when the environment is already ready and
    persisted.
    """

    def __init__(
        self,
        environment: ProvisionedEnvironment,
        *,
        lines: Optional[list[LogLine]] = None,
    ) -> None:
        self._environment = environment
        self._lines = list(lines or [])
        self.provision_calls = 0

    def origin(self) -> TranslationSource:
        return TranslationSource.HUMAN_CURATION

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        self.provision_calls += 1
        for line in self._lines:
            on_log(line)
        return self._environment

    def deprovision(self, environment, *, on_log=lambda _l: None):  # pragma: no cover
        raise AssertionError("deprovision must not be called in this test")

    def prepare(self, campaign: str):  # pragma: no cover
        raise AssertionError("prepare must not be called in active provisioning")


# ---------------------------------------------------------------------------
# ProvisionedEnvironment builders (Stage 5 Output).
# ---------------------------------------------------------------------------


def _environment_ready(containers: list[str]) -> ProvisionedEnvironment:
    """A **ready** Output: health + Abilities + isolation approved (Req. 5.5)."""
    return ProvisionedEnvironment(
        campaign_id=_CAMPAIGN,
        platform=EnvironmentPlatform.CONTAINER,
        target_containers=containers,
        compose_file="docker/docker-compose.yml",
        health=HealthSummary(
            approved=True,
            verdicts=[
                HealthVerdict(container=c, running=True, healthy=True)
                for c in containers
            ],
        ),
        loaded_abilities=True,
        isolation=IsolationSummary(
            approved=True,
            verdicts=[
                ContainerVerdict(container=c, isolated=True) for c in containers
            ],
        ),
        origin=TranslationSource.HUMAN_CURATION,
    )


def _failed_environment(
    containers: list[str],
    *,
    failed_step: str = "aguardar_saude",
    error_message: str = "the target container is not healthy",
) -> ProvisionedEnvironment:
    """An Output with a step failure: Stage 5 must not complete (Req. 7.1)."""
    return ProvisionedEnvironment(
        campaign_id=_CAMPAIGN,
        platform=EnvironmentPlatform.CONTAINER,
        target_containers=containers,
        compose_file="docker/docker-compose.yml",
        health=HealthSummary(
            approved=False,
            verdicts=[
                HealthVerdict(
                    container=containers[0],
                    running=True,
                    healthy=False,
                    reason="healthcheck reportou status 'unhealthy'",
                )
            ],
        ),
        loaded_abilities=False,
        isolation=IsolationSummary(approved=False),
        origin=TranslationSource.HUMAN_CURATION,
        failed_step=failed_step,
        error_message=error_message,
    )


# ---------------------------------------------------------------------------
# Ephemeral session (SQLite engine on a temporary file + per-test schema).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create an ephemeral SQLite engine + full schema, return ``(session, db_path, engine)``.

    The schema is created via ``Base.metadata.create_all`` (same as the
    ``test_provisioning_auto_property.py`` fixtures), so the
    ``provisioned_environment`` table (task 5.2 model) exists — the Output
    persistence and idempotency (Req. 1.6) depend on it.
    """
    tmp_dir = tempfile.mkdtemp(prefix="pipeline5_env_service_")
    db_path = Path(tmp_dir) / "env_service.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return factory(), db_path, engine


@pytest.fixture()
def session() -> "object":
    """Provide an ephemeral SQLite ``Session``, discarding the file at the end."""
    sess, db_path, engine = _build_ephemeral_session()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()  # type: ignore[attr-defined]
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


def _seed_stage(
    sess: Session, campaign_id: str, stage: int, state: StageState
) -> None:
    """Seed a ``StageRun`` row for ``(campaign_id, stage)`` in the given state."""
    sess.add(
        StageRun(
            campaign_id=campaign_id,
            stage=stage,
            state=state,
            progress=100 if state is StageState.COMPLETED else 0,
        )
    )
    sess.commit()


def _stage5_row(sess: Session, campaign_id: str) -> Optional[StageRun]:
    """The ``StageRun`` row of ``(campaign_id, stage=5)``, if any."""
    return (
        sess.execute(
            select(StageRun)
            .where(StageRun.campaign_id == campaign_id)
            .where(StageRun.stage == STAGE_ENVIRONMENT)
            .order_by(StageRun.id.asc())
        )
        .scalars()
        .first()
    )


def _params() -> EnvironmentParameters:
    """``EnvironmentParameters`` with two resolved hosts (Stage 5 Input)."""
    return EnvironmentParameters(
        campaign_id=_CAMPAIGN,
        hosts=[
            TargetHost(label="target-1", container=_C1),
            TargetHost(label="target-2", container=_C2),
        ],
    )


def _make_service(
    sess: Session,
    provisioner: EnvironmentProvisioner,
    buffer: ProvisioningLogBuffer,
) -> EnvironmentStageService:
    """Build the service with the injected provisioner/buffer and a ``params_provider`` *lambda*."""
    return EnvironmentStageService(
        sess,
        provisioner=provisioner,
        log_buffer=buffer,
        params_provider=lambda _CAMPAIGN: _params(),
    )


# ===========================================================================
# 1. Automatic provisioning (Req. 1.1)
# ===========================================================================


def test_auto_provisioning_completes_stage5(session: Session) -> None:
    """Stage 4 completed + environment ready ⇒ pipeline runs with no manual action and completes.

    Provisioning is triggered by ``provision_stage5`` with no manual "provision"
    action (Req. 1.1): the fake provisioner runs the pipeline and returns a ready
    environment, the service completes Stage 5 (``StageRun`` stage=5 →
    ``COMPLETED``/100) and returns ``ready=True``.

    _Requirements: 1.1_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.COMPLETED)
    prov = _FakeProvisioner(_environment_ready(containers))
    service = _make_service(session, prov, ProvisioningLogBuffer())

    result = service.provision_stage5(_CAMPAIGN)

    # The pipeline actually ran (no manual action) and completed.
    assert prov.provision_calls == 1
    assert result.blocked_by_gate is False
    assert result.ready is True
    assert result.reused is False
    assert result.failed_step is None
    assert result.error_message is None
    assert result.environment is not None
    assert result.environment.target_containers == containers

    # The Stage 5 StageRun was completed at 100.
    row = _stage5_row(session, _CAMPAIGN)
    assert row is not None
    assert row.state is StageState.COMPLETED
    assert row.progress == 100
    assert row.failed_step is None
    assert row.error_message is None


# ===========================================================================
# 2. Gate — Stage 4 not completed (Req. 1.1)
# ===========================================================================


def test_gate_blocks_without_stage4_completed(session: Session) -> None:
    """Stage 4 not completed ⇒ ``blocked_by_gate``, provision NOT called.

    The sequential gate prevents provisioning: the fake ``provision`` is never
    called and no ``stage=5`` row is completed.

    _Requirements: 1.1_
    """
    containers = [_C1, _C2]
    # Stage 4 in progress (not completed) + Stage 5 not started yet.
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.IN_PROGRESS)
    prov = _FakeProvisioner(_environment_ready(containers))
    service = _make_service(session, prov, ProvisioningLogBuffer())

    result = service.provision_stage5(_CAMPAIGN)

    assert result.blocked_by_gate is True
    assert result.ready is False
    assert prov.provision_calls == 0
    # No completed stage=5 row.
    row = _stage5_row(session, _CAMPAIGN)
    assert row is None or row.state is not StageState.COMPLETED


# ===========================================================================
# 3. Completion only with the three prerequisites (Req. 5.5)
# ===========================================================================


def test_completes_only_with_three_prerequisites(session: Session) -> None:
    """Environment with ``failed_step`` ⇒ Stage 5 does NOT complete (``ERROR``).

    When the pipeline returns a **not ready** environment with ``failed_step``
    (e.g. ``aguardar_saude``), Stage 5 is not completed: the ``StageRun``
    stage=5 stays in ``ERROR`` with ``failed_step`` + ``error_message``.

    _Requirements: 5.5_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.COMPLETED)
    environment = _failed_environment(
        containers, failed_step="aguardar_saude", error_message="health rejected"
    )
    prov = _FakeProvisioner(environment)
    service = _make_service(session, prov, ProvisioningLogBuffer())

    result = service.provision_stage5(_CAMPAIGN)

    assert result.ready is False
    assert result.failed_step == "aguardar_saude"
    assert result.error_message is not None and result.error_message != ""

    row = _stage5_row(session, _CAMPAIGN)
    assert row is not None
    assert row.state is StageState.ERROR
    assert row.state is not StageState.COMPLETED
    assert row.failed_step == "aguardar_saude"
    assert row.error_message is not None


# ===========================================================================
# 4. Failure blocks Stage 6 (Req. 6.4)
# ===========================================================================


def test_failure_blocks_stage6(session: Session) -> None:
    """After a failure Stage 6 is not startable; after success it is released.

    The sequential gate of :meth:`StageService.is_stage_startable` depends only
    on the Stage 5 state: while Stage 5 is not ``COMPLETED``, Stage 6 stays
    blocked (Req. 6.4); once completed, it is released.

    _Requirements: 6.4_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.COMPLETED)
    stage_service = StageService(session)

    # Failure ⇒ Stage 5 in ERROR ⇒ Stage 6 blocked.
    prov_failure = _FakeProvisioner(_failed_environment(containers))
    service_failure = _make_service(session, prov_failure, ProvisioningLogBuffer())
    result_failure = service_failure.provision_stage5(_CAMPAIGN)
    assert result_failure.ready is False
    assert stage_service.is_stage_startable(_CAMPAIGN, STAGE_EXECUTION) is False

    # Success ⇒ Stage 5 COMPLETED ⇒ Stage 6 released.
    prov_ok = _FakeProvisioner(_environment_ready(containers))
    service_ok = _make_service(session, prov_ok, ProvisioningLogBuffer())
    result_ok = service_ok.provision_stage5(_CAMPAIGN)
    assert result_ok.ready is True
    assert stage_service.is_stage_startable(_CAMPAIGN, STAGE_EXECUTION) is True


# ===========================================================================
# 5. Buffer and recovery (Req. 7.1, 7.3)
# ===========================================================================


def test_buffer_receives_lines_in_order_and_clears_between_runs(
    session: Session,
) -> None:
    """Lines reach the buffer in ``seq`` order (from 1); a fresh run clears it.

    The fake provisioner emits several :class:`LogLine` via ``on_log``; the
    service forwards them to the injected :class:`ProvisioningLogBuffer`.
    ``read(campaign)`` returns them in monotonic ``seq`` order starting at 1. A
    second (fresh) run clears the buffer: the previous run's lines do **not**
    leak and ``seq`` restarts at 1. ``new_since(campaign, k)`` returns only the
    delta (``seq>k``).

    _Requirements: 7.1, 7.3_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.COMPLETED)
    buffer = ProvisioningLogBuffer()

    lines_run1 = [
        LogLine(step="subir_conteineres", stream="command", text="docker compose up"),
        LogLine(step="subir_conteineres", stream="stdout", text="Container criado"),
        LogLine(step="aguardar_saude", stream="info", text="checking health"),
        LogLine(step="verificar_isolamento", stream="info", text="isolation ok"),
    ]
    # First run: failure (not completed) to allow a second fresh run (once
    # completed, idempotency would reuse without running the pipeline).
    prov1 = _FakeProvisioner(
        _failed_environment(containers, failed_step="verificar_isolamento"),
        lines=lines_run1,
    )
    service1 = _make_service(session, prov1, buffer)
    service1.provision_stage5(_CAMPAIGN)

    buffered = buffer.read(_CAMPAIGN)
    # All emitted lines reached the buffer, in order, seq monotonic from 1.
    assert [l.text for l in buffered] == [l.text for l in lines_run1]
    assert [l.seq for l in buffered] == [1, 2, 3, 4]
    # Strictly increasing seq order (monotonic).
    seqs = [l.seq for l in buffered]
    assert seqs == sorted(seqs)

    # new_since returns only the delta (seq > k).
    assert [l.seq for l in buffer.new_since(_CAMPAIGN, 2)] == [3, 4]
    assert buffer.new_since(_CAMPAIGN, 4) == []

    # Second fresh run: the buffer is cleared, seq restarts at 1, and the
    # previous run's lines do not leak.
    lines_run2 = [
        LogLine(step="subir_conteineres", stream="stdout", text="new run"),
        LogLine(step="aguardar_saude", stream="stdout", text="healthy"),
    ]
    prov2 = _FakeProvisioner(_environment_ready(containers), lines=lines_run2)
    service2 = _make_service(session, prov2, buffer)
    service2.provision_stage5(_CAMPAIGN)

    buffered2 = buffer.read(_CAMPAIGN)
    assert [l.text for l in buffered2] == ["new run", "healthy"]
    assert [l.seq for l in buffered2] == [1, 2]  # seq restarted at 1
    # No line from the first run survived.
    texts2 = {l.text for l in buffered2}
    assert texts2.isdisjoint({l.text for l in lines_run1})


# ===========================================================================
# 6. Idempotency (Req. 1.6)
# ===========================================================================


def test_idempotency_reuses_without_running_pipeline(session: Session) -> None:
    """After success + persisted Output, the 2nd call reuses without calling provision.

    The first call completes Stage 5 and persists the ready Output
    (``ProvisionedEnvironmentRow``). The second call **reuses**: the fake
    ``provision`` is NOT called again (``provision_calls`` stays 1),
    ``reused=True``, ``ready=True`` and the ``ProvisionedEnvironment`` is rebuilt
    from the persisted JSON.

    _Requirements: 1.6_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.COMPLETED)
    prov = _FakeProvisioner(_environment_ready(containers))
    service = _make_service(session, prov, ProvisioningLogBuffer())

    # 1st call: runs the pipeline, completes and persists the Output.
    first = service.provision_stage5(_CAMPAIGN)
    assert first.ready is True
    assert first.reused is False
    assert prov.provision_calls == 1
    # The Output was persisted as a ready row.
    row = (
        session.execute(
            select(ProvisionedEnvironmentRow).where(
                ProvisionedEnvironmentRow.campaign_id == _CAMPAIGN
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.ready is True

    # 2nd call: reuses without running the pipeline again.
    second = service.provision_stage5(_CAMPAIGN)
    assert second.reused is True
    assert second.ready is True
    assert prov.provision_calls == 1  # provision was NOT called again
    # The ProvisionedEnvironment was rebuilt from the persisted JSON (same as original).
    assert second.environment is not None
    assert second.environment.target_containers == containers
    assert second.environment.ready is True
    assert second.environment.origin is TranslationSource.HUMAN_CURATION


# ===========================================================================
# 7. error_message ↔ console consistency (Req. 7.5, supports 7.3)
# ===========================================================================


def test_error_message_derived_from_last_console_error_line(
    session: Session,
) -> None:
    """The ``StageRun``'s ``error_message`` is the text of the step's last error line.

    When a step fails, the service derives the ``error_message`` from the
    **last** ``stderr``/``erro`` line the failed step left in the buffer — the
    same text the live terminal/console shows —, guaranteeing by construction the
    screen ↔ console consistency.

    _Requirements: 7.3_
    """
    containers = [_C1, _C2]
    _seed_stage(session, _CAMPAIGN, STAGE_OPERATIONALIZATION, StageState.COMPLETED)
    buffer = ProvisioningLogBuffer()

    step = "aguardar_saude"
    ultima_linha_erro = "healthcheck reportou status 'unhealthy' para sticks-nginx-target"
    lines = [
        LogLine(step="subir_conteineres", stream="stdout", text="Container up"),
        LogLine(step=step, stream="info", text="awaiting health..."),
        LogLine(step=step, stream="stderr", text="first erro intermediário"),
        LogLine(step=step, stream="stderr", text=ultima_linha_erro),
        # An error line from ANOTHER step must not be chosen.
        LogLine(step="subir_conteineres", stream="stderr", text="erro de outra step"),
    ]
    environment = _failed_environment(
        containers, failed_step=step, error_message="fallback do provisioner"
    )
    prov = _FakeProvisioner(environment, lines=lines)
    service = _make_service(session, prov, buffer)

    result = service.provision_stage5(_CAMPAIGN)

    # The error_message is exactly the text of the last error line OF THE failed step.
    assert result.error_message == ultima_linha_erro
    row = _stage5_row(session, _CAMPAIGN)
    assert row is not None
    assert row.error_message == ultima_linha_erro

    # And that line is, in fact, in the buffer the console renders.
    buffer_texts = [l.text for l in buffer.read(_CAMPAIGN)]
    assert ultima_linha_erro in buffer_texts
