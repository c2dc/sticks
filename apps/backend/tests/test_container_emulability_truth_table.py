"""Deterministic truth-table test for ``ContainerEmulabilityService`` (task 12.3).

Property 6 — Emulabilidade correta.
**Validates: Requirements 6.2**

Two deterministic cases exercise the core derivation rule of
:meth:`ContainerEmulabilityService.derive` (design Component 5, Req. 6.2):

* **All container/dispensable -> True.** A campaign whose every technique is
  classified ``container`` or ``dispensable`` is marked
  ``container_emulable=True`` with an empty ``blocking_techniques`` list.
* **One not_container -> False.** A campaign with a single ``not_container``
  technique (the rest ``container``) is marked ``container_emulable=False``,
  and that technique is listed in ``blocking_techniques``.

No property/Hypothesis generation: the truth table is exercised by two
hand-built, fully deterministic cases.

Test infra
----------
Each case seeds ``Ability`` + ``TechniqueEmulability`` rows via the ORM on an
ephemeral temp-file SQLite ``Session`` (the same ``Base.metadata.create_all``
pattern as the sibling ``test_docker_catalog_ingest_idempotency.py``), then
calls ``ContainerEmulabilityService(session).derive(campaign_id)``. No network,
no mocks of the service under test — the real relational reads and upsert run.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Ability, TechniqueEmulability
from app.models.enums import TechniqueEmulabilityKind
from app.services.container_emulability_service import ContainerEmulabilityService


# ---------------------------------------------------------------------------
# Ephemeral session helper (fresh temp-file SQLite + ORM schema).
# ---------------------------------------------------------------------------


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    """Create a fresh temp-file SQLite engine + ORM schema and return a session.

    Returns ``(session, tmp_dir, engine)`` so the caller can dispose the engine
    and remove the directory afterwards. Mirrors the temp-file SQLite +
    ``Base.metadata.create_all`` pattern of the sibling ingest test.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="container_emulability_"))
    db_path = tmp_dir / "container_emulability.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), tmp_dir, engine


def _dispose(session: Session, engine: Any, tmp_dir: Path) -> None:
    """Close the session, dispose the engine and remove the temp dir."""
    session.close()
    engine.dispose()
    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def db_session() -> Any:
    """Yield an ephemeral SQLite session, disposed after the test."""
    session, tmp_dir, engine = _build_ephemeral_session()
    try:
        yield session
    finally:
        _dispose(session, engine, tmp_dir)


# ---------------------------------------------------------------------------
# Seeding helpers (ORM rows: Ability + TechniqueEmulability).
# ---------------------------------------------------------------------------


def _seed_ability(session: Session, campaign_id: str, technique_id: str) -> None:
    """Seed one ``Ability`` row tying ``technique_id`` to ``campaign_id``.

    ``ability_id`` is the primary key; a deterministic ``{campaign}-{technique}``
    id keeps each seeded ability unique within a test.
    """
    session.add(
        Ability(
            ability_id=f"{campaign_id}-{technique_id}",
            campaign_id=campaign_id,
            technique_id=technique_id,
        )
    )


def _seed_classification(
    session: Session, technique_id: str, kind: TechniqueEmulabilityKind
) -> None:
    """Seed one ``TechniqueEmulability`` classification row for ``technique_id``."""
    session.add(TechniqueEmulability(technique_id=technique_id, emulability=kind))


# ===========================================================================
# Case 1 — all container/dispensable -> container_emulable is True
# ===========================================================================


def test_all_container_or_dispensable_is_emulable(db_session: Session) -> None:
    """A campaign of only container/dispensable techniques is emulable (Req. 6.2).

    Two techniques — one ``container``, one ``dispensable`` — so every technique
    is non-blocking. ``derive`` must mark the campaign ``container_emulable`` with
    an empty ``blocking_techniques`` list.
    """
    campaign_id = "campaign-emulable"
    _seed_ability(db_session, campaign_id, "T1059")
    _seed_ability(db_session, campaign_id, "T1566")
    _seed_classification(db_session, "T1059", TechniqueEmulabilityKind.CONTAINER)
    _seed_classification(db_session, "T1566", TechniqueEmulabilityKind.DISPENSABLE)
    db_session.flush()

    result = ContainerEmulabilityService(db_session).derive(campaign_id)

    assert result.campaign_id == campaign_id
    assert result.container_emulable is True
    assert result.blocking_techniques == []
    assert result.row_id is not None


# ===========================================================================
# Case 2 — one not_container -> container_emulable is False, technique blocks
# ===========================================================================


def test_one_not_container_technique_blocks_emulability(db_session: Session) -> None:
    """A single ``not_container`` technique blocks the campaign (Req. 6.2).

    Two techniques — one ``container``, one ``not_container`` — so the campaign
    is NOT fully emulable. ``derive`` must mark it ``container_emulable=False`` and
    list exactly the ``not_container`` technique in ``blocking_techniques``.
    """
    campaign_id = "campaign-blocked"
    _seed_ability(db_session, campaign_id, "T1059")
    _seed_ability(db_session, campaign_id, "T1200")
    _seed_classification(db_session, "T1059", TechniqueEmulabilityKind.CONTAINER)
    _seed_classification(db_session, "T1200", TechniqueEmulabilityKind.NOT_CONTAINER)
    db_session.flush()

    result = ContainerEmulabilityService(db_session).derive(campaign_id)

    assert result.campaign_id == campaign_id
    assert result.container_emulable is False
    assert result.blocking_techniques == ["T1200"]
    assert result.row_id is not None


# ===========================================================================
# read_many — the read-only batch lookup backing the campaign-list API
# (task 13.1, Req. 6.4, 6.5). Returns the already-derived markings keyed by
# campaign_id; campaigns with no derived row are simply absent.
# ===========================================================================


def test_read_many_returns_persisted_markings_without_deriving(
    db_session: Session,
) -> None:
    """``read_many`` returns the derived markings and omits undived campaigns.

    Derives two campaigns (one emulable, one blocked), then reads back the
    marking of both plus an undived third campaign in a single batch. The read
    must surface the two persisted verdicts (with their blocking techniques) and
    simply omit the undived one — the API treats an absent entry as "not derived
    yet" (``container_emulable=None``, still listed — Req. 6.5). ``read_many``
    must not derive or write: the undived campaign must stay absent even though
    it has abilities seeded.
    """
    emulable = "campaign-emulable"
    blocked = "campaign-blocked"
    undived = "campaign-undived"

    _seed_ability(db_session, emulable, "T1059")
    _seed_classification(db_session, "T1059", TechniqueEmulabilityKind.CONTAINER)
    _seed_ability(db_session, blocked, "T1200")
    _seed_classification(db_session, "T1200", TechniqueEmulabilityKind.NOT_CONTAINER)
    _seed_ability(db_session, undived, "T1059")
    db_session.flush()

    service = ContainerEmulabilityService(db_session)
    service.derive(emulable)
    service.derive(blocked)

    markings = service.read_many([emulable, blocked, undived, ""])

    # The undived campaign (and the blank id) are absent — no derivation on read.
    assert set(markings) == {emulable, blocked}
    assert markings[emulable].container_emulable is True
    assert markings[emulable].blocking_techniques == []
    assert markings[blocked].container_emulable is False
    assert markings[blocked].blocking_techniques == ["T1200"]

    # Empty input yields an empty mapping.
    assert service.read_many([]) == {}
