"""Health check of the target containers (Stage 5) — read-only.

This module implements the ``await_health`` step of the Stage 5 provisioning
pipeline (Simulation-environment creation): **confirming**, through a read-only
Docker inspection, that **all** the lab's target containers are in the
``running`` state and, when the service defines a ``healthcheck`` in the
``compose``, with health status ``healthy`` (Req. 5.1). The
``docker compose up -d --wait`` of the
:class:`~app.services.environment.compose_executor.DockerComposeExecutor` already
waits on health through Compose itself; this check **confirms** the state before
proceeding and produces the prerequisite-checklist line (Req. 5.6).

Read-only / side-effect-free guarantee (Req. 9.2)
-------------------------------------------------
Like the isolation check (:mod:`app.services.containment.isolation`), the health
check does **only inspection**: it never starts, stops, creates or modifies
containers or networks. The Docker access sits behind the injectable
:class:`ContainerStateInspector` seam — a ``@runtime_checkable Protocol`` that, by
contract, calls only read APIs (``containers.get`` / ``.attrs``).

Reuse of the read-only inspector seam
-------------------------------------
The design reuses the **same pattern** as the read-only inspector that
``inspect_isolation`` uses (:mod:`app.services.containment.isolation`): an
injectable ``Protocol`` + a simple data ``dataclass`` (mockable without a daemon)
+ a production adapter (:class:`DockerSdkStateInspector`) built **lazily** only
when no inspector is injected — exactly like
:class:`~app.services.containment.DockerSdkInspector`. This way importing and
unit-testing this module never requires a Docker daemon (friendly to Windows
dev), and the ``State``/``Health`` inspection sits behind the same seam as the
``NetworkSettings``/``HostConfig`` one of the isolation check.

> The existing :class:`~app.services.containment.ContainerInspector` exposes only
> ``NetworkSettings`` (networks) and ``HostConfig.Dns`` — not the
> ``State``/``Health`` that health requires. Instead of inflating that isolation
> seam with a method foreign to its responsibility, this module defines a seam
> **focused** on ``State`` following the same pattern (``Protocol`` + ``dataclass``
> + lazy SDK adapter), keeping each read-only inspection coherent with its
> responsibility.

Health rule (Req. 5.1)
----------------------
A target container is **healthy** iff:

1. It is in the ``running`` state (``State.Running == true``); and
2. When the service defines a ``healthcheck`` (``State.Health`` present), the
   health status is ``healthy``. When the service does **not** define a
   ``healthcheck``, there is no health status to require — ``healthy`` stays
   ``None`` and does **not** fail (the ``running`` state is enough).

If **any** target container is not healthy, the overall verdict fails and the
offending container is identified with a readable reason. Fail-closed: an empty
list of target containers fails (there is nothing to prove healthy), and a
container missing from the Docker Engine API fails with the reason.

_Requirements: 5.1, 9.2_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Read-only state/health inspector seam + data carrier (mockable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerStateInfo:
    """Read-only view of the state/health of **one** container (Req. 5.1).

    Mirrors the fields the health rule needs from ``Container.attrs["State"]`` of
    the ``docker`` SDK (``State.Running`` and, when there is a ``healthcheck``,
    ``State.Health.Status``). Kept as a simple *frozen* ``dataclass`` so the tests
    fabricate it **without** a Docker daemon, exactly like
    :class:`~app.services.containment.ContainerNetworkInfo` does in the isolation
    check.

    Attributes:
        name: Name/id of the inspected container (used in verdicts/messages).
        running: ``True`` iff the container is in the ``running`` state
            (``State.Running``).
        health: The container's health status when the service defines a
            ``healthcheck`` — ``"healthy"``, ``"unhealthy"`` or ``"starting"``
            (``State.Health.Status``). ``None`` when the service does **not**
            define a ``healthcheck`` (there is no health status to require — Req.
            5.1).
    """

    name: str
    running: bool = False
    health: Optional[str] = None


class ContainerStateNotFound(LookupError):
    """Raised by an inspector when the requested container does not exist."""


@runtime_checkable
class ContainerStateInspector(Protocol):
    """Read-only state/health inspection surface, injectable for tests.

    Follows the same pattern as
    :class:`~app.services.containment.ContainerInspector` (the seam
    ``inspect_isolation`` uses): the implementations MUST perform only read
    operations (``containers.get`` / ``.attrs``) — never starting, stopping,
    creating or modifying containers (Req. 9.2). The tests inject a *fake* that
    returns fabricated :class:`ContainerStateInfo` (running/unhealthy/no
    healthcheck) without a real daemon; production uses the
    :class:`DockerSdkStateInspector`.
    """

    def get_container_state(self, container: str) -> ContainerStateInfo:
        """Return the read-only state/health of ``container``.

        Raises:
            ContainerStateNotFound: if the container does not exist.
        """
        ...


# ---------------------------------------------------------------------------
# Typed output models (Pydantic projection for the JSON contracts)
# ---------------------------------------------------------------------------


class HealthVerdict(BaseModel):
    """Health verdict of **one** target container (Req. 5.1).

    Serializable Pydantic model that composes the :class:`HealthSummary` and the
    JSON contracts of the API/screen, mirroring :class:`ContainerStateInfo` with
    the health-rule decision already applied (``running`` + ``healthy``),
    analogous to the
    :class:`~app.services.environment.provisioner.ContainerVerdict` of the
    isolation.

    Attributes:
        container: Name/id of the inspected container.
        running: ``True`` iff the container is in the ``running`` state (Req. 5.1).
        healthy: ``True`` when the service's ``healthcheck`` reports ``healthy``;
            ``False`` when it reports ``unhealthy``/``starting``; ``None`` when the
            service does **not** define a ``healthcheck`` (there is no health to
            require — does not fail by itself, Req. 5.1).
        reason: Readable cause when the container is **not** healthy (not
            ``running`` or ``unhealthy``); ``None`` when approved.
    """

    container: str
    running: bool = False
    healthy: Optional[bool] = None
    reason: Optional[str] = None

    @property
    def approved(self) -> bool:
        """``True`` iff the container is ``running`` and (no healthcheck **or** ``healthy``).

        A container is healthy iff ``running is True`` **and** (``healthy is None``
        — service without ``healthcheck`` — **or** ``healthy is True``). A
        ``healthy is False`` (``unhealthy``/``starting``) fails (Req. 5.1).
        """
        return self.running and (self.healthy is None or self.healthy is True)


class HealthSummary(BaseModel):
    """Typed health summary — per-container + overall verdict (Req. 5.1, 5.6).

    Serializable projection for the Stage 5 output and the JSON contracts,
    analogous to the
    :class:`~app.services.environment.provisioner.IsolationSummary` of the
    isolation. Carries the **overall** result (``approved``) and the **per-
    container** verdict (``verdicts``), so the Stage 5 screen shows the reason for
    each unhealthy container (Req. 5.6, 7.2).

    Attributes:
        approved: ``True`` iff **all** target containers are healthy (overall
            pass/fail result — Req. 5.1). Fail-closed: an empty list of containers
            fails.
        verdicts: Per-container verdict, in the order they were checked.
    """

    approved: bool = False
    verdicts: list[HealthVerdict] = Field(default_factory=list)

    @property
    def unhealthy(self) -> list[HealthVerdict]:
        """The verdicts of the containers that failed the health check."""
        return [v for v in self.verdicts if not v.approved]


# ---------------------------------------------------------------------------
# Core health logic (pure; operates over the inspector seam)
# ---------------------------------------------------------------------------


def _classify_container(
    container: str, inspector: ContainerStateInspector
) -> HealthVerdict:
    """Classify a single container as healthy or not, with the reason.

    Pure with respect to Docker: all access goes through the ``inspector``. A
    missing container is treated as an unhealthy verdict (fail-closed): if we
    cannot prove it is ``running``/``healthy``, we do not approve it (Req. 5.1).

    The rule (Req. 5.1): ``running`` is required; when there is a ``healthcheck``
    (``health`` not ``None``), ``healthy`` is required; without a ``healthcheck``
    (``health is None``) ``running`` is enough and ``healthy`` stays ``None``.
    """
    try:
        state = inspector.get_container_state(container)
    except ContainerStateNotFound:
        return HealthVerdict(
            container=container,
            running=False,
            healthy=None,
            reason="container not found in the Docker Engine API",
        )

    if not state.running:
        return HealthVerdict(
            container=container,
            running=False,
            healthy=None,
            reason="container is not in the running state",
        )

    # It is running. No healthcheck -> healthy=None, does not fail (Req. 5.1).
    if state.health is None:
        return HealthVerdict(container=container, running=True, healthy=None)

    healthy = state.health == "healthy"
    reason = None if healthy else f"healthcheck reported status '{state.health}'"
    return HealthVerdict(
        container=container,
        running=True,
        healthy=healthy,
        reason=reason,
    )


def check_health(
    containers: list[str], inspector: ContainerStateInspector
) -> HealthSummary:
    """Verify that all target containers are healthy (read-only, pure).

    Pure entry point used by the tests and by the ``await_health`` step of the
    provisioning (Req. 5.1). Approves iff **all** target containers are
    ``running`` and (no ``healthcheck`` **or** ``healthy``); otherwise it fails and
    identifies the offending container(s) with the reason.

    Args:
        containers: Names/ids of the environment's target containers.
        inspector: Any implementation of :class:`ContainerStateInspector` (the
            real SDK adapter in production, a *fake* returning fabricated states in
            the tests).

    Returns:
        A :class:`HealthSummary` with the overall pass/fail result and the per-
        container verdicts. An empty list returns ``approved=False`` (nothing to
        prove healthy — fail-closed, instead of approving vacuously), analogous to
        :func:`~app.services.containment.verify_isolation`.
    """
    if not containers:
        return HealthSummary(approved=False, verdicts=[])

    verdicts = [_classify_container(c, inspector) for c in containers]
    approved = all(v.approved for v in verdicts)
    return HealthSummary(approved=approved, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Real docker SDK adapter (built only when no inspector is injected)
# ---------------------------------------------------------------------------


class DockerSdkStateInspector:
    """Adapt the real ``docker`` SDK to the :class:`ContainerStateInspector` interface.

    READ-ONLY: it uses only ``client.containers.get`` and reads ``.attrs`` — it
    never starts or modifies anything (Req. 9.2). Follows the same pattern as
    :class:`~app.services.containment.DockerSdkInspector`: the ``docker`` package
    is imported **lazily** inside ``from_env``, so this module (and everything that
    imports the ``environment`` package) imports cleanly on a machine without
    Docker, and the unit tests never touch a daemon.
    """

    def __init__(self, client: object) -> None:
        """Wrap an existing ``docker.DockerClient`` (or any read client)."""
        self._client = client

    @classmethod
    def from_env(cls) -> "DockerSdkStateInspector":
        """Build an inspector from the Docker environment (read-only use).

        Imports ``docker`` lazily; raises ``RuntimeError`` with a clear message if
        the SDK/daemon is unavailable, so the callers surface a contained failure
        instead of an import error.
        """
        try:
            import docker  # local import: keeps the module importable without docker
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise RuntimeError(
                "docker SDK unavailable; cannot inspect health"
            ) from exc
        try:
            client = docker.from_env()
        except Exception as exc:  # pragma: no cover - needs a real daemon
            raise RuntimeError(
                "Docker Engine API unavailable; cannot inspect health"
            ) from exc
        return cls(client)

    def get_container_state(self, container: str) -> ContainerStateInfo:
        """Read ``State.Running`` and ``State.Health.Status`` of the container (read-only).

        ``State.Health`` only exists when the service defines a ``healthcheck`` —
        in its absence, ``health`` stays ``None`` (there is no health to require,
        Req. 5.1).
        """
        try:
            obj = self._client.containers.get(container)  # type: ignore[attr-defined]
        except Exception as exc:  # docker.errors.NotFound and the like
            raise ContainerStateNotFound(container) from exc
        attrs = getattr(obj, "attrs", {}) or {}
        state = attrs.get("State", {}) or {}
        running = bool(state.get("Running", False))
        health_obj = state.get("Health")
        health = None
        if isinstance(health_obj, dict):
            status = health_obj.get("Status")
            health = status if status else None
        return ContainerStateInfo(
            name=getattr(obj, "name", container) or container,
            running=running,
            health=health,
        )


def inspect_health(
    containers: list[str],
    inspector: ContainerStateInspector | None = None,
) -> HealthSummary:
    """Check health, using the real Docker SDK when no inspector is given.

    Convenience wrapper for the ``await_health`` step of the provisioning,
    analogous to :func:`~app.services.containment.inspect_isolation`: the tests
    inject a *fake*, while production omits the argument and receives a
    :class:`DockerSdkStateInspector` built from the Docker environment
    (read-only).

    Args:
        containers: Names/ids of the target containers.
        inspector: Optional injected inspector. When ``None``, a real SDK adapter
            is built lazily (which raises ``RuntimeError`` if Docker is unavailable
            — never on import, only when actually called).

    Returns:
        The :class:`HealthSummary` of :func:`check_health`.
    """
    if inspector is None:
        inspector = DockerSdkStateInspector.from_env()
    return check_health(containers, inspector)


__all__ = [
    "ContainerStateInfo",
    "ContainerStateNotFound",
    "ContainerStateInspector",
    "HealthVerdict",
    "HealthSummary",
    "check_health",
    "DockerSdkStateInspector",
    "inspect_health",
]
