"""REST endpoint for the operationalization listing (Req. 6.2, 6.3, 6.4, 6.6).

``GET /api/stix/bases/{version}/campaigns/{campaign_key}/operationalization``
returns, for each technique of the campaign, the exact Atomic Red Team commands
from ``technique_commands`` plus the ingestion state of the ``atomic`` source.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy.orm import Session

from app.api.stix import _read_persisted
from app.db.session import get_db
from app.services.operationalization_listing import (
    OperationalizationListing,
    OperationalizationListingService,
)
from app.services.stix.stix_package_read_service import CampaignNotFoundError

router = APIRouter(prefix="/api", tags=["operationalization"])


@router.get(
    "/stix/bases/{version}/campaigns/{campaign_key}/operationalization",
    response_model=OperationalizationListing,
    summary="Campaign techniques with their exact Atomic Red Team commands",
)
def get_operationalization(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    campaign_key: str = Path(..., description="Campaign key (curated slug or STIX id)"),
    db: Session = Depends(get_db),
) -> OperationalizationListing:
    """Return the operationalization listing, read only from the database."""
    try:
        return _read_persisted(
            db,
            version,
            lambda: OperationalizationListingService(db).list_for_campaign(version, campaign_key),
        )
    except CampaignNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"reason": "campaign_not_found", "message": str(exc)},
        ) from exc
