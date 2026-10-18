"""Property-based test for the fail-closed Stage 5 → Stage 6 advance gate (task 8.2).

# Feature: stage6-execution-redesign, Property 16: Fail-closed do avanço do Estágio 5

*Para qualquer* conjunto de verificações de ambiente do Estágio 5, se ao menos uma
verificação falha, o avanço para o Estágio 6 é bloqueado (fail-closed) e o fluxo
permanece no Estágio 5.

**Validates: Requirements 5.3**

The Stage 5 → Stage 6 advance is the **aggregate** of the full set of Stage 5
environment verifications (Req. 5.1): the provisioning prerequisites — healthy
target containers (``health.approved``), loaded Abilities (``loaded_abilities``)
and approved isolation (``isolation.approved``), summarized by
:attr:`ProvisionedEnvironment.ready` — **and** the agent-health gate
(:meth:`EnvironmentStageService.verify_agents_health` ⇒ ``released`` iff ≥ 1
``alive=true`` agent in the expected group).

This property drives the **real** :class:`EnvironmentStageService` end-to-end over
an ephemeral SQLite engine, composing the aggregate advance verdict exactly the way
the Stage 5 flow does:

* ``provision_stage5`` completes Stage 5 (``StageRun`` stage=5 → ``COMPLETED``)
  **only** when the provisioning is ``ready`` (the three environment prerequisites
  passed); any failed prerequisite leaves Stage 5 **not** completed; and
* the sequential gate (:meth:`StageService.is_stage_startable` for stage 6)
  releases Stage 6 **only** when Stage 5 is ``COMPLETED``; and
* the agent-health gate (``verify_agents_health``) must also release.

So the aggregate ``advance_released`` is ``True`` **iff ALL** checks pass; **any**
failing check blocks the advance (fail-closed) and the flow stays in Stage 5
(Stage 6 not startable).

The injected dependencies follow the existing conventions: a controllable
:class:`_FakeProvisioner` (structurally satisfies ``EnvironmentProvisioner``,
never touching Docker/network/AI) returning a configurable
:class:`ProvisionedEnvironment`; a :class:`httpx.MockTransport`-backed
``CalderaClient`` serving ``GET /api/v2/agents`` for the agent-health gate; and an
ephemeral SQLite ``Session`` seeded with Stage 4 ``COMPLETED`` (so the sequential
gate releases Stage 5 provisioning). *No real emulation, no network, no AI.*

An **independent oracle** recomputes the aggregate advance straight from the
generated per-check outcomes, so the assertions never trust the service to decide
what "advance released" means.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable, Optional

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StageRun
from app.models.enums import StageState, TranslationSource
from app.services.caldera.client import AGENTS_PATH, CalderaClient
from app.services.environment.compose_executor import LogLine
from app.services.environment.health import HealthSummary, HealthVerdict
from app.services.environment.log_buffer import ProvisioningLogBuffer
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
    TargetHost,
)
from app.services.environment.provisioner import (
    ContainerVerdict,
    IsolationSummary,
    ProvisionedEnvironment,
)
from app.services.environment.stage_service import EnvironmentStageService
from app.services.stage.stage_service import (
    STAGE_EXECUTION,
    STAGE_OPERATIONALIZATION,
    StageService,
)

_CAMPAIGN = "shadowray"
_C1 = "sticks-nginx-target"
_C2 = "sticks-mariadb-target"
_CONTAINERS = [_C1, _C2]
EXPECTED_GROUP = "red"


# ---------------------------------------------------------------------------
# Fake EnvironmentProvisioner — returns a configurable Output, no Docker/network.
# ---------------------------------------------------------------------------


class _FakeProvisioner:
    """Test double of :class:`EnvironmentProvisioner` returning a fixed Output.

    Structurally satisfies the ``@runtime_checkable`` ``EnvironmentProvisioner``
    (exposes ``origin``/``provision``/``deprovision``/``prepare``), never touching
    Docker, the network or AI. ``provision`` returns the configured
    :class:`ProvisionedEnvironment` (ready or with a failed prerequisite),
    emitting no lines (the aggregate advance does not depend on the console).
    """

    def __init__(self, environment: ProvisionedEnvironment) -> None:
        self._environment = environment

    def origin(self) -> TranslationSource:
        return TranslationSource.HUMAN_CURATION

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        return self._environment

    def deprovision(self, environment, *, on_log=lambda _l: None):  # pragma: no cover
        raise AssertionError("deprovision must not be called in this test")

    def prepare(self, campaign: str):  # pragma: no cover
        raise AssertionError("prepare must not be called in active provisioning")


# ---------------------------------------------------------------------------
# ProvisionedEnvironment builder parametrized by the three env prerequisites.
# ---------------------------------------------------------------------------


def _environment(
    *, health_ok: bool, abilities_ok: bool, isolation_ok: bool
) -> ProvisionedEnvironment:
    """Build a Stage 5 Output with the three prerequisites set independently.

    ``ProvisionedEnvironment.ready`` is ``True`` iff ``failed_step is None`` and
    all three of ``health.approved``, ``loaded_abilities`` and
    ``isolation.approved`` are ``True`` (Req. 5.5). We mirror the real pipeline's
    early-return contract: when a prerequisite fails, ``failed_step`` names the
    first failing step (health → abilities → isolation), so ``ready`` is ``False``.
    """
    if not health_ok:
        failed_step: Optional[str] = "aguardar_saude"
    elif not abilities_ok:
        failed_step = "carregar_abilities"
    elif not isolation_ok:
        failed_step = "verificar_isolamento"
    else:
        failed_step = None

    error_message = None if failed_step is None else f"falha em {failed_step}"

    return ProvisionedEnvironment(
        campaign_id=_CAMPAIGN,
        platform=EnvironmentPlatform.CONTAINER,
        target_containers=_CONTAINERS,
        compose_file="docker/docker-compose.yml",
        health=HealthSummary(
            approved=health_ok,
            verdicts=[
                HealthVerdict(container=c, running=True, healthy=health_ok)
                for c in _CONTAINERS
            ],
        ),
        loaded_abilities=abilities_ok,
        isolation=IsolationSummary(
            approved=isolation_ok,
            verdicts=[
                ContainerVerdict(container=c, isolated=isolation_ok)
                for c in _CONTAINERS
            ],
        ),
        origin=TranslationSource.HUMAN_CURATION,
        failed_step=failed_step,
        error_message=error_message,
    )


# ---------------------------------------------------------------------------
# Ephemeral session (SQLite engine on a temporary file + per-example schema).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, object]:
    """Create an ephemeral SQLite engine + full schema; return (session, db_path, engine)."""
    tmp_dir = tempfile.mkdtemp(prefix="stage5_advance_failclosed_")
    db_path = Path(tmp_dir) / "advance.db"
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


def _seed_stage(sess: Session, stage: int, state: StageState) -> None:
    """Seed a ``StageRun`` row for ``(_CAMPAIGN, stage)`` in the given state."""
    sess.add(
        StageRun(
            campaign_id=_CAMPAIGN,
            stage=stage,
            state=state,
            progress=100 if state is StageState.COMPLETED else 0,
        )
    )
    sess.commit()


def _params() -> EnvironmentParameters:
    """``EnvironmentParameters`` with two resolved hosts (Stage 5 Input)."""
    return EnvironmentParameters(
        campaign_id=_CAMPAIGN,
        hosts=[
            TargetHost(label="target-1", container=_C1),
            TargetHost(label="target-2", container=_C2),
        ],
    )


def _caldera_serving(*, agent_alive: bool) -> CalderaClient:
    """Build a CalderaClient whose ``GET /api/v2/agents`` returns one agent.

    When ``agent_alive`` is ``True`` the expected group has one ``alive=true``
    agent (gate releases); when ``False`` it has only an orphan (``alive=false``),
    so the agent-health gate blocks (no active agent in the group).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == AGENTS_PATH
        payload = [{"paw": "agent-1", "group": EXPECTED_GROUP, "alive": agent_alive}]
        return httpx.Response(200, json=payload)

    return CalderaClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    health_ok=st.booleans(),
    abilities_ok=st.booleans(),
    isolation_ok=st.booleans(),
    agent_ok=st.booleans(),
)
def test_stage5_advance_released_iff_all_checks_pass(
    health_ok: bool,
    abilities_ok: bool,
    isolation_ok: bool,
    agent_ok: bool,
) -> None:
    """Advance to Stage 6 is released iff ALL Stage 5 checks pass (fail-closed).

    # Feature: stage6-execution-redesign, Property 16: Fail-closed do avanço do Estágio 5
    **Validates: Requirements 5.3**

    For any combination of the four per-check outcomes (health, abilities,
    isolation, agent health), the aggregate advance verdict is released **iff all
    four pass**; if **any** check fails the advance is blocked (fail-closed) and
    the flow stays in Stage 5 (Stage 6 is not startable).
    """
    # Independent oracle: the aggregate advance is the conjunction of every check.
    expected_release = health_ok and abilities_ok and isolation_ok and agent_ok

    session, db_path, engine = _build_ephemeral_session()
    try:
        # Stage 4 completed so the sequential gate lets Stage 5 provisioning run.
        _seed_stage(session, STAGE_OPERATIONALIZATION, StageState.COMPLETED)

        provisioner = _FakeProvisioner(
            _environment(
                health_ok=health_ok,
                abilities_ok=abilities_ok,
                isolation_ok=isolation_ok,
            )
        )
        caldera = _caldera_serving(agent_alive=agent_ok)
        service = EnvironmentStageService(
            session,
            provisioner=provisioner,
            log_buffer=ProvisioningLogBuffer(),
            params_provider=lambda _c: _params(),
            caldera_client=caldera,
            agent_group=EXPECTED_GROUP,
        )
        stage_service = StageService(session)

        # --- Run the full set of Stage 5 environment verifications. -------
        provision_result = service.provision_stage5(_CAMPAIGN)
        agent_health = service.verify_agents_health(_CAMPAIGN)

        # The provisioning readiness mirrors the three environment prerequisites.
        env_prereqs_ok = health_ok and abilities_ok and isolation_ok
        assert provision_result.ready is env_prereqs_ok

        # Stage 5 completes (and thus the sequential gate releases Stage 6) ONLY
        # when the provisioning is ready — any failed prerequisite leaves it blocked.
        stage6_startable = stage_service.is_stage_startable(_CAMPAIGN, STAGE_EXECUTION)
        assert stage6_startable is env_prereqs_ok

        # Aggregate advance verdict composed the way the Stage 5 flow does: Stage 5
        # completed (sequential gate) AND agent-health gate released.
        advance_released = stage6_startable and agent_health.released

        # Core biconditional (Req. 5.3): released iff ALL checks pass.
        assert advance_released is expected_release

        # Fail-closed: any failing check blocks the advance and the flow stays in
        # Stage 5 (Stage 6 never reachable with the environment not ready).
        if not expected_release:
            assert advance_released is False
            assert stage_service.is_stage_startable(_CAMPAIGN, STAGE_EXECUTION) == (
                env_prereqs_ok
            )
            # With any env prerequisite failing, Stage 5 is NOT completed.
            if not env_prereqs_ok:
                row = (
                    session.query(StageRun)
                    .filter(StageRun.campaign_id == _CAMPAIGN)
                    .filter(StageRun.stage == 5)
                    .order_by(StageRun.id.desc())
                    .first()
                )
                assert row is not None
                assert row.state is not StageState.COMPLETED
    finally:
        session.close()
        engine.dispose()  # type: ignore[attr-defined]
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass


@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    health_ok=st.booleans(),
    abilities_ok=st.booleans(),
    isolation_ok=st.booleans(),
    agent_ok=st.booleans(),
)
def test_any_single_failing_check_blocks_advance(
    health_ok: bool,
    abilities_ok: bool,
    isolation_ok: bool,
    agent_ok: bool,
) -> None:
    """If at least one check fails, the advance is blocked (fail-closed).

    # Feature: stage6-execution-redesign, Property 16: Fail-closed do avanço do Estágio 5
    **Validates: Requirements 5.3**

    This is the contrapositive framing of the biconditional, stated directly in
    the language of Req. 5.3: *if at least one verification fails, the advance to
    Stage 6 is blocked and the flow stays in Stage 5.*
    """
    checks = (health_ok, abilities_ok, isolation_ok, agent_ok)
    any_failure = not all(checks)

    session, db_path, engine = _build_ephemeral_session()
    try:
        _seed_stage(session, STAGE_OPERATIONALIZATION, StageState.COMPLETED)

        service = EnvironmentStageService(
            session,
            provisioner=_FakeProvisioner(
                _environment(
                    health_ok=health_ok,
                    abilities_ok=abilities_ok,
                    isolation_ok=isolation_ok,
                )
            ),
            log_buffer=ProvisioningLogBuffer(),
            params_provider=lambda _c: _params(),
            caldera_client=_caldera_serving(agent_alive=agent_ok),
            agent_group=EXPECTED_GROUP,
        )
        stage_service = StageService(session)

        service.provision_stage5(_CAMPAIGN)
        agent_health = service.verify_agents_health(_CAMPAIGN)
        advance_released = (
            stage_service.is_stage_startable(_CAMPAIGN, STAGE_EXECUTION)
            and agent_health.released
        )

        if any_failure:
            # Fail-closed: blocked, flow stays in Stage 5.
            assert advance_released is False
        else:
            # Only the all-pass combination releases the advance.
            assert advance_released is True
    finally:
        session.close()
        engine.dispose()  # type: ignore[attr-defined]
        try:
            db_path.unlink()
            db_path.parent.rmdir()
        except OSError:
            pass
