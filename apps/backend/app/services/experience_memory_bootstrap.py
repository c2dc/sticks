"""ExperienceMemoryBootstrap — prime the pgvector RAG with associative knowledge.

Phase 3.5 (design Component 9, Req. 6.1, 6.2). The ``experience_memory`` pgvector
store ships empty — the RAG has never been bootstrapped — so the AI_Graph has no
cross-campaign experience to prime its proposals. This service populates it, once
and idempotently, with the *associative* half of the Memory_Rule (design §F):

* **Patterns from the 8 curated cases.** The curated content was already ingested
  into the relational tables by :class:`CuratedIngestService`
  (``campaigns``/``abilities``/``adversaries``/``dags``). This bootstrap reads that
  curated content **from the database** (never from disk) and derives associative
  rows from it: one ``command_affinity`` per concrete executor command of each
  curated ability (technique↔command↔tool/platform), and one
  ``environment_affinity`` per curated campaign capturing the target-environment
  pattern its techniques/platforms exercised (Req. 6.1).
* **Command↔tool affinities.** These are normally populated by
  :class:`AtomicRedTeamIngestService` (task 11.1), which writes ``command_affinity``
  rows through the same :meth:`ExperienceMemory.store_affinity` primitive. The
  bootstrap relies on that ingest for the broad Atomic Red Team coverage and only
  adds the curated-derived affinities above, so it never duplicates the Atomic
  index.

Idempotency (Req. 6.2): every row is stored through
:meth:`ExperienceMemory.store_affinity`, which deduplicates on a deterministic
``metadata.dedup_key`` (SHA-256 of the stored ``content``) checked before insert.
Running :meth:`bootstrap` twice therefore produces exactly the same set of rows as
running it once — the second pass counts every entry under ``skipped_existing``.
Every stored entry carries an embedding (``nomic-embed-text`` in production via the
injected embedder).

The Memory_Rule boundary (Req. 6.3, 6.4) is respected: this service writes only
associative knowledge into the vector store; the exact/factual curated data stays
in the relational tables it reads from. Session-injected, embedder lazy — importing
this module performs no I/O (the ORM models are resolved lazily inside the methods),
matching the other Session-injected services (Req. 1.7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory

logger = logging.getLogger(__name__)

# Provenance recorded on the metadata of every affinity derived by this
# bootstrap, distinguishing curated-derived rows from the Atomic Red Team ones.
_SOURCE = "curated_bootstrap"


@dataclass(frozen=True)
class BootstrapSummary:
    """Outcome of an :meth:`ExperienceMemoryBootstrap.bootstrap` run.

    ``inserted`` counts the new ``experience_memory`` rows created this run;
    ``skipped_existing`` counts the equivalent entries already present (a
    deterministic dedup-key hit). A second bootstrap of an unchanged corpus
    reports ``inserted == 0`` with every entry under ``skipped_existing`` — the
    idempotency guarantee of Req. 6.2.
    """

    inserted: int = 0
    skipped_existing: int = 0


class ExperienceMemoryBootstrap:
    """Populate ``experience_memory`` with initial associative knowledge.

    Session-injected; the :class:`ExperienceMemory` is injected so the bootstrap
    shares the request-scoped unit of work and the same embedder. See the module
    docstring for the full contract (Req. 6.1, 6.2).
    """

    def __init__(self, session: Session, *, memory: ExperienceMemory) -> None:
        self._session = session
        self._memory = memory

    # ---- public API --------------------------------------------------------

    def bootstrap(self) -> BootstrapSummary:
        """Populate the experience memory from the curated corpus, idempotently.

        Reads the curated content from the relational tables (``campaigns``,
        ``abilities``, ``adversaries``) and stores associative rows via
        :meth:`ExperienceMemory.store_affinity`: ``command_affinity`` per concrete
        executor command and ``environment_affinity`` per curated campaign
        (Req. 6.1). Deduplicated by ``metadata.dedup_key`` so running it twice
        equals running it once (Req. 6.2). Returns the insert/skip counts.
        """
        from app.models.domain import Case as CampaignRow

        inserted = 0
        skipped = 0

        campaigns = (
            self._session.execute(select(CampaignRow).order_by(CampaignRow.id))
            .scalars()
            .all()
        )
        for campaign in campaigns:
            ins, skp = self._bootstrap_campaign(campaign.id)
            inserted += ins
            skipped += skp

        return BootstrapSummary(inserted=inserted, skipped_existing=skipped)

    # ---- per-campaign derivation ------------------------------------------

    def _bootstrap_campaign(self, campaign_id: str) -> tuple[int, int]:
        """Store the associative rows derived from one curated campaign."""
        from app.models.domain import Ability as AbilityRow

        inserted = 0
        skipped = 0

        abilities = (
            self._session.execute(
                select(AbilityRow)
                .where(AbilityRow.campaign_id == campaign_id)
                .order_by(AbilityRow.ability_id)
            )
            .scalars()
            .all()
        )

        # (a) command↔tool affinities: one per concrete executor command.
        for ability in abilities:
            for executor in self._iter_executors(ability.executors):
                command = self._clean(executor.get("command"))
                if not command:
                    continue
                technique_id = self._clean(ability.technique_id) or "unknown"
                tool = self._clean(executor.get("name")) or "unknown"
                platform = self._clean(executor.get("platform")) or "unknown"
                content = (
                    f"technique {technique_id} executor {tool} "
                    f"platform {platform}: {command}"
                )
                if self._memory.store_affinity(
                    kind="command_affinity",
                    content=content,
                    metadata={
                        "source": _SOURCE,
                        "campaign_id": campaign_id,
                        "technique_id": technique_id,
                        "command": command,
                        "executor": tool,
                        "platform": platform,
                    },
                ):
                    inserted += 1
                else:
                    skipped += 1

        # (b) an environment affinity capturing the target-environment pattern
        # (the set of techniques/platforms) the curated campaign exercised.
        env_content = self._environment_content(campaign_id, abilities)
        if env_content is not None:
            techniques, platforms = env_content
            content = (
                f"campaign {campaign_id} target environment: techniques "
                f"{', '.join(techniques)}; platforms {', '.join(platforms)}"
            )
            if self._memory.store_affinity(
                kind="environment_affinity",
                content=content,
                metadata={
                    "source": _SOURCE,
                    "campaign_id": campaign_id,
                    "techniques": techniques,
                    "platforms": platforms,
                },
            ):
                inserted += 1
            else:
                skipped += 1

        return inserted, skipped

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _iter_executors(executors: Any) -> list[dict[str, Any]]:
        """Yield the ``{name, platform, command}`` executor dicts of an ability.

        The ``abilities.executors`` column is a portable JSON list of executor
        mappings (see :class:`CuratedIngestService`); defensively ignore anything
        that is not a list of dicts so a malformed row never aborts the bootstrap.
        """
        if not isinstance(executors, list):
            return []
        return [item for item in executors if isinstance(item, dict)]

    @classmethod
    def _environment_content(
        cls, campaign_id: str, abilities: list[Any]
    ) -> tuple[list[str], list[str]] | None:
        """Derive the sorted (techniques, platforms) a curated campaign exercised.

        Returns ``None`` when the campaign has no curated techniques/platforms, so
        no empty environment affinity is stored.
        """
        techniques: set[str] = set()
        platforms: set[str] = set()
        for ability in abilities:
            technique_id = cls._clean(getattr(ability, "technique_id", None))
            if technique_id:
                techniques.add(technique_id)
            for executor in cls._iter_executors(getattr(ability, "executors", None)):
                platform = cls._clean(executor.get("platform"))
                if platform:
                    platforms.add(platform)
        if not techniques and not platforms:
            return None
        return sorted(techniques), sorted(platforms)

    @staticmethod
    def _clean(value: Any) -> str:
        """Trim a string-ish value to a stable token; empty string for non-strings."""
        if not isinstance(value, str):
            return ""
        return value.strip()
