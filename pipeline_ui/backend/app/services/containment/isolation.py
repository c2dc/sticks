"""Container isolation verification (task 4.4) — security core.

Part of the ``ContainmentValidator``. Before any Operation runs, the pre-flight
must confirm that **every** target container is *isolated*: connected exclusively
to Docker networks marked ``internal: true``, with no bridge / ``local-network``
that has external reachability, and with **no external DNS** configured.

The design's "Decisão de design destacada" records the concrete leak this guards
against: in the current ``docker-compose.yml`` all lab containers also join a
``local-network`` bridge and several declare ``dns: 8.8.8.8``. In that state the
environment does **not** satisfy Requisito 6, so this module must refuse it.

Read-only guarantee
-------------------
This module performs **only inspection**. It never starts, stops, creates or
modifies containers or networks. The injected inspector is expected to call only
read APIs (``containers.get`` / ``networks.get`` / ``.attrs``).

Injectable inspector
--------------------
The Docker access is behind the :class:`ContainerInspector` ``Protocol`` and the
plain data classes :class:`ContainerNetworkInfo` / :class:`NetworkInfo`. Task 4.5
can implement a fake inspector returning fabricated configs (isolated,
with-bridge, with-external-dns) with **no real Docker daemon**. :class:`DockerSdkInspector`
adapts the real ``docker`` SDK for production use, and :func:`inspect_isolation`
lazily builds one only when no inspector is injected — so importing and
unit-testing this module never requires a daemon (Windows dev friendly).

Isolation rule (Property 2)
---------------------------
A container is ISOLATED iff **all** hold:

1. It is attached to at least one network.
2. Every attached network is ``internal: true`` (Docker ``Internal`` flag),
   cross-checked against the known internal subnets from :mod:`.subnets`.
3. It has no external DNS configured (``HostConfig.Dns`` empty, or containing
   only loopback / internal-subnet resolvers).

If any target container is not isolated, the overall verdict fails and the
offending container(s) are identified with a human-readable reason.

_Requisitos: 6.3, 6.4_
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.services.containment.subnets import is_internal_ip, is_internal_subnet

# Network names that are always treated as external/bridge, regardless of flags.
# ``local-network`` and Docker's default ``bridge`` provide external reachability
# when ports are published. The authoritative signal is still each network's
# ``Internal`` flag (see :class:`NetworkInfo.internal`); these names are a
# defensive cross-check for the exact leak described in the design.
_KNOWN_EXTERNAL_NETWORK_NAMES: frozenset[str] = frozenset({"local-network", "bridge", "host"})


# ---------------------------------------------------------------------------
# Inspector interface + plain data carriers (mockable without a real daemon)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NetworkInfo:
    """Read-only view of a Docker network relevant to isolation.

    Mirrors the fields the isolation rule needs from ``docker`` SDK
    ``Network.attrs``. Kept as a plain dataclass so tests can fabricate it.

    Attributes:
        name: Network name (e.g. ``"caldera-kali-network"``, ``"local-network"``).
        internal: The network's Docker ``Internal`` flag — the authoritative
            signal for "no external reachability".
        subnets: IPAM subnets declared for the network in CIDR (may be empty when
            the API does not expose IPAM, e.g. the default bridge).
    """

    name: str
    internal: bool
    subnets: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContainerNetworkInfo:
    """Read-only view of a container's network attachment and DNS config.

    Mirrors the fields the isolation rule needs from ``docker`` SDK
    ``Container.attrs`` (``NetworkSettings.Networks`` keys and ``HostConfig.Dns``).

    Attributes:
        name: Container name or id (used in verdicts / error messages).
        network_names: The networks the container is attached to, by name.
        dns: DNS servers configured on the container (``HostConfig.Dns``). Empty
            when the container inherits Docker's embedded resolver.
    """

    name: str
    network_names: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()


@runtime_checkable
class ContainerInspector(Protocol):
    """Read-only Docker inspection surface, injectable for tests.

    Implementations MUST perform only read operations. Two methods so the
    isolation logic can resolve a container's attachments and then classify each
    attached network by its ``Internal`` flag / subnets.
    """

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        """Return the network attachment + DNS config for ``container``.

        Raises:
            ContainerNotFound: if the container does not exist.
        """
        ...

    def get_network_info(self, network_name: str) -> NetworkInfo:
        """Return the :class:`NetworkInfo` for a network by name.

        Raises:
            NetworkNotFound: if the network does not exist.
        """
        ...


class ContainerNotFound(LookupError):
    """Raised by an inspector when a requested container does not exist."""


class NetworkNotFound(LookupError):
    """Raised by an inspector when a requested network does not exist."""


# ---------------------------------------------------------------------------
# Typed result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerIsolationVerdict:
    """Per-container isolation verdict (Req. 6.4).

    Attributes:
        container: Container name/id inspected.
        isolated: True iff the container passed every isolation check.
        reasons: Human-readable reasons the container is **not** isolated. Empty
            when ``isolated`` is True. Examples: ``"connected to local-network
            bridge"``, ``"external dns 8.8.8.8"``, ``"attached to non-internal
            network 'X'"``.
        external_networks: Names of attached networks judged non-internal.
        external_dns: DNS servers judged external.
    """

    container: str
    isolated: bool
    reasons: tuple[str, ...] = ()
    external_networks: tuple[str, ...] = ()
    external_dns: tuple[str, ...] = ()


@dataclass(frozen=True)
class IsolationResult:
    """Overall pre-flight isolation result across all target containers (Req. 6.4).

    Attributes:
        passed: True iff **every** target container is isolated.
        verdicts: Per-container verdicts, in the order the containers were given.
    """

    passed: bool
    verdicts: tuple[ContainerIsolationVerdict, ...] = ()

    @property
    def offending(self) -> tuple[ContainerIsolationVerdict, ...]:
        """The verdicts of containers that failed isolation (empty when passed)."""
        return tuple(v for v in self.verdicts if not v.isolated)

    def summary(self) -> str:
        """One-line human-readable summary suitable for a 409 abort message."""
        if self.passed:
            return "Contenção satisfeita: todos os containers-alvo estão isolados."
        parts = []
        for verdict in self.offending:
            parts.append(f"{verdict.container}: {'; '.join(verdict.reasons)}")
        return "Contenção não satisfeita — " + " | ".join(parts)


# ---------------------------------------------------------------------------
# DNS classification
# ---------------------------------------------------------------------------


def _is_external_dns(server: str) -> bool:
    """Return True if a DNS server address is external (not loopback/internal).

    Loopback (127.0.0.0/8) and addresses inside the known internal subnets are
    acceptable; anything else (e.g. ``8.8.8.8``) is external. Non-IP strings are
    treated as external, since a resolver named by host could resolve/route out.
    """
    try:
        ip = ipaddress.ip_address(server)
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip.is_loopback:
        return False
    return not is_internal_ip(server)


# ---------------------------------------------------------------------------
# Core isolation logic (pure; operates on the inspector interface)
# ---------------------------------------------------------------------------


def _classify_container(
    container: str, inspector: ContainerInspector
) -> ContainerIsolationVerdict:
    """Classify a single container as isolated or not, with reasons.

    Pure with respect to Docker: all access goes through ``inspector``. Missing
    containers/networks are surfaced as a non-isolated verdict (fail-closed): if
    we cannot prove isolation, we do not approve the Operation.
    """
    reasons: list[str] = []
    external_networks: list[str] = []
    external_dns: list[str] = []

    try:
        info = inspector.get_container_network_info(container)
    except ContainerNotFound:
        return ContainerIsolationVerdict(
            container=container,
            isolated=False,
            reasons=("container não encontrado na Docker Engine API",),
        )

    # Rule 1: must be attached to at least one network.
    if not info.network_names:
        reasons.append("container não está conectado a nenhuma rede")

    # Rule 2: every attached network must be internal.
    for network_name in info.network_names:
        if network_name in _KNOWN_EXTERNAL_NETWORK_NAMES:
            external_networks.append(network_name)
            reasons.append(f"conectado à rede bridge externa '{network_name}'")
            continue

        try:
            net = inspector.get_network_info(network_name)
        except NetworkNotFound:
            external_networks.append(network_name)
            reasons.append(
                f"rede '{network_name}' não encontrada na Docker Engine API"
            )
            continue

        if not net.internal:
            external_networks.append(network_name)
            reasons.append(
                f"conectado à rede não-interna '{network_name}' (Internal=false)"
            )
            continue

        # Cross-check: an internal-flagged network whose subnets are not among the
        # known lab ranges is suspicious. Only flag when subnets are declared and
        # none of them is internal (empty subnets => trust the Internal flag).
        if net.subnets and not any(is_internal_subnet(cidr) for cidr in net.subnets):
            external_networks.append(network_name)
            reasons.append(
                f"rede '{network_name}' marcada internal mas com subnet fora das "
                f"faixas internas conhecidas ({', '.join(net.subnets)})"
            )

    # Rule 3: no external DNS configured.
    for server in info.dns:
        if _is_external_dns(server):
            external_dns.append(server)
            reasons.append(f"dns externo {server}")

    isolated = not reasons
    return ContainerIsolationVerdict(
        container=container,
        isolated=isolated,
        reasons=tuple(reasons),
        external_networks=tuple(external_networks),
        external_dns=tuple(external_dns),
    )


def verify_isolation(
    containers: list[str], inspector: ContainerInspector
) -> IsolationResult:
    """Verify that every target container is isolated (pre-flight, read-only).

    This is the pure entry point used by tests (task 4.5) and by the
    OperationRunner pre-flight (task 7.3). It approves the Operation *if and only
    if* all target containers are isolated (Property 2); otherwise it fails and
    identifies the offending container(s).

    Args:
        containers: Names/ids of the containers targeted by the Operation.
        inspector: Any :class:`ContainerInspector` implementation (real SDK
            adapter in production, a fake returning fabricated configs in tests).

    Returns:
        An :class:`IsolationResult` with the overall pass/fail and per-container
        verdicts. An empty container list yields ``passed=False`` (there is
        nothing to prove isolated, so fail-closed rather than vacuously approve).
    """
    if not containers:
        return IsolationResult(passed=False, verdicts=())

    verdicts = tuple(_classify_container(c, inspector) for c in containers)
    passed = all(v.isolated for v in verdicts)
    return IsolationResult(passed=passed, verdicts=verdicts)


# ---------------------------------------------------------------------------
# Real docker SDK adapter (only constructed when no inspector is injected)
# ---------------------------------------------------------------------------


class DockerSdkInspector:
    """Adapts the real ``docker`` SDK to the :class:`ContainerInspector` interface.

    READ-ONLY: uses only ``client.containers.get`` / ``client.networks.get`` and
    reads ``.attrs``. Never starts or modifies anything.

    The ``docker`` package is imported lazily inside ``from_env`` so this module
    (and everything importing the containment package) imports cleanly on a
    machine without Docker, and unit tests never touch a daemon.
    """

    def __init__(self, client: object) -> None:
        """Wrap an existing ``docker.DockerClient`` (or any read-compatible client)."""
        self._client = client

    @classmethod
    def from_env(cls) -> DockerSdkInspector:
        """Build an inspector from the ambient Docker environment (read-only use).

        Imports ``docker`` lazily; raises ``RuntimeError`` with a clear message if
        the SDK/daemon is unavailable, so callers can surface a contained failure
        instead of an import error.
        """
        try:
            import docker  # local import: keep module importable without docker
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(
                "docker SDK indisponível; não é possível inspecionar isolamento"
            ) from exc
        try:
            client = docker.from_env()
        except Exception as exc:  # pragma: no cover - needs a real daemon
            raise RuntimeError(
                "Docker Engine API indisponível; não é possível inspecionar isolamento"
            ) from exc
        return cls(client)

    def get_container_network_info(self, container: str) -> ContainerNetworkInfo:
        try:
            obj = self._client.containers.get(container)  # type: ignore[attr-defined]
        except Exception as exc:  # docker.errors.NotFound and friends
            raise ContainerNotFound(container) from exc
        attrs = getattr(obj, "attrs", {}) or {}
        networks = (
            attrs.get("NetworkSettings", {}).get("Networks", {}) or {}
        )
        host_config = attrs.get("HostConfig", {}) or {}
        dns = tuple(host_config.get("Dns", []) or [])
        return ContainerNetworkInfo(
            name=getattr(obj, "name", container) or container,
            network_names=tuple(networks.keys()),
            dns=dns,
        )

    def get_network_info(self, network_name: str) -> NetworkInfo:
        try:
            obj = self._client.networks.get(network_name)  # type: ignore[attr-defined]
        except Exception as exc:  # docker.errors.NotFound and friends
            raise NetworkNotFound(network_name) from exc
        attrs = getattr(obj, "attrs", {}) or {}
        internal = bool(attrs.get("Internal", False))
        ipam_config = (attrs.get("IPAM", {}) or {}).get("Config", []) or []
        subnets = tuple(
            cfg.get("Subnet")
            for cfg in ipam_config
            if isinstance(cfg, dict) and cfg.get("Subnet")
        )
        return NetworkInfo(
            name=getattr(obj, "name", network_name) or network_name,
            internal=internal,
            subnets=subnets,
        )


def inspect_isolation(
    containers: list[str],
    inspector: ContainerInspector | None = None,
) -> IsolationResult:
    """Verify isolation, defaulting to the real Docker SDK when no inspector given.

    Convenience wrapper for the OperationRunner pre-flight: tests inject a fake
    inspector, while production omits it and gets a :class:`DockerSdkInspector`
    built from the ambient (read-only) Docker environment.

    Args:
        containers: Names/ids of target containers.
        inspector: Optional injected inspector. When ``None``, a real SDK adapter
            is created lazily (which will raise ``RuntimeError`` if Docker is
            unavailable — never during import, only when actually called).

    Returns:
        The :class:`IsolationResult` from :func:`verify_isolation`.
    """
    if inspector is None:
        inspector = DockerSdkInspector.from_env()
    return verify_isolation(containers, inspector)
