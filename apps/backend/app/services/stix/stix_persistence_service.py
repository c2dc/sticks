"""StixPersistenceService — transactional, idempotent persistence of a STIX
package into Postgres (tasks 2.1–2.3).

This service is the single point in the system that writes the STIX tables. At
download time it derives the structured content of a bundle (type counts,
campaigns, techniques per campaign, actors and actor→campaign attributions) via
the existing :class:`~app.services.stix.stix_service.StixService` (parse reused,
never rewritten) and persists it inside a single transaction. Re-persisting a
version is idempotent: the row in ``stix_packages`` keyed by ``version`` is
reused and its derived content is replaced deterministically.

Design alignment
----------------
* Lives in the :mod:`app.services.stix` package next to
  :class:`StixService`, which it reuses for the parse.
* Takes a SQLAlchemy ``Session`` and an optional ``StixService`` on construction
  — the same pattern other session-scoped services in this backend follow (e.g.
  :class:`~app.services.session.session_state_service.SessionStateService`). The
  optional ``StixService`` keeps the single network egress mockable in tests
  (inject a ``StixService`` with an isolated ``data_dir`` and a fake
  ``http_client``).
* :class:`StixPackagePersistedResult` mirrors the design "Component 1" contract
  exactly, so the ``/api/stix/*`` handler maps 1:1.

This module defines the result DTO, the service, the transactional derivation
and write (``download_and_persist``), the idempotent upsert per version (reuse
the ``stix_packages`` row, delete-and-insert the derived child content) and the
on-demand backfill (``persist_from_disk``, no network egress). Both entry points
share the private ``_derive_and_write`` core, which wraps the write phase so a
failure during flush/insert/commit rolls the session back — leaving the package
unpersisted and the on-disk bundle intact — and re-raises for the API layer to
map to the ``falha_persistencia`` contract.

Vector indexing hook (task 16.1 / Req. 10.3)
--------------------------------------------
A ``Stix_Package_Version`` is a ``Pipeline_Input`` and must live in **both**
stores (Req. 10.1). This service remains the single writer of the exact,
relational half. The *semantic* half — indexing the version's campaigns into the
Vector_Store so an agent can retrieve STIX context by similarity (Req. 10.3) —
is delegated to an **optional, injected** :class:`StixVectorIndexer` invoked only
**after** the relational commit succeeds (so the index reads the just-committed
campaigns from the Relational_Store, the source of truth, never the deleted
bundle). The hook is additive and defensive: when no indexer is injected the
behaviour is exactly the relational-only one, and the hook never affects the
``persisted`` verdict of the committed write (Req. 10.6).

_Requirements: 1.1, 1.3, 1.4, 1.6, 2.1, 2.2, 2.3, 2.4, 4.6, 8.2, 8.4, 9.2, 10.1, 10.3_
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

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
    :meth:`StixPersistenceService.persist_from_disk`. ``downloaded`` reports
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
    """Download and persist a STIX package transactionally and idempotently.

    The single point that writes the STIX tables. It reuses an injected
    :class:`StixService` for the parse/derivation (defaulting to a fresh instance
    when none is supplied) and writes through the provided SQLAlchemy
    ``Session``.
    """

    def __init__(
        self,
        session: Session,
        stix_service: Optional[StixService] = None,
        *,
        vector_indexer: Optional[StixVectorIndexer] = None,
    ) -> None:
        self._session = session
        # Reuse the injected StixService (so tests can isolate the data dir and
        # mock the single network egress); otherwise build the default one.
        self._stix_service = stix_service if stix_service is not None else StixService()
        # Optional Vector_Store indexer invoked after a successful commit so the
        # persisted version is also a Pipeline_Input in the vector store
        # (Req. 10.1, 10.3). Keyword-only and defaulting to None so the existing
        # relational-only call sites and tests are unchanged and never need a real
        # embedder/pgvector.
        self._vector_indexer = vector_indexer

    def download_and_persist(self, version: str) -> StixPackagePersistedResult:
        """Download (if absent) and persist the package, idempotently.

        Reuses the bundle already on disk when present (so a re-download does not
        hit the network needlessly), otherwise triggers the single network egress
        via :meth:`StixService.download_base`. In both cases the structured
        content is derived from the file via :class:`StixService` and written in a
        single transaction (see :meth:`_derive_and_write`).

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            A :class:`StixPackagePersistedResult` with ``downloaded=True`` and
            ``persisted=True`` on success.
        """
        # Reuse the file on disk when already present; otherwise download it (the
        # single network egress, which validates the version and writes atomically).
        if not self._stix_service.base_path(version).exists():
            self._stix_service.download_base(version)

        return self._derive_and_write(version)

    def persist_from_disk(self, version: str) -> StixPackagePersistedResult:
        """Persist a bundle already on disk without a new network download.

        Used by the on-demand backfill (Req. 8.2): it never triggers a network
        egress (:meth:`StixService.download_base` is not called), it only derives
        the structured content from the file already present on disk and writes it
        through the shared transactional core in :meth:`_derive_and_write`. The
        read-only derivation (``read_bundle`` / ``dashboard`` / ``list_campaigns``
        / ``list_adversaries``) leaves the on-disk file intact (Req. 8.4).

        If the DB write fails after the bundle is available, the shared core rolls
        the session back — leaving the package unpersisted and the on-disk file
        untouched (Req. 9.2) — and re-raises so the API layer can map it to the
        ``falha_persistencia`` contract.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            A :class:`StixPackagePersistedResult` with ``downloaded=False`` (the
            bundle was not fetched — it was already present on disk) and
            ``persisted=True`` on success.
        """
        result = self._derive_and_write(version)
        # The bundle was already on disk — it was not (re)downloaded. Override the
        # ``downloaded`` flag that the shared core reports as True for the download
        # path so the backfill result reflects "present, not fetched".
        return result.model_copy(update={"downloaded": False})

    # ------------------------------------------------------------------
    # Transactional derivation + write (design "download com persistência")
    # ------------------------------------------------------------------

    def _derive_and_write(self, version: str) -> StixPackagePersistedResult:
        """Derive the structured content via ``StixService`` and write it.

        Derives, from the bundle on disk, the per-type counts
        (:meth:`StixService.dashboard`), the campaigns
        (:meth:`StixService.list_campaigns`), the techniques a campaign uses and
        the actor→campaign attributions (:meth:`StixService.list_adversaries`),
        and persists ``stix_packages`` + ``stix_type_counts`` + ``stix_campaigns``
        + ``stix_campaign_techniques`` + ``stix_actors`` + ``stix_actor_campaigns``
        inside a single transaction, committing once (Req. 1.4). The parse is
        entirely reused from :class:`StixService` — never rewritten here.

        The write is idempotent per ``version`` (Req. 2.1–2.4): when a
        ``stix_packages`` row already exists for ``version`` it is reused (not
        duplicated), its ``downloaded_at`` is refreshed via :func:`_utcnow` and
        its ``source_file`` is updated, and all derived child content for that
        package is cleared and re-inserted (delete-and-insert) within the same
        transaction. Re-persisting the same bundle therefore yields identical DB
        content with no duplicate rows (``f(x) == f(f(x))``). This method is the
        shared core both ``download_and_persist`` and ``persist_from_disk`` build
        on.
        """
        # --- Derivation via StixService (parse reused, never rewritten) -----
        dashboard = self._stix_service.dashboard(version)
        campaigns = self._stix_service.list_campaigns(version)
        adversaries = self._stix_service.list_adversaries(version)
        bundle = self._stix_service.read_bundle(version)

        # Techniques a campaign uses, keyed by the campaign stix_id, derived from
        # the "uses" relationships (campaign source_ref → attack-pattern target_ref).
        techniques_by_campaign = self._derive_campaign_techniques(bundle)
        # Rich per-campaign fields (external_id, aliases, first/last seen) read
        # straight from the bundle so stix_campaigns rows are fully populated.
        details_by_campaign = self._derive_campaign_details(bundle)

        # --- Single transaction --------------------------------------------
        # Wrap the whole write phase (insert/flush/commit) so that any failure
        # after the bundle is available rolls the session back — leaving the
        # package unpersisted and the on-disk file untouched (Req. 8.2, 9.2) —
        # and re-raises for the API layer to map to ``falha_persistencia``. The
        # read-only derivation above never mutates the session, so it stays out
        # of the rollback scope.
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
            # A failed flush/insert/commit leaves the transaction in an unknown
            # state; roll it back so no partial package rows survive, then let the
            # error propagate unchanged. The on-disk bundle is preserved — the
            # deletion below runs only on the committed (success) path (Req. 1.2,
            # 7.2).
            self._session.rollback()
            raise
        # Commit confirmed: the database is now the source of truth for this
        # version. Index the just-committed campaigns into the Vector_Store so the
        # version is a Pipeline_Input in both stores (Req. 10.1, 10.3). The index
        # reads the committed campaigns from the Relational_Store — never the
        # bundle — so it runs before the on-disk deletion below but would work
        # regardless of it. It is best-effort and never affects the ``persisted``
        # verdict of the committed write (Req. 10.6).
        self._index_into_vector_store(version)
        # Delete the on-disk bundle best-effort (Req. 1.1, 6.3). This runs before
        # ``persist_from_disk`` overrides ``downloaded`` and never affects
        # ``persisted`` (Req. 2.1).
        self._delete_bundle_file(version)
        return result

    def _index_into_vector_store(self, version: str) -> None:
        """Index the committed version into the Vector_Store (best-effort, Req. 10.3).

        No-op when no :class:`StixVectorIndexer` was injected, so relational-only
        callers (and their tests) keep the exact previous behaviour with no
        embedder/pgvector required. When an indexer is present it is invoked via
        its degrade-safe ``index_version_safe``, so a Vector_Store outage never
        propagates to the already-committed relational write (Req. 10.6).
        """
        if self._vector_indexer is None:
            return
        self._vector_indexer.index_version_safe(version)

    def _delete_bundle_file(self, version: str) -> None:
        """Delete the on-disk Bundle_File after a successful commit (best-effort).

        Resolves the bundle path via :meth:`StixService.base_path` and unlinks it
        with ``missing_ok=True``. Never raises and never affects persistence: a
        deletion failure is logged as a diagnostic and swallowed, so a committed
        version is never considered unavailable because of on-disk residue
        (Req. 2.1, 2.2, 2.3, 7.3).
        """
        path = self._stix_service.base_path(version)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001 — deletion is best-effort.
            logger.warning(
                "best-effort deletion of STIX bundle v%s at %s failed: %s",
                version,
                path,
                exc,
            )

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
        source_file = str(self._stix_service.base_path(version))
        # Reuse the existing row keyed by the UNIQUE ``version`` when present so
        # a re-persistence does not duplicate it (Req. 2.1); otherwise insert a
        # fresh one. ``downloaded_at`` is refreshed on every (re-)persistence
        # (Req. 2.4) and ``source_file`` is kept current.
        package = self._session.execute(
            select(StixPackage).where(StixPackage.version == version)
        ).scalar_one_or_none()
        if package is None:
            package = StixPackage(
                version=version,
                name=f"Enterprise ATT&CK v{version}",
                downloaded_at=_utcnow(),
                source_file=source_file,
            )
            self._session.add(package)
        else:
            # Clear the previously derived child content for this package before
            # re-inserting it, so re-persisting the same version is deterministic
            # and leaves no duplicate rows (Req. 2.2).
            self._clear_package_content(package.id)
            package.name = f"Enterprise ATT&CK v{version}"
            package.downloaded_at = _utcnow()
            package.source_file = source_file
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
        self._session.query(StixTypeCount).filter(
            StixTypeCount.package_id == package_id
        ).delete(synchronize_session=False)
        self._session.query(StixCampaign).filter(
            StixCampaign.package_id == package_id
        ).delete(synchronize_session=False)
        self._session.query(StixActor).filter(
            StixActor.package_id == package_id
        ).delete(synchronize_session=False)

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

            tactic: Optional[str] = None
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
                    "first_seen": raw_first_seen
                    if isinstance(raw_first_seen, str)
                    else None,
                    "last_seen": raw_last_seen
                    if isinstance(raw_last_seen, str)
                    else None,
                },
            )

        return details
