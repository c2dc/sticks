"""Property + example tests for the DB-backed campaign guard (task 2.5, Phase 2.3a).

This file guards the migration of ``_known_slug_or_404`` (the 8-curated-slug
gate) to ``_known_campaign_or_404`` (validation against the persisted 52 STIX
campaigns). The central guarantee is Property 3 of the design: the guard accepts
EVERY campaign persisted in ``stix_campaigns`` and refuses (404) any campaign
absent from it (Req. 2.2, 2.3, 4.4).

Everything here is offline and uses an ephemeral SQLite session built straight
from the ORM. STIX campaigns are seeded through the shared ``_stix_seed``
helper (``ensure_stix_campaigns``), exactly like the HTTP-surface tests. No
network and no AI client (there is none on this path).

Design's Correctness Property covered:

* **Property 3** (task 2.5) — the guard accepts every STIX campaign and refuses
  the absent ones. The property seeds a random set of persisted campaign keys,
  asserts the guard does NOT raise for any seeded key, and asserts it raises an
  ``HTTPException(404)`` for random keys that were NOT seeded (fusion of the old
  Properties 2 and 4).

Concrete examples in the same file (no separate example file):

* ``Stage2Service.select`` of a PERSISTED campaign completes
  (``completed=True``) with ``identity.found=True`` (Req. 2.1).
* The Stage 2 listing marks every persisted campaign ``selectable=True``
  (Req. 1.3, 2.7).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import HTTPException
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from _stix_seed import ensure_stix_campaigns

from app.api.campaigns import _known_campaign_or_404
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixCampaign, StixCampaignTechnique, StixPackage
from app.services.campaign_service import CaseService
from app.services.stage.stage_service import StageService
from app.services.stix.stage2_service import (
    CampaignSelectionResult,
    ListedCampaign,
    Stage2Service,
)
from app.services.stix.stix_service import CURATED_VERSION, StixService


VERSION = CURATED_VERSION


# ---------------------------------------------------------------------------
# Ephemeral SQLite engine + session factory (same pattern as sibling tests).
# ---------------------------------------------------------------------------


def _new_engine() -> tuple[object, sessionmaker[Session], Path]:
    """Build an isolated SQLite engine; return (engine, session_factory, tmp_dir)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="known_campaign_guard_"))
    db_path = tmp_dir / "guard.db"
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
    return engine, session_factory, tmp_dir


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """Yield a session factory over a fresh, isolated SQLite database."""
    engine, factory, tmp_dir = _new_engine()
    try:
        yield factory
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# A campaign-key strategy: lowercase ASCII tokens that look like a real
# ``Campaign_Key`` (a slug or a ``campaign--`` stix id). Constrained to the
# input space the guard actually sees — short, non-empty, no whitespace.
_key_text = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=3,
    max_size=12,
)
_campaign_keys = st.lists(_key_text, min_size=1, max_size=6, unique=True)
_absent_keys = st.lists(_key_text, min_size=1, max_size=6, unique=True)


# ===========================================================================
# Property 3 — the guard accepts every STIX campaign and refuses the absent.
# ===========================================================================


# Feature: stix-db-single-source-ai-pipeline, Property 3: O guard aceita toda campanha do STIX e recusa (404) as ausentes
@settings(max_examples=30, deadline=None)
@given(present=_campaign_keys, candidates=_absent_keys)
def test_guard_accepts_persisted_and_refuses_absent(
    present: list[str],
    candidates: list[str],
) -> None:
    """The guard accepts every persisted STIX campaign and 404s the absent ones.

    **Validates: Requirements 2.2, 2.3, 4.4**

    Seeds the ``present`` keys as ``stix_campaigns`` rows (via the shared
    ``ensure_stix_campaigns`` helper) and asserts:

    * every seeded key passes the guard without raising (Req. 2.2); and
    * any candidate key NOT among the seeded ones is refused with an
      ``HTTPException`` carrying status 404 (Req. 2.3, 4.4).

    Each Hypothesis example builds its OWN isolated SQLite database (not a
    shared function-scoped fixture), so one example's seeded campaigns never
    leak into the next — otherwise a key refused in one example could be
    silently accepted in another because it was seeded earlier.
    """
    engine, factory, tmp_dir = _new_engine()
    try:
        present_set = set(present)
        ensure_stix_campaigns(factory, present_set, version=VERSION)

        session = factory()
        try:
            # (a) Every persisted campaign is accepted: the guard does not raise.
            for key in present_set:
                # No raise == accepted (the guard returns None for known keys).
                assert _known_campaign_or_404(key, session, VERSION) is None

            # (b) Any candidate that was NOT seeded is refused with a 404.
            for key in set(candidates) - present_set:
                with pytest.raises(HTTPException) as excinfo:
                    _known_campaign_or_404(key, session, VERSION)
                assert excinfo.value.status_code == 404
        finally:
            session.close()
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ===========================================================================
# Concrete example (a) — Stage2Service.select of a persisted campaign.
# ===========================================================================


def _real_data_dir() -> Path:
    """tests/ -> backend/ -> apps/ -> repo root -> packages/cli/data (read-only)."""
    return Path(__file__).resolve().parents[3] / "packages" / "cli" / "data"


_SELECTED_STIX_ID = "campaign--selected-example"


def _seed_persisted_campaign(session_factory: sessionmaker[Session]) -> None:
    """Seed one persisted (non-curated) campaign with a technique for the example."""
    session = session_factory()
    try:
        package = StixPackage(version=VERSION, name=f"Enterprise ATT&CK v{VERSION}")
        session.add(package)
        session.flush()

        campaign = StixCampaign(
            package_id=package.id,
            stix_id=_SELECTED_STIX_ID,
            name="Selected Example Campaign",
            external_id="C9100",
            description="A persisted campaign used by the concrete example.",
            aliases=["Selected"],
            curated=False,
            curated_slug=None,
        )
        session.add(campaign)
        session.flush()

        session.add(
            StixCampaignTechnique(
                campaign_id=campaign.id,
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
            )
        )
        session.commit()
    finally:
        session.close()


def test_stage2_select_of_persisted_campaign_completes(
    session_factory: sessionmaker[Session],
) -> None:
    """Selecting a persisted campaign completes with ``identity.found=True`` (Req. 2.1).

    Concrete example backing Property 3: the same persisted campaign the guard
    accepts is selectable end to end through ``Stage2Service.select``, which
    reports ``completed=True`` and a found identity assembled from the database.
    """
    _seed_persisted_campaign(session_factory)

    session = session_factory()
    try:
        # The guard accepts the persisted campaign (no raise).
        assert _known_campaign_or_404(_SELECTED_STIX_ID, session, VERSION) is None

        service = Stage2Service(
            StageService(session, case_service=CaseService(data_dir=_real_data_dir()))
        )
        result = service.select(_SELECTED_STIX_ID, VERSION)

        assert isinstance(result, CampaignSelectionResult)
        assert result.completed is True
        assert result.error_message is None
        assert result.failed_step is None
        assert result.identity is not None and result.identity.found is True
        assert result.identity.external_id == "C9100"
        assert [t.technique_id for t in result.techniques] == ["T1059"]
    finally:
        session.close()


# ===========================================================================
# Concrete example (b) — the listing marks persisted campaigns selectable.
# ===========================================================================


def test_listing_marks_persisted_campaign_selectable(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The Stage 2 listing marks every listed campaign ``selectable=True`` (Req. 1.3, 2.7).

    Concrete example backing Property 3: the curated-campaign gate is gone, so
    the listing marks every campaign of the bundle as selectable — the
    selection contract stays coherent with the guard, which accepts every
    persisted campaign.
    """
    # A minimal on-disk bundle so ``list_campaigns`` has something to list.
    bundle = (
        '{"type": "bundle", "objects": ['
        '{"type": "campaign", "id": "' + _SELECTED_STIX_ID + '", '
        '"name": "Selected Example Campaign", '
        '"external_references": [{"external_id": "C9100"}]}]}'
    )
    stix = StixService(data_dir=tmp_path)
    base_path = stix.base_path(VERSION)
    base_path.parent.mkdir(parents=True, exist_ok=True)
    base_path.write_text(bundle, encoding="utf-8")

    session = session_factory()
    try:
        service = Stage2Service(StageService(session, stix_service=stix))
        campaigns = service.list_campaigns(VERSION)

        assert campaigns, "expected the bundle's campaigns to be listed"
        assert all(isinstance(c, ListedCampaign) for c in campaigns)
        assert all(c.selectable is True for c in campaigns)
    finally:
        session.close()
