"""Endpoint tests for ``/api/stix/*`` (reads from the database only).

Builds a FastAPI ``TestClient`` over ``app.main:create_app`` and exercises:

* ``GET /api/stix/bases`` - 200, bases ordered newest->oldest, ``downloaded`` /
  ``selectable`` / ``completed`` reflecting the database only;
* ``GET .../overview``, ``.../campaigns``, ``.../analytics`` and the campaign
  overview - 422 ``bundle_indisponivel`` for a version that is not persisted
  (there is no fallback to a file);
* ``POST .../download`` - starts the ``stix_packages`` ingestion source in the
  background through the orchestrator, using a fake HTTP client that serves the
  bundle from memory.

Isolation: the ``get_db`` dependency is overridden onto an ephemeral temp-file
SQLite built straight from the ORM (``Base.metadata.create_all``). Nothing
touches the network or any bundle file.
"""

from __future__ import annotations

import itertools
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from _stix_fakes import FakeStixHttp, small_bundle
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import (
    StixActor,
    StixActorCampaign,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
    StixTypeCount,
)
from app.models.enums import IngestSource, IngestStatus
from app.services.ingestion.orchestrator import IngestionOrchestrator
from app.services.ingestion.runners import StixPackagesSourceRunner
from app.services.stix.stix_service import (
    KNOWN_VERSIONS,
    StixService,
    StixVersionCatalog,
    _semantic_version_key,
    set_version_catalog,
)


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + ORM schema (removed on exit).

    Built straight from the ORM metadata (``Base.metadata.create_all``), so the
    six STIX tables exist without an Alembic run — mirroring the sibling route
    tests. On a fresh DB no version is persisted.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stix_endpoints_")
    db_path = Path(tmp_dir) / "stix_endpoints.db"
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


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """A TestClient over an ephemeral SQLite DB.

    The routes resolve the session via ``get_db``; we override it onto the
    ephemeral SQLite from :func:`session_factory`, so a version is persisted only
    when a test seeds it.
    """

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = _get_db_override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_get_bases_200_ordered_and_all_not_downloaded(client: TestClient) -> None:
    """``GET /api/stix/bases`` returns 200 with every known base, ordered
    newest->oldest, all ``downloaded``/``selectable`` false on a fresh database."""
    resp = client.get("/api/stix/bases")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    bases = body["bases"]
    assert {b["version"] for b in bases} == set(KNOWN_VERSIONS)

    # Strictly descending by semantic version (newest first).
    keys = [_semantic_version_key(b["version"]) for b in bases]
    for anterior, proximo in itertools.pairwise(keys):
        assert anterior > proximo, f"ordem decrescente violada: {anterior} !> {proximo}"

    # Fresh database: nothing downloaded.
    for base in bases:
        assert base["downloaded"] is False
        assert base["selectable"] is False


def test_get_dashboard_route_removed_404(client: TestClient) -> None:
    """The legacy ``/dashboard`` route no longer exists (task 5.3): its function
    is now served by ``/overview``. A request to the old path is a 404."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/dashboard")
    assert resp.status_code == 404, resp.text


def test_get_campaigns_version_not_persisted_422_unavailable_bundle(
    client: TestClient,
) -> None:
    """A campaigns request for a not-persisted version is 422 ``bundle_indisponivel``.

    The campaigns route now reads exclusively from the database
    (:class:`StixPackageReadService`). With no ``stix_packages`` row for the
    version, the handler maps the typed ``StixPackageNotPersistedError`` to the
    minimal ``bundle_indisponivel`` 422 contract (task 5.4 adds the on-demand
    backfill)."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/campaigns")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]


def test_get_actors_route_removed_404(client: TestClient) -> None:
    """The legacy ``/actors`` route no longer exists (task 5.3): it had no
    consumer and was removed. A request to the old path is a 404."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/actors")
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Happy-path tests for the DB-backed read routes (task 5.2). These seed a
# persisted package into the ephemeral SQLite that the client's get_db override
# points at, then assert each read route serves the DB content.
# ---------------------------------------------------------------------------


def _seed_persisted_package(session_factory: sessionmaker[Session]) -> str:
    """Seed one persisted STIX package with counts, a campaign, a technique and
    an attributed actor; return the seeded version."""
    version = "18.0"
    session = session_factory()
    try:
        package = StixPackage(version=version, name="Enterprise ATT&CK v18.0")
        session.add(package)
        session.flush()

        session.add_all(
            [
                StixTypeCount(
                    package_id=package.id,
                    stix_type="attack-pattern",
                    label="techniques",
                    count=3,
                    description="ATT&CK techniques",
                ),
                StixTypeCount(
                    package_id=package.id,
                    stix_type="x-mitre-tactic",
                    label="tactics",
                    count=2,
                    description="ATT&CK tactics",
                ),
            ]
        )

        campaign = StixCampaign(
            package_id=package.id,
            stix_id="campaign--abc",
            name="Example Campaign",
            external_id="C0045",
            description="An example campaign.",
            aliases=["Alias A", "Alias B"],
            curated=True,
            curated_slug="example-campaign",
        )
        session.add(campaign)
        session.flush()

        session.add(
            StixCampaignTechnique(
                campaign_id=campaign.id,
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
                description="Uses a scripting interpreter.",
            )
        )

        actor = StixActor(
            package_id=package.id,
            stix_id="intrusion-set--xyz",
            name="Example Actor",
        )
        session.add(actor)
        session.flush()
        session.add(StixActorCampaign(actor_id=actor.id, campaign_id=campaign.id))

        session.commit()
    finally:
        session.close()
    return version


def test_get_overview_persisted_version_200(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """``GET /overview`` of a persisted version returns the DB-backed overview."""
    version = _seed_persisted_package(session_factory)
    resp = client.get(f"/api/stix/bases/{version}/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == version
    assert body["name"] == "Enterprise ATT&CK v18.0"
    assert body["campaigns_total"] == 1
    assert body["actors_total"] == 1
    assert body["techniques_total"] == 3
    assert body["tactics_total"] == 2
    assert {c["stix_type"] for c in body["counts"]} == {
        "attack-pattern",
        "x-mitre-tactic",
    }


def test_get_campaigns_persisted_version_200_summary_list(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """``GET /campaigns`` of a persisted version returns the summary list from DB."""
    version = _seed_persisted_package(session_factory)
    resp = client.get(f"/api/stix/bases/{version}/campaigns")
    assert resp.status_code == 200, resp.text
    campaigns = resp.json()
    assert isinstance(campaigns, list)
    assert len(campaigns) == 1
    summary = campaigns[0]
    # campaign_key is the curated slug when present (Req. 4.5).
    assert summary["campaign_key"] == "example-campaign"
    assert summary["stix_id"] == "campaign--abc"
    assert summary["name"] == "Example Campaign"
    assert summary["curated"] is True


def test_get_campaign_overview_persisted_version_200(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """``GET /campaigns/{key}/overview`` returns the campaign's DB-backed view."""
    version = _seed_persisted_package(session_factory)
    resp = client.get(f"/api/stix/bases/{version}/campaigns/example-campaign/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_key"] == "example-campaign"
    assert body["stix_id"] == "campaign--abc"
    assert body["aliases"] == ["Alias A", "Alias B"]
    assert [t["technique_id"] for t in body["techniques"]] == ["T1059"]
    assert [a["stix_id"] for a in body["actors"]] == ["intrusion-set--xyz"]


def test_get_campaign_overview_unknown_key_404(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """An unknown ``campaign_key`` on a persisted version is 404
    ``campanha_nao_encontrada``."""
    version = _seed_persisted_package(session_factory)
    resp = client.get(f"/api/stix/bases/{version}/campaigns/does-not-exist/overview")
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "campanha_nao_encontrada"
    assert detail["mensagem"]


def test_get_overview_version_not_persisted_422(client: TestClient) -> None:
    """``GET /overview`` of a not-persisted version is 422 ``bundle_indisponivel``."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/overview")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]


# ---------------------------------------------------------------------------
# Analytics route — GET /api/stix/bases/{version}/analytics (task 10.4). These
# cover the DB-backed analytics dashboard over the same ephemeral SQLite and
# hermetic file access the read routes use: a seeded persisted version serves
# 200 with correct rankings/totals, an on-disk-but-not-persisted version is
# backfilled on demand and then served, and a version with neither a row nor a
# file maps to the ``bundle_indisponivel`` 422 contract.
# ---------------------------------------------------------------------------


def test_get_analytics_persisted_version_200_rankings_and_totals(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """``GET /analytics`` of a persisted version returns the DB-backed analytics.

    The seeded package has one curated campaign (one technique on the
    ``execution`` tactic) and one actor attributed to it, so the aggregates are
    deterministic: one curated campaign, zero uncurated, the actor ranked with
    one attributed campaign, the campaign ranked with one technique and a single
    ``execution`` tactic bucket.

    # Feature: stix-package-persistence-and-overview
    Validates: Requirements 12.1 (analytics read from the database).
    """
    version = _seed_persisted_package(session_factory)
    resp = client.get(f"/api/stix/bases/{version}/analytics")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["version"] == version
    # curated vs uncurated totals from the seeded rows (Req. 12.6).
    assert body["curated_total"] == 1
    assert body["uncurated_total"] == 0

    # top_actors: the one seeded actor ranked by its single attributed campaign
    # (Req. 12.2).
    assert len(body["top_actors"]) == 1
    actor = body["top_actors"][0]
    assert actor["stix_id"] == "intrusion-set--xyz"
    assert actor["name"] == "Example Actor"
    assert actor["campaigns_total"] == 1

    # top_campaigns: the one seeded campaign ranked by its single technique,
    # keyed by its curated slug (Req. 12.3, 12.5).
    assert len(body["top_campaigns"]) == 1
    campaign = body["top_campaigns"][0]
    assert campaign["campaign_key"] == "example-campaign"
    assert campaign["name"] == "Example Campaign"
    assert campaign["curated"] is True
    assert campaign["techniques_total"] == 1

    # tactics: a single ``execution`` bucket from the seeded technique (Req. 12.4).
    assert body["tactics"] == [{"tactic": "execution", "techniques_total": 1}]

    # top_techniques: the one seeded technique, used by a single campaign — the
    # inverse ranking (campaigns per technique).
    assert len(body["top_techniques"]) == 1
    technique = body["top_techniques"][0]
    assert technique["technique_id"] == "T1059"
    assert technique["campaigns_total"] == 1


def test_analytics_version_not_persisted_422(client: TestClient) -> None:
    """``GET /analytics`` of a not-persisted version is 422 ``bundle_indisponivel``."""
    resp = client.get(f"/api/stix/bases/{KNOWN_VERSIONS[0]}/analytics")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]


def test_campaign_overview_version_not_persisted_422(client: TestClient) -> None:
    """The campaign overview of a not-persisted version is 422, never a file read."""
    resp = client.get(f"/api/stix/bases/{KNOWN_VERSIONS[0]}/campaigns/shadowray/overview")
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["motivo"] == "bundle_indisponivel"


# ---------------------------------------------------------------------------
# Download POST: background ingestion through the orchestrator (task 21.3).
# A fake HTTP client serves the bundle from memory (no network, no disk).
# ---------------------------------------------------------------------------

_VERSION = KNOWN_VERSIONS[0]


@pytest.fixture()
def ingest_client(
    session_factory: sessionmaker[Session],
) -> Iterator[tuple[TestClient, FakeStixHttp, IngestionOrchestrator]]:
    """A client whose ``app.state.ingestion`` runs the STIX runner on a fake fetcher."""
    http = FakeStixHttp({_VERSION: small_bundle()})
    runner = StixPackagesSourceRunner(
        stix_service=StixService(http_client=http),
        versions=[],
        required_versions=[],
        index_vectors=False,
    )
    orchestrator = IngestionOrchestrator(session_factory, {IngestSource.STIX_PACKAGES: runner})

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.state.ingestion = orchestrator
    app.dependency_overrides[get_db] = _get_db_override
    try:
        yield TestClient(app), http, orchestrator
    finally:
        app.dependency_overrides.clear()


def test_download_runs_ingestion_in_background_and_persists(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
    session_factory: sessionmaker[Session],
) -> None:
    """POST answers 202 and the orchestrator persists the version in the database."""
    client, http, orchestrator = ingest_client
    assert client.get("/api/stix/bases").json()["bases"][0]["downloaded"] is False

    resp = client.post(f"/api/stix/bases/{_VERSION}/download")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["version"] == _VERSION
    assert body["persisted"] is False

    orchestrator.join(10)
    with session_factory() as session:
        assert (
            session.execute(
                select(func.count()).select_from(StixPackage).where(StixPackage.version == _VERSION)
            ).scalar_one()
            == 1
        )
    listed = {b["version"]: b for b in client.get("/api/stix/bases").json()["bases"]}
    assert listed[_VERSION]["downloaded"] is True
    assert listed[_VERSION]["selectable"] is True
    assert listed[_VERSION]["completed"] is True
    state = next(s for s in orchestrator.status() if s.source == IngestSource.STIX_PACKAGES)
    assert state.status == IngestStatus.READY
    assert len(http.calls) == 1


def test_download_of_persisted_version_is_idempotent_200_without_network(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
) -> None:
    client, http, orchestrator = ingest_client
    client.post(f"/api/stix/bases/{_VERSION}/download")
    orchestrator.join(10)
    calls = len(http.calls)

    again = client.post(f"/api/stix/bases/{_VERSION}/download")
    assert again.status_code == 200, again.text
    assert again.json()["persisted"] is True
    assert len(http.calls) == calls


def test_download_unknown_version_422_download_failure(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown version is refused before any network access or DB write."""
    client, http, _orchestrator = ingest_client
    unknown_version = "0.0-unknown"
    assert unknown_version not in KNOWN_VERSIONS

    resp = client.post(f"/api/stix/bases/{unknown_version}/download")

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "falha_download"
    assert detail["mensagem"]
    assert http.calls == []
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(StixPackage)).scalar_one() == 0


def test_download_network_failure_marks_source_failed_and_persists_nothing(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
    session_factory: sessionmaker[Session],
) -> None:
    client, http, orchestrator = ingest_client
    http.fail_versions.add(_VERSION)

    assert client.post(f"/api/stix/bases/{_VERSION}/download").status_code == 202
    orchestrator.join(10)

    state = next(s for s in orchestrator.status() if s.source == IngestSource.STIX_PACKAGES)
    assert state.status == IngestStatus.FAILED
    assert state.error == "network: StixDownloadError"
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(StixPackage)).scalar_one() == 0


def test_bases_listing_includes_persisted_version_outside_the_index(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        session.add(StixPackage(version="99.9", name="Enterprise ATT&CK v99.9"))
        session.commit()

    listed = client.get("/api/stix/bases").json()["bases"]

    assert listed[0]["version"] == "99.9"
    assert listed[0]["downloaded"] is True


# ---------------------------------------------------------------------------
# Replacements of the removed ``/stage/1`` and ``/stage/2`` listings
# (agentic-only-flow-redesign task 8.2/8.3; Req. 1.2, 6.1).
# ---------------------------------------------------------------------------


def test_bases_completed_is_true_only_for_persisted_versions(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """``completed`` is exposed on every base and mirrors DB persistence."""
    before = client.get("/api/stix/bases").json()["bases"]
    assert before and all(b["completed"] is False for b in before)
    # The per-version ``curated`` field was removed (dead field, task 33.2).
    assert all("curated" not in b for b in before)

    version = _seed_persisted_package(session_factory)
    after = {b["version"]: b for b in client.get("/api/stix/bases").json()["bases"]}
    assert after[version]["completed"] is True
    assert after[version]["completed"] == after[version]["downloaded"]
    assert all(b["completed"] is False for v, b in after.items() if v != version)


def test_campaigns_listing_carries_container_emulability_fields(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Not derived -> ``None``/``[]``; derived -> the stored verdict and blockers."""
    from app.models.domain import ContainerEmulability

    version = _seed_persisted_package(session_factory)

    first = client.get(f"/api/stix/bases/{version}/campaigns").json()[0]
    assert first["container_emulable"] is None
    assert first["blocking_techniques"] == []

    with session_factory() as session:
        session.add(
            ContainerEmulability(
                campaign_id="example-campaign",
                container_emulable=False,
                blocking_techniques=["T1059"],
            )
        )
        session.commit()

    marked = client.get(f"/api/stix/bases/{version}/campaigns").json()[0]
    assert marked["campaign_key"] == "example-campaign"
    assert marked["container_emulable"] is False
    assert marked["blocking_techniques"] == ["T1059"]


# ---------------------------------------------------------------------------
# Version list from the (fake) upstream index (task 38.3). No network: the
# process-wide catalog gets an injected fetcher; the conftest restores it.
# ---------------------------------------------------------------------------


def _fake_catalog(*versions: str, error: Exception | None = None) -> StixVersionCatalog:
    def _fetcher(timeout: float):
        if error is not None:
            raise error
        return {
            "collections": [
                {
                    "name": "Enterprise ATT&CK",
                    "versions": [
                        {
                            "version": v,
                            "url": "https://example.invalid/enterprise-attack/"
                            f"enterprise-attack-{v}.json",
                            "modified": "2025-01-01T00:00:00.000Z",
                        }
                        for v in versions
                    ],
                }
            ]
        }

    catalog = StixVersionCatalog(_fetcher)
    set_version_catalog(catalog)
    return catalog


def test_bases_listing_from_the_index_puts_the_newest_first_and_keeps_the_contract(
    client: TestClient,
) -> None:
    _fake_catalog("19.1", "21.0", "20.1")

    bases = client.get("/api/stix/bases").json()["bases"]

    assert [b["version"] for b in bases] == ["21.0", "20.1", "19.1"]
    for base in bases:
        assert {"version", "name", "downloaded", "selectable", "completed"} <= set(base)
        assert "curated" not in base
        assert base["downloaded"] is base["selectable"] is base["completed"] is False


def test_bases_listing_never_hides_a_persisted_version_missing_from_the_index(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _fake_catalog("21.0", "20.1")
    with session_factory() as session:
        session.add(StixPackage(version="18.0", name="Enterprise ATT&CK v18.0"))
        session.commit()

    listed = {b["version"]: b for b in client.get("/api/stix/bases").json()["bases"]}

    assert list(listed) == ["21.0", "20.1", "18.0"]
    assert listed["18.0"]["downloaded"] is True
    assert listed["18.0"]["completed"] is True
    assert listed["21.0"]["downloaded"] is False


def test_bases_listing_offline_falls_back_to_known_versions(client: TestClient) -> None:
    _fake_catalog(error=OSError("offline"))

    bases = client.get("/api/stix/bases").json()["bases"]

    assert {b["version"] for b in bases} == set(KNOWN_VERSIONS)
    assert bases[0]["version"] == "20.1"


def test_bases_listing_uses_the_cache_between_requests(client: TestClient) -> None:
    calls: list[float] = []

    def _fetcher(timeout: float):
        calls.append(timeout)
        return {"collections": []}

    set_version_catalog(StixVersionCatalog(_fetcher))
    client.get("/api/stix/bases")
    client.get("/api/stix/bases")

    assert len(calls) == 1  # empty index: negative cache, not one query per request


def test_download_accepts_a_version_listed_by_the_index(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
    session_factory: sessionmaker[Session],
) -> None:
    client, http, orchestrator = ingest_client
    _fake_catalog("21.0", "20.1")
    assert "21.0" not in KNOWN_VERSIONS
    http.bundles["21.0"] = small_bundle()

    resp = client.post("/api/stix/bases/21.0/download")

    assert resp.status_code == 202, resp.text
    orchestrator.join(10)
    with session_factory() as session:
        assert set(session.execute(select(StixPackage.version)).scalars()) == {"21.0"}


def test_download_rejects_versions_outside_the_index_with_422(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
    session_factory: sessionmaker[Session],
) -> None:
    client, http, _orchestrator = ingest_client
    _fake_catalog("21.0", "20.1")

    # Not in the index; "19.2" is only in the offline fallback, which applies
    # only while the index is unavailable.
    for version in ("99.9", "19.2"):
        resp = client.post(f"/api/stix/bases/{version}/download")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["motivo"] == "falha_download"

    assert http.calls == []
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(StixPackage)).scalar_one() == 0


def test_download_offline_accepts_fallback_versions_and_rejects_the_rest(
    ingest_client: tuple[TestClient, FakeStixHttp, IngestionOrchestrator],
) -> None:
    client, http, orchestrator = ingest_client
    _fake_catalog(error=OSError("offline"))
    http.bundles["19.2"] = small_bundle()

    assert client.post("/api/stix/bases/19.2/download").status_code == 202
    orchestrator.join(10)
    rejected = client.post("/api/stix/bases/99.9/download")

    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["motivo"] == "falha_download"
    assert len(http.calls) == 1
