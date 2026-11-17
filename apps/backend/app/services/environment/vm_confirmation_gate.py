"""VM_Confirmation_Gate — container is always provisioned; VM is a future contract.

A small, deterministic, code-level unit of the environment phase (Simulation-environment
creation) flow, modeled on the containment/isolation and
:mod:`app.services.environment.resource_gate` gates (agent proposes, gate
disposes). It encodes design decision #5 ("container always; VM is a future
contract", Req. 14.6-14.8): the application **always** provisions a container per
the ``Container_Preference`` (Req. 14.6); the
:class:`~app.models.enums.EnvironmentPlatform` enum already declares
``virtual_machine`` in the portable CHECK, but the effective provisioning never
becomes a VM.

When a campaign **would** require a VM (a conservative, explicitly-modeled signal
— see :func:`would_require_vm`), the gate is triggered and:

1. **Requests explicit confirmation from the Researcher** — surfaced as a
   structured flag (:attr:`VmConfirmationRecord.confirmation_requested`), never a
   block: the absence of the Researcher's confirmation does **not** stop the
   container provisioning (Req. 14.7).
2. **Records the VM request as a future contract NOT executed** — a structured
   marker (:attr:`VmConfirmationRecord.future_contract_not_executed`) carried on
   the provisioning output, so the audit shows the VM was asked for and
   deliberately not provisioned (Req. 14.8).
3. **Keeps the effective provisioning in a container** — the effective platform is
   always :attr:`~app.models.enums.EnvironmentPlatform.CONTAINER` and the count of
   VMs provisioned is **0** (Req. 14.6, 14.8).

The "would require a VM" trigger
--------------------------------
Nothing in this spec currently emits a VM requirement — ``EnvironmentParameters``
is container-only (Req. 17.1). So the trigger is modeled **conservatively** as a
pure predicate over the operationalization phase
:class:`~app.services.environment.operationalization.EnvironmentParameters`: a VM
is deemed required only when the parameters carry an **explicit**
``virtual_machine`` platform marker (:func:`would_require_vm`). Because no curated
derivation produces that marker today, the gate stays dormant on every current
path — it never fabricates a VM requirement — while remaining ready for a future
origin that could set the marker. The heuristic is intentionally narrow
(explicit-marker only) so the gate never second-guesses the agent's proposal.

Placement: :meth:`VmConfirmationGate.evaluate` is a pure function of the
parameters and the configured limits; the environment phase env path invokes it **before**
stamping the output platform and keeps the effective platform ``CONTAINER``
regardless of the verdict (wired into ``AiEnvironmentProvisioner.provision``). The
gate itself creates nothing — it is read-only, exactly like the ResourceGate.

_Requirements: 14.6, 14.7, 14.8_
"""

from __future__ import annotations

from pydantic import BaseModel

from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
)

# pt-BR text surfaced to the Researcher when the gate is triggered (Req. 14.7).
# The container provisioning is NOT blocked; this is a request for confirmation of
# the future VM contract, which the system records as not executed (Req. 14.8).
_MSG_VM_REQUESTED = (
    "A campanha indicou que exigiria uma máquina virtual. Conforme a preferência "
    "por contêineres, o provisionamento efetivo permanece em contêiner e nenhuma "
    "máquina virtual é criada. A solicitação de VM foi registrada como contrato "
    "futuro não executado e aguarda confirmação explícita do Researcher."
)


class VmConfirmationRecord(BaseModel):
    """Structured record of a :class:`VmConfirmationGate` evaluation (Req. 14.6-14.8).

    Attached to the environment phase output (``ProvisionedEnvironment.vm_confirmation``) so
    the screen/audit can show whether a VM was requested and that it was
    deliberately **not** provisioned. On the normal (container-only) path the
    record is dormant: ``triggered`` is ``False`` and no confirmation is requested.

    The record is **declarative, not executable**: it never provisions anything.
    The effective platform stays ``CONTAINER`` and ``vms_provisioned`` is always
    ``0`` — the gate is the *record* of the future contract, not its execution
    (Req. 14.8).

    Attributes:
        triggered: ``True`` iff the campaign would require a VM (per
            :func:`would_require_vm`); ``False`` on the normal container-only path.
        confirmation_requested: ``True`` iff the gate asked the Researcher for
            explicit confirmation of the future VM contract (Req. 14.7). Mirrors
            ``triggered`` — the request is surfaced as a flag, never a block.
        future_contract_not_executed: ``True`` iff a VM request was recorded as a
            future contract that was **not** executed (Req. 14.8). Mirrors
            ``triggered``.
        effective_platform: The platform actually provisioned — **always**
            :attr:`~app.models.enums.EnvironmentPlatform.CONTAINER`, on both the
            triggered and the dormant path (Container_Preference, Req. 14.6).
        vms_provisioned: The number of VMs actually provisioned — **always** ``0``;
            the gate never creates a VM (Req. 14.8).
        message: pt-BR text explaining the request/record for the screen when
            ``triggered``; ``None`` on the dormant path.
    """

    triggered: bool = False
    confirmation_requested: bool = False
    future_contract_not_executed: bool = False
    effective_platform: EnvironmentPlatform = EnvironmentPlatform.CONTAINER
    vms_provisioned: int = 0
    message: str | None = None


def would_require_vm(params: EnvironmentParameters) -> bool:
    """Conservative predicate: would this campaign require a VM? (Req. 14.7).

    The trigger for the :class:`VmConfirmationGate`, modeled narrowly so the gate
    never fabricates a VM requirement. A VM is deemed required **only** when the
    The operationalization phase :class:`EnvironmentParameters` carry an **explicit** ``virtual_machine``
    platform marker. ``EnvironmentParameters`` is container-only in this spec
    (Req. 17.1), so no current curated derivation sets that marker and the
    predicate returns ``False`` on every present path — the gate stays dormant —
    while remaining ready for a future origin that could set
    :attr:`~app.models.enums.EnvironmentPlatform.VM`.

    Args:
        params: The operationalization phase output whose platform marker is inspected.

    Returns:
        ``True`` iff ``params.platform`` is the explicit
        :attr:`~app.models.enums.EnvironmentPlatform.VM` marker; ``False``
        otherwise (the container-only normal path).
    """
    return params.platform == EnvironmentPlatform.VM


class VmConfirmationGate:
    """Deterministic, code-level gate: container always; VM is a future contract.

    Like the ResourceGate and the containment gate, this is a **code-level
    authority NOT entrusted to the agent**. The agent may *propose* a VM, but the
    gate in code keeps the effective provisioning in a container, records the VM
    request as a future contract not executed and asks the Researcher for explicit
    confirmation — it **never** provisions a VM (design §5, Req. 14.6-14.8).

    :meth:`evaluate` is a pure function of the parameters — same parameters always
    yield the same record — so it is trivially testable and auditable. It creates
    nothing (read-only): the env path consumes the returned
    :class:`VmConfirmationRecord` to stamp the output while keeping the effective
    platform ``CONTAINER``.

    _Requirements: 14.6, 14.7, 14.8_
    """

    def evaluate(self, params: EnvironmentParameters) -> VmConfirmationRecord:
        """Return the :class:`VmConfirmationRecord` for ``params`` (Req. 14.6-14.8).

        On the **normal** path (``would_require_vm`` is ``False``) the record is
        dormant: ``triggered=False``, no confirmation requested, effective platform
        ``CONTAINER``, ``vms_provisioned=0`` (Req. 14.6).

        On the **triggered** path (the campaign would require a VM) the gate: (a)
        requests explicit confirmation from the Researcher
        (``confirmation_requested=True``) **without** blocking (Req. 14.7); (b)
        records the VM request as a future contract not executed
        (``future_contract_not_executed=True``, Req. 14.8); and (c) keeps the
        effective platform ``CONTAINER`` with ``vms_provisioned=0`` (Req. 14.6,
        14.8). The container provisioning proceeds regardless — the gate never
        provisions a VM.

        Args:
            params: The operationalization phase output whose VM requirement is adjudicated.

        Returns:
            The :class:`VmConfirmationRecord`. ``effective_platform`` is
            **always** ``CONTAINER`` and ``vms_provisioned`` is **always** ``0``,
            on both paths.
        """
        triggered = would_require_vm(params)
        return VmConfirmationRecord(
            triggered=triggered,
            confirmation_requested=triggered,
            future_contract_not_executed=triggered,
            effective_platform=EnvironmentPlatform.CONTAINER,
            vms_provisioned=0,
            message=_MSG_VM_REQUESTED if triggered else None,
        )


__all__ = [
    "VmConfirmationGate",
    "VmConfirmationRecord",
    "would_require_vm",
]
