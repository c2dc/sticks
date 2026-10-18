"""Pure construction of the contained target-network topology (Stage 4 — Provisioning).

This module is the **single source** of the contained-lab topology-building
logic: the static host/role/network map (derived from
``docker/docker-compose.yml``), the typed models (``HostView``/``NetworkView``/
``TopologyResponse``) and the pure builders (``build_hosts``/
``build_networks``/``build_mermaid``). Everything is **read-only** and
side-effect-free: there are no Docker calls nor network access — the topology is
derived from the containment map (:mod:`app.services.containment.subnets`), the
same IP/subnet source of truth the security pre-flight uses, plus the compose for
display only.

The logic used to live in ``app/api/topology.py`` (the API layer). So the
Provisioning service (task 4.1) and the endpoint can **reuse** the same builders
without duplicating the lab map, the pure logic was extracted here;
``app/api/topology.py`` re-imports these names and the contract of
``GET /api/campaigns/{campaign}/topology`` stays identical.

_Requirements: 5.4, 5.7, 10.2_
"""

from __future__ import annotations

from pathlib import Path as FsPath
from typing import Optional

from pydantic import BaseModel, Field

from app.services.containment.subnets import (
    INTERNAL_NETWORK_SUBNETS,
    STATIC_IP_CONTAINERS,
)

# ---------------------------------------------------------------------------
# Static lab description (derived from docker/docker-compose.yml). Roles and
# network membership mirror the compose file; IPs come from the containment
# STATIC_IP_CONTAINERS map so there is a single source of truth for addresses.
# ---------------------------------------------------------------------------

# Logical host -> role. "attacker" = adversary side (C2 + offensive node);
# "target" = victim side (the hosts the campaign attacks).
HOST_ROLES: dict[str, str] = {
    "caldera": "attacker",  # Red-team C2 (sticks-caldera-attacker)
    "kali": "attacker",     # Offensive node (sticks-kali-attacker)
    "nginx": "target",      # Web target / SUT-A (sticks-nginx-target)
    "db": "target",         # Backend MariaDB target / SUT-B (sticks-mariadb-target)
}

# Human-facing container names (post-rename) for display. These are the SAME
# container names the isolation pre-flight inspects, so the Provisioner can
# verify exactly the containers the Execution (Stage 5) will target.
HOST_CONTAINERS: dict[str, str] = {
    "caldera": "sticks-caldera-attacker",
    "kali": "sticks-kali-attacker",
    "nginx": "sticks-nginx-target",
    "db": "sticks-mariadb-target",
}

# Which logical hosts sit on each internal network (from the compose file).
NETWORK_MEMBERS: dict[str, tuple[str, ...]] = {
    "caldera-kali-network": ("caldera", "kali"),
    "kali-nginx-network": ("kali", "nginx"),
    "nginx-db-network": ("nginx", "db"),
}


class HostView(BaseModel):
    """One lab host: its logical name, container, role and fixed IPs."""

    name: str
    container: str
    role: str = Field(description="attacker | target")
    ips: list[str] = Field(default_factory=list)


class NetworkView(BaseModel):
    """One internal-only lab network and the hosts attached to it."""

    name: str
    subnet: str
    internal: bool = True
    hosts: list[str] = Field(default_factory=list)


class TopologyResponse(BaseModel):
    """The contained lab topology for a case + a Mermaid diagram of it."""

    campaign_id: str
    name: str
    isolated: bool = Field(
        default=True,
        description=(
            "Todas as networks do plano de ataque são internal: true (sem rota ao "
            "host ou à internet). O ataque não sai do ambiente controlado."
        ),
    )
    hosts: list[HostView]
    networks: list[NetworkView]
    mermaid: str
    compose_file: Optional[str] = None


def ips_for_host(host: str) -> list[str]:
    """All fixed IPs assigned to ``host`` across the static lab map, sorted."""
    return sorted(ip for ip, name in STATIC_IP_CONTAINERS.items() if name == host)


def container_names() -> list[str]:
    """The display container names for EVERY lab host, in stable ``HOST_ROLES``
    order (attackers + targets).

    NOTE: this includes the attacker/C2 hosts (``caldera``/``kali``). It is used
    for display/topology, NOT for the isolation verdict. The isolation check
    must inspect only the **target** containers (the hosts the campaign
    attacks) — see :func:`target_container_names` — because the attacker C2
    (``caldera``) legitimately joins a non-internal management network so the
    operator can reach the Caldera UI. Verifying attackers would be a false
    negative; the real Execution gate (OperationRunner) only verifies targets.
    """
    return [HOST_CONTAINERS.get(host, host) for host in HOST_ROLES]


def target_container_names() -> list[str]:
    """The container names of the **target** hosts only (role == ``target``).

    These are the victim containers the campaign actually attacks (e.g.
    ``sticks-nginx-target``, ``sticks-mariadb-target``). Isolation MUST hold for
    exactly these — if a target is not isolated, attack commands could leak to
    the real network. The attacker/C2 hosts are intentionally excluded: they
    are not attacked, and the C2 needs a management network to be operated.
    This mirrors what the Execution gate (``OperationRunner``) verifies, so the
    Stage 5 panel and the Stage 6 gate agree on the same contract.
    """
    return [
        HOST_CONTAINERS.get(host, host)
        for host, role in HOST_ROLES.items()
        if role == "target"
    ]


def build_hosts() -> list[HostView]:
    """Build the list of :class:`HostView` from the static lab map (pure)."""
    return [
        HostView(
            name=host,
            container=HOST_CONTAINERS.get(host, host),
            role=role,
            ips=ips_for_host(host),
        )
        for host, role in HOST_ROLES.items()
    ]


def build_networks() -> list[NetworkView]:
    """Build the list of internal :class:`NetworkView` from the static map (pure)."""
    return [
        NetworkView(
            name=name,
            subnet=INTERNAL_NETWORK_SUBNETS[name],
            internal=True,
            hosts=list(members),
        )
        for name, members in NETWORK_MEMBERS.items()
    ]


def build_mermaid(hosts: list[HostView], networks: list[NetworkView]) -> str:
    """Render a Mermaid flowchart of the contained-lab topology.

    Each host appears as a **single node** (no duplication for multi-homed hosts),
    labelled with its role, container and every IP/interface. Networks are separate
    nodes (hexagonal shape) and edges labelled with the host's IP on that network
    connect them — so a host that bridges two networks has one node with two edges,
    each annotated with the corresponding interface address.
    """
    role_by_host = {h.name: h.role for h in hosts}
    ips_by_host = {h.name: h.ips for h in hosts}

    # Build the set of networks each host belongs to, with the IP for that net.
    # Also build a mapping host -> set of (net_name, ip).
    host_interfaces: dict[str, list[tuple[str, str]]] = {h.name: [] for h in hosts}
    for net in networks:
        # Prefix = first three octets of the subnet, e.g. "192.168.10.0/24"
        # -> "192.168.10.". Splitting on "." and taking the first three parts is
        # correct; rsplit(".", 2) would wrongly yield just "192.168.".
        octets = net.subnet.split("/")[0].split(".")
        prefix = ".".join(octets[:3]) + "."
        for host in net.hosts:
            ip = "?"
            for hip in ips_by_host.get(host, []):
                if hip.startswith(prefix):
                    ip = hip
                    break
            else:
                # fallback to first IP
                hip_list = ips_by_host.get(host, [])
                if hip_list:
                    ip = hip_list[0]
            host_interfaces[host].append((net.name, ip))

    lines: list[str] = ["flowchart TB"]

    # Host nodes — unique, with all their interfaces in the label.
    attacker_ids: list[str] = []
    target_ids: list[str] = []
    for h in hosts:
        hid = h.name.replace("-", "_")
        role = "\U0001f5e1\ufe0f atacante" if h.role == "attacker" else "\U0001f3af alvo"
        ifaces = " / ".join(ip for _, ip in host_interfaces.get(h.name, []))
        label = f"{h.name}<br/>{role}<br/>{h.container}<br/>{ifaces}"
        lines.append(f'  {hid}["{label}"]')
        (attacker_ids if h.role == "attacker" else target_ids).append(hid)

    # Network nodes — hexagonal shape, labelled with name + subnet.
    for net in networks:
        nid = "net_" + net.name.replace("-", "_")
        lines.append(f'  {nid}{{{{"\U0001f310 {net.name}<br/>{net.subnet}"}}}}')

    # Edges: host <--> network, labelled with the host's IP on that network.
    for h in hosts:
        hid = h.name.replace("-", "_")
        for net_name, ip in host_interfaces.get(h.name, []):
            nid = "net_" + net_name.replace("-", "_")
            lines.append(f"  {hid} ---|{ip}| {nid}")

    # Style attacker vs target.
    lines.append("  classDef attacker fill:#fee2e2,stroke:#dc2626,color:#111;")
    lines.append("  classDef target fill:#dbeafe,stroke:#2563eb,color:#111;")
    lines.append("  classDef network fill:#f3e8ff,stroke:#7c3aed,color:#111;")
    if attacker_ids:
        lines.append(f"  class {','.join(attacker_ids)} attacker;")
    if target_ids:
        lines.append(f"  class {','.join(target_ids)} target;")
    net_ids = ["net_" + n.name.replace("-", "_") for n in networks]
    if net_ids:
        lines.append(f"  class {','.join(net_ids)} network;")

    return "\n".join(lines)


def compose_path() -> Optional[str]:
    """Best-effort path to the lab compose file, for display only."""
    # app/services/environment/topology_builder.py -> parents[3] = app/,
    # parents[5] = repo root (…/app/services/environment -> …/).
    repo_root = FsPath(__file__).resolve().parents[5]
    candidate = repo_root / "infra" / "docker" / "docker-compose.yml"
    return str(candidate) if candidate.exists() else None


__all__ = [
    "HOST_ROLES",
    "HOST_CONTAINERS",
    "target_container_names",
    "NETWORK_MEMBERS",
    "HostView",
    "NetworkView",
    "TopologyResponse",
    "ips_for_host",
    "container_names",
    "build_hosts",
    "build_networks",
    "build_mermaid",
    "compose_path",
]
