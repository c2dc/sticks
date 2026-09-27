"""Internal-subnet constants and helpers (task 4.2 constants source).

The Pipeline_UI lab defines three Docker networks marked ``internal: true`` in
``docker/docker-compose.yml``. Every legitimate adversary destination and every
target container must live **exclusively** inside these subnets:

- ``caldera-kali-network`` — 172.20.0.0/24
- ``kali-nginx-network``   — 172.21.0.0/24
- ``nginx-db-network``     — 172.22.0.0/24

This module is the single source of truth for those subnets. Task 4.2 (destination
subnet validation) and task 4.4 (container isolation) both consume it: 4.2 to
classify a parsed destination as internal/external, and 4.4 to cross-check a
container's attached-network subnets against the known internal ranges (in
addition to trusting each network's Docker ``Internal`` flag).

Parsing is pure Python (``ipaddress``); no network access is performed, so it is
safe to import and unit-test on the Windows dev environment without a Docker
daemon.

_Requisitos: 6.1, 6.2, 6.3, 6.4_
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

# The three lab subnets, in ascending order. Named so error messages and the
# preview can refer to the compose network each range belongs to.
INTERNAL_SUBNET_CIDRS: tuple[str, ...] = (
    "172.20.0.0/24",  # caldera-kali-network
    "172.21.0.0/24",  # kali-nginx-network
    "172.22.0.0/24",  # nginx-db-network
)

# Map compose network name -> its internal subnet, for cross-checking a
# container's attached networks by name when the Docker API exposes IPAM data.
INTERNAL_NETWORK_SUBNETS: dict[str, str] = {
    "caldera-kali-network": "172.20.0.0/24",
    "kali-nginx-network": "172.21.0.0/24",
    "nginx-db-network": "172.22.0.0/24",
}

# Static IPv4 -> container-name map from the docker-compose lab layout. Each of
# the three internal networks assigns two fixed addresses (``.10`` and ``.20``),
# and containers that bridge two networks appear under both of their addresses
# (kali is 172.20.0.20 and 172.21.0.10; nginx is 172.21.0.20 and 172.22.0.10).
# Task 4.6 (command/destination preview) uses this to resolve an internal
# destination IP to the concrete target container for the confirmation modal.
# This is an IP->name lookup, NOT a second subnet source of truth: it complements
# INTERNAL_SUBNET_CIDRS / INTERNAL_NETWORK_SUBNETS rather than duplicating them.
STATIC_IP_CONTAINERS: dict[str, str] = {
    # caldera-kali-network (172.20.0.0/24)
    "172.20.0.10": "caldera",
    "172.20.0.20": "kali",
    # kali-nginx-network (172.21.0.0/24)
    "172.21.0.10": "kali",
    "172.21.0.20": "nginx",
    # nginx-db-network (172.22.0.0/24)
    "172.22.0.10": "nginx",
    "172.22.0.20": "db",
}


def container_for_ip(address: str) -> str | None:
    """Resolve a static lab IPv4 literal to its container name, if known.

    Args:
        address: An IPv4 literal (e.g. ``"172.21.0.20"``).

    Returns:
        The container name from :data:`STATIC_IP_CONTAINERS` when ``address`` is
        one of the fixed lab addresses; ``None`` for any other (including
        internal-subnet IPs without a static assignment, or external IPs).
        No DNS is performed.
    """
    return STATIC_IP_CONTAINERS.get(address)

# Pre-parsed network objects for fast, allocation-free membership tests. These
# are the parsed form of INTERNAL_SUBNET_CIDRS (the single source of the CIDR
# strings) — nothing below re-types a CIDR literal.
INTERNAL_NETWORKS: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.ip_network(cidr) for cidr in INTERNAL_SUBNET_CIDRS
)

# Individual named networks, resolved from INTERNAL_NETWORK_SUBNETS by compose
# network name so error messages and task 4.4 can refer to a specific subnet.
# Derived (not re-typed) from the same CIDR strings above.
#: caldera-kali-network (internal: true) — 172.20.0.0/24.
CALDERA_KALI_SUBNET: ipaddress.IPv4Network = ipaddress.ip_network(
    INTERNAL_NETWORK_SUBNETS["caldera-kali-network"]
)
#: kali-nginx-network (internal: true) — 172.21.0.0/24.
KALI_NGINX_SUBNET: ipaddress.IPv4Network = ipaddress.ip_network(
    INTERNAL_NETWORK_SUBNETS["kali-nginx-network"]
)
#: nginx-db-network (internal: true) — 172.22.0.0/24.
NGINX_DB_SUBNET: ipaddress.IPv4Network = ipaddress.ip_network(
    INTERNAL_NETWORK_SUBNETS["nginx-db-network"]
)

# Backwards-compatible alias for the pre-parsed tuple used by internal helpers.
_INTERNAL_NETWORKS: tuple[ipaddress.IPv4Network, ...] = INTERNAL_NETWORKS


def is_internal_ip(address: str) -> bool:
    """Return True if ``address`` is an IPv4 literal inside a known internal subnet.

    Non-IP strings (hostnames such as ``nmap.org``) return False: a hostname is
    never considered internal because it can resolve anywhere. Task 4.2 treats a
    non-internal destination as external.

    Args:
        address: An IPv4 literal (e.g. ``"172.21.0.20"``) or any other string.

    Returns:
        True only when ``address`` parses as an IPv4 address contained in one of
        :data:`INTERNAL_SUBNET_CIDRS`.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if not isinstance(ip, ipaddress.IPv4Address):
        return False
    return any(ip in network for network in _INTERNAL_NETWORKS)


def is_internal_subnet(cidr: str) -> bool:
    """Return True if ``cidr`` is (contained in) one of the known internal subnets.

    Used by task 4.4 to cross-check a container's attached-network IPAM subnet
    against the lab's internal ranges. A subnet counts as internal when it is
    equal to, or a subnet of, one of :data:`INTERNAL_SUBNET_CIDRS`.

    Args:
        cidr: A network in CIDR notation (e.g. ``"172.20.0.0/24"``).

    Returns:
        True when the network is one of / within the known internal subnets.
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    if not isinstance(network, ipaddress.IPv4Network):
        return False
    return any(network.subnet_of(internal) for internal in _INTERNAL_NETWORKS)


def all_internal(addresses: Iterable[str]) -> bool:
    """Return True if every address in ``addresses`` is internal.

    Convenience for callers that hold a batch of destinations. An empty iterable
    returns True (vacuously — a command with no destinations is purely local).
    """
    return all(is_internal_ip(address) for address in addresses)
