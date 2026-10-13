"""ContainmentValidator — security core of the Pipeline_UI backend.

The ``containment`` package implements the ``ContainmentValidator`` component
described in the design (see "Components and Interfaces" -> ``ContainmentValidator``
and "Correctness Properties" -> Properties 1 and 2). It is split into focused
modules so each concern can be developed and tested independently:

- ``command_parser`` (task 4.1): extracts destination addresses (IPs, URLs,
  hosts) from the concrete shell commands of the curated cases. It performs
  **only extraction** — it does not classify a destination as internal/external.
- ``subnets`` (task 4.2 constants source): single source of truth for the three
  internal lab subnets (172.20/21/22.0.0/24) and helpers to classify an IP or a
  network CIDR as internal.
- ``isolation`` (task 4.4): read-only container isolation verification via an
  injectable Docker inspector interface. A container is isolated iff attached
  exclusively to ``internal: true`` networks, with no external bridge and no
  external DNS. Produces a typed ``IsolationResult`` with per-container verdicts
  and an overall pass/fail (Req. 6.3, 6.4).

- ``subnet_validator`` (task 4.2): classifies each parsed destination as
  internal/external and produces the typed ``AbilityContainmentReport`` verdict
  the OperationRunner uses to gate execution.
- ``preview`` (task 4.6): produces the complete list of concrete commands with
  the resolved target container of each, flagging any external destination, for
  the confirmation modal (Req. 6.5). It presents; it does not enforce.

_Requisitos: 6.1, 6.2, 6.3, 6.4, 6.5_
"""

from __future__ import annotations

from app.services.containment.command_parser import (
    CommandDestinations,
    Destination,
    DestinationKind,
    extract_destinations,
    parse_command,
)
from app.services.containment.isolation import (
    ContainerInspector,
    ContainerIsolationVerdict,
    ContainerNetworkInfo,
    ContainerNotFound,
    DockerSdkInspector,
    IsolationResult,
    NetworkInfo,
    NetworkNotFound,
    inspect_isolation,
    verify_isolation,
)
from app.services.containment.preview import (
    AbilityPreview,
    CommandPreview,
    DEFAULT_LOCAL_TARGET,
    EmulationPreview,
    PreviewDestination,
    build_ability_preview,
    preview_abilities,
    preview_ability,
    preview_command_destinations,
    preview_commands,
)
from app.services.containment.subnets import (
    INTERNAL_NETWORK_SUBNETS,
    INTERNAL_SUBNET_CIDRS,
    STATIC_IP_CONTAINERS,
    all_internal,
    container_for_ip,
    is_internal_ip,
    is_internal_subnet,
)

__all__ = [
    # command_parser (task 4.1)
    "CommandDestinations",
    "Destination",
    "DestinationKind",
    "extract_destinations",
    "parse_command",
    # subnets (task 4.2 constants source)
    "INTERNAL_NETWORK_SUBNETS",
    "INTERNAL_SUBNET_CIDRS",
    "STATIC_IP_CONTAINERS",
    "all_internal",
    "container_for_ip",
    "is_internal_ip",
    "is_internal_subnet",
    # preview (task 4.6)
    "AbilityPreview",
    "CommandPreview",
    "DEFAULT_LOCAL_TARGET",
    "EmulationPreview",
    "PreviewDestination",
    "build_ability_preview",
    "preview_abilities",
    "preview_ability",
    "preview_command_destinations",
    "preview_commands",
    # isolation (task 4.4)
    "ContainerInspector",
    "ContainerIsolationVerdict",
    "ContainerNetworkInfo",
    "ContainerNotFound",
    "DockerSdkInspector",
    "IsolationResult",
    "NetworkInfo",
    "NetworkNotFound",
    "inspect_isolation",
    "verify_isolation",
]
