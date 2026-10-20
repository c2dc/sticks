"""Environment-creation seam: ``EnvironmentProvisioner`` + DTOs + curation.

This module defines the **per-origin extension point** of Stage 5 (Simulation-
environment creation) in the six-stage pipeline, and also carries the historical
Stage 4 (Provisioning) seam for compatibility. It separates *how* a Campaign's
environment is created/verified (today by curation over the static
``docker-compose`` lab, tomorrow by an AI agent that **creates**
containers/VMs/networks dynamically) from the *rest* of the system, so a
``gerado_ia`` implementation can be plugged in later **without** changing this
signature (Req. 9.1, 16.3, 16.4).

Typed Stage 5 output (task 7.1):

``IsolationSummary`` / ``ContainerVerdict``
    Serializable (Pydantic) projection of the ``containment`` package's
    :class:`IsolationResult` — per-container verdict + overall result (Req. 9.8,
    14.3).

``ProvisionedEnvironment``
    The Stage 5 output: a ready and isolated environment (campaign, platform,
    target containers, ``compose``, isolation summary, loaded Abilities, origin)
    — Req. 9.8.

``EnvironmentProvisioner``
    A ``@runtime_checkable`` :class:`typing.Protocol` with a single operation —
    ``prepare(campaign) -> ProvisioningResult`` — that prepares/verifies a
    Campaign's environment and returns a typed result carrying: whether the
    environment is **ready/isolated**, the per-container isolation verdicts and
    the **topology** (hosts/networks/mermaid) for display. The curation
    implementation only **verifies and exposes** (read-only); the future
    ``AiEnvironmentProvisioner`` will **create** the environment under the same
    signature.

``ProvisioningResult``
    The typed result of a preparation/verification: ``ready``/``isolated``, the
    :class:`~app.services.containment.IsolationResult` with the per-container
    verdicts and the
    :class:`~app.services.environment.topology_builder.TopologyResponse` with the
    contained-environment topology.

``CurationEnvironmentProvisioner``
    The curation implementation of this iteration. It does **NOT** start, create
    or modify containers (read-only); **no AI and no public network**. In
    ``provision`` it (1) resolves the target containers from the
    ``EnvironmentParameters``, (2) **loads the ordered Abilities** (by the DAG's
    ``atomic_ordering``) and the Adversary into Caldera reusing
    ``CalderaClient.load_abilities``/``load_adversary`` — the client is
    **injectable** (a *fake* in the tests, the real one in production) — and (3)
    **verifies the isolation** via
    :func:`app.services.containment.inspect_isolation` with an **injectable**
    :class:`~app.services.containment.ContainerInspector` (a fake inspector in the
    tests, the real Docker adapter in production), BEFORE completing (fail-closed:
    an empty list **or** an unavailable daemon fails). It verifies exactly the
    containers the Execution will attack.

As in the rest of the service packages (``dag``/``stage``), the seam is a
``@runtime_checkable Protocol`` and the external dependencies (inspector,
Campaign) are **injected**, with the production defaults resolved lazily.

_Requirements: 5.1, 5.4, 5.5, 5.7, 10.2_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import TranslationSource
from app.services.campaign_service import CaseData, CaseService
from app.services.containment import (
    ContainerInspector,
    IsolationResult,
    inspect_isolation,
)
from app.services.environment.compose_executor import (
    ComposeExecutor,
    DockerComposeExecutor,
    LogLine,
    CommandResult,
)
from app.services.environment.egress import (
    EGRESS_NETWORK,
    DockerEgressProber,
    DockerEgressWindow,
    EgressProber,
    EgressWindow,
    EgressVerdict,
    attack_networks_for,
    attack_networks_with_ip_for,
    egress_lost,
)
from app.services.environment.operationalization import (
    EnvironmentParameters,
    EnvironmentPlatform,
)
from app.services.environment.target_setup import (
    DockerTargetSetup,
    SetupPhase,
    TargetSetup,
    classify_commands,
)
from app.services.environment.health import (
    ContainerStateInspector,
    HealthSummary,
    inspect_health,
)
from app.services.environment.topology_builder import (
    HOST_CONTAINERS,
    TopologyResponse,
    compose_path,
    build_hosts,
    build_networks,
    target_container_names,
    build_mermaid,
)


# ---------------------------------------------------------------------------
# Typed Stage 5 output DTOs (task 7.1)
# ---------------------------------------------------------------------------


class ContainerVerdict(BaseModel):
    """Isolation verdict of **one** target container (Req. 9.8, 14.3, 14.4).

    Serializable (Pydantic) mirror of
    :class:`~app.services.containment.ContainerIsolationVerdict` (an internal
    ``dataclass`` of the ``containment`` package), to compose the typed Stage 5
    output and the API JSON contracts without leaking the internal dataclass.

    Attributes:
        container: Name/id of the inspected container.
        isolated: ``True`` iff the container passed **all** the isolation checks
            (attached exclusively to ``internal: true`` networks, with no external
            bridge and no external DNS — Req. 14.2).
        reasons: Readable reasons why the container is **not** isolated; empty
            when ``isolated`` is ``True`` (Req. 14.4).
        external_networks: Names of the attached networks judged non-internal.
        external_dns: DNS servers judged external.
    """

    container: str
    isolated: bool
    reasons: list[str] = Field(default_factory=list)
    external_networks: list[str] = Field(default_factory=list)
    external_dns: list[str] = Field(default_factory=list)


class IsolationSummary(BaseModel):
    """Typed isolation summary — per-container + overall verdict (Req. 9.8, 14.3).

    Serializable (Pydantic) projection of
    :class:`~app.services.containment.IsolationResult` for the Stage 5 output and
    the API JSON contracts. Carries the **overall** result (``approved``) and the
    **per-container** verdict (``verdicts``), so the Stage 5 screen shows the
    reason for each non-isolated container (Req. 9.7, 14.4).

    Attributes:
        approved: ``True`` iff **all** target containers are isolated (overall
            pass/fail result — Req. 14.3). Fail-closed: an empty list of
            containers fails.
        verdicts: Per-container verdict, in the order they were checked.
    """

    approved: bool = False
    verdicts: list[ContainerVerdict] = Field(default_factory=list)

    @classmethod
    def from_isolation_result(cls, result: IsolationResult) -> "IsolationSummary":
        """Convert the ``containment`` :class:`IsolationResult` into this summary.

        The Stage 5 seam reuses the ``containment`` package (single source of the
        read-only isolation check, Req. 14.1) and **projects** its ``dataclass``
        into this Pydantic model — without duplicating the internal/external
        classification logic.
        """
        return cls(
            approved=result.passed,
            verdicts=[
                ContainerVerdict(
                    container=v.container,
                    isolated=v.isolated,
                    reasons=list(v.reasons),
                    external_networks=list(v.external_networks),
                    external_dns=list(v.external_dns),
                )
                for v in result.verdicts
            ],
        )

    @property
    def non_isolated(self) -> list[ContainerVerdict]:
        """The verdicts of the containers that failed the isolation."""
        return [v for v in self.verdicts if not v.isolated]


class EgressVerdictView(BaseModel):
    """Serializable projection of one host's egress verdict (Req. 5.4, 8.1).

    Pydantic mirror of the ``egress`` package's ``EgressVerdict`` *dataclass*
    (defined later, under tasks 4/7), carried in the Stage 5 output and the API
    JSON contracts without leaking the internal dataclass. It records, per target
    container, whether egress was **positively verified as lost** and whether that
    read-only probe was **conclusive** — the signal the ``verificar_egresso`` step
    consumes before applying any offline setup (fail-closed: a non-conclusive
    probe is treated as egress-present — Req. 5.5).

    Attributes:
        container: Name/id of the probed target container.
        egress_lost: ``True`` iff the read-only external probe **failed** — i.e.
            the host could not reach the public endpoint, so egress is lost
            (approves). ``False`` when egress is still present (reproves).
        conclusive: ``True`` iff the probe produced a definite result; a
            non-conclusive probe is treated as egress-present (fail-closed).
        reason: Readable reason/outcome of the probe, for the screen and the
            console line consistency (Req. 8.2); empty when not applicable.
    """

    container: str
    egress_lost: bool = False
    conclusive: bool = False
    reason: str = ""


class SubStageSummary(BaseModel):
    """Outcome of the Stage-5 sub-stages 5.1 (Build online) / 5.2 (Runtime) — Req. 6.3.

    Serializable summary of the two sub-stages inserted into the provisioning
    pipeline. It is attached to the :class:`ProvisionedEnvironment` (``substages``)
    so the screen's prerequisites checklist can show "online prerequisites
    obtained", "egress lost (verified)" and keep "isolation approved", and so the
    :attr:`ProvisionedEnvironment.ready` property can require the new gates in
    addition to the inherited ones (Req. 6.3, 6.4).

    All fields default to ``False``/empty, so an output built before the
    sub-stages run (e.g. an early failure) carries a not-approved summary — the
    screen always receives the full shape, and ``ready`` stays ``False``.

    Attributes:
        build_online_ok: ``True`` iff sub-stage **5.1** (Build online) completed
            for **all** hosts — the online prerequisites were obtained inside the
            controlled egress window and the window was closed again.
        egress_closed: ``True`` iff the egress window was closed for **all** hosts
            — no target is left with egress open (containment invariant, Req. 8.3).
        egress_lost_verified: ``True`` iff the loss of egress was **positively
            verified** (conclusive probe failing to reach the outside) for all
            hosts **before** any offline setup (Req. 5.4). Fail-closed.
        offline_applied: ``True`` iff sub-stage **5.2** (Runtime isolated) applied
            the offline setup for all hosts.
        egress_verdicts: Per-host :class:`EgressVerdictView`, in the order the
            hosts were probed — the detail backing ``egress_lost_verified``.
    """

    build_online_ok: bool = False
    egress_closed: bool = False
    egress_lost_verified: bool = False
    offline_applied: bool = False
    egress_verdicts: list[EgressVerdictView] = Field(default_factory=list)


class ProvisionedEnvironment(BaseModel):
    """Stage 5 output — a ready and isolated environment (Req. 9.8).

    Typed artifact produced by :meth:`EnvironmentProvisioner.provision` when the
    campaign environment is ready: the target containers resolved from the
    :class:`~app.services.environment.operationalization.EnvironmentParameters`,
    the curated lab's ``compose`` file, the isolation summary (per-container +
    overall verdict) and the indicator that the ordered Abilities were loaded into
    Caldera (Req. 9.5, 9.8).

    Stage 5 only completes when the three prerequisites pass —
    ``health.approved`` is ``True`` (healthy containers — Req. 5.1),
    ``loaded_abilities`` is ``True`` (Abilities in Caldera — Req. 5.2) and
    ``isolation.approved`` is ``True`` (isolation approved — Req. 5.3) —, which the
    :attr:`ready` property summarizes (Req. 5.5); otherwise Stage 6 is blocked
    (Req. 6.4) and ``failed_step``/``error_message`` identify **what** and **why**
    the step failed (Req. 7.1, 7.2).

    Attributes:
        campaign_id: *Slug* of the Campaign whose environment was provisioned.
        platform: Environment platform — ``CONTAINER`` in this spec (Req. 9.3,
            17.1); VM admitted with no redesign (Req. 17.2).
        target_containers: Target containers resolved from the
            ``EnvironmentParameters`` (the same ones Stage 6's Execution will
            attack).
        compose_file: Path of the curated lab's ``docker/docker-compose.yml``
            (Req. 9.4); ``None`` when not applicable.
        isolation: :class:`IsolationSummary` with the per-container verdict and
            the overall result (Req. 9.8, 14.3). Empty (not approved) when the
            pipeline fails before the ``verificar_isolamento`` step.
        health: :class:`~app.services.environment.health.HealthSummary` with the
            per-container health verdict and the overall result of the
            ``aguardar_saude`` step (Req. 5.1). Empty (not approved) when the
            pipeline fails before that step (e.g. bringing the containers up
            failed).
        loaded_abilities: ``True`` iff the ordered Abilities were loaded into
            Caldera in the DAG order (Req. 5.2, 9.5).
        origin: Provisioning origin — ``curadoria_humana`` today; extensible to
            ``gerado_ia`` without changing the model (Req. 16.3, 16.4).
        substages: :class:`SubStageSummary` with the outcome of the Stage-5
            sub-stages 5.1 (Build online) / 5.2 (Runtime isolated) — the online
            prerequisites, the egress window closure, the positively-verified
            egress loss, the offline setup and the per-host egress verdicts
            (Req. 6.3). Defaults to a not-approved summary, so an output built
            before the sub-stages run keeps the full shape with ``ready`` ``False``.
        failed_step: The pipeline step that failed — one of the inherited
            ``"subir_conteineres"``, ``"aguardar_saude"``,
            ``"carregar_abilities"``, ``"verificar_isolamento"`` or one of the new
            sub-stage steps ``"build_online"``, ``"encerrar_egresso"``,
            ``"reisolar"``, ``"verificar_egresso"``, ``"setup_offline"`` — or
            ``None`` when the pipeline completed (Req. 7.1, 8.1). The screen uses
            this field to highlight the offending step.
        error_message: Readable cause of the step failure, consistent with the
            terminal's last error line (Req. 7.2, 7.5); ``None`` on success.
    """

    campaign_id: str
    platform: EnvironmentPlatform = EnvironmentPlatform.CONTAINER
    target_containers: list[str] = Field(default_factory=list)
    compose_file: Optional[str] = None
    isolation: IsolationSummary = Field(default_factory=IsolationSummary)
    health: HealthSummary = Field(default_factory=HealthSummary)
    loaded_abilities: bool = False
    substages: SubStageSummary = Field(default_factory=SubStageSummary)
    origin: TranslationSource = TranslationSource.HUMAN_CURATION
    failed_step: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def ready(self) -> bool:
        """``True`` iff all prerequisites passed and no step failed.

        The environment approval (Req. 5.5) requires **all** the execution
        prerequisites. The **inherited** gates: healthy containers
        (``health.approved``), Abilities loaded into Caldera (``loaded_abilities``)
        and isolation approved (``isolation.approved``). Plus the **new** Stage-5
        sub-stage gates (Req. 6.3): the online prerequisites obtained
        (``substages.build_online_ok``), the loss of egress positively verified
        (``substages.egress_lost_verified``) and the offline setup applied
        (``substages.offline_applied``). ``failed_step is None`` confirms the
        pipeline walked all the steps without an early return. While ``ready`` is
        ``False``, Stage 6 remains blocked (Req. 6.4).
        """
        return (
            self.failed_step is None
            and self.health.approved
            and self.loaded_abilities
            and self.isolation.approved
            and self.substages.build_online_ok
            and self.substages.egress_lost_verified
            and self.substages.offline_applied
        )


class DeprovisioningResult(BaseModel):
    """Typed result of the environment teardown at the end of Stage 6 (Req. 12).

    The deprovisioning is **always** invoked at the end of Stage 6, on technique
    success or failure (Req. 10.8, 12.1). Instead of raising an exception for the
    expected failure path, :meth:`EnvironmentProvisioner.deprovision` returns this
    typed result, so the completion seam (task 8.4) decides — the Stage 6
    completion only happens when the environment was actually deprovisioned (Req.
    10.9). When the teardown fails, the **remaining resources** are identified for
    the Researcher (Req. 12.3).

    Attributes:
        deprovisioned: ``True`` iff the campaign environment was removed
            successfully (the created containers were removed — Req. 12.2).
        remaining_resources: When ``deprovisioned`` is ``False``, the resources
            (containers/networks) that **remained** provisioned, for the report to
            identify to the Researcher (Req. 12.3). Empty on success.
        origin: The :class:`~app.models.enums.TranslationSource` of the
            provisioner that performed the teardown — keeps the origin coherent
            (Req. 12, 16.3).
        message: Readable text of the result (success or the cause of the failure).
    """

    deprovisioned: bool = False
    remaining_resources: list[str] = Field(default_factory=list)
    origin: TranslationSource = TranslationSource.HUMAN_CURATION
    message: str = ""


@dataclass(frozen=True)
class ProvisioningResult:
    """Typed result of preparing/verifying a Campaign's environment (Stage 4).

    Carries everything the Stage 4 auto-completion (task 4.2) and the Provisioning
    screen (``EnvironmentStageView``) need to decide and display:

    Attributes:
        campaign_id: *Slug* of the Campaign whose environment was prepared/verified.
        ready: ``True`` iff the environment is **ready for Execution** — that is,
            verified as isolated. For curation, ``ready == isolated`` (it creates
            nothing; it can only declare ready an already-isolated environment).
            For a future AI that creates the environment, ``ready`` may also
            encompass the actual provisioning, keeping the same signature.
        isolated: ``True`` iff **all** target containers are isolated
            (``isolation.passed``). It is the containment signal of Req. 5 — if
            ``False``, Stage 4 does not complete and Stage 5 is blocked.
        isolation: The :class:`~app.services.containment.IsolationResult` with the
            per-container verdicts (failure reasons for display/error).
        topology: The
            :class:`~app.services.environment.topology_builder.TopologyResponse` of
            the contained environment (hosts, internal networks, Mermaid), for the
            Stage 4 screen — displayed even when ``isolated`` is ``False`` (the
            reason accompanies the verdicts).
    """

    campaign_id: str
    ready: bool
    isolated: bool
    isolation: IsolationResult
    topology: TopologyResponse


@dataclass(frozen=True)
class _SubStageOutcome:
    """Internal result of the 5.1/5.2 orchestration (task 7).

    Carries what :meth:`CurationEnvironmentProvisioner._run_substages` resolved so
    the ``provision`` body can either return early with the offending
    ``failed_step`` + cause, or continue to the final isolation gate with the
    satisfied :class:`SubStageSummary` and ``loaded_abilities`` outcome.

    Attributes:
        summary: The :class:`SubStageSummary` built so far (partial on failure,
            satisfied on success — minus the final isolation gate which the caller
            still runs).
        loaded_abilities: ``True`` iff the ordered Abilities + Adversary were
            loaded into Caldera inside 5.2 (``carregar_abilities``).
        failed_step: The sub-stage step that failed (``build_online``/
            ``encerrar_egresso``/``reisolar``/``verificar_egresso``/
            ``setup_offline``/``carregar_abilities``), or ``None`` on success.
        error_message: Readable cause of the failure, or ``None`` on success.
    """

    summary: SubStageSummary
    loaded_abilities: bool = False
    failed_step: Optional[str] = None
    error_message: Optional[str] = None

    @classmethod
    def failure(
        cls, failed_step: str, error_message: str, *, summary: SubStageSummary
    ) -> "_SubStageOutcome":
        """Build a failed outcome, carrying the partial summary and the cause."""
        return cls(
            summary=summary,
            loaded_abilities=False,
            failed_step=failed_step,
            error_message=error_message,
        )


@runtime_checkable
class CalderaLoader(Protocol):
    """Minimal Caldera surface used by Stage 5 — load Abilities/Adversary.

    Structural projection (``@runtime_checkable``) of the load operations of the
    :class:`~app.services.caldera.CalderaClient` that Stage 5 reuses (Req. 9.5):
    pushing the curated Abilities and the Adversary to Caldera **before**
    completing. It is a ``Protocol`` so the tests inject a *fake* (no network, no
    daemon) and production injects the real client — the curation provisioner does
    not couple to the HTTP transport.

    Any object that exposes ``load_abilities``/``load_adversary`` with these
    signatures satisfies the protocol (structural duck typing), exactly as the
    :class:`~app.services.caldera.CalderaClient` already does.
    """

    def load_abilities(self, abilities: list[Mapping[str, Any]]) -> Any:
        """Load the list of curated Abilities into Caldera (``POST /api/v2/abilities``)."""
        ...

    def load_adversary(self, adversary: Mapping[str, Any]) -> Any:
        """Load the curated Adversary into Caldera (``POST /api/v2/adversaries``)."""
        ...


@runtime_checkable
class EnvironmentProvisioner(Protocol):
    """Per-**origin** Stage 5 (Environment creation) seam — Req. 9.1, 16.3.

    A ``@runtime_checkable Protocol`` that defines the **extension point** of the
    environment creation, following the precedent of the other per-origin seams
    (``DagGenerator`` in Stage 3, ``OperationalizerByOrigin`` in Stage 4): each
    origin (``curadoria_humana`` today, ``gerado_ia`` in the future) is an
    implementation interchangeable by injection, **without** redesign (Req. 16.3,
    16.4).

    Any object that exposes these operations satisfies the protocol — no explicit
    inheritance is required (structural duck typing):

    - ``origin()`` — the :class:`~app.models.enums.TranslationSource` this
      implementation stamps on the output (``ProvisionedEnvironment.origin``).
    - ``provision(params)`` — creates/prepares the environment from the
      :class:`~app.services.environment.operationalization.EnvironmentParameters`
      (Stage 4 output), loads the ordered Abilities into Caldera and verifies the
      isolation, returning the ready and isolated :class:`ProvisionedEnvironment`
      (Req. 9.5, 9.6, 9.8).
    - ``deprovision(...)`` — removes the provisioned environment at the end of
      Stage 6 (teardown), keeping the origin coherent (Req. 12).

    Today there is the curation implementation
    (:class:`CurationEnvironmentProvisioner`), which uses the curated
    ``docker/docker-compose.yml`` lab and only **verifies** the isolation
    (read-only, no network/AI). An AI implementation
    (``AiEnvironmentProvisioner``) that actually **creates** bespoke
    containers/VMs/networks fits later under the same signature (Req. 16.3).

    .. note::
        ``prepare(campaign)`` is kept for compatibility with the existing Stage 4
        auto-completion (``EnvironmentStageService``) and the topology endpoints;
        the new ``origin``/``provision``/``deprovision`` methods implement the
        Stage 5 seam of the six-stage pipeline.
    """

    def origin(self) -> TranslationSource:
        """The :class:`~app.models.enums.TranslationSource` of this implementation.

        Stamped in ``ProvisionedEnvironment.origin`` — ``HUMAN_CURATION`` in
        curation; ``gerado_ia`` in a future AI implementation (Req. 16.3).
        """
        ...

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        """Create/prepare the environment from ``params`` and return the output.

        It runs the provisioning as an **ordered and observable pipeline** of
        steps — ``subir_conteineres`` → ``aguardar_saude`` → ``carregar_abilities``
        → ``verificar_isolamento`` — each one emitting :class:`LogLine` through
        ``on_log`` (Req. 2). The curation implementation **brings up** the curated
        lab before loading the Abilities (Req. 9.5) and **verifying the isolation**
        read-only (Req. 5.3); the future AI creates the bespoke environment under
        the same signature (Req. 10.2).

        ``on_log`` is **keyword-only** with a *no-op* default, so the existing
        callers (``provision(params)``) stay valid unchanged; the Stage 5 service
        passes a callback that forwards the lines to the buffer and the screen
        terminal (Req. 2.2).

        Args:
            params: The Stage 4 output
                (:class:`~app.services.environment.operationalization.EnvironmentParameters`)
                — target containers and platform of the environment to provision.
            on_log: Callback invoked for each :class:`LogLine` emitted by the
                steps, in the order they come out; *no-op* default for
                compatibility.

        Returns:
            The :class:`ProvisionedEnvironment` — ready (``ready is True``) when the
            four steps pass, or with ``failed_step``/``error_message`` when one
            fails, keeping Stage 5 incomplete (Req. 7.1).
        """
        ...

    def deprovision(
        self,
        environment: ProvisionedEnvironment,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> DeprovisioningResult:
        """Remove the provisioned environment (teardown at the end of Stage 6 — Req. 8).

        Always invoked after measuring and generating the report, on technique
        success or failure (Req. 8.1). Driven by the **same** per-origin seam,
        keeping the origin coherent with the provisioning (Req. 8.3). It returns a
        typed :class:`DeprovisioningResult`: on success ``deprovisioned=True``; on
        failure ``deprovisioned=False`` with the ``remaining_resources`` identified
        for the Researcher (Req. 8.4). The curation implementation **tears down**
        the curated lab via :meth:`ComposeExecutor.down` (``docker compose down`` —
        Req. 8.2); a future AI implementation removes the bespoke resources it
        created here, under the same signature.

        ``on_log`` is **keyword-only** with a *no-op* default, so the existing
        callers (``deprovision(environment)``) stay valid unchanged; the Stage 6
        completion seam passes a callback that forwards the teardown lines to the
        screen terminal (Req. 2.2).

        Args:
            environment: The Stage 5 output (:class:`ProvisionedEnvironment`) whose
                environment must be deprovisioned — it carries the curated lab's
                ``compose_file`` and the campaign's ``target_containers``.
            on_log: Callback invoked for each :class:`LogLine` emitted by the
                teardown, in the order it comes out; *no-op* default for
                compatibility.
        """
        ...

    def prepare(self, campaign: str) -> ProvisioningResult:
        """Prepare/verify the environment of the *slug* ``campaign`` (Stage 4 compat.).

        Kept for the Stage 4 auto-completion (``EnvironmentStageService``) and the
        existing topology endpoints. The curation implementation **creates
        nothing** — it only verifies the isolation of the existing environment and
        exposes the topology.
        """
        ...


class CurationEnvironmentProvisioner:
    """Curation implementation of the :class:`EnvironmentProvisioner` seam (Req. 5.4, 5.5, 5.7).

    This iteration **does not provision dynamically**: the environment is the
    static ``docker/docker-compose.yml`` lab. Curation only:

    1. **Exposes the topology** of the contained environment — hosts,
       ``internal: true`` networks and the Mermaid diagram — built from the static
       map reused from :mod:`app.services.environment.topology_builder` (the same
       IP/subnet source of truth as the containment pre-flight, **without
       duplicating** the map).
    2. **Verifies the isolation** of the target containers via
       :func:`app.services.containment.inspect_isolation`, with an **injectable**
       :class:`~app.services.containment.ContainerInspector` (a fake inspector in
       the tests — no Docker daemon; the real adapter in production when ``None``).
       It verifies exactly the containers the Execution (Stage 5) will attack
       (:func:`...topology_builder.container_names`).

    **Read-only / no-network / no-AI guarantee:** no path of this implementation
    starts, creates or modifies containers/networks, accesses the network or
    invokes AI — all the Docker inspection is read-only and sits behind the
    injected inspector (Req. 5.5).

    It satisfies :class:`EnvironmentProvisioner` structurally — it exposes
    ``origin``/``provision``/``deprovision`` (Stage 5 seam) plus ``prepare``
    (Stage 4 compat.) — without explicit inheritance, exactly the point of the
    seam; it stamps ``curadoria_humana`` on the output (Req. 16.3).

    _Requirements: 5.4, 5.5, 5.7, 9.1, 9.8, 16.3_
    """

    def __init__(
        self,
        *,
        inspector: Optional[ContainerInspector] = None,
        case_service: Optional[CaseService] = None,
        caldera: Optional[CalderaLoader] = None,
        compose_executor: Optional[ComposeExecutor] = None,
        state_inspector: Optional[ContainerStateInspector] = None,
        egress_window: Optional[EgressWindow] = None,
        egress_prober: Optional[EgressProber] = None,
        target_setup: Optional[TargetSetup] = None,
    ) -> None:
        """Assemble the curation provisioner with injectable dependencies.

        Args:
            inspector: **Read-only** Docker inspector used in the isolation check.
                ``None`` in production (the real adapter is built lazily by
                :func:`inspect_isolation` only when the check runs); the tests
                inject a fake inspector so as not to touch any daemon.
            case_service: :class:`CaseService` to resolve the Campaign's display
                name and load the Campaign's curated Abilities/Adversary. Default:
                a production instance.
            caldera: Caldera client (:class:`CalderaLoader`) used to load the
                ordered Abilities and the Adversary (Req. 9.5). ``None`` in
                production — a real :class:`~app.services.caldera.CalderaClient` is
                built lazily only when the loading runs (the import never opens a
                socket); the tests inject a network-free *fake*.
            compose_executor: :class:`~app.services.environment.compose_executor.ComposeExecutor`
                seam used to **up**/**down** the contained ``docker-compose`` lab
                in streaming (Req. 3.1). ``None`` in production — a real
                :class:`~app.services.environment.compose_executor.DockerComposeExecutor`
                is built lazily by :meth:`_get_compose_executor` only when the
                provisioning runs (the import never opens a subprocess nor touches
                the daemon); the tests inject a subprocess-free *fake* (Req. 1.4,
                10.1).
            state_inspector: Read-only state/health inspector
                (:class:`~app.services.environment.health.ContainerStateInspector`)
                used by the ``aguardar_saude`` step (Req. 5.1). ``None`` in
                production — the health inspection is done via
                :func:`~app.services.environment.health.inspect_health`, which
                builds the real SDK adapter lazily only when no inspector is
                injected (never on import); the tests inject a daemon-free *fake*.
                Here it is only **stored** to be passed on to the health step in
                task 3.2.
        """
        self._inspector = inspector
        self._case_service: CaseService = (
            case_service if case_service is not None else CaseService()
        )
        self._caldera = caldera
        self._compose_executor = compose_executor
        self._state_inspector = state_inspector
        self._egress_window = egress_window
        self._egress_prober = egress_prober
        self._target_setup = target_setup

    @property
    def case_service(self) -> CaseService:
        """The :class:`CaseService` used to resolve the Campaign name/validation."""
        return self._case_service

    def _campaign_name(self, campaign: str) -> str:
        """Display name of the Campaign for the topology (fallback: the slug itself)."""
        return self._case_service.list_cases().get(campaign, campaign)

    def origin(self) -> TranslationSource:
        """Stamp ``curadoria_humana`` on the Stage 5 output (Req. 16.3)."""
        return TranslationSource.HUMAN_CURATION

    def _target_containers(self, params: EnvironmentParameters) -> list[str]:
        """Resolve the target containers to verify from the ``params``.

        Prefers the containers resolved in the ``hosts`` of the
        :class:`~app.services.environment.operationalization.EnvironmentParameters`
        (derived from the curated DAG commands); falls back to the curated lab's
        containers (:func:`...topology_builder.container_names`) when the
        parameters resolved none (Req. 9.4) — and that fallback is the
        **target** containers only (not the attacker/C2 hosts), consistent with
        the Execution gate. Preserves order and removes duplicates.
        """
        resolved: list[str] = []
        for host in params.hosts:
            if host.container:
                # Resolve the host's logical name (e.g. ``nginx``, ``db``) to the
                # lab's real ``container_name`` (e.g. ``sticks-nginx-target``,
                # ``sticks-mariadb-target``) via the stable ``HOST_CONTAINERS`` map,
                # so the read-only health/isolation inspection finds the container
                # by the name Docker knows. Already-resolved names (or ones with no
                # mapping) are preserved. Removes duplicates, preserves order.
                name = HOST_CONTAINERS.get(host.container, host.container)
                if name not in resolved:
                    resolved.append(name)
        if resolved:
            return resolved
        return target_container_names()

    def _get_caldera(self) -> CalderaLoader:
        """Return the Caldera client, building a real one lazily if needed.

        In production (``caldera is None``) a
        :class:`~app.services.caldera.CalderaClient` is built only here, on the
        first load — never on import, so importing this module never opens a
        socket. The tests inject a *fake* in the constructor and this path is not
        touched.
        """
        if self._caldera is None:
            # Local import: keeps the module importable without opening a
            # connection and avoids a circular import with the caldera package.
            from app.services.caldera import CalderaClient

            self._caldera = CalderaClient()
        return self._caldera

    def _get_compose_executor(self) -> ComposeExecutor:
        """Return the ``compose`` executor, building a real one lazily if needed.

        In production (``compose_executor is None``) a
        :class:`~app.services.environment.compose_executor.DockerComposeExecutor`
        is built only here, on the first need to up/down the lab — never on
        import, so importing this module never opens a subprocess nor touches the
        Docker daemon (friendly to Docker-less dev). The tests inject a *fake* in
        the constructor and this path is not touched (Req. 1.4, 3.1, 10.1).
        """
        if self._compose_executor is None:
            self._compose_executor = DockerComposeExecutor()
        return self._compose_executor

    def _get_egress_window(self) -> EgressWindow:
        """Return the egress-window seam, building the production one lazily.

        In production (``egress_window is None``) a :class:`DockerEgressWindow`
        wrapping the :meth:`_get_compose_executor` is built here, on the first
        need to open/close a host's egress window during sub-stage 5.1 — never on
        import, so importing this module never opens a subprocess nor touches the
        Docker daemon. The tests inject a *fake* in the constructor and this path
        is not touched (Req. 5.1, 5.2, 5.3).
        """
        if self._egress_window is None:
            self._egress_window = DockerEgressWindow(self._get_compose_executor())
        return self._egress_window

    def _get_egress_prober(self) -> EgressProber:
        """Return the egress-prober seam, building the production one lazily.

        In production (``egress_prober is None``) a :class:`DockerEgressProber`
        wrapping the :meth:`_get_compose_executor` is built here, on the first
        need to positively verify the loss of egress during ``verificar_egresso``
        (sub-stage 5.2) — never on import. The tests inject a *fake* and this path
        is not touched (Req. 3.2, 5.4, 5.5).
        """
        if self._egress_prober is None:
            self._egress_prober = DockerEgressProber(self._get_compose_executor())
        return self._egress_prober

    def _get_target_setup(self) -> TargetSetup:
        """Return the target-setup seam, building the production one lazily.

        In production (``target_setup is None``) a :class:`DockerTargetSetup`
        wrapping the :meth:`_get_compose_executor` is built here, on the first
        need to run the per-phase curated setup (``build_online`` of 5.1 and
        ``setup_offline`` of 5.2) — never on import. The tests inject a *fake* and
        this path is not touched (Req. 2.3, 3.3).
        """
        if self._target_setup is None:
            self._target_setup = DockerTargetSetup(self._get_compose_executor())
        return self._target_setup

    def _substages_supported(self) -> bool:
        """Whether the 5.1/5.2 network/exec orchestration can actually run.

        The sub-stages mutate container networks (``attach``/``detach``) and run
        ``exec`` setup commands through the :class:`ComposeExecutor` extension
        introduced for this spec (task 2). They can run when **either** a
        sub-stage seam was explicitly injected (the tests that exercise 5.1/5.2),
        **or** the resolved compose executor exposes the extended
        ``attach_network``/``detach_network``/``exec_in`` surface (production's
        :class:`DockerComposeExecutor`).

        When none of the seams is injected **and** the injected executor predates
        the task-2 extension (an ``up``/``down``-only fake), the orchestration
        degrades to the inherited placeholder: it skips the network/exec work and
        reports a satisfied :class:`SubStageSummary` — so the earlier Stage-5
        suites, which inject such executors and resolve hosts with no curated
        ``vulnerabilities``, keep their behavior (``ready`` and no sub-stage log
        lines) unchanged. The containment logic is unaffected on the real path.
        """
        if (
            self._egress_window is not None
            or self._egress_prober is not None
            or self._target_setup is not None
        ):
            return True
        executor = self._get_compose_executor()
        return (
            hasattr(executor, "attach_network")
            and hasattr(executor, "detach_network")
            and hasattr(executor, "exec_in")
        )

    @staticmethod
    def _ordered_abilities(case: CaseData) -> list[Any]:
        """Order the Campaign's Abilities by the Adversary's ``atomic_ordering`` (Req. 9.5).

        The DAG order is materialized by the curated Adversary's
        ``atomic_ordering`` (the ``ability_id`` in execution order). It preserves
        that order; Abilities outside the ``atomic_ordering`` (defensive) go to the
        end, in the original order. Without ``atomic_ordering`` it falls back to
        the file's natural order.
        """
        ordering = case.atomic_ordering
        if not ordering:
            return list(case.abilities)

        by_id = {a.ability_id: a for a in case.abilities}
        ordered: list[Any] = []
        seen: set[str] = set()
        for ability_id in ordering:
            ability = by_id.get(ability_id)
            if ability is not None and ability_id not in seen:
                ordered.append(ability)
                seen.add(ability_id)
        # Abilities present in the file but absent from atomic_ordering (defensive).
        for ability in case.abilities:
            if ability.ability_id not in seen:
                ordered.append(ability)
                seen.add(ability.ability_id)
        return ordered

    @staticmethod
    def _ability_payloads(abilities: Sequence[Any]) -> list[Mapping[str, Any]]:
        """Build the ``POST /api/v2/abilities`` payloads from the Abilities.

        Sends the fields the Caldera v2 Ability schema uses; absent fields are
        omitted (Caldera fills its own defaults). Reuses the same projection as the
        ``OperationRunner`` to keep the payload coherent.
        """
        payloads: list[Mapping[str, Any]] = []
        for ability in abilities:
            payload: dict[str, Any] = {}
            ability_id = getattr(ability, "ability_id", None)
            if ability_id:
                payload["ability_id"] = ability_id
            for attr in ("name", "tactic", "technique_name", "technique_id", "description"):
                value = getattr(ability, attr, None)
                if value is not None:
                    payload[attr] = value
            executors = getattr(ability, "executors", None)
            if executors:
                payload["executors"] = [
                    e.model_dump() if hasattr(e, "model_dump") else e
                    for e in executors
                ]
            payloads.append(payload)
        return payloads

    @staticmethod
    def _adversary_payload(case: CaseData) -> Optional[Mapping[str, Any]]:
        """Build the ``POST /api/v2/adversaries`` payload (v2 ``adversary_id``).

        Without a curated Adversary, it returns ``None`` — Caldera receives no
        group and the Abilities load still happens.
        """
        adversary = case.adversary
        if adversary is None:
            return None
        return {
            "adversary_id": adversary.id,
            "name": adversary.name or adversary.id,
            "description": adversary.description or "",
            "atomic_ordering": list(adversary.atomic_ordering),
        }

    def _load_abilities(self, params: EnvironmentParameters) -> bool:
        """Load the ordered Abilities + the curated Adversary into Caldera (Req. 9.5).

        Reads the curated Campaign by *slug* (``CaseService.try_load_case``,
        defensive and network-free), orders the Abilities by the DAG's
        ``atomic_ordering``, and pushes them to Caldera in order, followed by the
        Adversary. Fail-closed: if the Campaign does not load, there are no
        Abilities, or Caldera fails, it returns ``False`` (Stage 5 does not
        complete) — it never brings down the provisioning (the topology and the
        isolation verdict are still displayed).

        It invokes no AI on any path (Req. 9.4). The only egress is the load into
        the lab's Caldera (not the public Internet).
        """
        try:
            result = self._case_service.try_load_case(params.campaign_id)
        except KeyError:
            # Slug outside the curated catalog: fail-closed, no Abilities loaded.
            return False
        if not result.ok or result.case is None:
            return False
        case = result.case
        if not case.has_abilities:
            return False

        abilities = self._ordered_abilities(case)
        payloads = self._ability_payloads(abilities)
        if not payloads:
            return False

        try:
            caldera = self._get_caldera()
            caldera.load_abilities(payloads)
            adversary_payload = self._adversary_payload(case)
            if adversary_payload is not None:
                caldera.load_adversary(adversary_payload)
        except Exception:
            # Caldera unavailable/API error: fail-closed, Stage 5 does not
            # complete, but the provisioning still returns the topology/isolation.
            return False
        return True

    def _host_targets(
        self, params: EnvironmentParameters
    ) -> list[tuple[Any, str]]:
        """Pair each resolved host with its real lab container name.

        Mirrors :meth:`_target_containers`' resolution (the host's logical name —
        ``nginx``/``db`` — mapped to the lab's real ``container_name`` via
        :data:`HOST_CONTAINERS`), but keeps the originating
        :class:`~app.services.environment.operationalization.TargetHost` so the
        sub-stages can read its ``vulnerabilities``. Hosts with no resolved
        container are skipped (they carry nothing for 5.1/5.2 to act on), and
        duplicate containers are removed while preserving order.
        """
        pairs: list[tuple[Any, str]] = []
        seen: set[str] = set()
        for host in params.hosts:
            if not host.container:
                continue
            name = HOST_CONTAINERS.get(host.container, host.container)
            if name in seen:
                continue
            seen.add(name)
            pairs.append((host, name))
        return pairs

    def _fail_safe(
        self, container: str, *, on_log: Callable[[LogLine], None]
    ) -> None:
        """Best-effort close of a host's egress window on a failure path (Req. 8.3).

        No error path of 5.1/5.2 may leave a target with egress open. This closes
        the egress window of a single ``container`` best-effort — any error while
        closing is swallowed (the goal is to not leave egress open, never to raise
        a secondary failure that masks the original cause). It emits a ``5.1``
        marker line so the console reflects the safety action.
        """
        on_log(
            LogLine(
                step="encerrar_egresso",
                stream="info",
                text=f"fail-safe: encerrando a janela de egresso de {container}",
                substage="5.1",
            )
        )
        try:
            self._get_egress_window().close(container, on_log=on_log)
        except Exception:
            # Swallow: the fail-safe must never raise over the original failure.
            pass

    def _fail_safe_all(
        self, containers: list[str], *, on_log: Callable[[LogLine], None]
    ) -> None:
        """Best-effort close every host's egress window (Req. 8.3, 8.4).

        Used when an unexpected exception escapes the per-host 5.1 loop: it closes
        the egress window of **all** targets so no host is left reachable from the
        internet, regardless of which host was mid-window when the failure hit.
        """
        for container in containers:
            self._fail_safe(container, on_log=on_log)

    def _run_substages(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None],
    ) -> "_SubStageOutcome":
        """Run sub-stage 5.1 (Build online) then 5.2 (Runtime isolated) — Req. 1-5.

        Strict order (Req. 1.2, 1.3): 5.2 only begins once 5.1 has completed for
        **all** hosts; if 5.1 fails for any host, 5.2 never runs. Every network
        transition is ``detach`` before ``attach`` (one way at a time), and no
        error path leaves a target with egress open (``_fail_safe`` — Req. 8.3).

        5.1 (``build_online``/``encerrar_egresso``), per host: classify the host's
        ``vulnerabilities``; open the egress window (detach attack nets → attach
        egress); apply the ONLINE commands; close the window. 5.2
        (``reisolar``/``verificar_egresso``/``setup_offline``/``carregar_abilities``):
        reattach the attack networks; probe every host and require
        :func:`egress_lost`; apply the OFFLINE commands; load the ordered Abilities
        + Adversary into Caldera (moved here, after the offline setup — Req. 3.4).

        Returns a :class:`_SubStageOutcome`: ``failed_step is None`` with the
        satisfied summary on success, or the offending ``failed_step`` + cause (and
        the partial summary) on failure.
        """
        host_targets = self._host_targets(params)
        containers = [c for _h, c in host_targets]
        egress = self._get_egress_window()
        setup = self._get_target_setup()
        prober = self._get_egress_prober()
        attack_nets = attack_networks_for(containers)

        # ===== 5.1 Build (online) =====================================
        try:
            for host, container in host_targets:
                classified = classify_commands(list(host.vulnerabilities))
                on_log(
                    LogLine(
                        step="build_online",
                        stream="info",
                        text=f"5.1 build online de {container}: abrindo janela de egresso",
                        substage="5.1",
                    )
                )
                window = egress.open(container, attack_nets, on_log=on_log)
                if not window.opened:
                    self._fail_safe(container, on_log=on_log)
                    return _SubStageOutcome.failure(
                        "build_online",
                        window.reason
                        or f"falha ao abrir a janela de egresso de {container}",
                        summary=SubStageSummary(),
                    )

                online = [c for c in classified if c.phase is SetupPhase.ONLINE]
                applied = setup.apply(
                    container, online, SetupPhase.ONLINE, on_log=on_log,
                    best_effort=True,
                )
                closed = egress.close(container, on_log=on_log)

                if applied.code != 0:
                    # Build failed: the window is already closed above, but run the
                    # fail-safe to guarantee no egress remains (idempotent close).
                    self._fail_safe(container, on_log=on_log)
                    return _SubStageOutcome.failure(
                        "build_online",
                        f"falha ao instalar os pré-requisitos online de "
                        f"{container} (code={applied.code})",
                        summary=SubStageSummary(),
                    )
                if not closed.closed:
                    self._fail_safe(container, on_log=on_log)
                    return _SubStageOutcome.failure(
                        "encerrar_egresso",
                        closed.reason
                        or f"falha ao encerrar a janela de egresso de {container}",
                        summary=SubStageSummary(),
                    )
        except Exception as exc:  # pragma: no cover - defensive
            self._fail_safe_all(containers, on_log=on_log)
            return _SubStageOutcome.failure(
                "build_online",
                f"falha inesperada na sub-etapa 5.1: {exc}",
                summary=SubStageSummary(),
            )

        # 5.1 completed for ALL hosts; the egress window is closed everywhere.
        summary = SubStageSummary(build_online_ok=True, egress_closed=True)

        # ===== 5.2 Runtime (isolated) =================================
        # reisolar: reattach the attack networks to every host (Req. 3.1).
        try:
            executor = self._get_compose_executor()
            for container in containers:
                # Reattach ONLY the networks this host belongs to, each with its
                # FIXED compose IP, restoring the exact curated attack-plane
                # topology (e.g. nginx -> kali-nginx@192.168.20.30 +
                # nginx-db@192.168.30.30). Reattaching every network, or letting
                # Docker assign a dynamic IP, breaks routing from the attacker to
                # the target (the abilities target fixed IPs) - the bug this fixes.
                for network, fixed_ip in attack_networks_with_ip_for(container):
                    on_log(
                        LogLine(
                            step="reisolar",
                            stream="info",
                            text=(
                                f"5.2 reisolando {container}: reatando {network}"
                                + (f" ({fixed_ip})" if fixed_ip else "")
                            ),
                            substage="5.2",
                        )
                    )
                    result = executor.attach_network(
                        container, network, on_log=on_log, ip=fixed_ip
                    )
                    if result.code != 0:
                        self._fail_safe_all(containers, on_log=on_log)
                        return _SubStageOutcome.failure(
                            "reisolar",
                            f"falha ao reatar a rede de ataque {network!r} a "
                            f"{container!r} (code={result.code})",
                            summary=summary,
                        )
        except Exception as exc:  # pragma: no cover - defensive
            self._fail_safe_all(containers, on_log=on_log)
            return _SubStageOutcome.failure(
                "reisolar",
                f"falha inesperada ao reisolar: {exc}",
                summary=summary,
            )

        # verificar_egresso: positively verify the loss of egress for ALL hosts
        # BEFORE any offline setup (Req. 3.2, 5.4, 5.5). Fail-closed.
        verdicts: list[EgressVerdict] = []
        for container in containers:
            on_log(
                LogLine(
                    step="verificar_egresso",
                    stream="info",
                    text=f"5.2 verificando a perda de egresso de {container}",
                    substage="5.2",
                )
            )
            verdicts.append(prober.probe(container, on_log=on_log))
        summary = SubStageSummary(
            build_online_ok=True,
            egress_closed=True,
            egress_verdicts=[
                EgressVerdictView(
                    container=v.container,
                    egress_lost=v.egress_lost,
                    conclusive=v.conclusive,
                    reason=v.reason or "",
                )
                for v in verdicts
            ],
        )
        if not egress_lost(verdicts):
            cause = self._egress_cause(verdicts)
            on_log(
                LogLine(
                    step="verificar_egresso",
                    stream="stderr",
                    text=cause,
                    substage="5.2",
                )
            )
            return _SubStageOutcome.failure(
                "verificar_egresso", cause, summary=summary
            )
        summary = summary.model_copy(update={"egress_lost_verified": True})

        # setup_offline: apply the OFFLINE commands per host (Req. 3.3).
        for host, container in host_targets:
            classified = classify_commands(list(host.vulnerabilities))
            offline = [c for c in classified if c.phase is SetupPhase.OFFLINE]
            on_log(
                LogLine(
                    step="setup_offline",
                    stream="info",
                    text=f"5.2 aplicando o setup offline de {container}",
                    substage="5.2",
                )
            )
            applied = setup.apply(
                container, offline, SetupPhase.OFFLINE, on_log=on_log,
                best_effort=True,
            )
            if applied.code != 0:
                return _SubStageOutcome.failure(
                    "setup_offline",
                    f"falha ao aplicar o setup offline de {container} "
                    f"(code={applied.code})",
                    summary=summary,
                )
        summary = summary.model_copy(update={"offline_applied": True})

        # carregar_abilities: ordered Abilities + Adversary into Caldera (Req. 3.4).
        on_log(
            LogLine(
                step="carregar_abilities",
                stream="info",
                text="5.2 carregando as Abilities ordenadas e o Adversary no Caldera",
                substage="5.2",
            )
        )
        loaded_abilities = self._load_abilities(params)
        if not loaded_abilities:
            cause = (
                "falha ao carregar as Abilities no Caldera "
                "(Caldera indisponível, erro de API ou artefato ausente)"
            )
            on_log(
                LogLine(
                    step="carregar_abilities",
                    stream="stderr",
                    text=cause,
                    substage="5.2",
                )
            )
            return _SubStageOutcome.failure(
                "carregar_abilities", cause, summary=summary
            )

        return _SubStageOutcome(
            summary=summary,
            loaded_abilities=True,
            failed_step=None,
            error_message=None,
        )

    @staticmethod
    def _egress_cause(verdicts: list[EgressVerdict]) -> str:
        """Derive the readable cause of an egress-verification failure (Req. 5.5, 8.1).

        Identifies the hosts that still have egress (``egress_lost=False``) or whose
        probe was inconclusive (``conclusive=False``, treated as egress present,
        fail-closed). An empty list of verdicts fails closed with its own message.
        """
        if not verdicts:
            return (
                "nenhum host-alvo sondado; a verificação de egresso reprova "
                "fail-closed (lista vazia)"
            )
        offenders = [
            f"{v.container} ({v.reason or ('egresso presente' if not v.egress_lost else 'inconclusivo')})"
            for v in verdicts
            if not (v.egress_lost and v.conclusive)
        ]
        return "egresso não comprovadamente perdido: " + "; ".join(offenders)

    def provision(
        self,
        params: EnvironmentParameters,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> ProvisionedEnvironment:
        """Provision the curated environment as a streaming **pipeline of steps**.

        In this curation iteration the environment is the
        ``docker/docker-compose.yml`` lab (containers only, three internal
        networks — Req. 3.2, 3.3). The provisioning becomes an **ordered and
        observable** sequence of steps, each one emitting :class:`LogLine` through
        ``on_log`` (Req. 2.2, 2.4) and, on failure, **returning early** with
        ``failed_step`` + the cause — Stage 5 does not complete and Stage 6 is
        blocked (Req. 7.1):

        1. ``subir_conteineres`` — brings up the lab via
           :meth:`_get_compose_executor` (``docker compose up -d --wait``,
           streaming — Req. 3.1). ``code != 0`` ⇒ failure with the cause of the
           last ``stderr`` line (Req. 3.5).
        2. ``aguardar_saude`` — confirms that **all** target containers are
           ``running`` (and ``healthy`` when there is a ``healthcheck``) by
           read-only inspection
           (:func:`~app.services.environment.health.inspect_health`, Req. 5.1).
           Failed ⇒ failure identifying the unhealthy container and the reason.
        3. ``carregar_abilities`` — reuses :meth:`_load_abilities` (Abilities
           ordered by the DAG's ``atomic_ordering`` + Adversary into Caldera —
           Req. 4.1). ``False`` ⇒ failure (Caldera unavailable / missing artifact —
           Req. 4.4).
        4. ``verificar_isolamento`` — reuses :meth:`_check_isolation`
           (:func:`inspect_isolation` read-only — Req. 5.3). Failed (or daemon
           unavailable, fail-closed) ⇒ failure identifying the non-isolated
           container and the reason (Req. 7.4).

        When the four steps pass, it builds the complete typed output with the
        isolation summary, the :class:`~app.services.environment.health.HealthSummary`,
        ``loaded_abilities=True``, the target containers, the ``compose_file`` and
        the origin ``curadoria_humana`` (Req. 5.5, 6.1);
        :attr:`ProvisionedEnvironment.ready` becomes ``True``.

        The health and isolation checks are **read-only** — neither one
        starts/stops/modifies containers or networks (Req. 9.2); only the
        ``subir_conteineres`` step creates resources. It accesses no public network
        and invokes no AI on any path (Req. 1.5).

        Args:
            params: The Stage 4 output — target containers and platform.
            on_log: Callback for each :class:`LogLine` emitted by the steps, in
                order; *no-op* default for compatibility with the existing callers
                (Req. 2.2).

        Returns:
            The :class:`ProvisionedEnvironment` — ready when the four steps pass,
            or with ``failed_step``/``error_message`` of the step that failed.
        """
        containers = self._target_containers(params)

        def _output(
            *,
            isolation: Optional[IsolationResult] = None,
            health: Optional[HealthSummary] = None,
            loaded_abilities: bool = False,
            substages: Optional[SubStageSummary] = None,
            failed_step: Optional[str] = None,
            error_message: Optional[str] = None,
        ) -> ProvisionedEnvironment:
            """Build the typed output, filling the summaries not yet computed.

            The steps that have not run yet (because an earlier one failed) get
            empty (not approved) summaries, so the screen always receives the
            complete shape of the :class:`ProvisionedEnvironment`. The
            ``substages`` summary defaults to a not-approved
            :class:`SubStageSummary` for the early-return paths (so ``ready`` stays
            ``False``), and is passed in satisfied only on full completion.
            """
            return ProvisionedEnvironment(
                campaign_id=params.campaign_id,
                platform=params.platform,
                target_containers=containers,
                compose_file=compose_path(),
                isolation=(
                    IsolationSummary.from_isolation_result(isolation)
                    if isolation is not None
                    else IsolationSummary()
                ),
                health=health if health is not None else HealthSummary(),
                loaded_abilities=loaded_abilities,
                substages=substages if substages is not None else SubStageSummary(),
                origin=self.origin(),
                failed_step=failed_step,
                error_message=error_message,
            )

        # --- Step 1: subir_conteineres (docker compose up -d --wait) — Req. 3.1.
        up_result = self._get_compose_executor().up(
            compose_path(), on_log=on_log
        )
        if up_result.code != 0:
            cause = self._up_cause(up_result)
            return _output(failed_step="subir_conteineres", error_message=cause)

        # --- Step 2: aguardar_saude (running + healthy, read-only) — Req. 5.1.
        health = self._check_health(containers, on_log=on_log)
        if not health.approved:
            cause = self._health_cause(health)
            return _output(
                health=health, failed_step="aguardar_saude", error_message=cause
            )

        # --- Sub-stages 5.1 (Build online) / 5.2 (Runtime isolated) — Req. 1.1-1.5.
        #
        # Inserted BETWEEN ``aguardar_saude`` and the final ``verificar_isolamento``,
        # with ``carregar_abilities`` MOVED into 5.2 (after ``setup_offline``) per
        # the design state diagram. When the extended network/exec surface is not
        # available (an earlier up/down-only fake executor and no sub-stage seam
        # injected), the orchestration degrades to the inherited placeholder so the
        # earlier Stage-5 suites keep their behavior (see ``_substages_supported``).
        if self._substages_supported():
            substage_outcome = self._run_substages(params, on_log=on_log)
            if substage_outcome.failed_step is not None:
                # Any sub-stage failure returns early with the step + cause; the
                # fail-safe inside ``_run_substages`` already closed every egress
                # window so no target is left with egress open (Req. 8.3, 8.4).
                return _output(
                    health=health,
                    loaded_abilities=substage_outcome.loaded_abilities,
                    substages=substage_outcome.summary,
                    failed_step=substage_outcome.failed_step,
                    error_message=substage_outcome.error_message,
                )
            loaded_abilities = substage_outcome.loaded_abilities
            substages = substage_outcome.summary
        else:
            # Inherited placeholder: no extended network/exec surface available, so
            # there is no curated setup to run through this executor. Reproduce the
            # inherited ``carregar_abilities`` step (with no sub-stage marker) and a
            # satisfied sub-stage summary, keeping the earlier Stage-5 behavior.
            on_log(
                LogLine(
                    step="carregar_abilities",
                    stream="info",
                    text="Carregando as Abilities ordered e o Adversary no Caldera",
                )
            )
            loaded_abilities = self._load_abilities(params)
            if not loaded_abilities:
                cause = (
                    "falha ao carregar as Abilities no Caldera "
                    "(Caldera indisponível, erro de API ou artefato ausente)"
                )
                on_log(
                    LogLine(
                        step="carregar_abilities", stream="stderr", text=cause
                    )
                )
                return _output(
                    health=health,
                    failed_step="carregar_abilities",
                    error_message=cause,
                )
            on_log(
                LogLine(
                    step="carregar_abilities",
                    stream="info",
                    text="Abilities e Adversary carregados no Caldera",
                )
            )
            substages = SubStageSummary(
                build_online_ok=True,
                egress_closed=True,
                egress_lost_verified=True,
                offline_applied=True,
            )

        # --- Final gate: verificar_isolamento (inspect_isolation, read-only) — Req. 4.
        on_log(
            LogLine(
                step="verificar_isolamento",
                stream="info",
                text="Verificando o isolamento dos contêineres-alvo (somente leitura)",
            )
        )
        isolation = self._check_isolation(containers)
        if not isolation.passed:
            cause = self._isolation_cause(isolation)
            on_log(
                LogLine(
                    step="verificar_isolamento", stream="stderr", text=cause
                )
            )
            return _output(
                isolation=isolation,
                health=health,
                loaded_abilities=loaded_abilities,
                substages=substages,
                failed_step="verificar_isolamento",
                error_message=cause,
            )
        on_log(
            LogLine(
                step="verificar_isolamento",
                stream="info",
                text="Isolamento approved para todos os contêineres-alvo",
            )
        )

        # --- Completion: every prerequisite and both sub-stages passed (Req. 4.5).
        return _output(
            isolation=isolation,
            health=health,
            loaded_abilities=loaded_abilities,
            substages=substages,
        )

    @staticmethod
    def _up_cause(result: CommandResult) -> str:
        """Derive the readable cause of the up failure, from the last error line (Req. 3.5, 7.2).

        Looks for the last ``stderr`` line emitted by the ``up`` (screen ↔ console
        consistency — Req. 7.5); in its absence (all in ``stdout``) it uses the
        last output line; with no line, it reports the exit code.
        """
        stderr = [l.text for l in result.lines if l.stream == "stderr"]
        if stderr:
            last = stderr[-1]
        else:
            output = [l.text for l in result.lines if l.stream in ("stdout", "stderr")]
            last = output[-1] if output else ""
        prefix = (
            f"falha ao subir os contêineres (código {result.code})"
        )
        return f"{prefix}: {last}" if last else prefix

    def _check_health(
        self, containers: list[str], *, on_log: Callable[[LogLine], None]
    ) -> HealthSummary:
        """Confirm the target containers' health (read-only) emitting lines (Req. 5.1).

        Reuses :func:`~app.services.environment.health.inspect_health` with the
        injected ``state_inspector`` (a *fake* in the tests; the real SDK adapter
        built lazily in production when ``None``). It is **read-only** — it never
        starts/stops/modifies containers (Req. 9.2). It emits one ``info`` start
        line and one line per container with the verdict, so the screen terminal
        reflects the result (Req. 2.4).
        """
        on_log(
            LogLine(
                step="aguardar_saude",
                stream="info",
                text="Confirmando a saúde dos contêineres-alvo (running + healthy)",
            )
        )
        health = inspect_health(containers, self._state_inspector)
        for verdict in health.verdicts:
            if verdict.approved:
                text = f"{verdict.container}: saudável"
                stream = "stdout"
            else:
                text = f"{verdict.container}: {verdict.reason or 'não saudável'}"
                stream = "stderr"
            on_log(LogLine(step="aguardar_saude", stream=stream, text=text))
        return health

    @staticmethod
    def _health_cause(health: HealthSummary) -> str:
        """Derive the readable cause of the health failure (Req. 5.1, 7.2).

        Identifies the unhealthy container(s) and the reason, consistent with the
        ``stderr`` lines emitted in the step (Req. 7.5). Fail-closed: an empty list
        of containers fails with no verdicts — it reports that.
        """
        unhealthy = health.unhealthy
        if not unhealthy:
            return (
                "nenhum contêiner-alvo a verificar; a saúde reprova "
                "fail-closed (lista vazia)"
            )
        parts = [
            f"{v.container} ({v.reason or 'não saudável'})" for v in unhealthy
        ]
        return "contêineres não saudáveis: " + "; ".join(parts)

    @staticmethod
    def _isolation_cause(isolation: IsolationResult) -> str:
        """Derive the readable cause of the isolation failure (Req. 5.3, 7.4).

        Identifies the non-isolated container(s) and the reason(s), for the
        screen's failure message and the console's ``stderr`` line (Req. 7.4,
        7.5). Fail-closed: with no verdicts (empty list) it fails — it reports that.
        """
        non_isolated = [v for v in isolation.verdicts if not v.isolated]
        if not non_isolated:
            return (
                "nenhum contêiner-alvo a verificar; o isolamento reprova "
                "fail-closed (lista vazia)"
            )
        parts = [
            f"{v.container} ({'; '.join(v.reasons) or 'não isolado'})"
            for v in non_isolated
        ]
        return "contêineres não isolados: " + "; ".join(parts)

    def _check_isolation(self, containers: list[str]) -> IsolationResult:
        """Verify the isolation (read-only) with full fail-closed (Req. 9.6, 14).

        Reuses :func:`inspect_isolation` (single source of the read-only
        inspection). The function itself already fails an empty list
        (``passed=False``); here, in addition, the **unavailable Docker daemon** is
        caught: in production (``inspector is None``) :func:`inspect_isolation`
        builds the real adapter, which raises ``RuntimeError`` when the daemon does
        not respond — we treat that as a failure (fail-closed, Req. 9.7, 14.4)
        instead of letting the exception bring down the provisioning.
        """
        try:
            return inspect_isolation(containers, self._inspector)
        except RuntimeError:
            # Docker daemon unavailable: fail-closed. A failed verdict per target
            # container, with the reason, keeps Stage 5 incomplete.
            from app.services.containment import ContainerIsolationVerdict

            reason = "Docker Engine API indisponível; isolation não verificável"
            verdicts = tuple(
                ContainerIsolationVerdict(
                    container=c, isolated=False, reasons=(reason,)
                )
                for c in containers
            )
            return IsolationResult(passed=False, verdicts=verdicts)

    def deprovision(
        self,
        environment: ProvisionedEnvironment,
        *,
        on_log: Callable[[LogLine], None] = lambda _l: None,
    ) -> DeprovisioningResult:
        """Teardown of the campaign environment — an actual ``compose down`` (Req. 8).

        At the end of Stage 6 the lab that was **brought up** for the campaign is
        actually torn down: curation calls :meth:`ComposeExecutor.down`
        (``docker compose -f <compose> down``) to stop/remove the lab containers
        and free the hardware resources (Req. 8.1, 8.2). The teardown is driven by
        the **same** provisioning seam, keeping the origin coherent
        (``curadoria_humana`` — Req. 8.3).

        The ``compose`` file comes from the provisioned ``environment`` itself
        (``environment.compose_file``), falling back to :func:`compose_path` when
        the output does not carry it. Each ``down`` output line is pushed through
        ``on_log`` in near-real time, so the screen terminal reflects the teardown
        (Req. 2.2).

        Besides the ``compose down``, the teardown also **sweeps the ephemeral
        egress network** (:data:`~app.services.environment.egress.EGRESS_NETWORK`)
        that sub-stage 5.1 may have created and that could have survived an error
        (Req. 10.2) — covering what 5.1/5.2 created, not just the base lab. The
        sweep is best-effort: it never crashes the teardown, and a
        *not-found* network (already gone, the happy path) is treated as success,
        not a residue. The removal runs through the
        :meth:`ComposeExecutor.remove_network` operation; legacy ``up``/``down``-
        only executors (injected by the earlier Stage-5 suites) predate it, so a
        ``hasattr`` guard skips the sweep gracefully for them.

        It maps the teardown outcome to the typed :class:`DeprovisioningResult`:

        - ``compose down`` succeeds **and** the egress network is gone ⇒
          ``deprovisioned=True`` with no remaining resources — the environment was
          removed successfully (Req. 8.1, 10.2).
        - the ``compose down`` fails **or** the egress network could not be
          removed ⇒ ``deprovisioned=False`` with the ``remaining_resources`` (the
          campaign's ``target_containers`` when the ``down`` failed, and/or the
          ephemeral egress network name when its removal failed) and a readable
          ``message`` derived from the last error lines, for the report to
          identify to the Researcher what stayed up (Req. 8.4, 10.3).

        Args:
            environment: The Stage 5 output whose environment must be
                deprovisioned — it carries the campaign's ``compose_file`` and
                ``target_containers``.
            on_log: Callback for each teardown :class:`LogLine`, in order; *no-op*
                default for compatibility.

        Returns:
            The :class:`DeprovisioningResult` — ``deprovisioned=True`` on success,
            or ``deprovisioned=False`` with the ``remaining_resources`` and the
            cause when the ``down`` fails.
        """
        compose_file = environment.compose_file or compose_path()
        executor = self._get_compose_executor()
        result = executor.down(compose_file, on_log=on_log)

        # Sweep the ephemeral egress network (``EGRESS_NETWORK``) that sub-stage
        # 5.1 may have created and that could have survived an error (Req. 10.2).
        # It is best-effort: it must never crash the teardown, and a
        # "network not found" (already gone) is SUCCESS, not a residue. Legacy
        # executors (``up``/``down``-only fakes injected by the earlier Stage-5
        # suites) predate this operation — the ``hasattr`` guard skips the sweep
        # gracefully for them, so their behavior is unchanged.
        egress_failed = False
        egress_cause = ""
        if hasattr(executor, "remove_network"):
            egress_result = executor.remove_network(EGRESS_NETWORK, on_log=on_log)
            if egress_result.code != 0 and not self._network_already_gone(
                egress_result
            ):
                egress_failed = True
                egress_cause = self._egress_removal_cause(egress_result)

        down_failed = result.code != 0

        if not down_failed and not egress_failed:
            return DeprovisioningResult(
                deprovisioned=True,
                remaining_resources=[],
                origin=self.origin(),
                message="Ambiente deprovisioned: contêineres do laboratório removidos.",
            )

        # A teardown failure (``compose down`` failed OR the egress network could
        # not be removed) reports the resources that remained provisioned for the
        # Researcher (Req. 10.3): the campaign's target containers (when the
        # ``down`` failed) and/or the ephemeral egress network (when its removal
        # failed). The origin stays coherent (``curadoria_humana`` — Req. 10.1).
        remaining: list[str] = []
        causes: list[str] = []
        if down_failed:
            remaining.extend(environment.target_containers)
            causes.append(self._down_cause(result))
        if egress_failed:
            remaining.append(EGRESS_NETWORK)
            causes.append(egress_cause)

        return DeprovisioningResult(
            deprovisioned=False,
            remaining_resources=remaining,
            origin=self.origin(),
            message="; ".join(c for c in causes if c),
        )

    @staticmethod
    def _network_already_gone(result: CommandResult) -> bool:
        """Tell whether a failed ``remove_network`` only means the net was already gone.

        The egress sweep is best-effort: an ephemeral network that **never
        survived** (the happy path closed it in 5.2) makes ``docker network rm``
        fail with a *not-found* message — which is **not** a residue (Req. 10.2).
        This inspects the emitted lines for the daemon's "not found"/"no such
        network" marker; when present, the caller treats the removal as a success.
        """
        for line in result.lines:
            lowered = line.text.lower()
            if "not found" in lowered or "no such network" in lowered:
                return True
        return False

    @staticmethod
    def _egress_removal_cause(result: CommandResult) -> str:
        """Derive the readable cause of a failed egress-network removal (Req. 10.3).

        Mirrors :meth:`_down_cause`: it uses the last ``stderr`` line emitted by
        the ``remove_network`` (screen ↔ console consistency), falling back to the
        last output line and then to the bare exit code, so the report names what
        kept the ephemeral egress network provisioned.
        """
        stderr = [l.text for l in result.lines if l.stream == "stderr"]
        if stderr:
            last = stderr[-1]
        else:
            output = [
                l.text for l in result.lines if l.stream in ("stdout", "stderr")
            ]
            last = output[-1] if output else ""
        prefix = (
            f"falha ao remover a rede de egresso efêmera {EGRESS_NETWORK!r} "
            f"(código {result.code})"
        )
        return f"{prefix}: {last}" if last else prefix

    @staticmethod
    def _down_cause(result: CommandResult) -> str:
        """Derive the readable cause of the down failure, from the last error line (Req. 8.4).

        Mirrors :meth:`_up_cause`: it looks for the last ``stderr`` line emitted by
        the ``down`` (screen ↔ console consistency — Req. 7.5); in its absence (all
        in ``stdout``) it uses the last output line; with no line, it reports only
        the exit code.
        """
        stderr = [l.text for l in result.lines if l.stream == "stderr"]
        if stderr:
            last = stderr[-1]
        else:
            output = [
                l.text for l in result.lines if l.stream in ("stdout", "stderr")
            ]
            last = output[-1] if output else ""
        prefix = f"falha ao deprovision os contêineres (código {result.code})"
        return f"{prefix}: {last}" if last else prefix

    def prepare(self, campaign: str) -> ProvisioningResult:
        """Verify the contained environment's isolation and expose its topology (read-only).

        Builds the static topology (hosts/networks/Mermaid) and verifies the
        isolation of the target containers (the same ones the Execution will
        attack) via :func:`inspect_isolation` with the injected inspector. **It
        creates/starts nothing.** The environment is declared ``ready`` iff
        ``isolated`` — curation does not provision, so it can only approve an
        already-isolated environment (Req. 5.4, 5.6); if it is not isolated,
        ``isolated=False`` and the topology is still returned with the verdicts
        (the screen shows the reason and Stage 5 is blocked).

        It accesses no network and invokes no AI on any path (Req. 5.5, 5.7).
        """
        hosts = build_hosts()
        networks = build_networks()

        # Verify isolation of the TARGET containers only (the victim hosts the
        # campaign attacks), NOT the attacker/C2 hosts. The Caldera C2 legitimately
        # joins a non-internal management network so the operator can reach its UI;
        # including it here would be a false negative. This mirrors exactly what the
        # Execution gate (OperationRunner) verifies, so Stage 5 and Stage 6 agree.
        containers = target_container_names()
        isolation = inspect_isolation(containers, self._inspector)

        topology = TopologyResponse(
            campaign_id=campaign,
            name=self._campaign_name(campaign),
            isolated=isolation.passed,
            hosts=hosts,
            networks=networks,
            mermaid=build_mermaid(hosts, networks),
            compose_file=compose_path(),
        )

        return ProvisioningResult(
            campaign_id=campaign,
            ready=isolation.passed,
            isolated=isolation.passed,
            isolation=isolation,
            topology=topology,
        )


__all__ = [
    # typed Stage 5 output DTOs (task 7.1)
    "ContainerVerdict",
    "IsolationSummary",
    # Stage-5 sub-stage DTOs (task 6)
    "EgressVerdictView",
    "SubStageSummary",
    "ProvisionedEnvironment",
    # teardown result at the end of Stage 6 (task 8.4)
    "DeprovisioningResult",
    # seam + compat. result (Stage 4) + curation
    "ProvisioningResult",
    "CalderaLoader",
    "EnvironmentProvisioner",
    "CurationEnvironmentProvisioner",
]
