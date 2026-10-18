"""Read-only lab network topology for the Stage-4 "environment provisioning" view.

The researcher wants to SEE, before any emulation, the contained lab network a
campaign will be run against: which hosts are attackers, which are targets,
their fixed IPs, and the internal-only subnets that connect them. This endpoint
builds that picture purely from the containment source of truth
(``app.services.containment.subnets``) — the same static IP/subnet map the
security pre-flight uses — plus the lab compose file for display. It performs NO
Docker calls and NO network access, so it is safe and side-effect free.

It also renders a Mermaid ``flowchart`` string so the frontend can draw the
topology diagram without hand-maintaining it: attacker nodes, target nodes and
the internal networks as subgraphs, with each host labelled by its fixed IP.

The pure topology-building logic (the static lab map, the typed models and the
``build_hosts``/``build_networks``/``build_mermaid`` builders) lives in
:mod:`app.services.environment.topology_builder` so the Stage-4 Provisioning
service (:class:`~app.services.environment.CurationEnvironmentProvisioner`) can
reuse it without duplicating the lab map. This module imports those builders and
keeps the ``GET /api/campaigns/{campaign}/topology`` contract identical.

_Security note:_ this is presentation only. It never starts anything and never
leaves the host; it just describes the already-contained environment.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status

from app.services.campaign_service import CaseService
from app.services.environment.topology_builder import (
    HostView,
    NetworkView,
    TopologyResponse,
    build_hosts,
    build_mermaid,
    build_networks,
    compose_path,
)

router = APIRouter(prefix="/api", tags=["topology"])

# Re-export the typed models so callers can import them from this module.
__all__ = ["router", "HostView", "NetworkView", "TopologyResponse"]


@router.get(
    "/campaigns/{campaign}/topology",
    response_model=TopologyResponse,
    summary="Target-network (contained) topology of the campaign + Mermaid diagram",
)
def get_topology(
    campaign: str = Path(..., description="Curated campaign slug"),
) -> TopologyResponse:
    """Return the contained lab topology + a Mermaid diagram for the case.

    Read-only and side-effect free: built from the containment static map and
    the compose file, with no Docker or network calls. Unknown slug -> 404.
    """
    case_service = CaseService()
    if campaign not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {campaign!r}.",
        )
    name = case_service.list_cases()[campaign]

    hosts = build_hosts()
    networks = build_networks()
    return TopologyResponse(
        campaign_id=campaign,
        name=name,
        isolated=True,
        hosts=hosts,
        networks=networks,
        mermaid=build_mermaid(hosts, networks),
        compose_file=compose_path(),
    )
