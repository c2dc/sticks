"""Unit tests for the Stage 5 ``AiEnvironmentProvisioner`` with faked drivers (task 11.5).

These example-based unit tests exercise :class:`AiEnvironmentProvisioner`
(``app/services/ai/agents/env_agent.py``) with **fakes only** — no network, no
SEPINF endpoint, no Docker daemon, no database. The AI provisioner delegates the
**unchanged** Stage 5 containment/isolation pipeline to an **injected**
``CurationEnvironmentProvisioner``-shaped collaborator (constructor kwarg
``pipeline=``), then **re-stamps** the output's origin to
``gerado_ia`` (:attr:`TranslationSource.AI_GENERATED`). Injecting a fake pipeline
means no step ever touches Docker/Caldera; the fake simply returns a fixed
``ProvisionedEnvironment`` / ``DeprovisioningResult`` so the tests can pin the
re-stamping behaviour (Req. 2.3, 2.4, 8.3):

1. **Fixed input → ``gerado_ia`` output, verdicts preserved.** A fixed
   :class:`EnvironmentParameters` driven through a fake pipeline that returns a
   fully-ready, isolated environment yields a :class:`ProvisionedEnvironment`
   with ``origin=gerado_ia`` and ``ready is True`` — every verdict/health/
   isolation/sub-stage field the pipeline computed is preserved verbatim; only
   the origin changes (Req. 2.4, 2.7).

2. **Isolation failure blocks Stage 5 completion.** When the injected pipeline
   returns a **non-isolated** environment (``isolation.approved`` False,
   ``failed_step="verificar_isolamento"``), the AI provisioner's output keeps
   ``ready is False`` (Stage 5 does not complete) while the origin is still
   re-stamped ``gerado_ia`` — the read-only, fail-closed isolation gate stays the
   unchanged completion pre-condition, exactly like curation (Req. 8.3, 9.3).

3. **Deprovision re-stamps origin, preserves the teardown outcome.** The teardown
   is delegated to the same pipeline; the AI provisioner re-stamps the result's
   origin to ``gerado_ia`` and preserves ``deprovisioned`` /
   ``remaining_resources`` / ``message`` verbatim (Req. 2.4, 12).

_Requirements: 2.3, 2.4, 8.3_
"""

from __future__ import annotations

from collections.abc import Callable

from app.models.enums import EnvironmentPlatform, TranslationSource
from app.services.ai.agents.env_agent import AiEnvironmentProvisioner
from app.services.environment.compose_executor import LogLine
from app.services.environment.health import HealthSummary, HealthVerdict
from app.services.environment.operationalization import (
    EnvironmentParameters,
    TargetHost,
)
from app.services.environment.provisioner import (
    ContainerVerdict,
    DeprovisioningResult,
    IsolationSummary,
    ProvisionedEnvironment,
    SubStageSummary,
)
from app.services.environment.resource_gate import ResourceGate, ResourceLimits

_SLUG = "ai-env-faked"
_CONTAINER = "nginx"


# ---------------------------------------------------------------------------
# Fakes — a daemon-free pipeline collaborator and fixed Stage 5 inputs/outputs
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Daemon-free ``CurationEnvironmentProvisioner``-shaped pipeline.

    Exposes the exact surface the AI provisioner delegates to
    (``provision``/``deprovision``), returning fixed, pre-built outputs so no
    Docker daemon, Caldera client or isolation inspector is ever touched. It
    records the ``params``/``environment`` it was handed and drives ``on_log``
    with a single line, so a test can assert the AI provisioner actually
    delegated to it and forwarded the callback unchanged.
    """

    def __init__(
        self,
        *,
        environment: ProvisionedEnvironment | None = None,
        result: DeprovisioningResult | None = None,
    ) -> None:
        self._environment = environment
        self._result = result
        self.provision_calls: list[EnvironmentParameters] = []
        self.deprovision_calls: list[ProvisionedEnvironment] = []
        self.logs: list[LogLine] = []

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        self.provision_calls.append(params)
        line = LogLine(step="verificar_isolamento", stream="info", text="fake pipeline line")
        self.logs.append(line)
        on_log(line)
        assert self._environment is not None
        return self._environment

    def deprovision(
        self,
        environment: ProvisionedEnvironment,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> DeprovisioningResult:
        self.deprovision_calls.append(environment)
        line = LogLine(step="deprovision", stream="info", text="fake teardown line")
        self.logs.append(line)
        on_log(line)
        assert self._result is not None
        return self._result


def _params() -> EnvironmentParameters:
    """A fixed Stage 4 output (``curadoria_humana`` origin) feeding provision."""
    return EnvironmentParameters(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.CONTAINER,
        hosts=[TargetHost(label="host_a", address="192.168.20.30", container=_CONTAINER)],
        network_context=["internal lab segment"],
        origin=TranslationSource.HUMAN_CURATION,
    )


def _ready_environment() -> ProvisionedEnvironment:
    """A fully-ready, isolated environment as the curation pipeline would build it.

    All six ``ready`` prerequisites satisfied and ``failed_step is None`` — it is
    stamped ``curadoria_humana`` so the test proves the AI provisioner re-stamps
    it to ``gerado_ia`` without disturbing any other field.
    """
    return ProvisionedEnvironment(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.CONTAINER,
        target_containers=[_CONTAINER],
        compose_file="docker/docker-compose.yml",
        health=HealthSummary(
            approved=True,
            verdicts=[HealthVerdict(container=_CONTAINER, running=True)],
        ),
        loaded_abilities=True,
        isolation=IsolationSummary(
            approved=True,
            verdicts=[ContainerVerdict(container=_CONTAINER, isolated=True)],
        ),
        substages=SubStageSummary(
            build_online_ok=True,
            egress_closed=True,
            egress_lost_verified=True,
            offline_applied=True,
        ),
        origin=TranslationSource.HUMAN_CURATION,
    )


def _non_isolated_environment() -> ProvisionedEnvironment:
    """An environment that FAILED the isolation gate (fail-closed, Req. 8.3).

    ``isolation.approved`` is ``False`` with a per-container reason, and the
    pipeline stopped at ``verificar_isolamento`` — so ``ready`` is ``False`` and
    Stage 5 does not complete. Stamped ``curadoria_humana`` to prove the AI
    provisioner still re-stamps origin even on the failure path.
    """
    return ProvisionedEnvironment(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.CONTAINER,
        target_containers=[_CONTAINER],
        compose_file="docker/docker-compose.yml",
        health=HealthSummary(
            approved=True,
            verdicts=[HealthVerdict(container=_CONTAINER, running=True)],
        ),
        loaded_abilities=True,
        isolation=IsolationSummary(
            approved=False,
            verdicts=[
                ContainerVerdict(
                    container=_CONTAINER,
                    isolated=False,
                    reasons=["attached to external bridge network"],
                    external_networks=["bridge"],
                )
            ],
        ),
        substages=SubStageSummary(
            build_online_ok=True,
            egress_closed=True,
            egress_lost_verified=True,
            offline_applied=True,
        ),
        origin=TranslationSource.HUMAN_CURATION,
        failed_step="verificar_isolamento",
        error_message="container nginx is not isolated: attached to external bridge network",
    )


# ---------------------------------------------------------------------------
# 1. Fixed input -> gerado_ia ProvisionedEnvironment, verdicts preserved
#    (Req. 2.4, 2.7)
# ---------------------------------------------------------------------------


def test_provision_restamps_origin_and_preserves_ready_environment() -> None:
    """Fixed input → ready environment with ``origin=gerado_ia``, fields preserved.

    The injected fake pipeline returns a fully-ready, isolated environment; the
    AI provisioner re-stamps ONLY the origin to ``gerado_ia`` and returns an
    output whose ``ready is True`` with every verdict/health/isolation/sub-stage
    field preserved verbatim (Req. 2.4, 2.7).
    """
    pipeline = _FakePipeline(environment=_ready_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    environment = agent.provision(_params())

    # The agent delegated to the injected pipeline exactly once.
    assert len(pipeline.provision_calls) == 1
    assert pipeline.provision_calls[0].campaign_id == _SLUG

    # Origin re-stamped to gerado_ia on BOTH the seam and the produced artifact.
    assert agent.origin() is TranslationSource.AI_GENERATED
    assert isinstance(environment, ProvisionedEnvironment)
    assert environment.origin is TranslationSource.AI_GENERATED

    # Stage 5 completes: all prerequisites still pass, no step failed.
    assert environment.ready is True
    assert environment.failed_step is None
    assert environment.error_message is None

    # Every field the pipeline computed is preserved verbatim (only origin changed).
    expected = _ready_environment()
    assert environment.model_dump(exclude={"origin"}) == expected.model_dump(exclude={"origin"})
    assert environment.health.approved is True
    assert environment.loaded_abilities is True
    assert environment.isolation.approved is True
    assert environment.isolation.verdicts[0].container == _CONTAINER
    assert environment.substages.egress_lost_verified is True


def test_provision_forwards_on_log_callback_to_pipeline() -> None:
    """The ``on_log`` callback is forwarded to the delegated pipeline unchanged.

    The seam keeps ``on_log`` keyword-only with a no-op default; when a caller
    passes one, the AI provisioner must hand it to the reused pipeline so the
    ordered, observable steps still emit their lines (Req. 2).
    """
    pipeline = _FakePipeline(environment=_ready_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    seen: list[LogLine] = []
    agent.provision(_params(), on_log=seen.append)

    assert seen == pipeline.logs
    assert seen[0].step == "verificar_isolamento"


# ---------------------------------------------------------------------------
# 2. Isolation failure blocks Stage 5 completion (Req. 8.3, 9.3)
# ---------------------------------------------------------------------------


def test_non_isolated_environment_stays_not_ready_and_restamped() -> None:
    """A non-isolated environment keeps ``ready is False`` but origin is re-stamped.

    When the injected pipeline fails the read-only, fail-closed isolation gate
    (``isolation.approved`` False, ``failed_step="verificar_isolamento"``), the
    AI provisioner does NOT complete Stage 5 — ``ready`` stays ``False`` — yet it
    still re-stamps the origin to ``gerado_ia`` and preserves the failure
    verdict/cause verbatim (Req. 8.3, 9.3, 2.4).
    """
    pipeline = _FakePipeline(environment=_non_isolated_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    environment = agent.provision(_params())

    # Origin re-stamped even on the failure path.
    assert environment.origin is TranslationSource.AI_GENERATED

    # Stage 5 does NOT complete: the isolation gate blocked it.
    assert environment.ready is False
    assert environment.isolation.approved is False
    assert environment.failed_step == "verificar_isolamento"
    assert "not isolated" in (environment.error_message or "")

    # The per-container failure reason is preserved verbatim for the screen.
    assert environment.isolation.non_isolated[0].container == _CONTAINER
    assert environment.isolation.verdicts[0].reasons == ["attached to external bridge network"]

    # Only the origin differs from what the pipeline returned.
    expected = _non_isolated_environment()
    assert environment.model_dump(exclude={"origin"}) == expected.model_dump(exclude={"origin"})


def test_isolation_is_the_only_failing_gate_that_blocks_completion() -> None:
    """Flipping isolation back to approved is what lets the SAME output complete.

    Confirms the isolation gate is the decisive, unchanged completion
    pre-condition: with every other prerequisite already satisfied, the output is
    ready iff ``isolation.approved`` and ``failed_step`` is cleared — the AI
    provisioner neither weakens nor bypasses it (Req. 8.3, 9.3).
    """
    blocked = AiEnvironmentProvisioner(
        pipeline=_FakePipeline(environment=_non_isolated_environment())
    ).provision(_params())
    assert blocked.ready is False

    ready = AiEnvironmentProvisioner(
        pipeline=_FakePipeline(environment=_ready_environment())
    ).provision(_params())
    assert ready.ready is True

    # Both carry the AI origin; they differ only on the isolation gate outcome.
    assert blocked.origin is ready.origin is TranslationSource.AI_GENERATED
    assert blocked.isolation.approved is False
    assert ready.isolation.approved is True


# ---------------------------------------------------------------------------
# 3. Deprovision re-stamps origin, preserves the teardown outcome (Req. 2.4, 12)
# ---------------------------------------------------------------------------


def test_deprovision_success_restamps_origin_and_preserves_result() -> None:
    """Successful teardown → ``deprovisioned=True`` preserved, origin re-stamped.

    The teardown is delegated to the same pipeline; the AI provisioner re-stamps
    the result's origin to ``gerado_ia`` and preserves ``deprovisioned`` /
    ``remaining_resources`` / ``message`` verbatim (Req. 2.4, 12).
    """
    result = DeprovisioningResult(
        deprovisioned=True,
        remaining_resources=[],
        origin=TranslationSource.HUMAN_CURATION,
        message="environment removed",
    )
    pipeline = _FakePipeline(result=result)
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    out = agent.deprovision(_ready_environment())

    assert len(pipeline.deprovision_calls) == 1
    assert out.origin is TranslationSource.AI_GENERATED
    assert out.deprovisioned is True
    assert out.remaining_resources == []
    assert out.message == "environment removed"


def test_deprovision_failure_preserves_remaining_resources() -> None:
    """Failed teardown → ``deprovisioned=False`` with remaining resources preserved.

    On a teardown failure the AI provisioner still only re-stamps the origin; the
    ``remaining_resources`` the pipeline identified for the Researcher and the
    cause ``message`` are preserved verbatim (Req. 2.4, 12.3).
    """
    result = DeprovisioningResult(
        deprovisioned=False,
        remaining_resources=["nginx", "lab_net"],
        origin=TranslationSource.HUMAN_CURATION,
        message="compose down failed: daemon unavailable",
    )
    pipeline = _FakePipeline(result=result)
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    out = agent.deprovision(_ready_environment())

    assert out.origin is TranslationSource.AI_GENERATED
    assert out.deprovisioned is False
    assert out.remaining_resources == ["nginx", "lab_net"]
    assert out.message == "compose down failed: daemon unavailable"


# ---------------------------------------------------------------------------
# 4. ResourceGate wired BEFORE provisioning (task 6.2, Req. 3.1, 12.2)
#    The agent proposes targets; the code-level gate disposes. A rejected plan
#    fails the environment stage with the exceeded-limit reasons and creates NO
#    container (the pipeline is never called).
# ---------------------------------------------------------------------------


def _params_with_hosts(count: int) -> EnvironmentParameters:
    """A fixed Stage 4 output proposing ``count`` target hosts (one per container)."""
    return EnvironmentParameters(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.CONTAINER,
        hosts=[TargetHost(label=f"host_{i}", container=f"c{i}") for i in range(count)],
        network_context=["internal lab segment"],
        origin=TranslationSource.HUMAN_CURATION,
    )


def test_resource_gate_rejection_creates_no_container_and_records_reasons() -> None:
    """A plan exceeding the container limit fails the stage; pipeline never runs.

    With a gate limited to 2 containers and an agent proposing 3, the
    ResourceGate rejects BEFORE any container is created: the injected pipeline's
    ``provision`` is NOT called, the returned environment carries
    ``failed_step="resource_gate"`` (so ``ready`` stays ``False``) with the
    exceeded-limit reason in ``error_message``, and the origin is still
    ``gerado_ia`` (task 6.2, Req. 3.1, 12.2).
    """
    pipeline = _FakePipeline(environment=_ready_environment())
    agent = AiEnvironmentProvisioner(
        pipeline=pipeline,
        resource_gate=ResourceGate(ResourceLimits(max_containers=2)),
    )

    environment = agent.provision(_params_with_hosts(3))

    # No container was created: the pipeline was never delegated to.
    assert pipeline.provision_calls == []

    # The stage failed on the resource gate, keeping Stage 5 incomplete.
    assert environment.failed_step == "resource_gate"
    assert environment.ready is False
    assert environment.origin is TranslationSource.AI_GENERATED
    assert "3" in (environment.error_message or "")
    assert "2" in (environment.error_message or "")

    # The proposed targets are preserved for traceability.
    assert environment.target_containers == ["c0", "c1", "c2"]


def test_resource_gate_rejection_records_verdict_in_trace() -> None:
    """The rejected ResourceGate verdict is recorded in the trace gates block.

    The environment stage trace's ``processing.gates.resource_gate`` carries
    ``approved=False`` and the exceeded-limit reason(s), so the audit shows the
    code-level gate ran and why it rejected (design §E, task 6.2).
    """
    sink: list[dict] = []
    pipeline = _FakePipeline(environment=_ready_environment())
    agent = AiEnvironmentProvisioner(
        pipeline=pipeline,
        resource_gate=ResourceGate(ResourceLimits(max_containers=1)),
        trace_sink=sink,
    )

    agent.provision(_params_with_hosts(3))

    assert len(sink) == 1
    gate = sink[0]["processing"]["gates"]["resource_gate"]
    assert gate["approved"] is False
    assert gate["reasons"]
    assert any("3" in reason for reason in gate["reasons"])


def test_resource_gate_approval_proceeds_to_pipeline_and_records_verdict() -> None:
    """An approved plan proceeds to the pipeline and records an empty-reason verdict.

    When the proposed plan is within the limits the gate approves, the agent
    delegates to the pipeline (one container-provisioning call) and the trace
    records ``resource_gate.approved=True`` with no reasons (task 6.2).
    """
    sink: list[dict] = []
    pipeline = _FakePipeline(environment=_ready_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline, trace_sink=sink)

    environment = agent.provision(_params())

    # The gate approved, so the pipeline was delegated to exactly once.
    assert len(pipeline.provision_calls) == 1
    assert environment.ready is True
    assert environment.origin is TranslationSource.AI_GENERATED

    gate = sink[0]["processing"]["gates"]["resource_gate"]
    assert gate["approved"] is True
    assert gate["reasons"] == []


def test_empty_proposal_is_approved_and_proceeds() -> None:
    """An empty proposal (zero hosts) is approved — nothing proposed consumes nothing.

    A zero-footprint plan passes every limit, so the gate approves and the agent
    proceeds to the pipeline, matching ``build_resource_plan``'s zero-footprint
    contract (task 6.2).
    """
    pipeline = _FakePipeline(environment=_ready_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    environment = agent.provision(_params_with_hosts(0))

    assert len(pipeline.provision_calls) == 1
    assert environment.origin is TranslationSource.AI_GENERATED
