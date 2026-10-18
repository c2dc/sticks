"""Operationalization seam: ``OperationalizerByOrigin`` + curation.

This module defines the **per-origin extension point** of Stage 4
(Operationalization) in the six-stage pipeline. It separates *how* a Campaign is
operationalized (today by curation, reading the curated artifacts from disk;
tomorrow by an AI agent) from the *rest* of the system, so a ``gerado_ia``
implementation can be plugged in later **without** changing this signature (Req.
8.6, 16.2, 16.4), following the precedent of ``DagGenerator`` (Stage 3) and
``EnvironmentProvisioner`` (Stage 5).

Dual Output (two **separate** typed artifacts — Req. 8.4, 8.7):

``ProducedOperationalization``
    The typed (Pydantic) result of Stage 4 — carries **the ordered Abilities**
    (reusing :meth:`StageService.assemble_stage2` → ``Stage2Output``, with
    ``atomic_ordering`` and ``translation_source``) and **the
    ``EnvironmentParameters``** derived from the curated DAG nodes. The two
    artifacts live in distinct fields, never mixed.

``OperationalizerByOrigin``
    A :class:`typing.Protocol` ``@runtime_checkable`` with two operations —
    ``origin() -> TranslationSource`` and
    ``operationalize(campaign, dag) -> ProducedOperationalization``. Any object
    exposing the two signatures satisfies the protocol (structural duck typing),
    with no explicit inheritance.

``CurationOperationalizer``
    The curation implementation of this iteration. Reuses
    :meth:`StageService.assemble_stage2`/``CaseService`` to build the ordered
    Abilities (``atomic_ordering``, ``translation_source=curadoria_humana``) and
    **derives** the :class:`~app.services.environment.operationalization.EnvironmentParameters`
    from the curated DAG nodes: ``attacker_commands`` → ``parse_command`` →
    internal destinations → ``container_for_ip`` resolves ``address``/``container``;
    ``target_vuln_host_a``/``_b`` → ``vulnerabilities``; ``source_host_type``/
    ``target_host_type`` → ``host_type``; ``network_context`` → notes; services
    inferred from the commands. The platform is always ``CONTAINER`` in this spec
    (Req. 17.1). **No network and no AI** on any path (Req. 11.3, 11.4).

_Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 11.3, 11.4, 16.2_
"""

from __future__ import annotations

import re
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import TranslationSource
from app.services.campaign_service import CaseData, CaseService, DagNode
from app.services.containment import container_for_ip, is_internal_ip, parse_command
from app.services.dag.dag_service import DagView
from app.services.environment.operationalization import (
    TargetHost,
    EnvironmentParameters,
    EnvironmentPlatform,
    RequiredService,
)
from app.services.stage.stage_service import (
    Stage2Output,
    StageService,
    _ability_view,
    _adversary_view,
)


# ---------------------------------------------------------------------------
# Typed DTO of the Stage 4 output (task 5.2)
# ---------------------------------------------------------------------------


class ProducedOperationalization(BaseModel):
    """Stage 4 output — the **two** separate typed artifacts (Req. 8.4, 8.7).

    Stage 4 (Operationalization) has a **dual** output: the Abilities ordered by
    the DAG and the environment parameters derived from the curated nodes. These
    live in **distinct** fields of this result, never mixed (Req. 8.7):

    Attributes:
        campaign_id: *Slug* of the operationalized Campaign.
        abilities: 1st artifact — the :class:`~app.services.stage.stage_service.Stage2Output`
            with the Abilities (command per executor), the order
            (``atomic_ordering``), the Adversary and ``translation_source``
            (``curadoria_humana``) — reuse of
            :meth:`~app.services.stage.stage_service.StageService.assemble_stage2`
            (Req. 8.2, 8.5, 8.6). ``None`` when the operationalization is
            suppressed (no upstream technique — Req. 8.1).
        parameters: 2nd artifact — the
            :class:`~app.services.environment.operationalization.EnvironmentParameters`
            derived from the curated DAG nodes (hosts/services/``network_context``),
            destined for Stage 5 (Req. 8.3, 8.4).
        origin: Operationalization origin — ``curadoria_humana`` today;
            extensible to ``gerado_ia`` without changing the model (Req. 8.6,
            16.2, 16.4).
    """

    campaign_id: str
    abilities: Optional[Stage2Output] = None
    parameters: EnvironmentParameters
    origin: TranslationSource = TranslationSource.HUMAN_CURATION


# ---------------------------------------------------------------------------
# Per-origin seam (Protocol) — Stage 4 (task 5.2)
# ---------------------------------------------------------------------------


@runtime_checkable
class OperationalizerByOrigin(Protocol):
    """Per-**origin** Stage 4 (Operationalization) seam — Req. 8.6, 16.2.

    A ``@runtime_checkable Protocol`` that defines the **extension point** of the
    operationalization, following the precedent of the other per-origin seams
    (``DagGenerator`` in Stage 3, ``EnvironmentProvisioner`` in Stage 5): each
    origin (``curadoria_humana`` today, ``gerado_ia`` in the future) is an
    implementation interchangeable by injection, **without** redesign (Req. 16.2,
    16.4).

    Any object that exposes these operations satisfies the protocol — no explicit
    inheritance is required (structural duck typing):

    - ``origin()`` — the :class:`~app.models.enums.TranslationSource` this
      implementation stamps on the output (``ProducedOperationalization.origin``
      and ``EnvironmentParameters.origin``).
    - ``operationalize(campaign, dag)`` — produces the **two** typed Stage 4
      artifacts (ordered Abilities + ``EnvironmentParameters``) from the
      Campaign's *slug* and the DAG (Stage 3 output).

    Today there is the curation implementation
    (:class:`CurationOperationalizer`); an AI implementation
    (``AiOperationalizer``) fits later under the same signature (Req. 16.2).
    """

    def origin(self) -> TranslationSource:
        """The :class:`~app.models.enums.TranslationSource` of this implementation."""
        ...

    def operationalize(self, campaign: str, dag: "DagView") -> "ProducedOperationalization":
        """Produce the two Stage 4 artifacts (Abilities + ``EnvironmentParameters``).

        ``dag`` is the Stage 3 output (the chosen DAG); the curation
        implementation uses its presence as a signal of the curated path and
        derives the parameters from the curated nodes read from disk (no network,
        no AI — Req. 11.3, 11.4).
        """
        ...


# ---------------------------------------------------------------------------
# Pure service-inference heuristic from the commands (Req. 8.3)
# ---------------------------------------------------------------------------

# Patterns (regex) → readable description of the service/vulnerability to
# provision. Pure and deterministic; applied over the attacker_commands and the
# target_vuln_host_* of the curated nodes. The order is stable for a
# deterministic output.
_SERVICE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bflask\b", re.IGNORECASE), "servidor Flask (HTTP)"),
    (re.compile(r"\bport=\"?(\d+)\"?|:(\d{2,5})\b"), "serviço HTTP em porta exposta"),
    (re.compile(r"\bsshpass\b|\bssh\b", re.IGNORECASE), "acesso SSH"),
    (re.compile(r"\bsudo\b|\bNOPASSWD\b", re.IGNORECASE), "sudo sem senha"),
    (re.compile(r"\bnc\b|\bnetcat\b|/dev/tcp/", re.IGNORECASE), "listener/reverse shell (netcat)"),
    (re.compile(r"\bchmod \+s\b|setuid\(", re.IGNORECASE), "binário SUID (escalonamento)"),
    (re.compile(r"\bdnsmasq\b|\bdig\b|dnsutils", re.IGNORECASE), "serviço DNS"),
    (re.compile(r"docker\.sock", re.IGNORECASE), "socket Docker exposto"),
    (re.compile(r"\bxmrig\b", re.IGNORECASE), "minerador (xmrig)"),
)


def _infer_services(commands: list[str], technique: Optional[str]) -> list[RequiredService]:
    """Infer services/vulnerabilities to provision from ``commands``.

    Pure and deterministic: matches each pattern of :data:`_SERVICE_PATTERNS`
    against the concatenated text of the commands (``attacker_commands`` +
    ``target_vuln_host_*`` of a node) and returns one :class:`RequiredService` per
    unique matched pattern, stamping ``origin_technique`` with the node's
    ``technique_id`` when available (Req. 8.3). It accesses neither network nor AI.
    """
    if not commands:
        return []
    text = "\n".join(commands)
    services: list[RequiredService] = []
    seen: set[str] = set()
    for pattern, description in _SERVICE_PATTERNS:
        if description in seen:
            continue
        if pattern.search(text):
            services.append(RequiredService(description=description, origin_technique=technique))
            seen.add(description)
    return services


class CurationOperationalizer:
    """Curation implementation of the :class:`OperationalizerByOrigin` seam.

    Produces the **two** Stage 4 artifacts from the material **already curated by
    a human**, with no network and no AI on any path (Req. 11.3, 11.4):

    1. **Ordered Abilities** — reuses
       :meth:`~app.services.stage.stage_service.StageService.assemble_stage2`
       (``Stage2Output`` with ``atomic_ordering`` and
       ``translation_source=curadoria_humana``), the single source of the curated
       operationalization (Req. 8.2, 8.5, 8.6).
    2. **``EnvironmentParameters``** — **derives** them from the curated DAG nodes
       read via :class:`~app.services.campaign_service.CaseService`
       (``try_load_case``, defensive and network-free): for each node,
       ``attacker_commands`` → :func:`~app.services.containment.parse_command` →
       **internal** destinations → :func:`~app.services.containment.container_for_ip`
       resolves ``address``/``container``; ``target_vuln_host_a``/``_b`` →
       ``vulnerabilities`` of the matching host; ``source_host_type``/
       ``target_host_type`` → ``host_type``; ``network_context`` → network note;
       services inferred from the commands (:func:`_infer_services`). The platform
       is always ``CONTAINER`` (Req. 17.1).

    It satisfies :class:`OperationalizerByOrigin` structurally — it exposes
    ``origin()`` + ``operationalize()`` — without explicit inheritance, exactly
    the point of the seam; it stamps ``curadoria_humana`` on the output (Req. 8.6,
    16.2).

    _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.7, 8.8, 11.3, 11.4, 16.2_
    """

    def __init__(
        self,
        *,
        stage_service: Optional[StageService] = None,
        case_service: Optional[CaseService] = None,
    ) -> None:
        """Assemble the curation operationalizer with injectable dependencies.

        Args:
            stage_service: Optional *session-bound* :class:`StageService` used to
                assemble the ordered Abilities via
                :meth:`~app.services.stage.stage_service.StageService.assemble_stage2`
                (which persists the upstream Stage 1 ``StageRun``). The API layer,
                which already has the ``Session``, can inject it to reuse the
                canonical assembly. When omitted, the ordered Abilities are
                assembled **purely** straight from the ``CaseService`` (same data
                source that ``assemble_stage2`` reads — ``load_case`` → abilities/
                adversary), **without** persistence — useful for the testable
                derivation without a database and for parameters-only calls.
            case_service: :class:`CaseService` to read the curated Campaign/DAG
                from disk. Default: a production instance (reused from the
                ``stage_service`` when it is passed, to share the same catalog).
        """
        self._case_service: CaseService = (
            case_service
            if case_service is not None
            else (
                getattr(stage_service, "_case_service", None) or CaseService()
                if stage_service is not None
                else CaseService()
            )
        )
        self._stage_service: Optional[StageService] = stage_service

    @property
    def case_service(self) -> CaseService:
        """The :class:`CaseService` used to read the curated Campaign/DAG from disk."""
        return self._case_service

    @property
    def stage_service(self) -> Optional[StageService]:
        """The injected :class:`StageService` (or ``None`` in pure mode)."""
        return self._stage_service

    def origin(self) -> TranslationSource:
        """Stamp ``curadoria_humana`` on the Stage 4 output (Req. 8.6, 16.2)."""
        return TranslationSource.HUMAN_CURATION

    # -- Derivation of the EnvironmentParameters (pure, no network/AI) -------

    @staticmethod
    def _resolve_hosts(node: DagNode) -> list[TargetHost]:
        """Derive the :class:`TargetHost` of a curated DAG node (Req. 8.3).

        Resolves the target host(s) from the **internal** destinations of the
        node's ``attacker_commands`` (via :func:`parse_command` →
        :func:`container_for_ip`), associating ``address``/``container`` and the
        node's ``target_host_type``, and packing the ``target_vuln_host_a``
        commands as the ``vulnerabilities`` of the first resolved target host (and
        ``target_vuln_host_b`` as a second host, when present). Pure and
        deterministic; no network.
        """
        hosts: list[TargetHost] = []

        # Internal destinations resolved from the commands → target host(s) with
        # address and container. Preserves order and removes duplicate addresses.
        addresses: list[str] = []
        for command in node.attacker_commands:
            for dest in parse_command(command).destinations:
                if is_internal_ip(dest.value) and dest.value not in addresses:
                    addresses.append(dest.value)

        vuln_a = list(node.target_vuln_host_a)
        vuln_b = list(node.target_vuln_host_b)

        if addresses:
            first = addresses[0]
            hosts.append(
                TargetHost(
                    label="host_a",
                    address=first,
                    container=container_for_ip(first),
                    host_type=node.target_host_type,
                    vulnerabilities=vuln_a,
                )
            )
            # Additional internal destinations become extra hosts (e.g. nested ssh).
            for idx, address in enumerate(addresses[1:], start=1):
                label = "host_b" if idx == 1 else f"host_{chr(ord('a') + idx)}"
                hosts.append(
                    TargetHost(
                        label=label,
                        address=address,
                        container=container_for_ip(address),
                        host_type=node.target_host_type,
                        vulnerabilities=vuln_b if idx == 1 else [],
                    )
                )
        elif vuln_a or vuln_b or node.target_host_type is not None:
            # No internal destination resolved, but there are target-host signals:
            # we still register a host_a (address/container not resolved).
            hosts.append(
                TargetHost(
                    label="host_a",
                    host_type=node.target_host_type,
                    vulnerabilities=vuln_a,
                )
            )

        # target_vuln_host_b present without an additional destination: a second
        # host with no resolved address, carrying only the vulnerabilities.
        if vuln_b and all(h.label != "host_b" for h in hosts):
            hosts.append(TargetHost(label="host_b", vulnerabilities=vuln_b))

        return hosts

    def _derive_parameters(self, campaign: str) -> EnvironmentParameters:
        """Derive the :class:`EnvironmentParameters` from the curated DAG nodes (Req. 8.3).

        Reads the curated Campaign via :meth:`CaseService.try_load_case`
        (defensive, network-free) and walks the ``structural_nodes`` accumulating
        hosts (with resolved ``address``/``container``, ``host_type`` and
        vulnerabilities), services inferred from the commands and the
        ``network_context`` notes. The platform is always ``CONTAINER`` (Req.
        17.1) and the origin ``curadoria_humana`` (Req. 16.2). A Campaign with no
        curated DAG results in empty parameters (without raising).
        """
        hosts: list[TargetHost] = []
        services: list[RequiredService] = []
        network_context: list[str] = []
        seen_services: set[tuple[str, Optional[str]]] = set()
        hosts_by_container: dict[Optional[str], TargetHost] = {}

        result = self._case_service.try_load_case(campaign)
        dag = (
            result.case.dag
            if result.ok and result.case is not None
            else None
        )
        nodes: list[DagNode] = list(dag.structural_nodes) if dag is not None else []

        for node in nodes:
            # Per-node network notes (Req. 8.3), de-duplicated preserving order.
            if node.network_context and node.network_context not in network_context:
                network_context.append(node.network_context)

            # Target hosts: we merge by (container, address) so as not to
            # duplicate the same target container across nodes, accumulating the
            # vulnerabilities.
            for host in self._resolve_hosts(node):
                key = host.container if host.container is not None else host.address
                existing = hosts_by_container.get(key) if key is not None else None
                if existing is None:
                    hosts.append(host)
                    if key is not None:
                        hosts_by_container[key] = host
                else:
                    for vuln in host.vulnerabilities:
                        if vuln not in existing.vulnerabilities:
                            existing.vulnerabilities.append(vuln)
                    if existing.host_type is None and host.host_type is not None:
                        existing.host_type = host.host_type

            # Services inferred from the commands (attacker + target_vuln_host_*).
            commands = (
                list(node.attacker_commands)
                + list(node.target_vuln_host_a)
                + list(node.target_vuln_host_b)
            )
            for service in _infer_services(commands, node.technique_id):
                key_s = (service.description, service.origin_technique)
                if key_s not in seen_services:
                    services.append(service)
                    seen_services.add(key_s)

        return EnvironmentParameters(
            campaign_id=campaign,
            platform=EnvironmentPlatform.CONTAINER,
            hosts=hosts,
            services=services,
            network_context=network_context,
            origin=self.origin(),
        )

    # -- Montagem das Abilities ordenadas (1º artefato) ---------------------

    @staticmethod
    def _build_abilities_pure(case: Optional[CaseData]) -> Optional[Stage2Output]:
        """Assemble the :class:`Stage2Output` **purely** from the curated Campaign.

        Mirrors the output of :meth:`StageService.assemble_stage2` — Abilities
        (command per executor), order (the Adversary's ``atomic_ordering``, with a
        *fallback* to the natural order), Adversary and ``translation_source`` —
        but **without** running/persisting the Modeling (Stage 1), so as not to
        require a ``Session`` (Req. 8.2, 8.5, 8.6). It reuses the same pure mappers
        ``_ability_view``/``_adversary_view`` of the ``stage_service``.

        Returns ``None`` when there is no Campaign/Abilities (operationalization
        suppressed — Req. 8.1), mirroring the ``suppressed`` of ``assemble_stage2``.
        """
        if case is None:
            return None
        abilities = list(case.abilities)
        if not abilities:
            return None

        adversary = case.adversary
        ability_views = [_ability_view(a) for a in abilities]
        if adversary is not None:
            ordering = list(adversary.atomic_ordering)
            adversary_view = _adversary_view(adversary)
            adversary_note = None
        else:
            ordering = [a.ability_id for a in abilities]
            adversary_view = None
            adversary_note = (
                "Este campaign não possui Adversary associado; não há agrupamento "
                "de Abilities em Adversary."
            )

        return Stage2Output(
            abilities=ability_views,
            ordering=ordering,
            adversary=adversary_view,
            translation_source=TranslationSource.HUMAN_CURATION,
            human_curation=True,
            has_abilities=True,
            has_adversary=adversary is not None,
            adversary_note=adversary_note,
        )

    def _build_abilities(self, campaign: str) -> Optional[Stage2Output]:
        """Assemble the ordered Abilities, reusing ``assemble_stage2`` when possible.

        With a *session-bound* :class:`StageService` injected, it delegates to
        :meth:`StageService.assemble_stage2` (the canonical assembly, Req. 8.2).
        Without it (pure mode), it assembles straight from the ``CaseService`` via
        :meth:`_build_abilities_pure` — same data source, no persistence.
        """
        if self._stage_service is not None:
            return self._stage_service.assemble_stage2(campaign).output

        result = self._case_service.try_load_case(campaign)
        case = result.case if result.ok else None
        return self._build_abilities_pure(case)

    def operationalize(self, campaign: str, dag: DagView) -> ProducedOperationalization:
        """Produce the two Stage 4 artifacts for the *slug* ``campaign`` (Req. 8.4, 8.7).

        1. Assembles the **ordered Abilities** reusing
           :meth:`StageService.assemble_stage2` — ``atomic_ordering`` and
           ``translation_source=curadoria_humana`` (Req. 8.2, 8.5, 8.6). When the
           operationalization is suppressed (no upstream technique), ``abilities``
           is ``None`` (Req. 8.1).
        2. **Derives** the :class:`EnvironmentParameters` from the curated DAG
           nodes (Req. 8.3), with the platform always ``CONTAINER`` (Req. 17.1).

        The two artifacts come out in **separate** fields of
        :class:`ProducedOperationalization` (Req. 8.7). ``dag`` signals the
        curated path; the derivation reads the curated nodes from disk (no
        network, no AI — Req. 11.3, 11.4). Stage 4 completes when **both**
        artifacts are produced (Req. 8.8).
        """
        abilities = self._build_abilities(campaign)
        parameters = self._derive_parameters(campaign)

        return ProducedOperationalization(
            campaign_id=campaign,
            abilities=abilities,
            parameters=parameters,
            origin=self.origin(),
        )


__all__ = [
    "ProducedOperationalization",
    "OperationalizerByOrigin",
    "CurationOperationalizer",
]
