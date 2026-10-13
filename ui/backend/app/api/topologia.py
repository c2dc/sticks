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
``construir_hosts``/``construir_redes``/``montar_mermaid`` builders) lives in
:mod:`app.services.environment.topologia_builder` so the Stage-4 Provisioning
service (:class:`~app.services.environment.CuradoriaProvisionadorDeAmbiente`) can
reuse it without duplicating the lab map. This module imports those builders and
keeps the ``GET /api/casos/{caso}/topologia`` contract identical.

_Security note:_ this is presentation only. It never starts anything and never
leaves the host; it just describes the already-contained environment.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status

from app.services.case_service import CaseService
from app.services.environment.topologia_builder import (
    HostView,
    NetworkView,
    TopologyResponse,
    compose_path,
    construir_hosts,
    construir_redes,
    montar_mermaid,
)

router = APIRouter(prefix="/api", tags=["topologia"])

# Re-export the typed models at their historical import path so existing callers
# (``from app.api.topologia import TopologyResponse``) keep working unchanged.
__all__ = ["router", "HostView", "NetworkView", "TopologyResponse"]


@router.get(
    "/casos/{caso}/topologia",
    response_model=TopologyResponse,
    summary="Topologia da rede-alvo (contida) do caso + diagrama Mermaid",
)
def obter_topologia(
    caso: str = Path(..., description="Slug do caso curado"),
) -> TopologyResponse:
    """Return the contained lab topology + a Mermaid diagram for the case.

    Read-only and side-effect free: built from the containment static map and
    the compose file, with no Docker or network calls. Unknown slug -> 404.
    """
    case_service = CaseService()
    if caso not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {caso!r}.",
        )
    nome = case_service.list_cases()[caso]

    hosts = construir_hosts()
    redes = construir_redes()
    return TopologyResponse(
        caso_id=caso,
        nome=nome,
        isolado=True,
        hosts=hosts,
        redes=redes,
        mermaid=montar_mermaid(hosts, redes),
        compose_file=compose_path(),
    )
