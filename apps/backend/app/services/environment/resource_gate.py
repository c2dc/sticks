"""ResourceGate — a deterministic, code-level host-resource authority (task 5.1).

A new first-class component of the AI-first flow, modeled on the
containment/isolation gate (:mod:`app.services.containment.isolation`): the hard
host-resource limits are enforced **in code**, **NOT entrusted to the agent**.
The Environment agent may *propose* targets and *inspect* the host to plan, but
the authority that admits or rejects a plan is this gate — exactly like
containment (agent proposes, gate disposes).

The gate enforces three hard limits **before any container is created** and
**rejects** a plan that exceeds any of them (design §D):

======================================  =========  ===========
Limit                                   Default    Enforced
======================================  =========  ===========
Simultaneous containers per run         10         hard reject
Total RAM (GB)                          20         hard reject
Total disk (GB)                         300        hard reject
======================================  =========  ===========

``ResourceLimits``
    The *frozen*, configurable limits (the defaults above).

``ResourcePlan``
    The *frozen* resource footprint of a proposed environment — how many
    containers and how much RAM/disk the agent's proposed targets would consume.

``ResourceGateVerdict``
    The *frozen* outcome of :meth:`ResourceGate.evaluate` — ``approved`` plus the
    human-readable ``reasons`` naming each limit the plan exceeded (empty when
    approved).

``ResourceGate``
    The deterministic authority. :meth:`ResourceGate.evaluate` is **fail-closed**:
    it approves **iff every** limit holds (``container_count <= max_containers``
    **and** ``ram_gb <= max_ram_gb`` **and** ``disk_gb <= max_disk_gb``); otherwise
    it rejects, naming the exceeded limit(s) in ``reasons``.

Host-resource inspection
-------------------------
Like the containment package's injectable :class:`ContainerInspector`, the host
inspection sits behind the :class:`HostResourceInspector` ``@runtime_checkable
Protocol`` and the plain :class:`HostResources` / :class:`ProposedTarget` data
carriers, so the tests inject a fake with fabricated figures and no real Docker
daemon, and production injects :class:`DockerSdkHostResourceInspector` (resolved
lazily — importing/unit-testing this module never requires a daemon, Windows dev
friendly). :func:`build_resource_plan` turns the agent's proposed targets into a
:class:`ResourcePlan` the gate can evaluate.

Placement: the Environment agent builds a :class:`ResourcePlan` from its proposed
targets and invokes :meth:`ResourceGate.evaluate` **before** provisioning. A
rejected plan fails the environment stage with the exceeded-limit reasons recorded
in the trace; **no container is created** (design §D; wired by task 6.2).
``ResourceGate`` is a factual/structured authority (design §F) — not a
vector-memory concern.

_Requirements: 3.1, 12.2_
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence, runtime_checkable

# ---------------------------------------------------------------------------
# Limits / plan / verdict — the design §D contract (frozen dataclasses).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceLimits:
    """Hard host-resource limits the gate enforces (design §D, configurable).

    *Frozen* (immutable): the limits are a fixed, factual policy — a caller
    chooses a different policy by constructing a new :class:`ResourceLimits`, never
    by mutating one in place. The defaults are the design's locked values
    (10 containers / 20 GB RAM / 300 GB disk).

    Attributes:
        max_containers: Maximum number of simultaneous containers a single run may
            provision. Default ``10``.
        max_ram_gb: Maximum total RAM (GB) the run's containers may consume.
            Default ``20``.
        max_disk_gb: Maximum total disk (GB) the run's containers may consume.
            Default ``300``.
    """

    max_containers: int = 10
    max_ram_gb: int = 20
    max_disk_gb: int = 300


@dataclass(frozen=True)
class ResourcePlan:
    """Resource footprint of a proposed environment (design §D).

    *Frozen* snapshot of what the agent's proposed targets would consume, built by
    :func:`build_resource_plan` from the host inspection and the proposed targets.
    The gate reads exactly these three figures and nothing else.

    Attributes:
        container_count: How many containers the plan would provision.
        ram_gb: Total RAM (GB) the plan would consume (float — fractional GB are
            common once per-container requests are summed).
        disk_gb: Total disk (GB) the plan would consume.
    """

    container_count: int
    ram_gb: float
    disk_gb: float


@dataclass(frozen=True)
class ResourceGateVerdict:
    """Typed, *frozen* outcome of :meth:`ResourceGate.evaluate` (design §D).

    Attributes:
        approved: ``True`` iff **every** limit held. Fail-closed: any exceeded
            limit yields ``False``.
        reasons: The human-readable reason(s) naming each limit the plan exceeded,
            in a stable order (containers, RAM, disk). **Empty** when ``approved``
            is ``True``.
    """

    approved: bool
    reasons: list[str] = field(default_factory=list)


class ResourceGate:
    """Deterministic, code-level authority over the host-resource limits (design §D).

    Modeled on the containment/isolation gate: the limits are enforced **in code**
    and are **not entrusted to the agent**. :meth:`evaluate` is a pure function of
    the plan and the configured limits — same plan + same limits always yields the
    same verdict — so it is trivially testable and auditable (Property 1).

    The limits are **injectable** (default :class:`ResourceLimits`), so a caller
    can tighten or loosen the policy without touching the gate logic.

    _Requirements: 3.1, 12.2_
    """

    def __init__(self, limits: ResourceLimits = ResourceLimits()) -> None:
        """Assemble the gate with the limits it enforces.

        Args:
            limits: The :class:`ResourceLimits` policy to enforce. Defaults to the
                design's locked limits (10 containers / 20 GB RAM / 300 GB disk).
        """
        self._limits = limits

    @property
    def limits(self) -> ResourceLimits:
        """The :class:`ResourceLimits` this gate enforces (read-only)."""
        return self._limits

    def evaluate(self, plan: ResourcePlan) -> ResourceGateVerdict:
        """Fail-closed: approve iff every limit holds; else reject with reasons.

        Approves **iff** ``container_count <= max_containers`` **and**
        ``ram_gb <= max_ram_gb`` **and** ``disk_gb <= max_disk_gb``. Whenever any
        limit is exceeded the verdict is ``approved=False`` and every exceeded
        limit is named in ``reasons`` (in the stable order containers → RAM → disk),
        so the environment stage can record exactly which limit(s) the plan broke
        in the trace. No container is created on a rejection (enforced by the caller
        — the gate itself is read-only). An approving verdict always carries an
        empty ``reasons`` list.

        Args:
            plan: The :class:`ResourcePlan` footprint of the proposed environment.

        Returns:
            A :class:`ResourceGateVerdict` — ``approved=True`` with no reasons when
            every limit holds, else ``approved=False`` naming the exceeded limit(s).
        """
        reasons: list[str] = []

        if plan.container_count > self._limits.max_containers:
            reasons.append(
                f"contêineres solicitados ({plan.container_count}) excedem o "
                f"limite de {self._limits.max_containers}"
            )
        if plan.ram_gb > self._limits.max_ram_gb:
            reasons.append(
                f"RAM solicitada ({plan.ram_gb} GB) excede o limite de "
                f"{self._limits.max_ram_gb} GB"
            )
        if plan.disk_gb > self._limits.max_disk_gb:
            reasons.append(
                f"disco solicitado ({plan.disk_gb} GB) excede o limite de "
                f"{self._limits.max_disk_gb} GB"
            )

        return ResourceGateVerdict(approved=not reasons, reasons=reasons)


# ---------------------------------------------------------------------------
# Host-resource inspection — build a ResourcePlan from the proposed targets.
# Injectable inspector + plain data carriers (mockable without a real daemon),
# mirroring the containment package's ContainerInspector seam.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedTarget:
    """One target the agent proposes to provision, with its resource request.

    Plain, *frozen* carrier so the tests can fabricate proposed targets with no
    Docker daemon. Each target contributes one container and its RAM/disk request
    to the aggregate :class:`ResourcePlan`.

    Attributes:
        name: The proposed container/target name (for traceability).
        ram_gb: RAM (GB) this single target would consume.
        disk_gb: Disk (GB) this single target would consume.
    """

    name: str
    ram_gb: float = 0.0
    disk_gb: float = 0.0


@dataclass(frozen=True)
class HostResources:
    """Read-only snapshot of the host's available resources (for planning).

    Mirrors the figures the planning needs from the host: how much RAM/disk the
    host has available and how many containers already run. The gate itself only
    reads the :class:`ResourcePlan`, but exposing the host snapshot lets the agent
    *plan* within what the host can actually offer before the gate adjudicates.

    Attributes:
        total_ram_gb: Total RAM (GB) the host exposes.
        total_disk_gb: Total disk (GB) the host exposes.
        running_containers: Containers already running on the host.
    """

    total_ram_gb: float = 0.0
    total_disk_gb: float = 0.0
    running_containers: int = 0


@runtime_checkable
class HostResourceInspector(Protocol):
    """Read-only host-resource inspection surface, injectable for tests.

    A ``@runtime_checkable Protocol`` — like the containment package's
    :class:`~app.services.containment.ContainerInspector` and the other injectable
    seams — so any object exposing ``get_host_resources`` satisfies it structurally,
    without explicit inheritance. Implementations MUST perform **only read**
    operations (they inspect; they never create or modify anything). The tests
    inject a fake returning fabricated figures; production injects
    :class:`DockerSdkHostResourceInspector`.
    """

    def get_host_resources(self) -> HostResources:
        """Return the host's current :class:`HostResources` snapshot (read-only)."""
        ...


class DockerSdkHostResourceInspector:
    """Production :class:`HostResourceInspector` adapting the real ``docker`` SDK.

    Reads the host figures from the Docker daemon's ``info()`` (``MemTotal`` and the
    running-container count) — **read-only**, it never creates or modifies anything.
    The ``docker`` SDK is imported and the client built **lazily**, so importing and
    unit-testing :mod:`resource_gate` never requires a daemon (Windows dev friendly);
    the real client is only touched when :meth:`get_host_resources` actually runs.
    """

    def __init__(self, client: Optional[object] = None) -> None:
        """Assemble the inspector, optionally with an already-built Docker client.

        Args:
            client: An optional ``docker`` SDK client. When ``None`` the client is
                built lazily on first use (``docker.from_env()``), so merely
                constructing this inspector performs no I/O and needs no daemon.
        """
        self._client = client

    def _get_client(self) -> object:
        if self._client is None:
            import docker  # type: ignore[import-untyped]

            self._client = docker.from_env()
        return self._client

    def get_host_resources(self) -> HostResources:
        """Read the host RAM/disk and running-container count from the daemon."""
        client = self._get_client()
        info = client.info()  # type: ignore[attr-defined]

        mem_total_bytes = info.get("MemTotal", 0) or 0
        total_ram_gb = float(mem_total_bytes) / (1024 ** 3)
        running = int(info.get("ContainersRunning", 0) or 0)

        # The Docker ``info()`` does not expose a portable total-disk figure; it is
        # resolved elsewhere (or left 0.0) and does not gate the plan here.
        return HostResources(
            total_ram_gb=total_ram_gb,
            total_disk_gb=0.0,
            running_containers=running,
        )


def build_resource_plan(targets: Sequence[ProposedTarget]) -> ResourcePlan:
    """Build a :class:`ResourcePlan` from the agent's proposed targets.

    Aggregates the proposed targets into the footprint the gate evaluates: the
    container count is the number of targets, and the RAM/disk are the sums of the
    per-target requests. An **empty** proposal yields a zero-footprint plan
    (``container_count=0``), which the gate approves — "nothing proposed" consumes
    nothing. This is the pure projection the Environment agent feeds to
    :meth:`ResourceGate.evaluate` **before** any container is created (design §D).

    Args:
        targets: The targets the agent proposes to provision, each carrying its
            RAM/disk request.

    Returns:
        The aggregate :class:`ResourcePlan` footprint of the proposal.
    """
    return ResourcePlan(
        container_count=len(targets),
        ram_gb=sum(t.ram_gb for t in targets),
        disk_gb=sum(t.disk_gb for t in targets),
    )


__all__ = [
    "ResourceLimits",
    "ResourcePlan",
    "ResourceGateVerdict",
    "ResourceGate",
    "ProposedTarget",
    "HostResources",
    "HostResourceInspector",
    "DockerSdkHostResourceInspector",
    "build_resource_plan",
]
