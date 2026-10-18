"""Environment-parameter DTOs — 2nd Output of Stage 4 (Operationalization).

This module defines the typed (Pydantic) models of the **second** Output of
Stage 4 (Operationalization): the :class:`EnvironmentParameters`. The first
Output is the ordered Abilities (reusing ``Stage2*``/``CaseService``); this is a
**separate** typed artifact, destined for Stage 5 (Environment creation), and
**derived** from the curated fields embedded in the curated DAG nodes
(``target_vuln_host_a/b``, ``network_context``, ``source_host_type`` /
``target_host_type``) and from the destinations of the ``attacker_commands``
(Req. 8.3, 8.4, 8.7).

The environment platform is, for now, **always container** (Req. 17.1). The
design admits a future virtual machine via :class:`EnvironmentPlatform`
**without redesign** — the ``maquina_virtual`` member stays as a commented
placeholder and is **not** implemented in this spec (Req. 17.2), following the
precedent of ``DagOrigin`` (the enum serializes to VARCHAR+CHECK and new values
fit with no schema migration).

The origin (:class:`~app.models.enums.TranslationSource`) is explicit and
extensible: today the whole curated derivation uses ``curadoria_humana``; a
future ``gerado_ia`` operationalization fits without changing the model
(Req. 16.2, 16.4).

_Requirements: 8.3, 8.4, 17.1, 17.2_
"""

from __future__ import annotations

from typing import Optional

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
    """A target host derived from the curated DAG commands/nodes (Req. 8.3).

    Attributes:
        label: Logical label of the host in the DAG (e.g. ``"host_a"``,
            ``"host_b"``).
        address: Internal IP when resolved (e.g. ``"192.168.20.30"``), derived
            from the ``attacker_commands`` destinations via ``container_for_ip``;
            ``None`` when not resolved.
        container: Matching lab container (via ``subnets.container_for_ip``);
            ``None`` when not resolved.
        host_type: ``source_host_type``/``target_host_type`` of the DAG node.
        vulnerabilities: ``target_vuln_host_*`` commands of the node matching
            this host.
    """

    label: str
    address: Optional[str] = None
    container: Optional[str] = None
    host_type: Optional[str] = None
    vulnerabilities: list[str] = Field(default_factory=list)


class RequiredService(BaseModel):
    """A service/vulnerability to provision, inferred from the commands (Req. 8.3).

    Attributes:
        description: Description of the service/vulnerability inferred from the
            commands (e.g. ``"ssh root login"``, ``"flask on port 5000"``).
        origin_technique: ``technique_id`` of the DAG node that originated the
            service, when available.
    """

    description: str
    origin_technique: Optional[str] = None


class EnvironmentParameters(BaseModel):
    """Environment parameters — 2nd Output of Stage 4 (Req. 8.3, 8.4, 8.7).

    A typed artifact SEPARATE from the Abilities, destined for Stage 5. Derived
    from the curated fields embedded in the DAG nodes (``target_vuln_host_a``/
    ``_b``, ``network_context``, ``source_host_type``/``target_host_type``) and
    from the ``attacker_commands`` destinations.

    Attributes:
        campaign_id: *Slug* of the Campaign these parameters belong to.
        platform: Environment platform — always ``CONTAINER`` in this spec
            (Req. 17.1); VM admitted with no redesign (Req. 17.2).
        hosts: Target hosts derived from the DAG commands/nodes.
        services: Services/vulnerabilities inferred from the commands.
        network_context: Per-node network notes (``network_context`` from the DAG).
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
    "EnvironmentPlatform",
    "TargetHost",
    "RequiredService",
    "EnvironmentParameters",
]
