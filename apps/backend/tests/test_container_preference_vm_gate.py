"""Example tests for the always-container/never-VM provisioning gate (task 21.2).

These example-based unit tests exercise the ``VM_Confirmation_Gate`` end-to-end
through the Stage 5 ``AiEnvironmentProvisioner``
(``app/services/ai/agents/env_agent.py``) with **fakes only** — no network, no
Docker daemon, no database — reusing the daemon-free provision harness the
sibling env-agent tests use (``tests/test_ai_environment_agent_faked_drivers.py``).

Design decision #5 ("container always; VM is a future contract", Req. 14.6-14.8):
the application **always** provisions a container per the ``Container_Preference``
(Req. 14.6); when a campaign **would** require a VM, the
:class:`~app.services.environment.vm_confirmation_gate.VmConfirmationGate` requests
the Researcher's explicit confirmation (Req. 14.7), records the VM request as a
future contract **not** executed (Req. 14.8) and keeps the effective platform
``CONTAINER`` with ``0`` VMs — it **never** provisions a VM.

Two concrete cases:

1. **Effective platform == container (Req. 14.6).** A normal campaign (the
   default ``CONTAINER`` platform) driven through the provisioner yields a
   :class:`ProvisionedEnvironment` with ``platform == CONTAINER``, a **dormant**
   ``vm_confirmation`` record (``triggered`` False) and ``vms_provisioned == 0``.

2. **Campaign that would require a VM (Req. 14.7, 14.8).** A campaign whose
   :class:`EnvironmentParameters` carry the explicit ``VM`` marker drives the gate
   to trigger: it asks the Researcher for confirmation
   (``confirmation_requested`` True), records the request as a non-executed future
   contract (``future_contract_not_executed`` True), yet the effective platform is
   **still** ``CONTAINER`` and ``vms_provisioned == 0`` — no VM is ever
   provisioned. A direct unit assertion on
   :meth:`VmConfirmationGate.evaluate` pins the record for the VM-marker case.

_Requirements: 14.6, 14.7, 14.8_
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
    IsolationSummary,
    ProvisionedEnvironment,
    SubStageSummary,
)
from app.services.environment.vm_confirmation_gate import VmConfirmationGate

_SLUG = "vm-gate-example"
_CONTAINER = "nginx"


# ---------------------------------------------------------------------------
# Fakes — a daemon-free pipeline returning a fixed, ready container environment
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Daemon-free ``CurationEnvironmentProvisioner``-shaped pipeline.

    Returns a fixed, fully-ready **container** environment so no Docker daemon,
    Caldera client or isolation inspector is ever touched. The provisioner
    re-stamps the origin, forces the effective platform to ``CONTAINER`` and
    stamps the ``VmConfirmationRecord`` the gate produced — exactly the surface
    these tests assert.
    """

    def __init__(self, *, environment: ProvisionedEnvironment) -> None:
        self._environment = environment
        self.provision_calls: list[EnvironmentParameters] = []

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        self.provision_calls.append(params)
        on_log(LogLine(step="verificar_isolamento", stream="info", text="fake"))
        return self._environment


def _ready_container_environment() -> ProvisionedEnvironment:
    """A fully-ready, isolated container environment as the pipeline would build it.

    All six ``ready`` prerequisites satisfied, ``failed_step is None`` and
    ``platform == CONTAINER`` — stamped ``curadoria_humana`` so the provisioner
    re-stamps it to ``gerado_ia`` without disturbing any other field.
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


def _container_params() -> EnvironmentParameters:
    """A normal campaign's Stage 4 output — the default ``CONTAINER`` platform."""
    return EnvironmentParameters(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.CONTAINER,
        hosts=[TargetHost(label="host_a", address="192.168.20.30", container=_CONTAINER)],
        network_context=["internal lab segment"],
        origin=TranslationSource.HUMAN_CURATION,
    )


def _vm_params() -> EnvironmentParameters:
    """A campaign carrying the explicit ``VM`` marker — it WOULD require a VM."""
    return EnvironmentParameters(
        campaign_id=_SLUG,
        platform=EnvironmentPlatform.VM,
        hosts=[TargetHost(label="host_a", address="192.168.20.30", container=_CONTAINER)],
        network_context=["internal lab segment"],
        origin=TranslationSource.HUMAN_CURATION,
    )


# ---------------------------------------------------------------------------
# 1. Effective platform == container on the normal path (Req. 14.6)
# ---------------------------------------------------------------------------


def test_normal_campaign_effective_platform_is_container_and_gate_dormant() -> None:
    """A normal campaign provisions a container; the VM gate stays dormant (Req. 14.6).

    Driven with the default ``CONTAINER`` platform through the daemon-free
    pipeline, the resulting :class:`ProvisionedEnvironment` has
    ``platform == CONTAINER`` and a dormant ``vm_confirmation`` record:
    ``triggered`` False, no confirmation requested, no future contract recorded,
    effective platform ``CONTAINER`` and ``vms_provisioned == 0`` — no VM is
    provisioned.
    """
    pipeline = _FakePipeline(environment=_ready_container_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    environment = agent.provision(_container_params())

    # The container was provisioned and Stage 5 completes.
    assert len(pipeline.provision_calls) == 1
    assert environment.ready is True
    assert environment.origin is TranslationSource.AI_GENERATED

    # Effective platform is CONTAINER (Container_Preference, Req. 14.6).
    assert environment.platform is EnvironmentPlatform.CONTAINER

    # The VM gate is dormant: nothing requested, nothing recorded, no VM.
    record = environment.vm_confirmation
    assert record.triggered is False
    assert record.confirmation_requested is False
    assert record.future_contract_not_executed is False
    assert record.effective_platform is EnvironmentPlatform.CONTAINER
    assert record.vms_provisioned == 0
    assert record.message is None


# ---------------------------------------------------------------------------
# 2. A campaign that would require a VM (Req. 14.7, 14.8)
# ---------------------------------------------------------------------------


def test_vm_campaign_requests_confirmation_records_future_contract_keeps_container() -> None:
    """A VM-requiring campaign triggers the gate yet still provisions a container.

    Driven with the explicit ``VM`` marker through the daemon-free pipeline, the
    ``VM_Confirmation_Gate`` triggers: it asks the Researcher for confirmation
    (``confirmation_requested`` True — Req. 14.7) and records the VM request as a
    future contract **not** executed (``future_contract_not_executed`` True —
    Req. 14.8). The effective platform is **still** ``CONTAINER`` and
    ``vms_provisioned == 0`` — no VM is ever provisioned (Req. 14.6, 14.8).
    """
    pipeline = _FakePipeline(environment=_ready_container_environment())
    agent = AiEnvironmentProvisioner(pipeline=pipeline)

    environment = agent.provision(_vm_params())

    # The container is STILL provisioned — the gate never blocks (Req. 14.7).
    assert len(pipeline.provision_calls) == 1
    assert environment.origin is TranslationSource.AI_GENERATED

    # Effective platform forced to CONTAINER despite the VM marker (Req. 14.6).
    assert environment.platform is EnvironmentPlatform.CONTAINER

    record = environment.vm_confirmation
    # The gate triggered and asked the Researcher for confirmation (Req. 14.7).
    assert record.triggered is True
    assert record.confirmation_requested is True
    # The VM request was recorded as a non-executed future contract (Req. 14.8).
    assert record.future_contract_not_executed is True
    # Effective platform CONTAINER and NO VM provisioned (Req. 14.6, 14.8).
    assert record.effective_platform is EnvironmentPlatform.CONTAINER
    assert record.vms_provisioned == 0
    assert record.message is not None


def test_vm_confirmation_gate_evaluate_pins_the_record_for_the_vm_marker() -> None:
    """``VmConfirmationGate.evaluate`` pins the record for the VM-marker case.

    A direct unit assertion on the pure gate: given parameters carrying the
    explicit ``VM`` marker it returns a record that is ``triggered``, requests the
    Researcher's confirmation (Req. 14.7), records the non-executed future
    contract (Req. 14.8) and keeps the effective platform ``CONTAINER`` with ``0``
    VMs (Req. 14.6, 14.8).
    """
    record = VmConfirmationGate().evaluate(_vm_params())

    assert record.triggered is True
    assert record.confirmation_requested is True
    assert record.future_contract_not_executed is True
    assert record.effective_platform is EnvironmentPlatform.CONTAINER
    assert record.vms_provisioned == 0
    assert record.message is not None
