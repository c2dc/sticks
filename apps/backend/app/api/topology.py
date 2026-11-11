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

Database-only campaign resolution (Req. 1.1, 3.5, 4.1)
------------------------------------------------------
The campaign's DAG and Abilities — the content that confirms a campaign exists
and names it — are resolved **from the database** (the ``dags`` table via
:class:`DagService` and the ``Ability``/``campaigns`` rows keyed by
``campaign_id``). The request-time disk read through ``CaseService`` was removed
(sub-step 2.4): the runtime never touches ``packages/cli/data`` (Req. 4.1, 4.2),
and a campaign with no persisted DAG/Abilities is a 404. The static lab topology
itself is independent of a campaign's abilities/DAG, so the source change affects
only *how the campaign is resolved*, never the rendered topology for an
equivalent campaign.

_Security note:_ this is presentation only. It never starts anything and never
leaves the host; it just describes the already-contained environment.

_Requirements: 3.5_
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.domain import (
    Ability as AbilityModel,
    Case as CaseModel,
)
from app.services.dag.dag_service import DagService
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


def _resolve_campaign_name_from_db(campaign: str, db: Session) -> Optional[str]:
    """Resolve a campaign's display name from the DB when it has DAG + Abilities.

    Database-only resolution for the topology (Req. 1.1, 3.5, 4.1): a campaign is
    considered resolvable when its content is persisted — it has at least one
    ``Ability`` row and a ``dags`` row (DAG) keyed by ``campaign_id`` (persisted
    by the curated ingest of sub-step 2.1). The display name comes from the
    ``campaigns`` row when present, otherwise falls back to the slug.

    Returns ``None`` when the campaign's DAG/Abilities are not persisted; the
    caller maps that to a 404 (the runtime no longer reads
    ``packages/cli/data`` — Req. 4.1, 4.2). Pure read: no disk, no network.
    """
    has_ability = (
        db.execute(
            select(AbilityModel.ability_id)
            .where(AbilityModel.campaign_id == campaign)
            .limit(1)
        )
        .scalars()
        .first()
    )
    if has_ability is None:
        return None

    # The DAG is resolved from the ``dags`` table via DagService's DB-first path;
    # a missing persisted DAG means the campaign is not fully DB-resolvable yet.
    dag_view = DagService(session=db).get_dag(campaign)
    if dag_view.error is not None or not dag_view.nodes:
        return None

    row = (
        db.execute(select(CaseModel).where(CaseModel.id == campaign))
        .scalars()
        .first()
    )
    if row is not None and row.name:
        return row.name
    return campaign


@router.get(
    "/campaigns/{campaign}/topology",
    response_model=TopologyResponse,
    summary="Target-network (contained) topology of the campaign + Mermaid diagram",
)
def get_topology(
    campaign: str = Path(..., description="Curated campaign slug"),
    db: Session = Depends(get_db),
) -> TopologyResponse:
    """Return the contained lab topology + a Mermaid diagram for the case.

    Read-only and side-effect free: built from the containment static map and
    the compose file, with no Docker or network calls. The campaign's DAG +
    Abilities are resolved **from the database** (Req. 1.1, 3.5, 4.1); the
    request-time disk read through ``CaseService`` was removed (sub-step 2.4),
    so a campaign with no persisted DAG/Abilities is a 404 (Req. 4.1, 4.2).
    """
    name = _resolve_campaign_name_from_db(campaign, db)
    if name is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {campaign!r}.",
        )

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
