"""Idempotent application of the curated seed (JSON files) through the ORM.

``apply_curated_seed(session)`` upserts the curated campaigns, abilities,
adversaries and attack flows by their natural keys:

* campaigns by ``campaigns.id`` (the slug);
* abilities by ``ability_id``; adversaries by ``adversaries.id``;
* attack flows by ``(campaign_id, origin=human_curation, identifier)``.

Rules:

* Missing rows are inserted; running twice never duplicates anything.
* Existing rows are refreshed only when they belong to the seed itself
  (``translation_source = human_curation``). Rows edited or created by the AI
  (any other source) are never overwritten.
* Seed flows are stored with ``origin = human_curation`` and no provenance.
* The seed files carry neither a ``campaign.curated`` flag nor an
  ``attack_flow.origin``: the applier always sets ``curated = True`` on campaigns
  and ``origin = human_curation`` on flows.
* ``campaign.stix_version`` / ``campaign.stix_campaign_key`` are validated by the
  loader but NOT stored: ``campaigns`` has no column for them (the binding to the
  STIX package comes from the seed and :mod:`app.seeds.manifest`).
* The function only flushes; the caller owns the transaction and the commit.
* An ``atomic_ordering`` entry without a matching ability is NOT an error (the
  ``outer_space`` adversary has one in the source data).

The large data module is imported lazily, inside the function.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import Ability, Adversary, AttackFlowRow, Case
from app.models.enums import FlowOrigin, TranslationSource


@dataclass
class KindSummary:
    inserted: int = 0
    updated: int = 0
    existing: int = 0


@dataclass
class SeedSummary:
    """Result of one application: counts per kind (no secrets, no payloads)."""

    campaigns: KindSummary = field(default_factory=KindSummary)
    abilities: KindSummary = field(default_factory=KindSummary)
    adversaries: KindSummary = field(default_factory=KindSummary)
    attack_flows: KindSummary = field(default_factory=KindSummary)

    @property
    def inserted(self) -> int:
        return sum(k.inserted for k in self._kinds())

    @property
    def existing(self) -> int:
        return sum(k.existing for k in self._kinds())

    def _kinds(self) -> list[KindSummary]:
        return [self.campaigns, self.abilities, self.adversaries, self.attack_flows]

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {
            name: {"inserted": k.inserted, "updated": k.updated, "existing": k.existing}
            for name, k in zip(
                ("campaigns", "abilities", "adversaries", "attack_flows"), self._kinds()
            )
        }


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _source(value: str) -> TranslationSource:
    return TranslationSource(value)


def _is_seed_owned(row: Any) -> bool:
    return row.translation_source == TranslationSource.HUMAN_CURATION


def _upsert(
    session: Session,
    summary: KindSummary,
    existing: Any,
    factory: Any,
    values: Mapping[str, Any],
    *,
    owned: bool,
) -> None:
    """Insert ``factory(**values)`` or refresh a seed-owned row; never touch others."""
    if existing is None:
        session.add(factory(**values))
        summary.inserted += 1
        return
    if not owned:
        summary.existing += 1
        return
    changed = False
    for key, value in values.items():
        if getattr(existing, key) != value:
            setattr(existing, key, value)
            changed = True
    if changed:
        summary.updated += 1
    else:
        summary.existing += 1


def _by_key(session: Session, model: Any, column: Any, keys: Sequence[str]) -> dict[str, Any]:
    if not keys:
        return {}
    rows = session.execute(select(model).where(column.in_(list(keys)))).scalars().all()
    return {getattr(row, column.key): row for row in rows}


def apply_curated_seed(session: Session, *, now: dt.datetime | None = None) -> SeedSummary:
    """Upsert the curated seed; flush only (the caller commits)."""
    from app.seeds.loader import load_all  # lazy: reads the JSON files

    cases = load_all()
    seed_campaigns = [c['campaign'] for c in cases]
    seed_abilities = [a for c in cases for a in c['abilities']]
    seed_adversaries = [c['adversary'] for c in cases]
    seed_flows = [c['attack_flow'] for c in cases]

    created_at = now or _utcnow()
    summary = SeedSummary()

    # Campaigns (parents first so the FKs resolve at flush time).
    campaigns = _by_key(session, Case, Case.id, [c["id"] for c in seed_campaigns])
    for item in seed_campaigns:
        row = campaigns.get(item["id"])
        _upsert(
            session,
            summary.campaigns,
            row,
            Case,
            {
                "id": item["id"],
                "name": item["name"],
                "description": item.get("description"),
                "translation_source": _source(item["translation_source"]),
                "curated": True,
            },
            owned=row is not None and _is_seed_owned(row),
        )
    session.flush()

    abilities = _by_key(session, Ability, Ability.ability_id, [a["ability_id"] for a in seed_abilities])
    for item in seed_abilities:
        row = abilities.get(item["ability_id"])
        _upsert(
            session,
            summary.abilities,
            row,
            Ability,
            {
                "ability_id": item["ability_id"],
                "campaign_id": item["campaign_id"],
                "name": item.get("name"),
                "tactic": item.get("tactic"),
                "technique_name": item.get("technique_name"),
                "technique_id": item.get("technique_id"),
                "description": item.get("description"),
                "executors": item.get("executors"),
                "translation_source": _source(item["translation_source"]),
            },
            owned=row is not None and _is_seed_owned(row),
        )

    adversaries = _by_key(session, Adversary, Adversary.id, [a["id"] for a in seed_adversaries])
    for item in seed_adversaries:
        row = adversaries.get(item["id"])
        _upsert(
            session,
            summary.adversaries,
            row,
            Adversary,
            {
                "id": item["id"],
                "campaign_id": item["campaign_id"],
                "name": item.get("name"),
                "description": item.get("description"),
                "atomic_ordering": list(item.get("atomic_ordering") or []),
                "translation_source": _source(item["translation_source"]),
            },
            owned=row is not None and _is_seed_owned(row),
        )

    _apply_flows(session, summary.attack_flows, seed_flows, created_at)
    session.flush()
    return summary


def _apply_flows(
    session: Session,
    summary: KindSummary,
    flows: Iterable[Mapping[str, Any]],
    created_at: dt.datetime,
) -> None:
    flows = list(flows)
    campaign_ids = sorted({f["campaign_id"] for f in flows})
    existing: dict[tuple[str, str], AttackFlowRow] = {}
    if campaign_ids:
        rows = session.execute(
            select(AttackFlowRow).where(
                AttackFlowRow.campaign_id.in_(campaign_ids),
                AttackFlowRow.origin == FlowOrigin.HUMAN_CURATION,
            )
        ).scalars()
        existing = {(r.campaign_id, r.identifier): r for r in rows}
    for item in flows:
        row = existing.get((item["campaign_id"], item["identifier"]))
        values = {
            "campaign_id": item["campaign_id"],
            "origin": FlowOrigin.HUMAN_CURATION,
            "identifier": item["identifier"],
            "label": item.get("label"),
            "content": item["content"],
            "provenance": None,
        }
        # Flows with the human_curation origin are the seed's own rows.
        _upsert(
            session,
            summary,
            row,
            lambda **kw: AttackFlowRow(created_at=created_at, **kw),
            values,
            owned=row is not None,
        )


__all__ = ["KindSummary", "SeedSummary", "apply_curated_seed"]
