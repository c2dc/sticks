"""Unit tests for the database-only curated case reader (task 21.2).

These replace the removed ``CaseService`` tests: the curated case is persisted as
ORM rows in a temporary SQLite and rebuilt through
:mod:`app.services.curated_case_reader`; the curated set is the
``campaigns.curated`` flag, not an in-memory registry.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.services.curated_case_reader import (
    build_case_from_db,
    is_curated,
    list_curated_names,
)
from tests._curated_db import CaseDb, seed_case


@pytest.fixture()
def case_db() -> Iterator[CaseDb]:
    db = CaseDb()
    try:
        yield db
    finally:
        db.dispose()


def _abilities(slug: str) -> list[dict]:
    return [
        {
            "ability_id": f"{slug}-ab-1",
            "name": "first",
            "technique_id": "T1190",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 1"}],
        },
        {
            "ability_id": f"{slug}-ab-2",
            "name": "second",
            "technique_id": "T1059",
            "executors": [{"name": "sh", "platform": "linux", "command": "echo 2"}],
        },
    ]


def test_build_case_orders_abilities_by_atomic_ordering_and_reads_name_from_db(
    case_db: CaseDb,
) -> None:
    seed_case(
        case_db,
        "shadowray",
        name="Name From Row",
        abilities=_abilities("shadowray"),
        adversary={
            "id": "shadowray-adv",
            "name": "Adversary",
            "description": "seed",
            "atomic_ordering": ["shadowray-ab-2", "shadowray-ab-1"],
        },
        flow={
            "campaign_name": "Seed",
            "structural_nodes": [{"node_id": "n0", "technique_id": "T1190"}],
        },
    )
    session = case_db.factory()
    try:
        case = build_case_from_db(session, "shadowray")
    finally:
        session.close()

    assert case.name == "Name From Row"
    assert [a.ability_id for a in case.abilities] == ["shadowray-ab-2", "shadowray-ab-1"]
    assert case.adversary is not None and case.adversary.id == "shadowray-adv"
    assert case.flow is not None and case.flow.structural_nodes[0].technique_id == "T1190"
    assert case.has_abilities


def test_case_without_adversary_or_abilities_is_clean(case_db: CaseDb) -> None:
    seed_case(case_db, "c0010")
    session = case_db.factory()
    try:
        case = build_case_from_db(session, "c0010")
    finally:
        session.close()

    assert case.abilities == []
    assert case.adversary is None
    assert case.flow is None
    assert not case.has_abilities
    assert not hasattr(case, "has_adversary")
    assert case.atomic_ordering == []


def test_curated_set_is_the_database_flag(case_db: CaseDb) -> None:
    seed_case(case_db, "shadowray", name="ShadowRay")
    seed_case(case_db, "ai_made", name="AI made", curated=False)
    session = case_db.factory()
    try:
        assert list_curated_names(session) == {"shadowray": "ShadowRay"}
        assert is_curated(session, "shadowray") is True
        assert is_curated(session, "ai_made") is False
        assert is_curated(session, "missing") is False
    finally:
        session.close()
