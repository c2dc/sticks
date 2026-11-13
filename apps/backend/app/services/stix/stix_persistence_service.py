"""StixPersistenceService — transactional, idempotent persistence of a STIX
package into the database.

This service is the single point in the system that writes the STIX tables. It
receives a bundle **already in memory** (downloaded by
:meth:`~app.services.stix.stix_service.StixService.fetch_bundle`, never written
to disk), derives its structured content (type counts, campaigns, techniques per
campaign, actors and actor→campaign attributions) through the pure derivations of
:class:`~app.services.stix.stix_service.StixService` and persists it inside a
single transaction. Re-persisting a version is idempotent: the row in
``stix_packages`` keyed by ``version`` is reused and its derived content is
replaced deterministically.

It is driven by the ``stix_packages`` ingestion source
(:class:`~app.services.ingestion.runners.StixPackagesSourceRunner`).

Vector indexing hook (Req. 10.3)
--------------------------------
A ``Stix_Package_Version`` is a ``Pipeline_Input`` and must live in **both**
stores (Req. 10.1). The *semantic* half — indexing the version's campaigns into
the Vector_Store — is delegated to an **optional, injected**
:class:`StixVectorIndexer` invoked only **after** the relational commit succeeds.
The hook is additive and defensive: when no indexer is injected the behaviour is
exactly the relational-only one, and the hook never affects the ``persisted``
verdict of the committed write (Req. 10.6).

_Requirements: 1.1, 1.3, 1.4, 1.6, 2.1, 2.2, 2.3, 2.4, 4.6, 7.1, 9.2, 10.1, 10.3_
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import (
    StixActor,
    StixActorCampaign,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
    StixTypeCount,
    _utcnow,
)
from app.services.stix.stix_service import StixService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result DTO (design Component 1)
# ---------------------------------------------------------------------------


class StixPackagePersistedResult(BaseModel):
    """Outcome of persisting a STIX package (design Component 1).

    Returned by :meth:`StixPersistenceService.download_and_persist` and
    :meth:`StixPersistenceService.persist_bundle`. ``downloaded`` reports
    whether the bundle was (re)fetched/present and ``persisted`` whether the DB
    write committed successfully. ``counts_total`` is the sum of the per-type
    counts; ``campaigns_total`` and ``actors_total`` are the number of persisted
    campaigns and actors respectively.
    """

    version: str
    downloaded: bool
    persisted: bool
    counts_total: int
    campaigns_total: int
    actors_total: int


# ---------------------------------------------------------------------------
# Vector indexing hook (optional, injectable — task 16.1)
# ---------------------------------------------------------------------------


@runtime_checkable
class StixVectorIndexer(Protocol):
    """Indexes a persisted ``Stix_Package_Version`` into the Vector_Store.

    Structural contract satisfied by
    :class:`~app.services.stix.stix_vector_index_service.StixVectorIndexService`
    (its ``index_version_safe`` method). Kept as an injectable boundary so the
    persistence service can index the committed campaigns into pgvector without
    the relational-only tests needing a real embedder/pgvector: when no indexer
    is injected the hook is simply skipped (Req. 10.3).
    """

    def index_version_safe(self, version: str) -> Any:
        """Index the campaigns of ``version`` into the Vector_Store (best-effort)."""
        ...


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StixPersistenceService:
    """Persist a STIX bundle (held in memory) transactionally and idempotently.

    The single point that writes the STIX tables. It reuses an injected
    :class:`StixService` for the download and the derivations (defaulting to a
    fresh instance when none is supplied) and writes through the provided
    SQLAlchemy ``Session``.
    """

    def __init__(
        self,
        session: Session,
        stix_service: StixService | None = None,
        *,
        vector_indexer: StixVectorIndexer | None = None,
    ) -> None:
        self._session = session
        self._stix_service = stix_service if stix_service is not None else StixService()
        # Optional Vector_Store indexer invoked after a successful commit so the
        # persisted version is also a Pipeline_Input in the vector store
        # (Req. 10.1, 10.3). Defaults to None so relational-only call sites and
        # tests never need a real embedder/pgvector.
        self._vector_indexer = vector_indexer

    def download_and_persist(self, version: str) -> StixPackagePersistedResult:
        """Download the bundle into memory and persist it, idempotently.

        The single network egress is :meth:`StixService.fetch_bundle` (which
        validates the version first). The bundle never touches the disk. A failed
        download raises :class:`~app.services.stix.stix_service.StixDownloadError`
        before anything is written; a failed write rolls the session back.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            A :class:`StixPackagePersistedResult` with ``downloaded=True`` and
            ``persisted=True`` on success.
        """
        bundle = self._stix_service.fetch_bundle(version)
        return self.persist_bundle(version, bundle)

    def persist_bundle(self, version: str, bundle: dict) -> StixPackagePersistedResult:
        """Persist an in-memory ``bundle`` for ``version`` (no network access)."""
        return self._derive_and_write(version, bundle)

    # ------------------------------------------------------------------
    # Transactional derivation + write
    # ------------------------------------------------------------------

    def _derive_and_write(self, version: str, bundle: dict) -> StixPackagePersistedResult:
        """Derive the structured content from ``bundle`` and write it.

        Derives the per-type counts, the campaigns, the techniques a campaign
        uses and the actor→campaign attributions, and persists ``stix_packages``
        + ``stix_type_counts`` + ``stix_campaigns`` + ``stix_campaign_techniques``
        + ``stix_actors`` + ``stix_actor_campaigns`` inside a single transaction,
        committing once (Req. 1.4).

        The write is idempotent per ``version`` (Req. 2.1-2.4): when a
        ``stix_packages`` row already exists for ``version`` it is reused (not
        duplicated), its ``downloaded_at`` is refreshed via :func:`_utcnow` and all
        derived child content for that package is cleared and re-inserted
        (delete-and-insert) within the same transaction.
        """
        # --- Derivation (pure, over the in-memory bundle) -------------------
        dashboard = self._stix_service.dashboard(version, bundle)
        campaigns = self._stix_service.list_campaigns(bundle)
        adversaries = self._stix_service.list_adversaries(bundle)

        # Techniques a campaign uses, keyed by the campaign stix_id, derived from
        # the "uses" relationships (campaign source_ref → attack-pattern target_ref).
        techniques_by_campaign = self._derive_campaign_techniques(bundle)
        # Rich per-campaign fields (external_id, aliases, first/last seen).
        details_by_campaign = self._derive_campaign_details(bundle)

        # --- Single transaction --------------------------------------------
        # Any failure after the bundle is available rolls the session back —
        # leaving the package unpersisted (Req. 9.2) — and re-raises for the
        # caller to map.
        try:
            result = self._write_derived_content(
                version,
                dashboard=dashboard,
                campaigns=campaigns,
                adversaries=adversaries,
                techniques_by_campaign=techniques_by_campaign,
                details_by_campaign=details_by_campaign,
            )
        except Exception:
            self._session.rollback()
            raise
        # Commit confirmed: index the just-committed campaigns into the
        # Vector_Store (best-effort, never affects ``persisted`` — Req. 10.6).
        self._index_into_vector_store(version)
        return result

    def _index_into_vector_store(self, version: str) -> None:
        """Index the committed version into the Vector_Store (best-effort, Req. 10.3).

        No-op when no :class:`StixVectorIndexer` was injected. When one is present
        it is invoked via its degrade-safe ``index_version_safe``, so a
        Vector_Store outage never propagates to the already-committed write.
        """
        if self._vector_indexer is None:
            return
        self._vector_indexer.index_version_safe(version)

    def _write_derived_content(
        self,
        version: str,
        *,
        dashboard: Any,
        campaigns: Any,
        adversaries: Any,
        techniques_by_campaign: dict[str, list[dict[str, Any]]],
        details_by_campaign: dict[str, dict[str, Any]],
    ) -> StixPackagePersistedResult:
        """Write the derived content in a single transaction (rollback-wrapped).

        Called only by :meth:`_derive_and_write`, which wraps it so any failure
        during flush/insert/commit triggers a rollback (Req. 9.2). All writes for
        the package happen here and commit once (Req. 1.4).
        """
        # Reuse the existing row keyed by the UNIQUE ``version`` when present so
        # a re-persistence does not duplicate it (Req. 2.1); otherwise insert a
        # fresh one. ``downloaded_at`` is refreshed on every (re-)persistence
        # (Req. 2.4).
        package = self._session.execute(
            select(StixPackage).where(StixPackage.version == version)
        ).scalar_one_or_none()
        if package is None:
            package = StixPackage(
                version=version,
                name=f"Enterprise ATT&CK v{version}",
                downloaded_at=_utcnow(),
            )
            self._session.add(package)
        else:
            # Clear the previously derived child content for this package before
            # re-inserting it, so re-persisting the same version is deterministic
            # and leaves no duplicate rows (Req. 2.2).
            self._clear_package_content(package.id)
            package.name = f"Enterprise ATT&CK v{version}"
            package.downloaded_at = _utcnow()
        # Flush so the package id is assigned (fresh insert) and the deletes are
        # applied, all still inside the same open transaction. The package id is
        # then usable as the foreign key of the child rows.
        self._session.flush()

        counts_total = 0
        for type_count in dashboard.counts:
            counts_total += type_count.count
            self._session.add(
                StixTypeCount(
                    package_id=package.id,
                    stix_type=type_count.stix_type,
                    label=type_count.label,
                    count=type_count.count,
                    description=type_count.description,
                )
            )

        # Persist campaigns and keep a lookup from stix_id to the persisted row so
        # techniques and actor attributions can reference the right campaign id.
        campaign_row_by_stix_id: dict[str, StixCampaign] = {}
        for campaign in campaigns:
            detail = details_by_campaign.get(campaign.stix_id, {})
            # curated is True iff curated_slug is not null (Req. 4.6).
            campaign_row = StixCampaign(
                package_id=package.id,
                stix_id=campaign.stix_id,
                name=campaign.name,
                external_id=detail.get("external_id"),
                description=campaign.description,
                aliases=detail.get("aliases") or None,
                curated=campaign.curated_slug is not None,
                curated_slug=campaign.curated_slug,
                first_seen=detail.get("first_seen"),
                last_seen=detail.get("last_seen"),
            )
            self._session.add(campaign_row)
            campaign_row_by_stix_id.setdefault(campaign.stix_id, campaign_row)
        # Flush so campaign ids are assigned before wiring techniques/actors.
        self._session.flush()

        # Techniques per campaign (unique per campaign_id + technique_id).
        for campaign_stix_id, techniques in techniques_by_campaign.items():
            campaign_row = campaign_row_by_stix_id.get(campaign_stix_id)
            if campaign_row is None:
                continue
            seen_technique_ids: set[str] = set()
            for technique in techniques:
                technique_id = technique.get("technique_id")
                if not isinstance(technique_id, str) or technique_id in seen_technique_ids:
                    continue
                seen_technique_ids.add(technique_id)
                self._session.add(
                    StixCampaignTechnique(
                        campaign_id=campaign_row.id,
                        technique_id=technique_id,
                        technique_name=technique.get("technique_name"),
                        tactic=technique.get("tactic"),
                        description=technique.get("description"),
                    )
                )

        # Actors (intrusion-sets) and the actor→campaign attributions. The
        # synthetic "sem ator associado" group that list_adversaries appends for
        # unattributed campaigns is not a real intrusion-set, so it is skipped.
        actors_total = 0
        for adversary in adversaries:
            if adversary.stix_id == StixService.ADVERSARY_UNASSOCIATED_ID:
                continue
            actor_row = StixActor(
                package_id=package.id,
                stix_id=adversary.stix_id,
                name=adversary.name,
            )
            self._session.add(actor_row)
            self._session.flush()
            actors_total += 1

            seen_campaign_ids: set[int] = set()
            for campaign in adversary.campaigns:
                campaign_row = campaign_row_by_stix_id.get(campaign.stix_id)
                if campaign_row is None or campaign_row.id in seen_campaign_ids:
                    continue
                seen_campaign_ids.add(campaign_row.id)
                self._session.add(
                    StixActorCampaign(
                        actor_id=actor_row.id,
                        campaign_id=campaign_row.id,
                    )
                )

        # Single commit for the whole package (Req. 1.4).
        self._session.commit()

        return StixPackagePersistedResult(
            version=version,
            downloaded=True,
            persisted=True,
            counts_total=counts_total,
            campaigns_total=len(campaigns),
            actors_total=actors_total,
        )

    # ------------------------------------------------------------------
    # Idempotent delete-and-insert (clear a package's derived content)
    # ------------------------------------------------------------------

    def _clear_package_content(self, package_id: int) -> None:
        """Delete all derived child content of a package (delete-and-insert).

        Deletes in foreign-key order so no constraint is violated: the
        grandchildren (``stix_campaign_techniques`` referencing campaigns and
        ``stix_actor_campaigns`` referencing actors and campaigns) are removed
        first, then the children (``stix_type_counts``, ``stix_campaigns``,
        ``stix_actors``) of the package. The ``stix_packages`` row itself is kept
        and reused by the caller. Runs inside the caller's open transaction; the
        matching re-insert and the single commit happen in
        :meth:`_derive_and_write`.

        The grandchildren are located by the campaign/actor ids that belong to
        this package, queried from the session.
        """
        # Ids of this package's campaigns and actors, used to scope the
        # grandchildren deletions.
        campaign_ids = [
            row_id
            for (row_id,) in self._session.execute(
                select(StixCampaign.id).where(StixCampaign.package_id == package_id)
            ).all()
        ]
        actor_ids = [
            row_id
            for (row_id,) in self._session.execute(
                select(StixActor.id).where(StixActor.package_id == package_id)
            ).all()
        ]

        # Grandchildren first (they reference campaigns/actors).
        if campaign_ids:
            self._session.query(StixCampaignTechnique).filter(
                StixCampaignTechnique.campaign_id.in_(campaign_ids)
            ).delete(synchronize_session=False)
        if actor_ids or campaign_ids:
            self._session.query(StixActorCampaign).filter(
                StixActorCampaign.actor_id.in_(actor_ids)
            ).delete(synchronize_session=False)

        # Then the package's direct children.
        self._session.query(StixTypeCount).filter(StixTypeCount.package_id == package_id).delete(
            synchronize_session=False
        )
        self._session.query(StixCampaign).filter(StixCampaign.package_id == package_id).delete(
            synchronize_session=False
        )
        self._session.query(StixActor).filter(StixActor.package_id == package_id).delete(
            synchronize_session=False
        )

    # ------------------------------------------------------------------
    # Bundle derivations (reusing the parse; following list_adversaries)
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_campaign_techniques(
        bundle: dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """Map each campaign stix_id to the techniques it uses, from the bundle.

        Follows the ``list_adversaries`` pattern for walking relationships: it
        resolves the ``attack-pattern`` objects first, then walks the ``uses``
        relationships linking a campaign (``source_ref``) to an attack-pattern
        (``target_ref``). Each resolved technique carries ``technique_id`` (the
        ``external_id`` such as ``"T1059"``), ``technique_name`` (the object
        ``name``), ``tactic`` (the first ``kill_chain_phases`` ``phase_name``) and
        ``description``. Techniques without a usable ``external_id`` are skipped
        (they cannot key a ``stix_campaign_techniques`` row).
        """
        objects = bundle.get("objects")
        if not isinstance(objects, list):
            return {}

        # Resolve attack-pattern objects by their STIX id.
        attack_patterns_by_id: dict[str, dict[str, Any]] = {}
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != "attack-pattern":
                continue
            stix_id = obj.get("id")
            if not isinstance(stix_id, str):
                continue
            external_id = StixService._extract_external_id(obj)
            if external_id is None:
                continue

            raw_name = obj.get("name")
            technique_name = raw_name if isinstance(raw_name, str) and raw_name else None

            tactic: str | None = None
            phases = obj.get("kill_chain_phases")
            if isinstance(phases, list):
                for phase in phases:
                    if not isinstance(phase, dict):
                        continue
                    phase_name = phase.get("phase_name")
                    if isinstance(phase_name, str) and phase_name:
                        tactic = phase_name
                        break

            raw_description = obj.get("description")
            description = raw_description if isinstance(raw_description, str) else None

            attack_patterns_by_id[stix_id] = {
                "technique_id": external_id,
                "technique_name": technique_name,
                "tactic": tactic,
                "description": description,
            }

        # Walk the "uses" relationships: campaign (source_ref) → attack-pattern
        # (target_ref). Preserve order and avoid duplicate techniques per campaign.
        techniques_by_campaign: dict[str, list[dict[str, Any]]] = {}
        seen: dict[str, set[str]] = {}
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != "relationship":
                continue
            if obj.get("relationship_type") != "uses":
                continue
            source_ref = obj.get("source_ref")
            target_ref = obj.get("target_ref")
            if not isinstance(source_ref, str) or not isinstance(target_ref, str):
                continue
            if not source_ref.startswith("campaign--"):
                continue
            technique = attack_patterns_by_id.get(target_ref)
            if technique is None:
                continue

            seen_for_campaign = seen.setdefault(source_ref, set())
            technique_id = technique["technique_id"]
            if technique_id in seen_for_campaign:
                continue
            seen_for_campaign.add(technique_id)
            techniques_by_campaign.setdefault(source_ref, []).append(technique)

        return techniques_by_campaign

    @staticmethod
    def _derive_campaign_details(
        bundle: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Map each campaign stix_id to its rich identity fields, from the bundle.

        Reads ``external_id`` (via ``external_references``), ``aliases``,
        ``first_seen`` and ``last_seen`` straight from each ``campaign`` object so
        the ``stix_campaigns`` rows are fully populated. These fields complement
        the ``stix_id`` / ``name`` / ``description`` / curated flag already carried
        by :meth:`StixService.list_campaigns`.
        """
        objects = bundle.get("objects")
        if not isinstance(objects, list):
            return {}

        details: dict[str, dict[str, Any]] = {}
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != "campaign":
                continue
            stix_id = obj.get("id")
            if not isinstance(stix_id, str):
                continue

            raw_aliases = obj.get("aliases")
            aliases = (
                [a for a in raw_aliases if isinstance(a, str) and a]
                if isinstance(raw_aliases, list)
                else []
            )
            raw_first_seen = obj.get("first_seen")
            raw_last_seen = obj.get("last_seen")

            details.setdefault(
                stix_id,
                {
                    "external_id": StixService._extract_external_id(obj),
                    "aliases": aliases,
                    "first_seen": raw_first_seen if isinstance(raw_first_seen, str) else None,
                    "last_seen": raw_last_seen if isinstance(raw_last_seen, str) else None,
                },
            )

        return details
