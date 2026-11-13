"""StixVectorIndexService — index a persisted ``Stix_Package_Version`` into the
Vector_Store (task 16.1 / Fase 3.8).

A ``Stix_Package_Version`` is the ``Pipeline_Input`` of the whole emulation
pipeline and must live in **both** stores (Req. 10.1): the exact/structured data
in the Relational_Store (written by
:class:`~app.services.stix.stix_persistence_service.StixPersistenceService`, the
single source of exact data) **and** a semantic projection in the Vector_Store
(``experience_memory`` pgvector) so an agent can retrieve STIX context by
cosine similarity (Req. 10.3, 10.6).

This service owns the *vector* half only. It reads the already-persisted
campaigns of a version from the Relational_Store via
:class:`~app.services.stix.stix_package_read_service.StixPackageReadService`
(the bundle is never kept; it only exists in memory during ingestion) and stores one associative row per campaign through
:meth:`ExperienceMemory.store_affinity`, carrying ``metadata.kind ==
"stix_context"``.

Memory_Rule alignment (design §F)
---------------------------------
Per-campaign STIX context (identity, aliases, description, the techniques and
actors a campaign uses) is *associative* knowledge primed for similarity
retrieval, not an exact fact to be queried by key — so it belongs in the
Vector_Store, consistent with the two-memory rule. The exact rows stay in the
Relational_Store; this index is a semantic companion, never a replacement
(Req. 10.6: retrieval combines relational + vector).

A dedicated ``"stix_context"`` kind (added to
:data:`app.services.ai.memory.AFFINITY_KINDS`) distinguishes these rows from
command/environment affinities and run outcomes, so the three existing kinds
keep their meaning and STIX context is independently filterable.

Idempotency per ``version`` (Req. 10.5)
---------------------------------------
Each campaign's row carries a deterministic ``metadata.dedup_key`` that
**incorporates the version** (``stix_context|{version}|{stix_id}``). Re-indexing
the same version is a no-op per campaign (``store_affinity`` checks the
dedup_key before insert), so re-persisting a version produces the same set of
vector rows as indexing it once — mirroring the relational idempotency the
persistence service already guarantees.

Importing this module performs no I/O: the read service and the memory are the
only collaborators and both resolve their backends lazily, so there is no
network/DB access at import (Req. 1.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory
from app.services.stix.stix_package_read_service import (
    StixCampaignOverview,
    StixPackageNotPersistedError,
    StixPackageReadService,
)

logger = logging.getLogger(__name__)

# Provenance recorded on every row indexed by this service and the ``kind`` used
# to tag them in ``experience_memory.metadata`` (one of AFFINITY_KINDS).
_KIND = "stix_context"
_SOURCE = "stix_package_version"


@dataclass(frozen=True)
class StixIndexSummary:
    """Outcome of an :meth:`StixVectorIndexService.index_version` run.

    ``campaigns`` counts the persisted campaigns considered for indexing;
    ``indexed`` the new ``stix_context`` rows inserted (a re-index leaves this at
    0 for the unchanged campaigns, Req. 10.5); ``skipped`` the campaigns whose
    row already existed in the Vector_Store.
    """

    version: str
    campaigns: int = 0
    indexed: int = 0
    skipped: int = 0


class StixVectorIndexService:
    """Index a persisted ``Stix_Package_Version`` into the Vector_Store.

    Session-injected; the Experience_Memory is injected so the ``stix_context``
    rows share the request-scoped unit of work and use the same embedder the
    rest of the Vector_Store uses. A :class:`StixPackageReadService` may be
    injected for the relational reads (defaulting to one built over the same
    session), so the exact data is always read from the Relational_Store — the
    source of truth — and never from a bundle.
    """

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        read_service: StixPackageReadService | None = None,
    ) -> None:
        self._session = session
        self._memory = memory
        self._read_service = read_service or StixPackageReadService(session)

    def index_version(self, version: str) -> StixIndexSummary:
        """Index every persisted campaign of ``version`` into the Vector_Store.

        Reads the campaigns of the persisted package from the Relational_Store
        (Req. 10.6: the exact data stays relational) and stores one
        ``stix_context`` associative row per campaign through
        :meth:`ExperienceMemory.store_affinity`, embedding a human-readable
        snippet of the campaign's identity, description and techniques so an
        agent can retrieve STIX context by similarity (Req. 10.3).

        Idempotent per ``version`` (Req. 10.5): each row's ``dedup_key``
        incorporates the version and the campaign ``stix_id``, so re-indexing the
        same version inserts nothing new (and commits nothing).

        The new ``stix_context`` rows are committed here so the Vector_Store half
        is durable even though the relational commit already happened upstream and
        the request session is not committed again.

        Args:
            version: The ATT&CK Enterprise version (e.g. ``"18.0"``).

        Returns:
            A :class:`StixIndexSummary` with the campaign/indexed/skipped counts.

        Raises:
            StixPackageNotPersistedError: when ``version`` has no persisted
                package — the relational write must happen first (this service
                only indexes already-persisted content).
        """
        summaries = self._read_service.list_campaigns(version)

        indexed = 0
        skipped = 0
        for summary in summaries:
            overview = self._read_service.campaign_overview(version, summary.campaign_key)
            content = self._build_content(version, overview)
            dedup_key = self._dedup_key(version, overview.stix_id)
            inserted = self._memory.store_affinity(
                kind=_KIND,
                content=content,
                dedup_key=dedup_key,
                metadata={
                    "source": _SOURCE,
                    "version": version,
                    "campaign_key": overview.campaign_key,
                    "stix_id": overview.stix_id,
                    "campaign_name": overview.name,
                },
            )
            if inserted:
                indexed += 1
            else:
                skipped += 1

        # The relational package was already committed by the persistence
        # service before this index runs; ``store_affinity`` only flushes the new
        # ``stix_context`` rows, so commit them here to make the Vector_Store half
        # durable for the version (the request-scoped session is not committed
        # again by the download route).
        if indexed:
            self._session.commit()

        return StixIndexSummary(
            version=version,
            campaigns=len(summaries),
            indexed=indexed,
            skipped=skipped,
        )

    def index_version_safe(self, version: str) -> StixIndexSummary | None:
        """Index ``version``, degrading gracefully if the Vector_Store is down.

        Wraps :meth:`index_version` so the vector indexing never breaks the
        (already committed) relational persistence it accompanies: a missing
        embedder/pgvector or any Vector_Store unavailability is logged and
        swallowed, returning ``None``. The exact data in the Relational_Store —
        the source of truth — is unaffected (Req. 10.2, 10.6). A
        :class:`StixPackageNotPersistedError` is also treated as a skip, since
        there is nothing persisted to index yet.
        """
        try:
            return self.index_version(version)
        except StixPackageNotPersistedError:
            logger.warning(
                "STIX vector indexing skipped for v%s: the version is not "
                "persisted in the Relational_Store.",
                version,
            )
            return None
        except Exception as exc:  # noqa: BLE001 -- degrade on any unavailability
            logger.warning(
                "STIX vector indexing skipped for v%s due to Vector_Store "
                "unavailability (%s: %s); the relational data is unaffected.",
                version,
                type(exc).__name__,
                exc,
            )
            return None

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _dedup_key(version: str, stix_id: str) -> str:
        """Deterministic dedup key per (version, campaign) — the idempotency key.

        Incorporates the ``version`` so each ``Stix_Package_Version`` indexes its
        own campaigns independently and re-indexing the same version collapses to
        the existing rows (Req. 10.5). Built from stable identifiers (not the
        content hash) so a benign re-rendering of the snippet never produces a
        duplicate row for the same campaign/version.
        """
        return f"{_KIND}|{version}|{stix_id}"

    @staticmethod
    def _build_content(version: str, overview: StixCampaignOverview) -> str:
        """Render the semantic snippet embedded for a campaign (Req. 10.3).

        Projects the campaign's identity, aliases, description and the ATT&CK
        techniques and actors it uses into a compact, human-readable string so
        the embedding captures the STIX context an agent would retrieve by
        similarity. Empty parts are omitted so the snippet stays focused.
        """
        parts: list[str] = [
            f"STIX campaign {overview.name} (version {version})",
        ]
        if overview.external_id:
            parts.append(f"external id {overview.external_id}")
        if overview.aliases:
            parts.append("aliases: " + ", ".join(overview.aliases))
        if overview.description:
            parts.append(overview.description)
        if overview.techniques:
            techniques = ", ".join(
                (
                    f"{technique.technique_id} {technique.technique_name}"
                    if technique.technique_name
                    else technique.technique_id
                )
                for technique in overview.techniques
            )
            parts.append("techniques: " + techniques)
        if overview.actors:
            actors = ", ".join(actor.name for actor in overview.actors)
            parts.append("actors: " + actors)
        return ". ".join(parts)
