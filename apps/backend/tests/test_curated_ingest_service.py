"""Tests for CuratedIngestService - disk->DB ingest (Phase 2, sub-step 2.1).

Reads the REAL curated cases from packages/cli/data via CaseService and asserts
they are persisted into the DB (campaigns/dags/abilities/adversaries), that the
ingest is idempotent (a second run does not duplicate rows), and spot-checks the
persisted content shape. Uses an ephemeral SQLite session.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Ability, Adversary, Case, Dag
from app.services.campaign_service import CURATED_CASES, CaseService
from app.services.curated_ingest_service import CuratedIngestService


def _real_data_dir() -> Path:
    """tests/ -> backend/ -> apps/ -> repo root -> packages/cli/data (read-only)."""
    return Path(__file__).resolve().parents[3] / "packages" / "cli" / "data"


@pytest.fixture()
def session() -> Iterator[Session]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_ingest_"))
    db_path = tmp_dir / "ingest.db"
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
    db_session = session_factory()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _counts(session: Session) -> dict[str, int]:
    return {
        "campaigns": session.execute(select(func.count()).select_from(Case)).scalar_one(),
        "dags": session.execute(select(func.count()).select_from(Dag)).scalar_one(),
        "abilities": session.execute(select(func.count()).select_from(Ability)).scalar_one(),
        "adversaries": session.execute(select(func.count()).select_from(Adversary)).scalar_one(),
    }


def _service(session: Session) -> CuratedIngestService:
    return CuratedIngestService(session, case_service=CaseService(data_dir=_real_data_dir()))


def test_ingest_persists_curated_cases(session: Session) -> None:
    """Ingesting the curated cases persists campaigns/dags/abilities/adversaries."""
    service = _service(session)
    summary = service.ingest_all()

    counts = _counts(session)
    # At least some curated content was ingested (the checkout has the files).
    assert counts["campaigns"] > 0, summary
    assert counts["dags"] > 0, summary
    assert counts["abilities"] > 0, summary

    # The persisted campaigns are a subset of the known curated slugs (only those
    # whose files loaded cleanly).
    slugs = set(
        session.execute(select(Case.id)).scalars().all()
    )
    assert slugs, "expected at least one curated campaign persisted"
    assert slugs.issubset(set(CURATED_CASES)), slugs
    # Any slug that failed to load is reported, not persisted.
    assert set(summary["failed"]).isdisjoint(slugs)


def test_ingest_is_idempotent(session: Session) -> None:
    """Running the ingest twice yields identical row counts (no duplicates)."""
    service = _service(session)
    service.ingest_all()
    first = _counts(session)
    service.ingest_all()
    second = _counts(session)
    assert first == second, (first, second)


def test_ingest_content_shape_spotcheck(session: Session) -> None:
    """A known curated slug persists a DAG with structural_nodes and abilities."""
    service = _service(session)
    service.ingest_all()

    # Pick any persisted campaign that has a DAG and abilities.
    dag = session.execute(select(Dag)).scalars().first()
    if dag is None:
        pytest.skip("No curated DAG present in the checkout.")
    assert isinstance(dag.content, dict)
    assert "structural_nodes" in dag.content
    assert "edges" in dag.content

    ability = session.execute(select(Ability)).scalars().first()
    if ability is not None:
        assert isinstance(ability.executors, list)
