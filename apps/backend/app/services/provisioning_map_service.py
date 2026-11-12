"""ProvisioningMapService (NEW) — the per-campaign Provisioning_Map (Req. 8).

Phase 4.1 (design Component 12). This *session-bound* service derives, per
campaign, the recommended target environment (the ``Provisioning_Map``) from the
techniques/platforms of the campaign's abilities and persists it across the
two memories per the Memory_Rule (design §F):

* **Relational half (Req. 8.1, 8.2).** :meth:`ProvisioningMapService.derive`
  computes the recommended ``target_os`` (``linux`` | ``windows``) and a *fixed
  inventory* (images/services/topology) from the ``Ability`` rows of the
  campaign — reading each ability's ``technique_id`` and the ``platform`` of its
  executors — and persists exactly **one** ``provisioning_map`` row per campaign.
  Re-deriving updates that single row (``latest-wins`` upsert on the unique
  ``campaign_id``), so the derivation is idempotent (Req. 8.2).
* **Vector half (Req. 8.3).** The "environments that worked" associations are
  stored into ``experience_memory`` through
  :meth:`ExperienceMemory.store_affinity` with ``kind="environment_affinity"``,
  so similar target patterns can be retrieved by similarity later. That store is
  the *associative* half of the Memory_Rule and is **best-effort**: a
  Vector_Store/embedder outage never fails the committed relational write — the
  Relational_Store is the source of truth (same contract as the STIX vector
  indexer, Req. 8.3).

Stage 5 consults :meth:`derive` (or the persisted ``provisioning_map`` row) as
the source of the recommended environment (Req. 8.4); the wiring is additive and
does not change the existing provisioner flow.

Conventions carried over from the other Session-injected services
(``ExperienceMemoryBootstrap`` / ``ThreatIntelIngestService``): the ORM models
and the embedder are resolved lazily inside the methods, so importing this
module performs no I/O and has no circular import with :mod:`app.models`
(Req. 1.7, 9.*). The service does not open or close the ``Session``; it
``flush``es the relational row and leaves the ``commit`` to the caller so the
map persistence can share the caller's unit of work.

_Requirements: 8.1, 8.2, 8.3, 8.4_
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory

logger = logging.getLogger(__name__)

# The two target operating systems the Provisioning_Map derives (design
# Component 12 / the ``image_catalog``/``provisioning_map`` ``target_os``
# column): ``linux`` | ``windows``.
TARGET_OS_LINUX = "linux"
TARGET_OS_WINDOWS = "windows"

# Executor platform tokens (``abilities.executors[].platform``) that map to the
# Windows target OS. Everything else (``linux``, ``darwin``, ``nix``, unknown,
# …) resolves to the default Linux target, so the derivation is **total**: every
# campaign gets a ``target_os`` in ``{linux, windows}`` (design Property 21).
_WINDOWS_PLATFORM_TOKENS = frozenset({"windows", "win", "win32", "win64"})

# Default recommended image per target OS — the fixed inventory's base image.
# A small, deterministic default so the inventory is reproducible; it is the
# *recommended* base, not a provisioning decision (the ImageResolver of task 4.2
# refines concrete images — this map is the per-campaign recommendation).
_DEFAULT_IMAGE_BY_OS = {
    TARGET_OS_LINUX: "ubuntu:22.04",
    TARGET_OS_WINDOWS: "mcr.microsoft.com/windows/servercore:ltsc2022",
}


@dataclass(frozen=True)
class ProvisioningMap:
    """Typed result of :meth:`ProvisioningMapService.derive` (design Component 12).

    A small, typed DTO mirroring the persisted ``provisioning_map`` row (Req.
    8.2): the campaign it was derived for, the recommended ``target_os``
    (``linux`` | ``windows``) and the fixed inventory ``content``
    (images/services/topology). The ``row_id`` is the primary key of the
    persisted row so the caller (e.g. Stage 5) can correlate the DTO with the
    relational row it reads (Req. 8.4).
    """

    campaign_id: str
    target_os: str
    content: dict[str, Any] = field(default_factory=dict)
    row_id: Optional[int] = None


class ProvisioningMapService:
    """Derive and persist the per-campaign Provisioning_Map (design Component 12).

    Session-injected; the :class:`ExperienceMemory` is injected so the
    ``environment_affinity`` rows share the request-scoped unit of work and the
    same embedder as the rest of the Vector_Store (matching
    :class:`ExperienceMemoryBootstrap` / the STIX vector indexer wiring). See the
    module docstring for the full contract (Req. 8.1–8.4).
    """

    def __init__(self, session: Session, *, memory: ExperienceMemory) -> None:
        self._session = session
        self._memory = memory

    # ---- public API --------------------------------------------------------

    def derive(self, campaign_id: str) -> ProvisioningMap:
        """Compute, persist and return the Provisioning_Map of ``campaign_id``.

        Reads the campaign's ``Ability`` rows and derives the recommended
        ``target_os`` and a fixed inventory (images/services/topology) from their
        techniques/platforms (Req. 8.1). Persists exactly **one**
        ``provisioning_map`` row per campaign — updating the existing row when
        present, creating it otherwise (``latest-wins`` upsert on the unique
        ``campaign_id``), so re-deriving never duplicates the row (Req. 8.2).

        Stores the "environments that worked" association into
        ``experience_memory`` (``kind="environment_affinity"``) for
        similarity retrieval (Req. 8.3); that vector write is **best-effort** and
        never fails the relational derivation.

        Returns a typed :class:`ProvisioningMap` DTO mirroring the persisted row
        (the source Stage 5 consults — Req. 8.4). The relational row is
        ``flush``ed into the session; the ``commit`` is the caller's, so the
        derivation can share the caller's unit of work.
        """
        techniques, platforms = self._collect_techniques_and_platforms(campaign_id)
        target_os = self._derive_target_os(platforms)
        content = self._build_inventory(target_os, techniques, platforms)

        row = self._persist_map(campaign_id, target_os, content)

        # (Req. 8.3) best-effort vector association: a Vector_Store/embedder
        # outage must never fail the committed relational Provisioning_Map.
        self._store_environment_affinity(campaign_id, target_os, techniques, platforms)

        return ProvisioningMap(
            campaign_id=campaign_id,
            target_os=target_os,
            content=content,
            row_id=row.id,
        )

    # ---- relational derivation + persistence (Req. 8.1, 8.2) ---------------

    def _collect_techniques_and_platforms(
        self, campaign_id: str
    ) -> tuple[list[str], list[str]]:
        """Read the sorted (techniques, platforms) the campaign's abilities exercise.

        Collects each ability's ``technique_id`` and the ``platform`` of each of
        its executors (``abilities.executors`` is a portable JSON list of
        ``{name, platform, command}``), de-duplicated and sorted so the derived
        inventory is deterministic. Malformed executor entries degrade defensively
        to being ignored so a single bad row never aborts the derivation.
        """
        from app.models.domain import Ability as AbilityRow

        abilities = (
            self._session.execute(
                select(AbilityRow)
                .where(AbilityRow.campaign_id == campaign_id)
                .order_by(AbilityRow.ability_id)
            )
            .scalars()
            .all()
        )

        techniques: set[str] = set()
        platforms: set[str] = set()
        for ability in abilities:
            technique_id = self._clean(getattr(ability, "technique_id", None))
            if technique_id:
                techniques.add(technique_id)
            for executor in self._iter_executors(getattr(ability, "executors", None)):
                platform = self._clean(executor.get("platform"))
                if platform:
                    platforms.add(platform)

        return sorted(techniques), sorted(platforms)

    @classmethod
    def _derive_target_os(cls, platforms: list[str]) -> str:
        """Resolve the recommended ``target_os`` from the ability platforms (Req. 8.1).

        Total and coherent with the platforms (design Property 21): returns
        :data:`TARGET_OS_WINDOWS` when the campaign's abilities target Windows
        (any executor platform in :data:`_WINDOWS_PLATFORM_TOKENS`) and no
        non-Windows platform dominates; otherwise defaults to
        :data:`TARGET_OS_LINUX`. The decision is a simple majority of the
        OS-mapped platforms, with Linux as the stable default when there is no
        platform signal or a tie — so every campaign gets a value in
        ``{linux, windows}``.
        """
        windows_votes = 0
        linux_votes = 0
        for platform in platforms:
            if platform.lower() in _WINDOWS_PLATFORM_TOKENS:
                windows_votes += 1
            else:
                # Every non-Windows platform token (linux/darwin/nix/unknown)
                # counts toward the Linux default — the lab provisions Linux
                # containers unless Windows clearly dominates.
                linux_votes += 1
        if windows_votes > linux_votes:
            return TARGET_OS_WINDOWS
        return TARGET_OS_LINUX

    @classmethod
    def _build_inventory(
        cls, target_os: str, techniques: list[str], platforms: list[str]
    ) -> dict[str, Any]:
        """Build the fixed inventory (images/services/topology) stored as JSON (Req. 8.2).

        Deterministic given the derived ``target_os`` and the sorted
        techniques/platforms, so re-deriving an unchanged campaign writes the exact
        same ``content`` (idempotency — Req. 8.2). The inventory is the per-campaign
        *recommendation* the Stage 5 provisioner consults (Req. 8.4), not a
        provisioning decision.
        """
        image = _DEFAULT_IMAGE_BY_OS.get(target_os, _DEFAULT_IMAGE_BY_OS[TARGET_OS_LINUX])
        return {
            "target_os": target_os,
            "images": [image],
            "services": [],
            "topology": {
                "target_os": target_os,
                "nodes": [{"role": "target", "image": image}],
            },
            "derived_from": {
                "techniques": techniques,
                "platforms": platforms,
            },
        }

    def _persist_map(
        self, campaign_id: str, target_os: str, content: dict[str, Any]
    ) -> Any:
        """Upsert the single ``provisioning_map`` row for the campaign (Req. 8.2).

        Keeps **one row per campaign** (``latest-wins`` upsert on the unique
        ``campaign_id`` — the same upsert contract as ``ContainerEmulability``):
        updates the existing row when present, creates it otherwise. Only
        ``add``s/updates the row and ``flush``es it; the ``commit`` is the
        caller's, so the derivation shares the caller's unit of work.
        """
        from app.models.domain import ProvisioningMap as ProvisioningMapRow

        row = (
            self._session.execute(
                select(ProvisioningMapRow)
                .where(ProvisioningMapRow.campaign_id == campaign_id)
                .order_by(ProvisioningMapRow.id.asc())
            )
            .scalars()
            .all()
        )
        existing = row[-1] if row else None

        if existing is None:
            existing = ProvisioningMapRow(campaign_id=campaign_id)
            self._session.add(existing)
        existing.target_os = target_os
        existing.content = content
        self._session.flush()
        return existing

    # ---- vector association (Req. 8.3) -------------------------------------

    def _store_environment_affinity(
        self,
        campaign_id: str,
        target_os: str,
        techniques: list[str],
        platforms: list[str],
    ) -> None:
        """Store the "environment that worked" association into ``experience_memory``.

        Writes one ``environment_affinity`` row capturing the recommended target
        environment pattern (target OS + techniques + platforms) through
        :meth:`ExperienceMemory.store_affinity`, so similar patterns can be
        retrieved by similarity (Req. 8.3). Idempotent via the affinity's
        deterministic dedup key.

        **Best-effort:** a Vector_Store/embedder outage (no embedder injected,
        pgvector unreachable, …) is logged and swallowed so it never fails the
        already-derived relational Provisioning_Map — the Relational_Store is the
        source of truth (Req. 8.3, mirroring the STIX vector indexer contract).
        """
        content = (
            f"campaign {campaign_id} recommended environment: target_os "
            f"{target_os}; techniques {', '.join(techniques)}; platforms "
            f"{', '.join(platforms)}"
        )
        try:
            self._memory.store_affinity(
                kind="environment_affinity",
                content=content,
                metadata={
                    "source": "provisioning_map",
                    "campaign_id": campaign_id,
                    "target_os": target_os,
                    "techniques": techniques,
                    "platforms": platforms,
                },
            )
        except Exception as exc:  # noqa: BLE001 - vector write is best-effort
            logger.warning(
                "Environment affinity not stored for campaign %s (%s: %s); the "
                "relational Provisioning_Map is unaffected.",
                campaign_id,
                type(exc).__name__,
                exc,
            )

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _iter_executors(executors: Any) -> list[dict[str, Any]]:
        """Yield the ``{name, platform, command}`` executor dicts of an ability.

        The ``abilities.executors`` column is a portable JSON list of executor
        mappings; defensively ignore anything that is not a list of dicts so a
        malformed row never aborts the derivation (same helper as
        :class:`ExperienceMemoryBootstrap`).
        """
        if not isinstance(executors, list):
            return []
        return [item for item in executors if isinstance(item, dict)]

    @staticmethod
    def _clean(value: Any) -> str:
        """Trim a string-ish value to a stable token; empty string for non-strings."""
        if not isinstance(value, str):
            return ""
        return value.strip()


__all__ = [
    "ProvisioningMapService",
    "ProvisioningMap",
    "TARGET_OS_LINUX",
    "TARGET_OS_WINDOWS",
]
