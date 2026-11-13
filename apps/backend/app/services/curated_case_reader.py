"""Database readers for the curated data kept after the curated pipeline removal.

The curated campaigns survive as data (``campaigns.curated`` flag, ``abilities``,
``adversaries`` and ``attack_flows`` rows with ``human_curation`` origin). This
module holds the small, session-bound helpers that other features still need to
read that data, extracted from the removed curated pipeline services:

* :func:`resolve_flow_data_from_db` - persisted flow ``content`` as :class:`FlowData`.
* :func:`build_case_from_db` - full :class:`CaseData` (abilities, adversary, flow).
* :func:`upsert_case_row` - ensure the ``campaigns`` row exists (FK for flows).
* :func:`utcnow` - naive UTC timestamp (portable between PostgreSQL and SQLite).

Pure database access: no disk, no network, no AI.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import (
    Ability as AbilityModel,
)
from app.models.domain import (
    Adversary as AdversaryModel,
)
from app.models.domain import (
    AttackFlowRow,
)
from app.models.domain import (
    Case as CaseModel,
)
from app.models.enums import FlowOrigin
from app.services.curated_case_models import (
    AbilityData,
    AdversaryData,
    CaseData,
    Executor,
    FlowData,
)


def utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable between PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def upsert_case_row(session: Session, campaign: str, label: str | None) -> None:
    """Ensure the ``campaigns`` row exists to satisfy the ``attack_flows`` FK.

    Only fills ``name`` when the row is new (or has no name), so it never
    overwrites metadata that is already persisted.
    """
    db_case = session.get(CaseModel, campaign)
    if db_case is None:
        db_case = CaseModel(id=campaign, name=label or campaign)
        session.add(db_case)
    elif not db_case.name:
        db_case.name = label or campaign
    session.flush()


def flow_data_from_content(content: Any) -> FlowData:
    """Rebuild a :class:`FlowData` from a persisted ``attack_flows.content`` JSON."""
    payload = content if isinstance(content, dict) else {}
    return FlowData.model_validate(payload)


def resolve_flow_data_from_db(session: Session, campaign: str) -> FlowData | None:
    """Persisted flow of ``campaign`` as :class:`FlowData`, or ``None``.

    When several flows exist, the human-curation flow is preferred and, within an
    origin, the earliest by ``id`` (stable result).
    """
    row = (
        session.execute(
            select(AttackFlowRow)
            .where(AttackFlowRow.campaign_id == campaign)
            .order_by(
                (AttackFlowRow.origin == FlowOrigin.HUMAN_CURATION).desc(),
                AttackFlowRow.id.asc(),
            )
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    return flow_data_from_content(row.content)


def _executors_from_json(executors: Any) -> list[Executor]:
    if not isinstance(executors, list):
        return []
    return [
        Executor(
            name=entry.get("name"),
            platform=entry.get("platform"),
            command=entry.get("command"),
        )
        for entry in executors
        if isinstance(entry, dict)
    ]


def list_curated_names(session: Session) -> dict[str, str]:
    """Display names of the curated campaigns (``campaigns.curated`` flag), by slug."""
    rows = session.execute(
        select(CaseModel.id, CaseModel.name)
        .where(CaseModel.curated.is_(True))
        .order_by(CaseModel.id)
    ).all()
    return dict(rows)


def is_curated(session: Session, slug: str) -> bool:
    """Whether ``slug`` is a campaign flagged ``curated`` in the database."""
    flag = session.execute(
        select(CaseModel.curated).where(CaseModel.id == slug)
    ).scalar_one_or_none()
    return bool(flag)


def build_case_from_db(session: Session, slug: str) -> CaseData:
    """Assemble a full :class:`CaseData` from the database.

    Abilities are ordered by the Adversary's ``atomic_ordering`` when present;
    abilities not referenced by the ordering are appended by id so none are
    dropped. The display name comes from ``campaigns.name`` (fallback: the slug).
    """
    case_row = session.get(CaseModel, slug)
    name = case_row.name if case_row is not None and case_row.name else slug

    adversary_row = (
        session.execute(
            select(AdversaryModel)
            .where(AdversaryModel.campaign_id == slug)
            .order_by(AdversaryModel.id)
        )
        .scalars()
        .first()
    )
    rows = (
        session.execute(select(AbilityModel).where(AbilityModel.campaign_id == slug))
        .scalars()
        .all()
    )
    by_id = {r.ability_id: r for r in rows}
    ordered: list[AbilityModel] = []
    seen: set[str] = set()
    if adversary_row is not None and isinstance(adversary_row.atomic_ordering, list):
        for ability_id in adversary_row.atomic_ordering:
            row = by_id.get(ability_id)
            if row is not None and row.ability_id not in seen:
                ordered.append(row)
                seen.add(row.ability_id)
    for row in sorted(rows, key=lambda r: r.ability_id):
        if row.ability_id not in seen:
            ordered.append(row)
            seen.add(row.ability_id)

    abilities = [
        AbilityData(
            ability_id=a.ability_id,
            name=a.name,
            tactic=a.tactic,
            technique_name=a.technique_name,
            technique_id=a.technique_id,
            description=a.description,
            executors=_executors_from_json(a.executors),
        )
        for a in ordered
    ]

    adversary = None
    if adversary_row is not None:
        ordering = adversary_row.atomic_ordering
        adversary = AdversaryData(
            id=adversary_row.id,
            name=adversary_row.name,
            description=adversary_row.description,
            atomic_ordering=list(ordering) if isinstance(ordering, list) else [],
        )

    return CaseData(
        id=slug,
        name=name,
        abilities=abilities,
        adversary=adversary,
        flow=resolve_flow_data_from_db(session, slug),
    )


__all__ = [
    "build_case_from_db",
    "flow_data_from_content",
    "is_curated",
    "list_curated_names",
    "resolve_flow_data_from_db",
    "upsert_case_row",
    "utcnow",
]
