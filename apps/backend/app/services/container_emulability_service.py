"""ContainerEmulabilityService (NEW) — the per-campaign Container_Emulability (Req. 6).

Task 12.2 of the ``agentic-phase-model-refactor`` spec (design Component 5 /
DD5). This *session-bound* service derives, per campaign, whether **all** of its
techniques are emulable in a container (or dispensable) and persists that
verdict idempotently, so the campaign list can show the container icon only on
campaigns that are 100% container-emulable (Req. 6.4, 6.5).

Contract (design Component 5, Req. 6.2, 6.3):

* **Read the campaign techniques.** :meth:`ContainerEmulabilityService.derive`
  reads the ``Ability`` rows of the campaign and collects their
  ``technique_id`` values — the same relational source the
  :class:`~app.services.provisioning_map_service.ProvisioningMapService` reads,
  keyed by the string ``campaign_id`` (FK to ``campaigns.id``).
* **Look up each technique's classification.** Each collected technique id is
  matched against its :class:`~app.models.domain.TechniqueEmulability` row
  (``container`` | ``dispensable`` | ``not_container`` —
  :class:`~app.models.enums.TechniqueEmulabilityKind`).
* **Derive the verdict.** ``container_emulable`` is ``True`` **iff** every
  technique of the campaign is ``container`` or ``dispensable`` (Req. 6.2). Any
  technique that is ``not_container`` — **or that has no classification row at
  all** — blocks container emulation and is listed in ``blocking_techniques``:
  an unclassified technique cannot be proven emulable, so soundness requires it
  to block the "all techniques emulable" verdict.
* **Persist idempotently.** The verdict is upserted into
  ``container_emulability`` keyed by the UNIQUE ``campaign_id`` (``latest-wins``
  upsert), so re-deriving updates the **same** row rather than duplicating it
  (Req. 6.3) — the same idempotency contract as ``ProvisioningMap``.

Conventions carried over from :class:`ProvisioningMapService` /
``ExperienceMemoryBootstrap``: the ORM models are resolved lazily inside the
methods so importing this module performs no I/O and has no circular import with
:mod:`app.models`; the service does not open or close the ``Session`` — it
``flush``es the relational row and leaves the ``commit`` to the caller so the
derivation can share the caller's unit of work.

_Requirements: 6.2, 6.3_
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import TechniqueEmulabilityKind

logger = logging.getLogger(__name__)

# The two classifications that do NOT block container emulation of a campaign:
# a technique emulable in a container, or one that needs no emulation at all
# (e.g. social engineering). A technique is "ok" iff its classification is one
# of these; ``not_container`` and unclassified techniques block (Req. 6.2).
_NON_BLOCKING_KINDS = frozenset(
    {
        TechniqueEmulabilityKind.CONTAINER,
        TechniqueEmulabilityKind.DISPENSABLE,
    }
)


@dataclass(frozen=True)
class ContainerEmulability:
    """Typed result of :meth:`ContainerEmulabilityService.derive` (design Component 5).

    A small, typed DTO mirroring the persisted ``container_emulability`` row
    (Req. 6.3): the campaign it was derived for, the boolean verdict, and the
    sorted list of blocking technique ids (the ``not_container`` / unclassified
    ones) that the UI surfaces as the accessible reason a non-emulable campaign
    lacks the container icon (Req. 6.5). ``blocking_techniques`` is empty exactly
    when ``container_emulable`` is ``True``. The ``row_id`` is the primary key of
    the persisted row so the caller can correlate the DTO with the relational
    row it reads.
    """

    campaign_id: str
    container_emulable: bool
    blocking_techniques: list[str] = field(default_factory=list)
    row_id: int | None = None


class ContainerEmulabilityService:
    """Derive and persist the per-campaign Container_Emulability (design Component 5).

    Session-injected; see the module docstring for the full contract
    (Req. 6.2, 6.3).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- public API --------------------------------------------------------

    def derive(self, campaign_id: str) -> ContainerEmulability:
        """Compute, persist and return the Container_Emulability of ``campaign_id``.

        Reads the campaign's techniques (Req. 6.2), classifies each against its
        :class:`TechniqueEmulability` row, and marks the campaign
        ``container_emulable`` **iff** every technique is ``container`` or
        ``dispensable``; otherwise lists the blocking techniques
        (``not_container`` or unclassified). Upserts exactly **one**
        ``container_emulability`` row per campaign (``latest-wins`` on the unique
        ``campaign_id``), so re-deriving never duplicates the row (Req. 6.3).

        Returns a typed :class:`ContainerEmulability` DTO mirroring the persisted
        row. The relational row is ``flush``ed into the session; the ``commit``
        is the caller's, so the derivation shares the caller's unit of work.
        """
        techniques = self._collect_techniques(campaign_id)
        classification = self._load_classification(techniques)

        # A technique blocks unless it is explicitly classified container or
        # dispensable; an unclassified technique blocks (cannot be proven
        # emulable). ``blocking_techniques`` is sorted for a deterministic,
        # stable verdict (idempotent re-derivation writes the same content).
        blocking = sorted(
            technique_id
            for technique_id in techniques
            if classification.get(technique_id) not in _NON_BLOCKING_KINDS
        )
        container_emulable = not blocking

        row = self._persist_emulability(campaign_id, container_emulable, blocking)

        return ContainerEmulability(
            campaign_id=campaign_id,
            container_emulable=container_emulable,
            blocking_techniques=blocking,
            row_id=row.id,
        )

    def read_many(self, campaign_ids: list[str]) -> dict[str, ContainerEmulability]:
        """Read the persisted Container_Emulability of each of ``campaign_ids``.

        A **read-only** batch lookup for the campaign-list API (Req. 6.4, 6.5):
        it returns the already-derived ``container_emulability`` rows keyed by
        ``campaign_id``, so the listing can surface the container-icon marking
        without re-deriving (the derivation is the one-off
        :meth:`derive`). Campaign ids with no persisted row are simply absent
        from the returned mapping, so the caller treats them as "not derived
        yet" (``container_emulable = None`` — the campaign is still listed,
        Req. 6.5). Returns an empty mapping for an empty input. This method
        performs no writes and leaves the session untouched.
        """
        wanted = sorted({self._clean(cid) for cid in campaign_ids} - {""})
        if not wanted:
            return {}

        from app.models.domain import ContainerEmulability as ContainerEmulabilityRow

        rows = (
            self._session.execute(
                select(ContainerEmulabilityRow)
                .where(ContainerEmulabilityRow.campaign_id.in_(wanted))
                .order_by(ContainerEmulabilityRow.id.asc())
            )
            .scalars()
            .all()
        )

        # One row per campaign is the invariant (UNIQUE campaign_id); iterating
        # in ascending id order keeps the last-wins semantics defensive even if
        # a duplicate ever slipped in.
        result: dict[str, ContainerEmulability] = {}
        for row in rows:
            blocking = list(row.blocking_techniques or [])
            result[row.campaign_id] = ContainerEmulability(
                campaign_id=row.campaign_id,
                container_emulable=row.container_emulable,
                blocking_techniques=blocking,
                row_id=row.id,
            )
        return result

    # ---- relational reads (Req. 6.2) ---------------------------------------

    def _collect_techniques(self, campaign_id: str) -> list[str]:
        """Read the sorted, de-duplicated technique ids the campaign exercises.

        Collects each ``Ability.technique_id`` of the campaign — the same
        relational source :class:`ProvisioningMapService` reads, keyed by the
        string ``campaign_id`` — de-duplicated and sorted so the derivation is
        deterministic. Blank/missing technique ids are ignored defensively so a
        malformed ability row never aborts the derivation.
        """
        from app.models.domain import Ability as AbilityRow

        abilities = (
            self._session.execute(
                select(AbilityRow.technique_id).where(AbilityRow.campaign_id == campaign_id)
            )
            .scalars()
            .all()
        )

        techniques: set[str] = set()
        for technique_id in abilities:
            cleaned = self._clean(technique_id)
            if cleaned:
                techniques.add(cleaned)
        return sorted(techniques)

    def _load_classification(self, techniques: list[str]) -> dict[str, TechniqueEmulabilityKind]:
        """Map each collected technique id to its ``TechniqueEmulability`` kind.

        Reads the :class:`TechniqueEmulability` rows for exactly the campaign's
        technique ids (a single ``IN`` query). Technique ids with no row are
        simply absent from the returned mapping, so :meth:`derive` treats them as
        blocking (unclassified cannot be proven emulable — Req. 6.2). Returns an
        empty mapping when the campaign has no techniques.
        """
        if not techniques:
            return {}

        from app.models.domain import TechniqueEmulability as TechniqueEmulabilityRow

        rows = (
            self._session.execute(
                select(TechniqueEmulabilityRow).where(
                    TechniqueEmulabilityRow.technique_id.in_(techniques)
                )
            )
            .scalars()
            .all()
        )
        return {row.technique_id: row.emulability for row in rows}

    # ---- persistence (Req. 6.3) --------------------------------------------

    def _persist_emulability(
        self, campaign_id: str, container_emulable: bool, blocking: list[str]
    ) -> Any:
        """Upsert the single ``container_emulability`` row for the campaign (Req. 6.3).

        Keeps **one row per campaign** (``latest-wins`` upsert on the unique
        ``campaign_id`` — the same contract as ``ProvisioningMap``): updates the
        existing row when present, creates it otherwise. ``blocking_techniques``
        is stored as a JSON list (``None`` when empty, i.e. when the campaign is
        container-emulable — Req. 6.5). Only ``add``s/updates the row and
        ``flush``es it; the ``commit`` is the caller's, so the derivation shares
        the caller's unit of work.
        """
        from app.models.domain import ContainerEmulability as ContainerEmulabilityRow

        existing = (
            self._session.execute(
                select(ContainerEmulabilityRow)
                .where(ContainerEmulabilityRow.campaign_id == campaign_id)
                .order_by(ContainerEmulabilityRow.id.asc())
            )
            .scalars()
            .first()
        )

        if existing is None:
            existing = ContainerEmulabilityRow(campaign_id=campaign_id)
            self._session.add(existing)
        existing.container_emulable = container_emulable
        # Null when the campaign is container-emulable (no blocking techniques),
        # a JSON list of the blocking technique ids otherwise (Req. 6.5).
        existing.blocking_techniques = blocking or None
        self._session.flush()
        return existing

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _clean(value: Any) -> str:
        """Trim a string-ish value to a stable token; empty string for non-strings."""
        if not isinstance(value, str):
            return ""
        return value.strip()


__all__ = [
    "ContainerEmulability",
    "ContainerEmulabilityService",
]
