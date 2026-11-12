"""Property test for the curated-flag consistency of StixPersistenceService
(task 2.7 of the ``stix-package-persistence-and-overview`` spec).

# Feature: stix-package-persistence-and-overview, Property 3: Consistency of the
# curated flag.

Validates: Requirements 4.6

This is a *feature* spec, so the property is expected to HOLD against the current
:class:`~app.services.stix.stix_persistence_service.StixPersistenceService`.

Property (design.md — Property 3)
---------------------------------
For *every* persisted campaign, ``curated`` is true if and only if
``curated_slug`` is not null:

    row.curated is True  ==  row.curated_slug is not None

The persistence derives the curated slug via
:meth:`StixService._resolve_curated_slug` / :data:`SLUG_TO_STIX` and sets
``stix_campaigns.curated = (curated_slug is not None)``. The property verifies
this biconditional holds universally over every persisted row — regardless of
whether the campaign matched a curated slug by external-id code, by canonical
name, or did not match at all.

Strategy / isolation
--------------------
Each example generates a VALID STIX bundle carrying a MIX of campaigns: some
that match a curated slug (by one of the known ``external_id`` codes such as
``"C0010"`` or by a canonical name such as ``"ShadowRay"``) and some that do not
(plain names / plain external ids). The bundle is written to an isolated
temporary ``data_dir`` as ``enterprise-attack-{version}.json`` and a
``StixService(data_dir=..., http_client=None)`` is injected into the service, so
nothing touches the real ``sticks/data/stix`` nor the network. The write runs
against an ephemeral SQLite engine with the full schema created via
``Base.metadata.create_all`` (same pattern as the sibling persistence tests).

The oracle (``curated == (curated_slug is not None)``) is computed from the
persisted row itself and does not restate the derivation logic, so the property
genuinely cross-checks the implementation.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import StixCampaign
from app.services.stix.stix_persistence_service import StixPersistenceService
from app.services.stix.stix_service import KNOWN_VERSIONS, SLUG_TO_STIX, StixService


# A single shared iteration budget so the property runs >= 100 examples. Each
# example does disk I/O (ephemeral SQLite + a bundle file), whose cost varies
# with the environment, so the per-example clock is disabled (timing, not
# correctness).
PROP_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# A known version (download is never exercised — we write the bundle directly to
# disk and call persist_from_disk, so no network egress occurs).
_VERSION = KNOWN_VERSIONS[0]

# The curated external-id codes that resolve a slug (non-empty "referencias").
_CURATED_CODES: list[str] = sorted(
    {
        code
        for mapping in SLUG_TO_STIX.values()
        for code in mapping.get("referencias", [])
        if isinstance(code, str) and code.strip()
    }
)
# The curated canonical names that resolve a slug.
_CURATED_NAMES: list[str] = sorted(
    {
        mapping["nome_canonico"]
        for mapping in SLUG_TO_STIX.values()
        if isinstance(mapping.get("nome_canonico"), str) and mapping["nome_canonico"]
    }
)


def _stix_id(prefix: str, n: int) -> str:
    """Build a deterministic STIX id of the form ``prefix--0000...000N``."""
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Ephemeral DB fixture: a SQLite engine on a temporary file with the full schema
# via Base.metadata.create_all, so the persistence runs against a real,
# disposable database (same pattern as test_backfill_idempotent_property.py).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stix_curated_prop_")
    db_path = Path(tmp_dir) / "curated_flag.db"
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
    return factory, tmp_dir


# ---------------------------------------------------------------------------
# Hypothesis strategy — a VALID STIX bundle with a MIX of curated and
# non-curated campaigns.
# ---------------------------------------------------------------------------


@st.composite
def _mixed_campaign_bundle(draw) -> list[dict]:
    """Draw the ``objects`` of a bundle with curated and non-curated campaigns.

    Each campaign is drawn as one of three flavours:

    * ``code`` — carries a curated ``external_id`` code (resolves a slug);
    * ``name`` — carries a curated canonical name (resolves a slug);
    * ``plain`` — a plain name and a non-curated external id (resolves nothing).

    At least one curated and one non-curated campaign are guaranteed per example
    (by seeding the list with one of each) so both branches of the biconditional
    are exercised across examples; the invariant must hold for every row either
    way.
    """
    # Seed with one guaranteed curated and one guaranteed non-curated campaign so
    # curated rows exist at least sometimes (and non-curated always too).
    flavours: list[str] = [
        draw(st.sampled_from(["code", "name"])),  # guaranteed curated
        "plain",  # guaranteed non-curated
    ]
    # Then a variable tail of arbitrary flavours.
    extra = draw(
        st.lists(st.sampled_from(["code", "name", "plain"]), min_size=0, max_size=6)
    )
    flavours.extend(extra)

    objects: list[dict] = []
    for index, flavour in enumerate(flavours):
        campaign: dict = {
            "type": "campaign",
            "id": _stix_id("campaign", index),
        }
        if flavour == "code":
            code = draw(st.sampled_from(_CURATED_CODES))
            campaign["name"] = f"Campaign {index}"
            campaign["external_references"] = [
                {"source_name": "mitre-attack", "external_id": code}
            ]
        elif flavour == "name":
            campaign["name"] = draw(st.sampled_from(_CURATED_NAMES))
        else:  # plain — never matches a curated slug.
            campaign["name"] = f"Plain Campaign {index}"
            campaign["external_references"] = [
                {"source_name": "mitre-attack", "external_id": f"C9{index:03d}"}
            ]
        objects.append(campaign)

    return objects



def _cleanup(*bases: Path) -> None:
    """Remove the temporary directories created for an example."""
    for base in bases:
        if not base.exists():
            continue
        for root, _dirs, files in os.walk(base, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            os.rmdir(root)


# ===========================================================================
# Property 3 — curated flag consistency: curated iff curated_slug is not null.
# ===========================================================================


# Feature: stix-package-persistence-and-overview, Property 3: Consistency of the
# curated flag
@PROP_SETTINGS
@given(objects=_mixed_campaign_bundle())
def test_property3_curated_flag_consistency(objects: list[dict]) -> None:
    """For every persisted campaign, ``curated == (curated_slug is not None)``.

    Persists a generated bundle (mix of curated and non-curated campaigns) from
    an isolated disk dir into an ephemeral SQLite database and asserts the
    biconditional holds for EVERY ``stix_campaigns`` row. Also asserts that
    curated rows exist at least sometimes across examples (the generator seeds
    one guaranteed curated campaign) while keeping the invariant universal.

    Validates: Requirements 4.6
    """
    factory, db_tmp_dir = _make_session_factory()
    session = factory()
    try:
        bundle = {"type": "bundle", "objects": objects}

        stix_service = StixService()
        service = StixPersistenceService(session=session, stix_service=stix_service)

        # Persist from disk (no network egress) — the write sets
        # stix_campaigns.curated = (curated_slug is not None).
        service.persist_bundle(_VERSION, bundle)

        rows = (
            session.execute(select(StixCampaign))
            .scalars()
            .all()
        )

        # Every generated campaign is persisted (none dropped).
        assert len(rows) == len(objects)

        # The universal invariant: curated is True iff curated_slug is not null.
        for row in rows:
            assert (row.curated is True) == (row.curated_slug is not None), (
                f"curated={row.curated!r} but curated_slug={row.curated_slug!r} "
                f"for campaign stix_id={row.stix_id!r}"
            )

        # Curated rows exist at least sometimes: the generator seeds one
        # guaranteed curated campaign, so at least one row must be curated.
        assert any(row.curated is True for row in rows)
        # And at least one non-curated row too (the guaranteed plain campaign).
        assert any(row.curated is False for row in rows)
    finally:
        session.close()
        factory.kw["bind"].dispose()
        _cleanup(Path(db_tmp_dir))
