"""Construção pura da topologia da rede-alvo contida (Estágio 4 — Provisionamento).

Este módulo é a **fonte única** da lógica de construção da topologia do laboratório
contido: o mapa estático de hosts/papéis/redes (derivado de
``docker/docker-compose.yml``), os modelos tipados (``HostView``/``NetworkView``/
``TopologyResponse``) e os construtores puros (``construir_hosts``/
``construir_redes``/``montar_mermaid``). Tudo é **read-only** e livre de efeitos:
não há chamadas Docker nem acesso à rede — a topologia é derivada do mapa de
containment (:mod:`app.services.containment.subnets`), a mesma fonte de verdade
de IPs/subnets que o pré-voo de segurança usa, mais o compose só para exibição.

Antes a lógica vivia em ``app/api/topologia.py`` (camada de API). Para que o
serviço de Provisionamento (task 4.1) e o endpoint possam **reusar** os mesmos
construtores sem duplicar o mapa do laboratório, a lógica pura foi extraída para
cá; ``app/api/topologia.py`` reimporta destes nomes e o contrato de
``GET /api/casos/{caso}/topologia`` permanece idêntico.

_Requisitos: 5.4, 5.7, 10.2_
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
# container names the isolation pre-flight inspects, so the Provisionador can
# verify exactly the containers the Execution (Estágio 5) will target.
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

    nome: str
    container: str
    papel: str = Field(description="attacker | target")
    ips: list[str] = Field(default_factory=list)


class NetworkView(BaseModel):
    """One internal-only lab network and the hosts attached to it."""

    nome: str
    subnet: str
    internal: bool = True
    hosts: list[str] = Field(default_factory=list)


class TopologyResponse(BaseModel):
    """The contained lab topology for a case + a Mermaid diagram of it."""

    caso_id: str
    nome: str
    isolado: bool = Field(
        default=True,
        description=(
            "Todas as redes do plano de ataque são internal: true (sem rota ao "
            "host ou à internet). O ataque não sai do ambiente controlado."
        ),
    )
    hosts: list[HostView]
    redes: list[NetworkView]
    mermaid: str
    compose_file: Optional[str] = None


def ips_for_host(host: str) -> list[str]:
    """All fixed IPs assigned to ``host`` across the static lab map, sorted."""
    return sorted(ip for ip, name in STATIC_IP_CONTAINERS.items() if name == host)


def container_names() -> list[str]:
    """The display container names for every lab host (isolation targets).

    This is the exact list the isolation verification inspects, in the stable
    ``HOST_ROLES`` order, so the Provisionador verifies the SAME containers the
    Execution stage will target.
    """
    return [HOST_CONTAINERS.get(host, host) for host in HOST_ROLES]


def construir_hosts() -> list[HostView]:
    """Build the list of :class:`HostView` from the static lab map (pure)."""
    return [
        HostView(
            nome=host,
            container=HOST_CONTAINERS.get(host, host),
            papel=role,
            ips=ips_for_host(host),
        )
        for host, role in HOST_ROLES.items()
    ]


def construir_redes() -> list[NetworkView]:
    """Build the list of internal :class:`NetworkView` from the static map (pure)."""
    return [
        NetworkView(
            nome=name,
            subnet=INTERNAL_NETWORK_SUBNETS[name],
            internal=True,
            hosts=list(members),
        )
        for name, members in NETWORK_MEMBERS.items()
    ]


def montar_mermaid(hosts: list[HostView], redes: list[NetworkView]) -> str:
    """Render a Mermaid flowchart of the contained-lab topology.

    Each host appears as a **single node** (no duplication for multi-homed hosts),
    labelled with its role, container and every IP/interface. Networks are separate
    nodes (hexagonal shape) and edges labelled with the host's IP on that network
    connect them — so a host that bridges two networks has one node with two edges,
    each annotated with the corresponding interface address.
    """
    role_by_host = {h.nome: h.papel for h in hosts}
    ips_by_host = {h.nome: h.ips for h in hosts}

    # Build the set of networks each host belongs to, with the IP for that net.
    # Also build a mapping host -> set of (net_name, ip).
    host_interfaces: dict[str, list[tuple[str, str]]] = {h.nome: [] for h in hosts}
    for net in redes:
        # Prefix = first three octets of the subnet, e.g. "192.168.10.0/24"
        # -> "192.168.10.". Splitting on "." and taking the first three parts is
        # correct; rsplit(".", 2) would wrongly yield just "192.168.".
        octetos = net.subnet.split("/")[0].split(".")
        prefix = ".".join(octetos[:3]) + "."
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
            host_interfaces[host].append((net.nome, ip))

    lines: list[str] = ["flowchart TB"]

    # Host nodes — unique, with all their interfaces in the label.
    attacker_ids: list[str] = []
    target_ids: list[str] = []
    for h in hosts:
        hid = h.nome.replace("-", "_")
        role = "\U0001f5e1\ufe0f atacante" if h.papel == "attacker" else "\U0001f3af alvo"
        ifaces = " / ".join(ip for _, ip in host_interfaces.get(h.nome, []))
        label = f"{h.nome}<br/>{role}<br/>{h.container}<br/>{ifaces}"
        lines.append(f'  {hid}["{label}"]')
        (attacker_ids if h.papel == "attacker" else target_ids).append(hid)

    # Network nodes — hexagonal shape, labelled with name + subnet.
    for net in redes:
        nid = "net_" + net.nome.replace("-", "_")
        lines.append(f'  {nid}{{{{"\U0001f310 {net.nome}<br/>{net.subnet}"}}}}')

    # Edges: host <--> network, labelled with the host's IP on that network.
    for h in hosts:
        hid = h.nome.replace("-", "_")
        for net_name, ip in host_interfaces.get(h.nome, []):
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
    net_ids = ["net_" + n.nome.replace("-", "_") for n in redes]
    if net_ids:
        lines.append(f"  class {','.join(net_ids)} network;")

    return "\n".join(lines)


def compose_path() -> Optional[str]:
    """Best-effort path to the lab compose file, for display only."""
    # app/services/environment/topologia_builder.py -> parents[3] = app/,
    # parents[5] = repo root (…/app/services/environment -> …/).
    repo_root = FsPath(__file__).resolve().parents[5]
    candidate = repo_root / "docker" / "docker-compose.yml"
    return str(candidate) if candidate.exists() else None


__all__ = [
    "HOST_ROLES",
    "HOST_CONTAINERS",
    "NETWORK_MEMBERS",
    "HostView",
    "NetworkView",
    "TopologyResponse",
    "ips_for_host",
    "container_names",
    "construir_hosts",
    "construir_redes",
    "montar_mermaid",
    "compose_path",
]
