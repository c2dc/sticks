"""REST endpoints for the STIX intelligence flow.

Wires the ``/api/stix/*`` contracts from the design's "REST endpoints" section.
Read handlers are kept **thin** and read **exclusively from the database** via
:class:`~app.services.stix.stix_package_read_service.StixPackageReadService`;
the single write/egress route delegates to
:class:`~app.services.stix.stix_persistence_service.StixPersistenceService`.

Endpoints implemented here:

* ``GET  /api/stix/bases`` — the known ATT&CK Enterprise bases, newest→oldest,
  each flagged ``baixada``/``selecionavel`` by live disk presence. Side-effect
  free: no network egress (Req. 2.1).
* ``GET  /api/stix/bases/{version}/overview`` — the package overview (per-type
  counts and totals) read from the database, the view that replaces the legacy
  ``/dashboard`` route (Req. 3.1, 5.1). A non-persisted version maps to the
  ``{"detail": {"motivo": "bundle_indisponivel", ...}}`` 422 contract.
* ``GET  /api/stix/bases/{version}/campaigns`` — the campaign summaries read from
  the database (Req. 4.1, 4.3, 5.2). Same ``bundle_indisponivel`` 422 contract.
* ``GET  /api/stix/bases/{version}/campaigns/{campaign_key}/overview`` — the
  overview of a single campaign (identity, aliases, techniques, actors) read
  from the database (Req. 4.3, 5.2). A missing campaign maps to
  ``{"detail": {"motivo": "campanha_nao_encontrada", ...}}`` 404.
* ``GET  /api/stix/bases/{version}/analytics`` — the package analytics dashboard
  (actor/campaign rankings, curated vs uncurated totals and the technique-per-
  tactic distribution) read from the database (Req. 12.1). Shares the same
  on-demand backfill and ``bundle_indisponivel`` 422 contract as the other read
  routes (Req. 12.7).
* ``POST /api/stix/bases/{version}/download`` — the ONLY route with a side effect:
  the single network egress *and* the single DB write, triggered only by this
  explicit POST. Delegates to
  :meth:`StixPersistenceService.download_and_persist` and responds with a
  :class:`StixPackagePersistedResult` carrying ``downloaded=true`` and
  ``persisted=true`` on success. A download failure maps to
  ``{"detail": {"motivo": "falha_download", ...}}`` — 422 when the version is
  unknown (rejected before touching disk/network), 502 for a network/content/
  write failure (Req. 1.1, 1.5, 5.5).

_Requirements: 2.1, 3.1, 4.1, 4.3, 5.1, 5.2, 5.3, 5.4_
"""

from __future__ import annotations

from typing import Callable, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.stix.stage1_service import BaseStixListada, Stage1Service
from app.services.stix.stix_package_read_service import (
    CampaignNotFoundError,
    StixCampaignOverview,
    StixCampaignSummary,
    StixPackageAnalytics,
    StixPackageNotPersistedError,
    StixPackageOverview,
    StixPackageReadService,
)
from app.services.stix.stix_persistence_service import (
    StixPackagePersistedResult,
    StixPersistenceService,
)
from app.services.stix.stix_service import (
    StixDownloadError,
    StixFileError,
    StixService,
)

router = APIRouter(prefix="/api", tags=["stix"])

# Type of the value a read callable returns (overview / campaign list / campaign
# overview), threaded through the shared backfill helper so it stays type-safe.
ReadResult = TypeVar("ReadResult")


# ---------------------------------------------------------------------------
# Error contracts (design "REST endpoints" / Req. 9)
# ---------------------------------------------------------------------------


def _bundle_unavailable(mensagem: str) -> HTTPException:
    """Build the ``bundle_indisponivel`` 422 contract (Req. 8.3, 9.4).

    Used when a read is requested for a version that has neither a
    ``stix_packages`` row nor a valid on-disk bundle to backfill from.
    """
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"motivo": "bundle_indisponivel", "mensagem": mensagem},
    )


def _persistence_failure(mensagem: str) -> HTTPException:
    """Build the ``falha_persistencia`` 502 contract (Req. 9.3).

    Used when the DB write fails after the bundle is already available (on disk
    during a backfill, or freshly downloaded on the download route): the
    persistence service has rolled back, leaving the package unpersisted and the
    on-disk file intact.
    """
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"motivo": "falha_persistencia", "mensagem": mensagem},
    )


def _read_with_backfill(
    db: Session,
    version: str,
    read: Callable[[], ReadResult],
) -> ReadResult:
    """Run a DB-only read, backfilling from disk on demand, with error mapping.

    This is the shared core of the three read routes (Req. 8.1–8.3, 9.3, 9.4),
    keeping the routes thin and the error contract centralized:

    * Run ``read`` (a DB-only read closure). On success return its result.
    * If the version is not persisted (:class:`StixPackageNotPersistedError`),
      check whether the on-disk bundle exists via
      :meth:`StixService.base_path`. The ``StixService`` is built from the
      module-level symbol so test monkeypatches that pin an isolated
      ``data_dir`` keep affecting this path.

      - **File present** — trigger the on-demand backfill
        (:meth:`StixPersistenceService.persist_from_disk`, no network egress —
        Req. 8.2), then re-run ``read`` and return its result (Req. 8.1). The
        re-persistence makes :meth:`StixPackageReadService.is_persisted` true
        (Req. 8.3). If the on-disk bundle is invalid, ``persist_from_disk``
        surfaces a :class:`StixFileError` (there is no valid bundle to serve):
        map it to ``bundle_indisponivel`` 422 (Req. 9.4). If the DB write fails
        after the bundle is available, the service rolls back and re-raises a
        generic error: map it to ``falha_persistencia`` 502 (Req. 9.3).
      - **File absent** — neither a row nor a file exists, so map to
        ``bundle_indisponivel`` 422 (Req. 9.4).
    """
    try:
        return read()
    except StixPackageNotPersistedError as not_persisted:
        stix_service = StixService()
        if not stix_service.base_path(version).exists():
            # Neither a persisted row nor an on-disk bundle (Req. 9.4).
            raise _bundle_unavailable(str(not_persisted)) from not_persisted

        # The bundle is on disk but not persisted: backfill it without any
        # network egress (Req. 8.2), then serve the requested view (Req. 8.1).
        try:
            StixPersistenceService(db, stix_service).persist_from_disk(version)
        except StixFileError as invalid_bundle:
            # The on-disk bundle is unreadable/invalid — there is no valid bundle
            # to serve (Req. 9.4). No DB row was written.
            raise _bundle_unavailable(invalid_bundle.detail) from invalid_bundle
        except Exception as write_error:
            # The DB write failed after the bundle was available; the service has
            # rolled back, leaving the package unpersisted (Req. 9.3).
            raise _persistence_failure(str(write_error)) from write_error

        # Re-run the read now that the version is persisted (Req. 8.1, 8.3).
        return read()


# ---------------------------------------------------------------------------
# Response envelopes (small wrappers around the service list results so the
# JSON matches the design's contracts: a named key per collection).
# ---------------------------------------------------------------------------


class BasesResponse(BaseModel):
    """``GET /api/stix/bases`` payload — the known bases listing (Req. 2.1, 5.5).

    Each base carries the ``baixada``/``selecionavel`` flags (live disk presence)
    plus the six-stage ``curada`` flag of the version (``True`` only for the
    curated version 18 — Req. 5.5, 5.6), projected via
    :class:`~app.services.stix.stage1_service.Stage1Service`. ``curada`` is an
    additive field: existing consumers that read only ``version``/``baixada``/
    ``selecionavel``/``arquivo`` are unaffected.
    """

    bases: list[BaseStixListada] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stix/bases",
    response_model=BasesResponse,
    summary="List the known MITRE ATT&CK bases (newest → oldest)",
)
def list_bases(db: Session = Depends(get_db)) -> BasesResponse:
    """List the known ATT&CK Enterprise bases, newest→oldest (Req. 2.1, 5.5).

    Delegates to :meth:`Stage1Service.list_bases` (which reuses
    ``StixService.list_bases``), enumerating the backend-maintained version
    index, flagging each base ``baixada`` / ``selecionavel`` from the
    **database** persistence state (``is_persisted`` — the single source of
    truth, Req. 3.1, 3.2, 3.3) and projecting the six-stage ``curada`` flag per
    version (``True`` only for the curated version 18 — Req. 5.5, 5.6).
    READ-ONLY and side-effect free: no network egress (the only egress is the
    explicit download POST).

    The ``Session`` is injected via ``Depends(get_db)`` (the same pattern used
    by the other routes) and threaded into
    :class:`StixPackageReadService` so the state-deriving layer can query the
    database. The ``StixService`` is built from the module-level ``StixService``
    symbol so test monkeypatches that pin an isolated ``data_dir`` keep
    affecting this route.
    """
    return BasesResponse(
        bases=Stage1Service(
            StixService(), StixPackageReadService(db)
        ).list_bases()
    )


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

    This is the dashboard / package view that replaces the legacy
    ``/dashboard`` endpoint (Req. 3.1, 5.1): it delegates to
    :meth:`StixPackageReadService.package_overview`, which reads exclusively from
    the persisted STIX tables (never reopening the on-disk bundle).

    When the version is not yet persisted but its bundle is on disk, the shared
    :func:`_read_with_backfill` helper triggers the on-demand backfill
    (``persist_from_disk``, no network egress — Req. 8.1, 8.2) and then serves
    the overview; when neither a row nor a file exists it maps to
    ``bundle_indisponivel`` 422 (Req. 9.4), and a DB write failure during the
    backfill maps to ``falha_persistencia`` 502 (Req. 9.3).
    """
    return _read_with_backfill(
        db, version, lambda: StixPackageReadService(db).package_overview(version)
    )


@router.get(
    "/stix/bases/{version}/campaigns",
    response_model=list[StixCampaignSummary],
    summary="List the base's campaigns (summaries), read from the database",
)
def list_campaigns(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    db: Session = Depends(get_db),
) -> list[StixCampaignSummary]:
    """Return the campaign summaries of the base, reading only from the database.

    The path stays the same but now reads **exclusively from the database**
    (no file reprocessing) and serves the ``StixCampaignSummary`` list the
    clickable UI consumes (Req. 4.1, 4.3, 5.2). Delegates to
    :meth:`StixPackageReadService.list_campaigns`.

    When the version is not yet persisted but its bundle is on disk, the shared
    :func:`_read_with_backfill` helper triggers the on-demand backfill
    (``persist_from_disk``, no network egress — Req. 8.1, 8.2) and then serves
    the campaign list; when neither a row nor a file exists it maps to
    ``bundle_indisponivel`` 422 (Req. 9.4), and a DB write failure during the
    backfill maps to ``falha_persistencia`` 502 (Req. 9.3).
    """
    return _read_with_backfill(
        db, version, lambda: StixPackageReadService(db).list_campaigns(version)
    )


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

    Delegates to :meth:`StixPackageReadService.campaign_overview`, which reads
    exclusively from the persisted STIX tables and scopes the techniques and
    actors to that campaign in that package only (Req. 4.3, 5.2).

    When the version is not yet persisted but its bundle is on disk, the shared
    :func:`_read_with_backfill` helper triggers the on-demand backfill
    (``persist_from_disk``, no network egress — Req. 8.1, 8.2) and then serves
    the campaign overview; when neither a row nor a file exists it maps to
    ``bundle_indisponivel`` 422 (Req. 9.4), and a DB write failure during the
    backfill maps to ``falha_persistencia`` 502 (Req. 9.3). A ``campaign_key``
    with no matching campaign in the persisted package maps to
    ``campanha_nao_encontrada`` 404 (Req. 9.5) — this is raised by the read
    itself (after the backfill), so it propagates past the backfill helper and
    is mapped here.
    """
    try:
        return _read_with_backfill(
            db,
            version,
            lambda: StixPackageReadService(db).campaign_overview(
                version, campaign_key
            ),
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

    Delegates to :meth:`StixPackageReadService.package_analytics`, which reads
    exclusively from the persisted STIX tables (no bundle reprocessing, no
    network egress) and aggregates, scoped to the package: the top actors by
    attributed campaigns, the top campaigns by number of techniques, the curated
    vs uncurated campaign totals and the technique-per-tactic distribution
    (Req. 12.1).

    When the version is not yet persisted but its bundle is on disk, the shared
    :func:`_read_with_backfill` helper triggers the on-demand backfill
    (``persist_from_disk``, no network egress — Req. 12.7) and then serves the
    analytics; when neither a row nor a file exists it maps to
    ``bundle_indisponivel`` 422 (Req. 12.7), and a DB write failure during the
    backfill maps to ``falha_persistencia`` 502 (Req. 9.3).
    """
    return _read_with_backfill(
        db, version, lambda: StixPackageReadService(db).package_analytics(version)
    )


@router.post(
    "/stix/bases/{version}/download",
    response_model=StixPackagePersistedResult,
    summary="Download and persist the ATT&CK Enterprise base (the only network egress)",
)
def download_base(
    version: str = Path(..., description="ATT&CK Enterprise base version (e.g. 19.1)"),
    db: Session = Depends(get_db),
) -> StixPackagePersistedResult:
    """Download and persist the ``enterprise-attack-{version}`` bundle (Req. 1.1, 1.5, 5.5).

    This is the ONLY route with both a network egress *and* a write: the single
    download of the bundle followed by the transactional persistence of its
    derived content. Delegates to
    :meth:`StixPersistenceService.download_and_persist`, which reuses the on-disk
    package without re-downloading when already present and otherwise triggers
    the single network egress via ``StixService.download_base`` — validating
    ``version`` against the known index *before* touching disk/network and
    writing atomically — then persists the derived content in a single
    transaction.

    On success it responds with the typed
    :class:`StixPackagePersistedResult`, carrying ``downloaded=True`` and
    ``persisted=True``. On a download failure it raises a typed
    :class:`StixDownloadError`, mapped to the ``falha_download`` contract: HTTP
    422 when the version is unknown (rejected pre-flight), HTTP 502 for a
    network/content/write failure, without writing anything to the DB (Req. 9.1).
    A DB write failure *after* a successful download is a different error — the
    persistence service has already rolled back (Req. 9.2) — and is caught
    distinctly from :class:`StixDownloadError` and mapped to
    ``falha_persistencia`` 502 (Req. 9.3).

    The ``StixService`` is built from the module-level ``StixService`` symbol so
    test monkeypatches that pin an isolated ``data_dir`` keep affecting this
    route.
    """
    try:
        # After the relational commit, index the version's campaigns into the
        # Vector_Store so a persisted Stix_Package_Version is a Pipeline_Input in
        # both stores (Req. 10.1, 10.3). The indexer is best-effort: a
        # Vector_Store/embedder outage never fails the committed download
        # (Req. 10.6). The Experience_Memory is built over the same request
        # session (matching the threat-intel ingest wiring).
        from app.services.ai.memory import ExperienceMemory
        from app.services.stix.stix_vector_index_service import (
            StixVectorIndexService,
        )

        indexer = StixVectorIndexService(db, memory=ExperienceMemory(db))
        return StixPersistenceService(
            db, StixService(), vector_indexer=indexer
        ).download_and_persist(version)
    except StixDownloadError as exc:
        # Download failure (Req. 9.1): nothing was written to the DB.
        # Unknown version → 422 (rejected before any disk/network access);
        # any network/content/write failure → 502 (upstream/egress failure).
        from app.services.stix.stix_service import KNOWN_VERSIONS

        unknown = exc.version is not None and exc.version not in KNOWN_VERSIONS
        http_code = (
            status.HTTP_422_UNPROCESSABLE_CONTENT
            if unknown
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=http_code,
            detail={"motivo": "falha_download", "mensagem": exc.detail},
        ) from exc
    except Exception as write_error:
        # The download succeeded but the DB write failed; the persistence service
        # has rolled back, leaving the package unpersisted and the on-disk file
        # intact (Req. 9.2). Map to ``falha_persistencia`` 502 (Req. 9.3),
        # distinct from the download failure above.
        raise _persistence_failure(str(write_error)) from write_error
