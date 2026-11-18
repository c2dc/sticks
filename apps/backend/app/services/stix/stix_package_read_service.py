"""StixPackageReadService — read-only package/campaign views from Postgres
(tasks 4.1-4.2).

This service serves the STIX package and campaign overviews reading **only**
from the database (no bundle is kept anywhere else). It is
the counterpart of :class:`~app.services.stix.stix_persistence_service.StixPersistenceService`
(the single writer): the read service only queries the six STIX tables
persisted at download time and assembles the response DTOs. It is the source of
the ``GET /api/stix/bases/{version}/overview`` route and of the AI agent's reads.

Design alignment
----------------
* Lives in the :mod:`app.services.stix` package next to
  :class:`StixService` and :class:`StixPersistenceService`.
* Takes a SQLAlchemy ``Session`` on construction — the same session-scoped
  pattern the persistence service and other read services in this backend follow.
* The response DTOs (:class:`TypeCount`, :class:`StixPackageOverview`) mirror the
  design "Component 4" / Data Models TypeScript contract field-for-field, so the
  ``/api/stix/*`` handler and the TS client map 1:1.
* Reads are issued with SQLAlchemy ``select`` on the injected session, scoped by
  the package resolved from ``version``.

Task 4.1 implemented :meth:`StixPackageReadService.package_overview` and
:meth:`StixPackageReadService.is_persisted`. Task 4.2 adds ``list_campaigns`` and
``campaign_overview`` to this same class/file, together with the campaign DTOs
(:class:`StixCampaignSummary`, :class:`StixCampaignOverview`,
:class:`TechniqueElement`) and the typed :class:`CampaignNotFoundError`.

_Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 4.5, 8.3_
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.domain import (
    StixActor,
    StixActorCampaign,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
    StixTypeCount,
)


def _isoformat_utc(value: dt.datetime) -> str:
    """Serialize a naive-UTC timestamp as an explicit UTC ISO-8601 string.

    The ``stix_packages.downloaded_at`` column stores a timezone-naive UTC
    value (project convention via ``_utcnow``). Emitting it without a zone makes
    a JS ``new Date(...)`` read it as local time, shifting the displayed value.
    We append the ``Z`` (Zulu/UTC) designator so the contract is unambiguous.
    """
    aware = value.replace(tzinfo=dt.UTC)
    return aware.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Typed error (read of a non-persisted version)
# ---------------------------------------------------------------------------


class StixPackageNotPersistedError(Exception):
    """Raised when a read is requested for a version with no ``stix_packages`` row.

    :meth:`StixPackageReadService.package_overview` raises this when the given
    ``version`` is not persisted (no row in ``stix_packages``). The route layer
    maps it to the ``bundle_unavailable`` 422 contract (Req. 8.3). Carries the offending :attr:`version` for the handler.
    """

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"STIX package v{version} is not persisted: no row in stix_packages.")


class CampaignNotFoundError(Exception):
    """Raised when a ``campaign_key`` has no matching campaign in the package.

    :meth:`StixPackageReadService.campaign_overview` raises this when, within the
    persisted package of the given ``version``, no campaign has a
    ``campaign_key`` (``curated_slug`` when present, else ``stix_id`` — Req. 4.5)
    equal to the requested one. The route layer (task 5.4) maps it to the
    ``campaign_not_found`` 404 contract. Carries both the offending
    :attr:`version` and :attr:`campaign_key` for the handler.
    """

    def __init__(self, version: str, campaign_key: str) -> None:
        self.version = version
        self.campaign_key = campaign_key
        super().__init__(f"STIX campaign '{campaign_key}' not found in package v{version}.")


# ---------------------------------------------------------------------------
# Response DTOs (design Component 4 / Data Models)
# ---------------------------------------------------------------------------


class TypeCount(BaseModel):
    """A per-type object count for the package overview (design ``TypeCount``).

    Parallel DTO mirroring :class:`app.services.stix.stix_service.TypeCount` and
    the ``stix_type_counts`` row: ``label`` is the human label, ``stix_type`` the
    raw STIX ``type``, ``count`` the number of objects of that type and
    ``description`` a non-empty description of what the type means.
    """

    label: str
    stix_type: str
    count: int
    description: str


class StixPackageOverview(BaseModel):
    """Package overview assembled from the database (design Component 4).

    Identification (``version`` / ``name`` / ``downloaded_at``) comes from the
    ``stix_packages`` row; ``counts`` from the ``stix_type_counts`` rows; and the
    totals from the persisted rows and per-type counts (Req. 3.2, 3.3).
    ``downloaded_at`` is serialized to an ISO-8601 string (or ``None`` when
    unknown).
    """

    version: str
    name: str
    downloaded_at: str | None = None
    counts: list[TypeCount] = Field(default_factory=list)
    campaigns_total: int
    actors_total: int
    techniques_total: int
    tactics_total: int
    malware_total: int
    relationships_total: int


class StixCampaignSummary(BaseModel):
    """A campaign row of a package for the clickable list (design Component 4).

    Mirrors the TS ``StixCampaignSummary`` field-for-field. ``campaign_key`` is
    the stable key the UI/route use to address the campaign: the ``curated_slug``
    when the campaign is curated, otherwise the ``stix_id`` (Req. 4.5).
    """

    campaign_key: str
    stix_id: str
    name: str
    external_id: str | None = None
    curated: bool
    curated_slug: str | None = None


class TechniqueElement(BaseModel):
    """One ATT&CK technique a campaign uses (design ``TechniqueElement``).

    Projection of a ``stix_campaign_techniques`` row: the ATT&CK ``technique_id``
    (e.g. ``"T1059"``), its ``technique_name``, the ``tactic`` and a free-text
    ``description`` — all optional except the id.
    """

    technique_id: str
    technique_name: str | None = None
    tactic: str | None = None
    description: str | None = None


class ActorRef(BaseModel):
    """An actor attributed to a campaign (design ``{ stix_id, name }``)."""

    stix_id: str
    name: str


class StixCampaignOverview(BaseModel):
    """Overview of a single campaign of a package (design Component 4).

    Mirrors the TS ``StixCampaignOverview`` field-for-field. Carries the
    campaign identity (``campaign_key`` / ``stix_id`` / ``name`` / ``external_id``
    / ``curated`` / ``curated_slug``), its ``description`` and ``aliases``, the
    ATT&CK ``techniques`` it uses and the ``actors`` attributed to it — all
    scoped to that campaign in that package only (Req. 4.4).
    """

    campaign_key: str
    stix_id: str
    name: str
    external_id: str | None = None
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    curated: bool
    curated_slug: str | None = None
    techniques: list[TechniqueElement] = Field(default_factory=list)
    actors: list[ActorRef] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Analytics DTOs (design Component 5 / Package_Analytics)
# ---------------------------------------------------------------------------


class ActorRank(BaseModel):
    """One actor in the "top actors by attributed campaigns" ranking.

    Mirrors the design ``ActorRank`` field-for-field: the actor's ``stix_id``
    and ``name`` plus ``campaigns_total``, the number of campaigns attributed to
    the actor within the package (counted over ``stix_actor_campaigns``).
    """

    stix_id: str
    name: str
    campaigns_total: int


class CampaignRank(BaseModel):
    """One campaign in the "top campaigns by number of techniques" ranking.

    Mirrors the design ``CampaignRank`` field-for-field. ``campaign_key`` is the
    stable key (``curated_slug`` when present, else ``stix_id`` — Req. 12.5),
    ``name`` the campaign name, ``curated`` its curated flag and
    ``techniques_total`` the number of ATT&CK techniques the campaign uses
    (counted over ``stix_campaign_techniques``).
    """

    campaign_key: str
    name: str
    curated: bool
    techniques_total: int


class TacticDistribution(BaseModel):
    """One bucket of the technique-per-tactic distribution (design DTO).

    Mirrors the design ``TacticDistribution`` field-for-field: a ``tactic`` name
    and ``techniques_total``, the number of campaign techniques carrying that
    tactic within the package. Rows with a null ``tactic`` are excluded from the
    distribution (Req. 12.4).
    """

    tactic: str
    techniques_total: int


class TechniqueRank(BaseModel):
    """One technique in the "campaigns per technique" ranking.

    The inverse view of :class:`CampaignRank`: for a single ATT&CK technique,
    ``campaigns_total`` is the number of DISTINCT campaigns in the package that
    use it (counted over ``stix_campaign_techniques``). ``technique_id`` is the
    ATT&CK id (e.g. ``T1059``) and ``technique_name`` its human-readable name
    when known.
    """

    technique_id: str
    technique_name: str | None = None
    campaigns_total: int


class StixPackageAnalytics(BaseModel):
    """Analytic dashboard of a package, aggregated from the database (Component 5).

    Mirrors the design ``StixPackageAnalytics`` field-for-field. Carries the
    package ``version``, the ``curated_total`` / ``uncurated_total`` campaign
    counts, the ``top_actors`` ranking (top-N by attributed campaigns), the
    ``top_campaigns`` ranking (top-N by number of techniques) and the ``tactics``
    distribution (techniques per tactic). All aggregates are scoped to the
    package of ``version`` and computed from the persisted rows only — no bundle
    reprocessing, no network (Req. 12.1).
    """

    version: str
    curated_total: int
    uncurated_total: int
    top_actors: list[ActorRank] = Field(default_factory=list)
    top_campaigns: list[CampaignRank] = Field(default_factory=list)
    tactics: list[TacticDistribution] = Field(default_factory=list)
    top_techniques: list[TechniqueRank] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StixPackageReadService:
    """Serve package/campaign overviews reading only from the database.

    Assembles the response DTOs from the six STIX tables without reprocessing the
    bundle on disk. Takes the SQLAlchemy ``Session`` to query on construction.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def is_persisted(self, version: str) -> bool:
        """True when ``version`` already has a row in ``stix_packages``.

        Used by the download route and the base listing (Req. 8.3): a version is persisted when its
        root row exists. Issues a scalar ``EXISTS``-style lookup on the injected
        session, so it never touches the disk.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            ``True`` if a ``stix_packages`` row exists for ``version``, else
            ``False``.
        """
        package_id = self._session.execute(
            select(StixPackage.id).where(StixPackage.version == version)
        ).scalar_one_or_none()
        return package_id is not None

    def list_persisted_versions(self) -> list[str]:
        """List the versions of every persisted ``Stix_Package_Version`` (Req. 10.4).

        Each ``Stix_Package_Version`` is a ``Pipeline_Input`` of the emulation
        pipeline (Req. 10.1); this read enumerates the versions actually
        persisted in the Relational_Store so the base-selection surface can
        present the available versions as selectable inputs (Req. 10.4) without
        relying on the fixed ``KNOWN_VERSIONS`` index. Reads **only** from the
        database (one scalar query over ``stix_packages``), newest-first by the
        ``version`` string for a stable listing.

        Returns:
            The persisted versions (empty list when nothing is persisted yet).
        """
        rows = (
            self._session.execute(select(StixPackage.version).order_by(StixPackage.version.desc()))
            .scalars()
            .all()
        )
        return list(rows)

    def package_overview(self, version: str) -> StixPackageOverview:
        """Assemble the package overview from the database only (Req. 3.1-3.4).

        Resolves the ``stix_packages`` row by ``version`` and, reading **only**
        from the database (no bundle is kept elsewhere), builds the
        :class:`StixPackageOverview`:

        * **Identification** — ``version`` / ``name`` from the package row and
          ``downloaded_at`` serialized to an ISO-8601 string (or ``None``).
        * **Counts** — one :class:`TypeCount` per ``stix_type_counts`` row of the
          package.
        * **Totals** — ``campaigns_total`` / ``actors_total`` are counted from the
          ``stix_campaigns`` / ``stix_actors`` rows of the package;
          ``techniques_total`` / ``tactics_total`` / ``malware_total`` /
          ``relationships_total`` come from the already-persisted per-type counts
          (``attack-pattern`` / ``x-mitre-tactic`` / ``malware`` / ``relationship``
          respectively), defaulting to ``0`` when a type has no count row.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            The :class:`StixPackageOverview` for the persisted version.

        Raises:
            StixPackageNotPersistedError: when ``version`` has no row in
                ``stix_packages`` (the route maps it to ``bundle_unavailable`` 422 — Req. 8.3).
        """
        package = self._session.execute(
            select(StixPackage).where(StixPackage.version == version)
        ).scalar_one_or_none()
        if package is None:
            raise StixPackageNotPersistedError(version)

        # Per-type counts for the package, and a lookup by stix_type so the
        # type-derived totals can be read without a second query.
        count_rows = (
            self._session.execute(
                select(StixTypeCount).where(StixTypeCount.package_id == package.id)
            )
            .scalars()
            .all()
        )
        counts = [
            TypeCount(
                label=row.label,
                stix_type=row.stix_type,
                count=row.count,
                description=row.description,
            )
            for row in count_rows
        ]
        count_by_type = {row.stix_type: row.count for row in count_rows}

        # Campaign/actor totals counted directly from the persisted child rows.
        campaigns_total = self._session.execute(
            select(func.count())
            .select_from(StixCampaign)
            .where(StixCampaign.package_id == package.id)
        ).scalar_one()
        actors_total = self._session.execute(
            select(func.count()).select_from(StixActor).where(StixActor.package_id == package.id)
        ).scalar_one()

        return StixPackageOverview(
            version=package.version,
            name=package.name,
            downloaded_at=(
                _isoformat_utc(package.downloaded_at) if package.downloaded_at is not None else None
            ),
            counts=counts,
            campaigns_total=campaigns_total,
            actors_total=actors_total,
            # Type-derived totals come from the persisted per-type counts.
            techniques_total=count_by_type.get("attack-pattern", 0),
            tactics_total=count_by_type.get("x-mitre-tactic", 0),
            malware_total=count_by_type.get("malware", 0),
            relationships_total=count_by_type.get("relationship", 0),
        )

    @staticmethod
    def _campaign_key(campaign: StixCampaign) -> str:
        """Stable key of a campaign: ``curated_slug`` when present, else ``stix_id``.

        Applied consistently in :meth:`list_campaigns` and
        :meth:`campaign_overview` so the key the list exposes is the same key the
        overview lookup resolves (Req. 4.5).
        """
        return campaign.curated_slug or campaign.stix_id

    def _resolve_package_id(self, version: str) -> int:
        """Resolve the ``stix_packages`` id for ``version`` or raise.

        Raises:
            StixPackageNotPersistedError: when ``version`` has no row in
                ``stix_packages``.
        """
        package_id = self._session.execute(
            select(StixPackage.id).where(StixPackage.version == version)
        ).scalar_one_or_none()
        if package_id is None:
            raise StixPackageNotPersistedError(version)
        return package_id

    def list_campaigns(self, version: str) -> list[StixCampaignSummary]:
        """List the campaigns of a persisted package, from the database (Req. 4.1).

        Resolves the ``stix_packages`` row by ``version`` and returns one
        :class:`StixCampaignSummary` per ``stix_campaigns`` row of that package,
        reading **only** from the database. The ``campaign_key`` is the
        ``curated_slug`` when present, otherwise the ``stix_id`` (Req. 4.5).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            The campaign summaries of the package (empty list when the package
            has no campaigns).

        Raises:
            StixPackageNotPersistedError: when ``version`` has no row in
                ``stix_packages``.
        """
        package_id = self._resolve_package_id(version)
        campaigns = (
            self._session.execute(select(StixCampaign).where(StixCampaign.package_id == package_id))
            .scalars()
            .all()
        )
        return [
            StixCampaignSummary(
                campaign_key=self._campaign_key(campaign),
                stix_id=campaign.stix_id,
                name=campaign.name,
                external_id=campaign.external_id,
                curated=campaign.curated,
                curated_slug=campaign.curated_slug,
            )
            for campaign in campaigns
        ]

    def campaign_overview(self, version: str, campaign_key: str) -> StixCampaignOverview:
        """Assemble the overview of one campaign, from the database (Req. 4.2-4.5).

        Resolves the ``stix_packages`` row by ``version`` and finds the campaign
        of that package whose ``campaign_key`` (``curated_slug`` when present,
        else ``stix_id`` — Req. 4.5) equals the requested one. Reading **only**
        from the database, it builds the :class:`StixCampaignOverview` with the
        campaign identity, its deserialized ``aliases`` (default ``[]``), the
        ATT&CK ``techniques`` loaded from ``stix_campaign_techniques`` for that
        campaign id only, and the ``actors`` loaded from ``stix_actors`` joined
        via ``stix_actor_campaigns`` for that campaign id only — no cross-version
        or cross-campaign leakage (Req. 4.4).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).
            campaign_key: The campaign key (curated slug or STIX id).

        Returns:
            The :class:`StixCampaignOverview` for the campaign.

        Raises:
            StixPackageNotPersistedError: when ``version`` has no row in
                ``stix_packages``.
            CampaignNotFoundError: when no campaign of the package matches
                ``campaign_key`` (so the route maps it to
                ``campaign_not_found`` 404).
        """
        package_id = self._resolve_package_id(version)

        # All campaigns of the package, so the key lookup uses the same
        # (curated_slug or stix_id) rule the list exposes (Req. 4.5). A plain
        # Python lookup keeps the match independent of whether the key is a
        # curated slug or a raw stix_id.
        campaigns = (
            self._session.execute(select(StixCampaign).where(StixCampaign.package_id == package_id))
            .scalars()
            .all()
        )
        campaign = next(
            (c for c in campaigns if self._campaign_key(c) == campaign_key),
            None,
        )
        if campaign is None:
            raise CampaignNotFoundError(version, campaign_key)

        # Techniques of THIS campaign only (scoped by campaign_id — Req. 4.4).
        technique_rows = (
            self._session.execute(
                select(StixCampaignTechnique).where(
                    StixCampaignTechnique.campaign_id == campaign.id
                )
            )
            .scalars()
            .all()
        )
        techniques = [
            TechniqueElement(
                technique_id=row.technique_id,
                technique_name=row.technique_name,
                tactic=row.tactic,
                description=row.description,
            )
            for row in technique_rows
        ]

        # Actors attributed to THIS campaign only, via stix_actor_campaigns
        # joined to stix_actors (scoped by campaign_id — Req. 4.4).
        actor_rows = (
            self._session.execute(
                select(StixActor)
                .join(
                    StixActorCampaign,
                    StixActorCampaign.actor_id == StixActor.id,
                )
                .where(StixActorCampaign.campaign_id == campaign.id)
            )
            .scalars()
            .all()
        )
        actors = [ActorRef(stix_id=actor.stix_id, name=actor.name) for actor in actor_rows]

        # Deserialize aliases JSON to list[str], defaulting to [] when absent or
        # not a list (the column is nullable JSON).
        raw_aliases = campaign.aliases
        aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []

        return StixCampaignOverview(
            campaign_key=self._campaign_key(campaign),
            stix_id=campaign.stix_id,
            name=campaign.name,
            external_id=campaign.external_id,
            description=campaign.description,
            aliases=aliases,
            curated=campaign.curated,
            curated_slug=campaign.curated_slug,
            techniques=techniques,
            actors=actors,
        )

    def package_analytics(self, version: str, top_n: int = 10) -> StixPackageAnalytics:
        """Aggregate the analytic dashboard of a package from the database (Req. 12.*).

        Resolves the ``stix_packages`` row by ``version`` and, reading **only**
        from the database (no bundle reprocessing, no network — Req. 12.1), runs
        aggregate queries scoped by the package id and assembles the
        :class:`StixPackageAnalytics`:

        * **``top_actors``** — ``stix_actors`` joined to ``stix_actor_campaigns``,
          grouped by actor, ordered by the number of attributed campaigns
          descending, limited to ``top_n`` (Req. 12.2).
        * **``top_campaigns``** — ``stix_campaigns`` joined to
          ``stix_campaign_techniques``, grouped by campaign, ordered by the
          number of techniques descending, limited to ``top_n``; the
          ``campaign_key`` is the ``curated_slug`` when present, else the
          ``stix_id`` (Req. 12.3, 12.5).
        * **``curated_total`` / ``uncurated_total``** — ``stix_campaigns`` counted
          grouped by the ``curated`` flag (Req. 12.6).
        * **``tactics``** — ``stix_campaign_techniques`` grouped by ``tactic``
          (rows with a null ``tactic`` excluded), ordered by count descending
          (Req. 12.4).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).
            top_n: Maximum number of rows for the actor/campaign rankings
                (default ``10``).

        Returns:
            The :class:`StixPackageAnalytics` for the persisted version.

        Raises:
            StixPackageNotPersistedError: when ``version`` has no row in
                ``stix_packages`` (the route maps it to ``bundle_unavailable`` 422 — Req. 12.7).
        """
        package_id = self._resolve_package_id(version)

        # curated_total / uncurated_total: count campaigns grouped by the curated
        # flag (Req. 12.6). Missing buckets default to 0.
        curated_rows = self._session.execute(
            select(StixCampaign.curated, func.count())
            .where(StixCampaign.package_id == package_id)
            .group_by(StixCampaign.curated)
        ).all()
        curated_total = 0
        uncurated_total = 0
        for is_curated, total in curated_rows:
            if is_curated:
                curated_total = total
            else:
                uncurated_total = total

        # top_actors: actors of the package ranked by attributed campaigns
        # (stix_actors JOIN stix_actor_campaigns), ordered desc, limited to
        # top_n (Req. 12.2).
        actor_campaigns_count = func.count(StixActorCampaign.id)
        actor_rows = self._session.execute(
            select(
                StixActor.stix_id,
                StixActor.name,
                actor_campaigns_count.label("campaigns_total"),
            )
            .join(StixActorCampaign, StixActorCampaign.actor_id == StixActor.id)
            .where(StixActor.package_id == package_id)
            .group_by(StixActor.id, StixActor.stix_id, StixActor.name)
            .order_by(actor_campaigns_count.desc())
            .limit(top_n)
        ).all()
        top_actors = [
            ActorRank(
                stix_id=stix_id,
                name=name,
                campaigns_total=campaigns_total,
            )
            for stix_id, name, campaigns_total in actor_rows
        ]

        # top_campaigns: campaigns of the package ranked by number of techniques
        # (stix_campaigns JOIN stix_campaign_techniques), ordered desc, limited
        # to top_n; campaign_key = curated_slug or stix_id (Req. 12.3, 12.5).
        techniques_count = func.count(StixCampaignTechnique.id)
        campaign_rows = self._session.execute(
            select(
                StixCampaign.stix_id,
                StixCampaign.curated_slug,
                StixCampaign.name,
                StixCampaign.curated,
                techniques_count.label("techniques_total"),
            )
            .join(
                StixCampaignTechnique,
                StixCampaignTechnique.campaign_id == StixCampaign.id,
            )
            .where(StixCampaign.package_id == package_id)
            .group_by(
                StixCampaign.id,
                StixCampaign.stix_id,
                StixCampaign.curated_slug,
                StixCampaign.name,
                StixCampaign.curated,
            )
            .order_by(techniques_count.desc())
            .limit(top_n)
        ).all()
        top_campaigns = [
            CampaignRank(
                campaign_key=curated_slug or stix_id,
                name=name,
                curated=curated,
                techniques_total=techniques_total,
            )
            for stix_id, curated_slug, name, curated, techniques_total in campaign_rows
        ]

        # tactics: distribution of campaign techniques per tactic within the
        # package, excluding rows with a null tactic, ordered desc (Req. 12.4).
        tactic_count = func.count(StixCampaignTechnique.id)
        tactic_rows = self._session.execute(
            select(
                StixCampaignTechnique.tactic,
                tactic_count.label("techniques_total"),
            )
            .join(
                StixCampaign,
                StixCampaign.id == StixCampaignTechnique.campaign_id,
            )
            .where(
                StixCampaign.package_id == package_id,
                StixCampaignTechnique.tactic.is_not(None),
            )
            .group_by(StixCampaignTechnique.tactic)
            .order_by(tactic_count.desc())
        ).all()
        tactics = [
            TacticDistribution(tactic=tactic, techniques_total=techniques_total)
            for tactic, techniques_total in tactic_rows
        ]

        # top_techniques: the inverse of top_campaigns — each ATT&CK technique of
        # the package ranked by the number of DISTINCT campaigns that use it
        # (stix_campaign_techniques grouped by technique_id), ordered desc,
        # limited to top_n. Counts distinct campaigns so a technique repeated
        # within one campaign is not double-counted.
        technique_campaigns_count = func.count(func.distinct(StixCampaignTechnique.campaign_id))
        technique_rows = self._session.execute(
            select(
                StixCampaignTechnique.technique_id,
                func.max(StixCampaignTechnique.technique_name).label("technique_name"),
                technique_campaigns_count.label("campaigns_total"),
            )
            .join(
                StixCampaign,
                StixCampaign.id == StixCampaignTechnique.campaign_id,
            )
            .where(StixCampaign.package_id == package_id)
            .group_by(StixCampaignTechnique.technique_id)
            .order_by(technique_campaigns_count.desc())
            .limit(top_n)
        ).all()
        top_techniques = [
            TechniqueRank(
                technique_id=technique_id,
                technique_name=technique_name,
                campaigns_total=campaigns_total,
            )
            for technique_id, technique_name, campaigns_total in technique_rows
        ]

        return StixPackageAnalytics(
            version=version,
            curated_total=curated_total,
            uncurated_total=uncurated_total,
            top_actors=top_actors,
            top_campaigns=top_campaigns,
            tactics=tactics,
            top_techniques=top_techniques,
        )
