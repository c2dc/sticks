"""REST routes for attack flows (replace the legacy ``/dag`` and ``/dags`` routes).

* ``GET /api/campaigns/{campaign}/flows`` — flows of a campaign (with provenance).
* ``GET /api/campaigns/{campaign}/flows/{flow_id}`` — graph, Mermaid, cycles,
  back edges and provenance of one campaign flow.
* ``GET /api/attack-flows/{flow_id}`` — a flow by id (e.g. a corpus flow shown
  as a reference in a provenance badge).

Read-only database access: no network, no AI.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.flow.flow_service import FlowDetail, FlowService, FlowSummary

router = APIRouter(prefix="/api", tags=["flows"])


class FlowListResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/flows`` payload."""

    campaign_id: str
    flows: list[FlowSummary] = Field(default_factory=list)


def _not_found(flow_id: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"reason": "flow_not_found", "message": f"Flow {flow_id} not found."},
    )


@router.get(
    "/campaigns/{campaign}/flows",
    response_model=FlowListResponse,
    summary="List the attack flows of a campaign",
)
def list_flows(
    campaign: str = Path(..., description="Campaign id"),
    db: Session = Depends(get_db),
) -> FlowListResponse:
    return FlowListResponse(
        campaign_id=campaign, flows=FlowService(db).list_flows(campaign)
    )


@router.get(
    "/campaigns/{campaign}/flows/{flow_id}",
    response_model=FlowDetail,
    summary="Graph, Mermaid, cycles and provenance of a campaign flow",
)
def get_flow(
    campaign: str = Path(..., description="Campaign id"),
    flow_id: int = Path(..., description="Id of the persisted flow"),
    db: Session = Depends(get_db),
) -> FlowDetail:
    detail = FlowService(db).get_flow(campaign, flow_id)
    if detail is None:
        raise _not_found(flow_id)
    return detail


@router.get(
    "/attack-flows/{flow_id}",
    response_model=FlowDetail,
    summary="An attack flow by id (corpus reference)",
)
def get_attack_flow(
    flow_id: int = Path(..., description="Id of the persisted flow"),
    db: Session = Depends(get_db),
) -> FlowDetail:
    detail = FlowService(db).get_corpus_flow(flow_id)
    if detail is None:
        raise _not_found(flow_id)
    return detail
