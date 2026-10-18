"""REST endpoints for the STIX intelligence flow (task 5.1).

Wires the ``/api/stix/*`` contracts from the design's "REST endpoints" section
to :class:`~app.services.stix.StixService`.
Handlers are kept **thin**: each one constructs a ``StixService()`` and
delegates, returning the service's already-typed Pydantic results (wrapped in a
small response model where the design envelopes the list under a named key).

Endpoints implemented here (Req. 2.1, 2.5, 2.7, 3.1, 3.2, 3.5, 4.2, 4.3, 4.5):

* ``GET  /api/stix/bases`` — the known ATT&CK Enterprise bases, newest→oldest,
  each flagged ``baixada``/``selecionavel`` by live disk presence. Side-effect
  free: no network egress (Req. 2.1).
* ``GET  /api/stix/bases/{version}/dashboard`` — per-type intelligence counts for
  a selected base. A bundle read/parse failure maps to the
  ``{"detail": {"motivo": "bundle_invalido", ...}}`` 422 contract (Req. 3.1,
  3.2, 3.5). Side-effect free.
* ``GET  /api/stix/bases/{version}/campaigns`` — the ``campaign`` objects with the
  curated-case flag, enveloped as ``{"version", "campaigns"}`` (Req. 4.2, 4.5).
  Side-effect free; same 422 contract on an invalid bundle.
* ``GET  /api/stix/bases/{version}/adversaries`` — the ``intrusion-set`` actors with
  their attributed campaigns, enveloped as ``{"version", "adversaries"}`` (Req. 4.3).
  Side-effect free; same 422 contract on an invalid bundle.
* ``POST /api/stix/bases/{version}/download`` — the ONLY route with a side effect:
  the single network egress, triggered only by this explicit POST. A download
  failure maps to ``{"detail": {"motivo": "falha_download", ...}}`` — 422 when
  the version is unknown (rejected before touching disk/network), 502 for a
  network/content/write failure (Req. 2.5, 2.7).

This module does NOT register itself with the root router — that is task 5.2.

_Requirements: 2.1, 2.5, 2.7, 3.1, 3.2, 3.5, 4.2, 4.3, 4.5_
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.services.stix.estagio1_service import BaseStixListada, Stage1Service
from app.services.stix.stix_service import (
    AdversaryStix,
    BaseStixInfo,
    CampaignStix,
    DashboardStix,
    StixDownloadError,
    StixFileError,
    StixService,
)

router = APIRouter(prefix="/api", tags=["stix"])


# ---------------------------------------------------------------------------
# Response envelopes (small wrappers around the service list results so the
# JSON matches the design's contracts: a named key per collection).
# ---------------------------------------------------------------------------


class BasesResponse(BaseModel):
    """``GET /api/stix/bases`` payload — the known bases listing (Req. 2.1, 5.5).

    Each base carries the ``baixada``/``selecionavel`` flags (live disk presence)
    plus the six-stage ``curada`` flag of the version (``True`` only for the
    curated version 18 — Req. 5.5, 5.6), projected via
    :class:`~app.services.stix.estagio1_service.Stage1Service`. ``curada`` is an
    additive field: existing consumers that read only ``version``/``baixada``/
    ``selecionavel``/``arquivo`` are unaffected.
    """

    bases: list[BaseStixListada] = Field(default_factory=list)


class CampaignsResponse(BaseModel):
    """``GET /api/stix/bases/{version}/campaigns`` payload (Req. 4.2, 4.5)."""

    version: str
    campaigns: list[CampaignStix] = Field(default_factory=list)


class AdversariesResponse(BaseModel):
    """``GET /api/stix/bases/{version}/adversaries`` payload (Req. 4.3)."""

    version: str
    adversaries: list[AdversaryStix] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stix/bases",
    response_model=BasesResponse,
    summary="List the known MITRE ATT&CK bases (newest → oldest)",
)
def list_bases() -> BasesResponse:
    """List the known ATT&CK Enterprise bases, newest→oldest (Req. 2.1, 5.5).

    Delegates to :meth:`Stage1Service.list_bases` (which reuses
    ``StixService.list_bases``), enumerating the backend-maintained version
    index, flagging each base ``baixada`` / ``selecionavel`` from live disk
    presence and projecting the six-stage ``curada`` flag per version (``True``
    only for the curated version 18 — Req. 5.5, 5.6). READ-ONLY and side-effect
    free: no network egress (the only egress is the explicit download POST).

    The service is built from the module-level ``StixService`` symbol so test
    monkeypatches that pin ``StixService`` to an isolated ``data_dir`` keep
    affecting this route.
    """
    return BasesResponse(bases=Stage1Service(StixService()).list_bases())


@router.get(
    "/stix/bases/{version}/dashboard",
    response_model=DashboardStix,
    summary="Intelligence dashboard (per-type counts) for the selected base",
)
def get_dashboard(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
) -> DashboardStix:
    """Return the per-type intelligence counts for the selected base (Req. 3.1, 3.2).

    Delegates to :meth:`StixService.dashboard`. READ-ONLY and side-effect free.
    A bundle read/parse failure (missing / malformed / invalid structure) is a
    typed :class:`StixFileError`, mapped to the ``bundle_invalido`` 422 contract
    so no partial set of counts is ever returned as if valid (Req. 3.5).
    """
    try:
        return StixService().dashboard(version)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc


@router.get(
    "/stix/bases/{version}/campaigns",
    response_model=CampaignsResponse,
    summary="List the base's campaigns, with the curated-case flag",
)
def list_campaigns(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
) -> CampaignsResponse:
    """Return the ``campaign`` objects of the base with the curated flag (Req. 4.2, 4.5).

    Delegates to :meth:`StixService.list_campaigns`. READ-ONLY and side-effect
    free. An invalid bundle surfaces the ``bundle_invalido`` 422 contract.
    """
    try:
        campaigns = StixService().list_campaigns(version)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc
    return CampaignsResponse(version=version, campaigns=campaigns)


@router.get(
    "/stix/bases/{version}/actors",
    response_model=AdversariesResponse,
    summary="List the adversaries (intrusion-sets) with their attributed campaigns",
)
def list_adversaries(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
) -> AdversariesResponse:
    """Return the ``intrusion-set`` actors with attributed campaigns (Req. 4.3).

    Delegates to :meth:`StixService.list_adversaries`. READ-ONLY and side-effect
    free. An invalid bundle surfaces the ``bundle_invalido`` 422 contract.
    """
    try:
        adversaries = StixService().list_adversaries(version)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc
    return AdversariesResponse(version=version, adversaries=adversaries)


@router.post(
    "/stix/bases/{version}/download",
    response_model=BaseStixInfo,
    summary="Download the ATT&CK Enterprise base (the only network egress)",
)
def download_base(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
) -> BaseStixInfo:
    """Download and persist the ``enterprise-attack-{version}`` bundle (Req. 2.5, 2.7).

    This is the ONLY route with a side effect: the single network egress,
    triggered exclusively by this explicit POST. Delegates to
    :meth:`Stage1Service.download_base`, which reuses the on-disk package
    without re-downloading when already present (Req. 5.3) and otherwise
    triggers the single network egress via ``StixService.download_base`` — which
    validates ``version`` against the known index *before* touching disk/network
    and writes atomically. On failure it raises a typed
    :class:`StixDownloadError`, mapped to the ``falha_download`` contract: HTTP
    422 when the version is unknown (rejected pre-flight), HTTP 502 for a
    network/content/write failure.
    """
    try:
        return Stage1Service().download_base(version)
    except StixDownloadError as exc:
        # Unknown version → 422 (rejected before any disk/network access);
        # any network/content/write failure → 502 (upstream/egress failure).
        from app.services.stix.stix_service import VERSOES_CONHECIDAS

        unknown = exc.version is not None and exc.version not in VERSOES_CONHECIDAS
        http_code = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if unknown
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=http_code,
            detail={"motivo": "falha_download", "mensagem": exc.detail},
        ) from exc
