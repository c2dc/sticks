"""Environment-parameter DTOs — 2nd Output of the operationalization phase.

This module defines the typed (Pydantic) models of the **second** Output of
the operationalization phase: the :class:`EnvironmentParameters`. The first
Output is the ordered Abilities (reusing ``Operationalization*``/``build_case_from_db``); this is a
**separate** typed artifact, destined for the environment phase (Environment creation), and
**derived** by the operationalizer from the operationalization phase commands and the attack
flow techniques: target hosts (label, address, host type, vulnerabilities),
inferred services and per-node network notes (Req. 8.3, 8.4, 8.7).

The environment platform is, for now, **always container** (Req. 17.1). The
design admits a future virtual machine via :class:`EnvironmentPlatform`
**without redesign** — the ``maquina_virtual`` member stays as a commented
placeholder and is **not** implemented in this spec (Req. 17.2), following the
precedent of ``FlowOrigin`` (the enum serializes to VARCHAR+CHECK and new values
fit with no schema migration).

The origin (:class:`~app.models.enums.TranslationSource`) is explicit and
extensible: today the whole curated derivation uses ``curadoria_humana``; a
future ``gerado_ia`` operationalization fits without changing the model
(Req. 16.2, 16.4).

_Requirements: 8.3, 8.4, 17.1, 17.2_
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.enums import EnvironmentPlatform, TranslationSource

# ``EnvironmentPlatform`` now lives in the models layer
# (:mod:`app.models.enums`) so persistence
# (``environment_parameters.platform``) can reference it without creating an
# import cycle between models and services. It is re-exported here to preserve
# the historical import point
# ``app.services.environment.operationalization.EnvironmentPlatform`` used by the
# rest of the environment package and the tests. The enum still has only
# ``CONTAINER`` implemented (Req. 17.1); ``maquina_virtual`` stays out of the
# enum (Req. 17.2), admitted only in the persisted column's CHECK.


class TargetHost(BaseModel):
    """A target host derived from the operationalization commands (Req. 8.3).

    Attributes:
        label: Logical label of the host in the attack flow (e.g. ``"host_a"``,
            ``"host_b"``).
        address: Internal IP when resolved (e.g. ``"192.168.20.30"``), derived
            from the destinations of the attacker commands via
            ``container_for_ip``; ``None`` when not resolved.
        container: Matching lab container (via ``subnets.container_for_ip``);
            ``None`` when not resolved.
        host_type: Kind of the host (e.g. attacker or target system).
        vulnerabilities: Setup commands that install the vulnerabilities on
            this host.
    """

    label: str
    address: str | None = None
    container: str | None = None
    host_type: str | None = None
    vulnerabilities: list[str] = Field(default_factory=list)


class RequiredService(BaseModel):
    """A service/vulnerability to provision, inferred from the commands (Req. 8.3).

    Attributes:
        description: Description of the service/vulnerability inferred from the
            commands (e.g. ``"ssh root login"``, ``"flask on port 5000"``).
        origin_technique: ``technique_id`` of the attack flow technique that
            originated the service, when available.
    """

    description: str
    origin_technique: str | None = None


class EnvironmentParameters(BaseModel):
    """Environment parameters — 2nd Output of the operationalization phase (Req. 8.3, 8.4, 8.7).

    A typed artifact SEPARATE from the Abilities, destined for the environment phase. Derived
    from the operationalization phase commands and the attack flow techniques (hosts, inferred
    services and per-node network notes).

    Attributes:
        campaign_id: *Slug* of the Campaign these parameters belong to.
        platform: Environment platform — always ``CONTAINER`` in this spec
            (Req. 17.1); VM admitted with no redesign (Req. 17.2).
        hosts: Target hosts derived from the operationalization phase commands.
        services: Services/vulnerabilities inferred from the commands.
        network_context: Per-node network notes describing the lab network.
        origin: Derivation origin — ``curadoria_humana`` today; extensible to
            ``gerado_ia`` without changing the model (Req. 16.2, 16.4).
    """

    campaign_id: str
    platform: EnvironmentPlatform = EnvironmentPlatform.CONTAINER
    hosts: list[TargetHost] = Field(default_factory=list)
    services: list[RequiredService] = Field(default_factory=list)
    network_context: list[str] = Field(default_factory=list)
    origin: TranslationSource = TranslationSource.HUMAN_CURATION


__all__ = [
    "EnvironmentParameters",
    "EnvironmentPlatform",
    "RequiredService",
    "TargetHost",
]
