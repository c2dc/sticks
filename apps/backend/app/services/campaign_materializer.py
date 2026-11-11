"""CampaignMaterializer — idempotent upsert of a campaigns row from the STIX.

Phase 2, sub-step 2.3a of the disk->DB migration. Any of the 52 campaigns
persisted in the STIX tables must have a row in the ``campaigns`` relational
table **before** any FK-bearing record (``agent_runs.campaign_id`` /
``abilities.campaign_id`` / ``dags.campaign_id``) is written for a run. This
service guarantees that row exists, materializing it from the STIX package
(served by :class:`StixPackageReadService`) without loosening any foreign key.

Design alignment (Component 1)
------------------------------
* Session-injected, same convention as the other services in this backend.
* ``materialize(campaign_key, version)`` upserts a single ``campaigns`` row and
  returns ``campaigns.id``.
* The id follows the Campaign_Key rule: ``curated_slug`` when present, otherwise
  ``stix_id`` — exactly the ``campaign_key`` the
  :meth:`StixPackageReadService.campaign_overview` already resolves.
* ``name`` / ``description`` come from the ``Campaign_Overview``.
* A pre-existing row (a curated campaign already ingested by
  ``CuratedIngestService``) is a no-op that preserves its ``translation_source``.
* A campaign absent from ``stix_campaigns`` raises
  :class:`CampaignNotPersistedError` (the ``_known_campaign_or_404`` guard
  already returns 404 before this in the request path; this error only surfaces
  on improper programmatic use).

This module performs no network access and reads STIX only through the database.

_Requirements: 2.5, 2.6_
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.domain import Case
from app.models.enums import TranslationSource
from app.services.stix.stix_package_read_service import (
    CampaignNotFoundError,
    StixPackageNotPersistedError,
    StixPackageReadService,
)


class CampaignNotPersistedError(Exception):
    """Raised when a campaign is not persisted in ``stix_campaigns``.

    :meth:`CampaignMaterializer.materialize` raises this when the requested
    ``campaign_key`` (for the given ``version``) has no matching row in the STIX
    tables, so there is nothing to materialize into ``campaigns``. In the request
    path the ``_known_campaign_or_404`` guard already returns 404 before reaching
    here (design note), so this error only occurs on improper programmatic use.
    Carries both the offending :attr:`version` and :attr:`campaign_key`.
    """

    def __init__(self, campaign_key: str, version: str) -> None:
        self.campaign_key = campaign_key
        self.version = version
        super().__init__(
            f"Campaign '{campaign_key}' is not persisted in stix_campaigns "
            f"for package v{version}; nothing to materialize."
        )


class CampaignMaterializer:
    """Ensure a ``campaigns`` row exists for any STIX-persisted campaign.

    Idempotent per campaign: materializing the same ``(campaign_key, version)``
    once or many times results in exactly one ``campaigns`` row. Takes the
    SQLAlchemy ``Session`` on construction and reads the STIX identity through
    :class:`StixPackageReadService` on the same session.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._read_service = StixPackageReadService(session)

    def materialize(self, campaign_key: str, version: str) -> str:
        """Idempotently upsert ``campaigns`` from the STIX and return ``campaigns.id``.

        The id follows the Campaign_Key rule (``curated_slug`` when present, else
        ``stix_id``) — the same value :meth:`StixPackageReadService.campaign_overview`
        resolves as ``campaign_key``. ``name`` / ``description`` come from the
        ``Campaign_Overview``. A pre-existing row (a curated campaign already
        ingested) is a no-op that preserves its ``translation_source``; a new row
        is written with ``TranslationSource.AI_GENERATED`` (the campaign is
        materialized for an AI run, not human-curated).

        Args:
            campaign_key: The campaign key (curated slug or STIX id).
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            The ``campaigns.id`` of the materialized (or pre-existing) row —
            equal to the resolved Campaign_Key.

        Raises:
            CampaignNotPersistedError: when the campaign is absent from
                ``stix_campaigns`` for ``version`` (nothing to materialize).
        """
        try:
            overview = self._read_service.campaign_overview(version, campaign_key)
        except (CampaignNotFoundError, StixPackageNotPersistedError) as exc:
            raise CampaignNotPersistedError(campaign_key, version) from exc

        # id follows the Campaign_Key rule; campaign_overview already resolved it
        # (curated_slug when present, else stix_id).
        campaign_id = overview.campaign_key

        existing = self._session.get(Case, campaign_id)
        if existing is None:
            # New row: materialized for an AI run. translation_source is
            # AI_GENERATED; curated rows (ingested by CuratedIngestService) never
            # reach this branch, so human curation is never overwritten.
            self._session.add(
                Case(
                    id=campaign_id,
                    name=overview.name,
                    description=overview.description,
                    translation_source=TranslationSource.AI_GENERATED,
                )
            )
            self._session.flush()
        else:
            # Pre-existing row (curated): refresh name/description from the STIX
            # overview but PRESERVE translation_source (no-op for curated rows).
            existing.name = overview.name
            existing.description = overview.description

        return campaign_id
