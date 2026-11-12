"""Property test for CampaignMaterializer idempotent materialization.

Phase 2, sub-step 2.3a. Exercises the invariant that materializing a
STIX-persisted campaign into the ``campaigns`` relational table is idempotent
and follows the Campaign_Key rule:

* calling ``materialize(campaign_key, version)`` one or more times leaves
  exactly one ``campaigns`` row for that key;
* the row id is the ``curated_slug`` when present, otherwise the ``stix_id``;
* a pre-existing row's ``translation_source`` is preserved across re-materialize.

Each generated example gets its **own** ephemeral SQLite database (built with
the usual temp-file SQLite engine/session pattern) so
Hypothesis examples never share state — a function-scoped fixture would be
reused across examples, accumulating rows and tripping the
``(package_id, stix_id)`` unique constraint. The STIX tables are seeded directly
so both curated (``curated_slug``) and non-curated (bare ``stix_id``) campaigns
are covered. The service under test reads STIX only from the database and
performs no network access, so a real session is enough — no mocks.

# Feature: stix-db-single-source-ai-pipeline, Property 1: Materialização de campanha é idempotente e segue o Campaign_Key
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import Case, StixCampaign, StixPackage
from app.models.enums import TranslationSource
from app.services.campaign_materializer import (
    CampaignMaterializer,
    CampaignNotPersistedError,
)
from app.services.stix.stix_service import CURATED_VERSION


# ---------------------------------------------------------------------------
# Per-example ephemeral SQLite database (NOT a fixture: Hypothesis would reuse a
# function-scoped fixture across every generated example, accumulating rows).
# ---------------------------------------------------------------------------


@contextmanager
def _ephemeral_session_factory() -> Iterator[sessionmaker[Session]]:
    """Yield a fresh session factory backed by a throwaway SQLite file."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_materializer_"))
    db_path = tmp_dir / "materializer.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")
    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    try:
        yield factory
    finally:
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# STIX seeding helper — controls curated_slug / stix_id / name / description so
# the Campaign_Key rule and the name/description copy can both be asserted.
# ---------------------------------------------------------------------------


def _seed_stix_campaign(
    factory: sessionmaker[Session],
    *,
    stix_id: str,
    name: str,
    description: str | None,
    curated_slug: str | None,
    version: str = CURATED_VERSION,
) -> str:
    """Persist one ``stix_packages`` + ``stix_campaigns`` row and return its key.

    The returned key follows the Campaign_Key rule: ``curated_slug`` when
    present, otherwise ``stix_id`` — the same value
    ``StixPackageReadService.campaign_overview`` resolves and the materializer
    uses as ``campaigns.id``.
    """
    session = factory()
    try:
        package_id = session.execute(
            select(StixPackage.id).where(StixPackage.version == version)
        ).scalar_one_or_none()
        if package_id is None:
            package = StixPackage(version=version, name=f"Enterprise ATT&CK v{version}")
            session.add(package)
            session.flush()
            package_id = package.id

        session.add(
            StixCampaign(
                package_id=package_id,
                stix_id=stix_id,
                name=name,
                description=description,
                curated=curated_slug is not None,
                curated_slug=curated_slug,
            )
        )
        session.commit()
    finally:
        session.close()
    return curated_slug if curated_slug is not None else stix_id


def _count_campaigns_with_id(factory: sessionmaker[Session], campaign_id: str) -> int:
    session = factory()
    try:
        return session.execute(
            select(func.count()).select_from(Case).where(Case.id == campaign_id)
        ).scalar_one()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Identifier-ish tokens for stix_id / curated_slug (English-only, ASCII), kept
# distinct per role so the Campaign_Key rule is unambiguous.
_token = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),  # a-z
    min_size=3,
    max_size=10,
)
_free_text = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=0,
    max_size=40,
)


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


@settings(max_examples=30)
@given(
    stix_token=_token,
    slug_token=_token,
    has_slug=st.booleans(),
    name=st.text(min_size=1, max_size=40),
    description=st.one_of(st.none(), _free_text),
    call_count=st.integers(min_value=1, max_value=5),
)
def test_materialize_is_idempotent_and_follows_campaign_key(
    stix_token: str,
    slug_token: str,
    has_slug: bool,
    name: str,
    description: str | None,
    call_count: int,
) -> None:
    """materialize() 1..N times -> exactly one row; id follows Campaign_Key.

    *For any* STIX-persisted campaign, calling ``materialize`` one or more times
    yields exactly one ``campaigns`` row whose id is the ``curated_slug`` when
    present, otherwise the ``stix_id``.

    **Validates: Requirements 2.5**
    """
    stix_id = f"campaign--{stix_token}"
    curated_slug = f"slug-{slug_token}" if has_slug else None

    with _ephemeral_session_factory() as factory:
        expected_key = _seed_stix_campaign(
            factory,
            stix_id=stix_id,
            name=name,
            description=description,
            curated_slug=curated_slug,
        )

        returned_ids: list[str] = []
        for _ in range(call_count):
            session = factory()
            try:
                materializer = CampaignMaterializer(session)
                returned_ids.append(
                    materializer.materialize(expected_key, CURATED_VERSION)
                )
                session.commit()
            finally:
                session.close()

        # id follows the Campaign_Key rule (curated_slug when present, else
        # stix_id) and is stable across calls.
        assert all(cid == expected_key for cid in returned_ids), returned_ids

        # Idempotent: exactly one row regardless of how many times it was called.
        assert _count_campaigns_with_id(factory, expected_key) == 1

        # name/description were copied from the Campaign_Overview.
        session = factory()
        try:
            row = session.get(Case, expected_key)
            assert row is not None
            assert row.name == name
            assert row.description == description
        finally:
            session.close()


@settings(max_examples=30)
@given(
    stix_token=_token,
    slug_token=_token,
    has_slug=st.booleans(),
    name=st.text(min_size=1, max_size=40),
    description=st.one_of(st.none(), _free_text),
    preexisting_source=st.sampled_from(list(TranslationSource)),
    call_count=st.integers(min_value=1, max_value=4),
)
def test_materialize_preserves_preexisting_translation_source(
    stix_token: str,
    slug_token: str,
    has_slug: bool,
    name: str,
    description: str | None,
    preexisting_source: TranslationSource,
    call_count: int,
) -> None:
    """A pre-existing campaigns row keeps its translation_source across calls.

    *For any* campaign whose ``campaigns`` row already exists (e.g. a curated row
    ingested earlier), re-materializing it is a no-op for ``translation_source``.

    **Validates: Requirements 2.5**
    """
    stix_id = f"campaign--{stix_token}"
    curated_slug = f"slug-{slug_token}" if has_slug else None

    with _ephemeral_session_factory() as factory:
        expected_key = _seed_stix_campaign(
            factory,
            stix_id=stix_id,
            name=name,
            description=description,
            curated_slug=curated_slug,
        )

        # Pre-create the campaigns row with an explicit translation_source, as if
        # it had been persisted as curated before.
        session = factory()
        try:
            session.add(
                Case(
                    id=expected_key,
                    name="stale-name",
                    description="stale-description",
                    translation_source=preexisting_source,
                )
            )
            session.commit()
        finally:
            session.close()

        for _ in range(call_count):
            session = factory()
            try:
                CampaignMaterializer(session).materialize(expected_key, CURATED_VERSION)
                session.commit()
            finally:
                session.close()

        # Still exactly one row, translation_source preserved, name/description
        # refreshed from the STIX overview.
        assert _count_campaigns_with_id(factory, expected_key) == 1
        session = factory()
        try:
            row = session.get(Case, expected_key)
            assert row is not None
            assert row.translation_source == preexisting_source
            assert row.name == name
            assert row.description == description
        finally:
            session.close()


def test_materialize_raises_when_campaign_absent() -> None:
    """A campaign absent from stix_campaigns raises CampaignNotPersistedError.

    Concrete example in the same file: the guard 404 covers this in the request
    path, but programmatic misuse surfaces the typed error.
    """
    with _ephemeral_session_factory() as factory:
        session = factory()
        try:
            with pytest.raises(CampaignNotPersistedError):
                CampaignMaterializer(session).materialize(
                    "missing-key", CURATED_VERSION
                )
        finally:
            session.close()
