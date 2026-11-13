"""REST endpoints for the STIX intelligence flow.

Wires the ``/api/stix/*`` contracts. Read handlers are kept **thin** and read
**exclusively from the database** via
:class:`~app.services.stix.stix_package_read_service.StixPackageReadService`.
Nothing is read from or written to disk: the STIX bundle is downloaded in memory
and persisted by the ``stix_packages`` ingestion source of the
:class:`~app.services.ingestion.orchestrator.IngestionOrchestrator`.

Endpoints implemented here:

* ``GET  /api/stix/bases`` — the known ATT&CK Enterprise bases (the maintained
  version index plus any version persisted in the database), newest→oldest, each
  flagged ``downloaded``/``selectable`` by its presence in the database.
  Side-effect free: no network egress (Req. 2.1).
* ``GET  /api/stix/bases/{version}/overview`` — the package overview (per-type
  counts and totals) read from the database. A non-persisted version maps to the
  ``{"detail": {"motivo": "bundle_indisponivel", ...}}`` 422 contract.
* ``GET  /api/stix/bases/{version}/campaigns`` — the campaign summaries read from
  the database. Same ``bundle_indisponivel`` 422 contract.
* ``GET  /api/stix/bases/{version}/campaigns/{campaign_key}/overview`` — the
  overview of a single campaign. A missing campaign maps to
  ``{"detail": {"motivo": "campanha_nao_encontrada", ...}}`` 404.
* ``GET  /api/stix/bases/{version}/analytics`` — the package analytics dashboard.
* ``POST /api/stix/bases/{version}/download`` — asks the orchestrator to ingest a
  version that is not in the database yet. The ingestion runs in the background
  (``202``); the client follows ``GET /api/stix/bases`` (``downloaded``) and
  ``GET /api/ingestion/status``. A version already persisted answers ``200``
  without any work. An unknown version maps to
  ``{"detail": {"motivo": "falha_download", ...}}`` 422.

_Requirements: 2.1, 3.1, 4.1, 4.3, 5.1, 5.2, 5.3, 5.4, 7.1_
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import IngestSource
from app.services.stix.stix_package_read_service import (
    CampaignNotFoundError,
    StixCampaignOverview,
    StixCampaignSummary,
    StixPackageAnalytics,
    StixPackageNotPersistedError,
    StixPackageOverview,
    StixPackageReadService,
)
from app.services.stix.stix_persistence_service import StixPackagePersistedResult
from app.services.stix.stix_service import (
    BaseStixInfo,
    _semantic_version_key,
    list_remote_versions,
    version_is_known,
)

router = APIRouter(prefix="/api", tags=["stix"])

# Type of the value a read callable returns (overview / campaign list / campaign
# overview), threaded through the shared read helper so it stays type-safe.
ReadResult = TypeVar("ReadResult")


# ---------------------------------------------------------------------------
# Error contracts
# ---------------------------------------------------------------------------


def _bundle_unavailable(mensagem: str) -> HTTPException:
    """Build the ``bundle_indisponivel`` 422 contract (Req. 8.3, 9.4).

    Used when a read is requested for a version that is not persisted in the
    database (it may still be ingested through the download route).
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"motivo": "bundle_indisponivel", "mensagem": mensagem},
    )


def _read_persisted(
    db: Session,
    version: str,
    read: Callable[[], ReadResult],
) -> ReadResult:
    """Run a DB-only read, mapping a non-persisted version to ``bundle_indisponivel``.

    There is no fallback to a file: a version is available only once the
    ``stix_packages`` ingestion source persisted it in the database.
    """
    try:
        return read()
    except StixPackageNotPersistedError as not_persisted:
        raise _bundle_unavailable(str(not_persisted)) from not_persisted


# ---------------------------------------------------------------------------
# Response envelopes
# ---------------------------------------------------------------------------


class BaseListItem(BaseStixInfo):
    """A base of the listing, with the ``completed`` verdict of the old stage 1.

    ``completed`` is true exactly when the version is persisted in the database,
    which is what ``downloaded`` already reflects for this listing.
    """

    completed: bool = False


class BasesResponse(BaseModel):
    """``GET /api/stix/bases`` payload — the bases listing (Req. 2.1, 5.5).

    Each base carries ``downloaded``/``selectable``/``completed`` (presence in the
    database). There is no per-version ``curated`` field: the curated marker lives
    on each campaign (``GET /stix/bases/{version}/campaigns``).
    """

    bases: list[BaseListItem] = Field(default_factory=list)


class CampaignListItem(StixCampaignSummary):
    """A campaign row of ``GET /api/stix/bases/{version}/campaigns``.

    Extends the database-backed summary with the per-campaign container
    emulability marking: ``container_emulable`` is ``None`` when the marking was
    not derived yet (the campaign is still listed) and ``blocking_techniques``
    lists the technique ids that block container emulation.
    """

    container_emulable: bool | None = None
    blocking_techniques: list[str] = Field(default_factory=list)


def _annotate_container_emulability(
    db: Session, summaries: list[StixCampaignSummary]
) -> list[CampaignListItem]:
    """Attach the already-derived emulability marking to each campaign (read-only)."""
    from app.services.container_emulability_service import (
        ContainerEmulabilityService,
    )

    markings = ContainerEmulabilityService(db).read_many(
        [summary.campaign_key for summary in summaries]
    )
    items: list[CampaignListItem] = []
    for summary in summaries:
        item = CampaignListItem(**summary.model_dump())
        marking = markings.get(summary.campaign_key)
        if marking is not None:
            item.container_emulable = marking.container_emulable
            item.blocking_techniques = list(marking.blocking_techniques)
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stix/bases",
    response_model=BasesResponse,
    summary="List the ATT&CK bases (newest → oldest)",
)
def list_bases(db: Session = Depends(get_db)) -> BasesResponse:
    """List the ATT&CK Enterprise bases, newest→oldest (Req. 2.1, 5.5).

    The listing is the upstream MITRE version index (cached ~1 h, short timeout;
    ``KNOWN_VERSIONS`` as the offline fallback) plus every version persisted in
    the database (so a persisted version is never hidden).
    ``downloaded`` / ``selectable`` / ``completed`` come from the **database**
    (``is_persisted`` — the single source of truth), never from disk.
    READ-ONLY; the only network egress is the cached index lookup.
    """
    read_service = StixPackageReadService(db)
    persisted = set(read_service.list_persisted_versions())
    versions = sorted(
        set(list_remote_versions()) | persisted,
        key=_semantic_version_key,
        reverse=True,
    )
    items = [
        BaseListItem(
            version=version,
            name=f"Enterprise ATT&CK v{version}",
            downloaded=version in persisted,
            selectable=version in persisted,
            completed=version in persisted,
        )
        for version in versions
    ]
    return BasesResponse(bases=items)


@router.get(
    "/stix/bases/{version}/overview",
    response_model=StixPackageOverview,
    summary="Package overview (per-type counts and totals), read from the database",
)
def get_package_overview(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    db: Session = Depends(get_db),
) -> StixPackageOverview:
    """Return the package overview of the base, reading only from the database.

    A version that is not persisted maps to ``bundle_indisponivel`` 422.
    """
    return _read_persisted(
        db, version, lambda: StixPackageReadService(db).package_overview(version)
    )


@router.get(
    "/stix/bases/{version}/campaigns",
    response_model=list[CampaignListItem],
    summary="List the base's campaigns (summaries), read from the database",
)
def list_campaigns(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    db: Session = Depends(get_db),
) -> list[CampaignListItem]:
    """Return the campaign summaries of the base, reading only from the database.

    A version that is not persisted maps to ``bundle_indisponivel`` 422.
    """
    summaries = _read_persisted(
        db, version, lambda: StixPackageReadService(db).list_campaigns(version)
    )
    return _annotate_container_emulability(db, summaries)


@router.get(
    "/stix/bases/{version}/campaigns/{campaign_key}/overview",
    response_model=StixCampaignOverview,
    summary="Overview of a single campaign (identity, aliases, techniques, actors)",
)
def get_campaign_overview(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    campaign_key: str = Path(..., description="Campaign key (curated slug or STIX id)"),
    db: Session = Depends(get_db),
) -> StixCampaignOverview:
    """Return the overview of one campaign of the base, reading only from the database.

    A version that is not persisted maps to ``bundle_indisponivel`` 422; a
    ``campaign_key`` with no matching campaign maps to ``campanha_nao_encontrada``
    404 (Req. 9.5).
    """
    try:
        return _read_persisted(
            db,
            version,
            lambda: StixPackageReadService(db).campaign_overview(version, campaign_key),
        )
    except CampaignNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"motivo": "campanha_nao_encontrada", "mensagem": str(exc)},
        ) from exc


@router.get(
    "/stix/bases/{version}/analytics",
    response_model=StixPackageAnalytics,
    summary="Package analytics (rankings and distributions), read from the database",
)
def get_package_analytics(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    db: Session = Depends(get_db),
) -> StixPackageAnalytics:
    """Return the package analytics dashboard of the base, read only from the database.

    A version that is not persisted maps to ``bundle_indisponivel`` 422.
    """
    return _read_persisted(
        db, version, lambda: StixPackageReadService(db).package_analytics(version)
    )


@router.post(
    "/stix/bases/{version}/download",
    response_model=StixPackagePersistedResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest an ATT&CK Enterprise base into the database (background)",
)
def download_base(
    request: Request,
    response: Response,
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    db: Session = Depends(get_db),
) -> StixPackagePersistedResult:
    """Ask the ``stix_packages`` ingestion source to ingest ``version`` (Req. 7.1).

    * Already persisted: ``200`` with ``persisted=true`` and the stored totals; no
      work is done (idempotent).
    * Unknown version (not in the maintained index): ``422`` ``falha_download``,
      before any network access.
    * Otherwise the version is registered on the runner and the source is started
      in the background (``202``, ``persisted=false``). When the source is already
      running, the running ingestion picks the request up. The bundle is
      downloaded in memory and persisted directly in the database; the client
      follows ``GET /api/stix/bases`` and ``GET /api/ingestion/status``.
    """
    read_service = StixPackageReadService(db)
    if read_service.is_persisted(version):
        overview = read_service.package_overview(version)
        response.status_code = status.HTTP_200_OK
        return StixPackagePersistedResult(
            version=version,
            downloaded=False,
            persisted=True,
            counts_total=sum(item.count for item in overview.counts),
            campaigns_total=overview.campaigns_total,
            actors_total=overview.actors_total,
        )

    if not version_is_known(version):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "motivo": "falha_download",
                "mensagem": f"Versão desconhecida '{version}': download recusado.",
            },
        )

    from app.api.ingestion import get_orchestrator

    orchestrator = get_orchestrator(request)
    runner = orchestrator.runner(IngestSource.STIX_PACKAGES)
    runner.request_version(version)  # type: ignore[attr-defined] -- the runner is typed as the generic IngestRunner; request_version exists only on the STIX runner
    # ``ALREADY_RUNNING`` is fine: the running ingestion re-reads the requests
    # before it finishes.
    orchestrator.start(IngestSource.STIX_PACKAGES)
    return StixPackagePersistedResult(
        version=version,
        downloaded=False,
        persisted=False,
        counts_total=0,
        campaigns_total=0,
        actors_total=0,
    )
