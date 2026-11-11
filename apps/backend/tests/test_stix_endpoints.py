"""Endpoint tests for ``/api/stix/*`` (stream-stix-dashboard, task 5.3).

Builds a FastAPI ``TestClient`` over ``app.main:create_app`` (matching the style
of ``test_api_endpoints.py``) and exercises the STIX inspection endpoints that
are reachable **without any network egress**:

* ``GET /api/stix/bases`` — 200, bases ordered newest->oldest, all ``downloaded``
  false on a fresh (empty) data dir;
* ``GET /api/stix/bases/{version}/overview`` — 422 ``bundle_indisponivel`` for a
  version that is not persisted (DB-backed via ``StixPackageReadService``);
* ``GET /api/stix/bases/{version}/campaigns`` — 422 ``bundle_indisponivel`` for a
  version that is not persisted (DB-backed via ``StixPackageReadService``);
* ``GET /api/stix/bases/{version}/dashboard`` and ``.../actors`` — 404, the legacy
  routes removed by task 5.3 (``/overview`` subsumes dashboard; actors had no
  consumer).

Isolation
---------
The ``GET /api/stix/bases`` handler constructs ``StixService()`` with the default
data dir (the real ``sticks/data/stix``, which may already hold a downloaded
base in the environment). To keep the tests deterministic and hermetic, the
``StixService`` symbol used by the handlers is monkeypatched to a factory that
pins ``data_dir`` to an empty ``tmp_path``.

The DB-backed read routes read only from the database, so the ``get_db``
dependency is overridden onto an ephemeral temp-file SQLite built straight from
the ORM (``Base.metadata.create_all``), mirroring the sibling route tests
(``test_ai_run_ground_truth.py`` et al.). On a fresh DB the version has no
``stix_packages`` row, so the campaigns route surfaces ``bundle_indisponivel``.

No real network is touched: the download POST (the only egress) is exercised
only through an injected fake ``http_client`` that serves a valid bundle from
memory (its success contract) or against an unknown version that the service
rejects before any disk/network access (its ``falha_download`` contract).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

import app.api.stix as stix_api
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
from app.services.stix.stix_service import (
    KNOWN_VERSIONS,
    StixService,
    _semantic_version_key,
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
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> Iterator[TestClient]:
    """A TestClient with hermetic STIX file access and an ephemeral SQLite DB.

    * The ``GET /api/stix/bases`` handler calls ``StixService()`` with no args;
      we replace the ``StixService`` name in the ``app.api.stix`` module with a
      factory that injects ``data_dir=tmp_path`` so the listing reads from an
      empty, isolated directory (every base is 'not downloaded').
    * The DB-backed routes resolve the session via ``get_db``; we override it
      onto the ephemeral SQLite from :func:`session_factory`, so a version is
      persisted only when a test seeds it.
    """

    def _factory() -> StixService:
        return StixService(data_dir=tmp_path)

    monkeypatch.setattr(stix_api, "StixService", _factory)

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
    newest->oldest, all ``downloaded``/``selectable`` false on a fresh dir."""
    resp = client.get("/api/stix/bases")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    bases = body["bases"]
    assert {b["version"] for b in bases} == set(KNOWN_VERSIONS)

    # Strictly descending by semantic version (newest first).
    keys = [_semantic_version_key(b["version"]) for b in bases]
    for anterior, proximo in zip(keys, keys[1:]):
        assert anterior > proximo, f"ordem decrescente violada: {anterior} !> {proximo}"

    # Fresh data dir: nothing downloaded.
    for base in bases:
        assert base["downloaded"] is False
        assert base["selectable"] is False
        assert base["file"] is None


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
        session.add(
            StixActorCampaign(actor_id=actor.id, campaign_id=campaign.id)
        )

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
    resp = client.get(
        f"/api/stix/bases/{version}/campaigns/example-campaign/overview"
    )
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
    resp = client.get(
        f"/api/stix/bases/{version}/campaigns/does-not-exist/overview"
    )
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
# On-demand backfill and the full error contract (task 5.4). These exercise the
# read routes against a version that is NOT persisted, with the STIX file access
# pinned to the same ``tmp_path`` the client fixture uses — so a bundle written
# into ``tmp_path`` is the on-disk file the backfill reads (``persist_from_disk``,
# no network egress), and an absent file yields ``bundle_indisponivel``.
# ---------------------------------------------------------------------------

# A known version so the StixService path/naming matches production. The download
# path is never exercised — the bundle is written straight to disk and the read
# route triggers the on-demand backfill, so there is no network egress.
_BACKFILL_VERSION = KNOWN_VERSIONS[0]
_BACKFILL_CAMPAIGN_ID = "campaign--00000000-0000-0000-0000-000000000001"
_BACKFILL_PATTERN_ID = "attack-pattern--00000000-0000-0000-0000-00000000000a"
_BACKFILL_ACTOR_ID = "intrusion-set--00000000-0000-0000-0000-0000000000f1"


def _backfill_bundle() -> dict:
    """One concrete STIX bundle with a curated campaign, a technique and an actor.

    Mirrors the shape the persistence unit tests use: a curated campaign
    (``external_id`` ``"C0045"`` resolves the ``shadowray`` slug), one
    attack-pattern it uses and one intrusion-set attributed to it, plus a tactic
    and a malware object so the per-type counts are non-zero.
    """
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "spec_version": "2.1",
        "objects": [
            {
                "type": "campaign",
                "id": _BACKFILL_CAMPAIGN_ID,
                "name": "ShadowRay",
                "description": "A curated campaign for the backfill test.",
                "aliases": ["Shadow", "Ray"],
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C0045"}
                ],
            },
            {
                "type": "attack-pattern",
                "id": _BACKFILL_PATTERN_ID,
                "name": "Command and Scripting Interpreter",
                "description": "Technique description.",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "T1059"}
                ],
                "kill_chain_phases": [
                    {"kill_chain_name": "mitre-attack", "phase_name": "execution"}
                ],
            },
            {
                "type": "x-mitre-tactic",
                "id": "x-mitre-tactic--00000000-0000-0000-0000-0000000000c1",
                "name": "Execution",
            },
            {
                "type": "malware",
                "id": "malware--00000000-0000-0000-0000-0000000000d1",
                "name": "ExampleMalware",
            },
            {
                "type": "intrusion-set",
                "id": _BACKFILL_ACTOR_ID,
                "name": "APT-Example",
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000011",
                "relationship_type": "uses",
                "source_ref": _BACKFILL_CAMPAIGN_ID,
                "target_ref": _BACKFILL_PATTERN_ID,
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000013",
                "relationship_type": "attributed-to",
                "source_ref": _BACKFILL_CAMPAIGN_ID,
                "target_ref": _BACKFILL_ACTOR_ID,
            },
        ],
    }


def _write_backfill_bundle(data_dir: Path) -> Path:
    """Write the backfill bundle into ``data_dir`` as the on-disk file."""
    bundle_path = data_dir / f"enterprise-attack-{_BACKFILL_VERSION}.json"
    bundle_path.write_text(json.dumps(_backfill_bundle()), encoding="utf-8")
    return bundle_path


def test_overview_backfills_from_disk_then_returns_200(
    client: TestClient,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    """A version present on disk but not persisted is backfilled, then served.

    The read route finds no ``stix_packages`` row, sees the on-disk bundle,
    triggers the on-demand backfill (``persist_from_disk``, no network egress —
    Req. 8.1, 8.2) and then responds 200 with the DB-backed overview. After the
    request the version is persisted (Req. 8.3) and the on-disk Bundle_File has
    been deleted best-effort following the successful commit (Req. 6.3).
    """
    bundle_path = _write_backfill_bundle(tmp_path)
    assert bundle_path.exists()  # precondition: the file is on disk

    # Precondition: nothing persisted yet.
    probe = session_factory()
    try:
        assert (
            probe.execute(
                select(func.count())
                .select_from(StixPackage)
                .where(StixPackage.version == _BACKFILL_VERSION)
            ).scalar_one()
            == 0
        )
    finally:
        probe.close()

    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/overview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == _BACKFILL_VERSION
    assert body["campaigns_total"] == 1
    assert body["actors_total"] == 1
    assert body["techniques_total"] == 1

    # The backfill made the version persisted (Req. 8.3).
    verify = session_factory()
    try:
        assert (
            verify.execute(
                select(func.count())
                .select_from(StixPackage)
                .where(StixPackage.version == _BACKFILL_VERSION)
            ).scalar_one()
            == 1
        )
    finally:
        verify.close()

    # The successful commit deleted the on-disk Bundle_File best-effort: the DB
    # is now the single source of truth for the version (Req. 6.3).
    assert not bundle_path.exists()


def test_campaigns_backfills_from_disk_then_returns_200(
    client: TestClient, tmp_path: Path
) -> None:
    """The campaigns route backfills an on-disk, not-persisted version then serves.

    After the successful backfill commit the on-disk Bundle_File is deleted
    best-effort (Req. 6.3).
    """
    bundle_path = _write_backfill_bundle(tmp_path)
    assert bundle_path.exists()  # precondition: the file is on disk
    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/campaigns")
    assert resp.status_code == 200, resp.text
    campaigns = resp.json()
    assert len(campaigns) == 1
    assert campaigns[0]["campaign_key"] == "shadowray"
    # The successful commit deleted the on-disk Bundle_File (Req. 6.3).
    assert not bundle_path.exists()


def test_campaign_overview_backfills_then_returns_200(
    client: TestClient, tmp_path: Path
) -> None:
    """The campaign-overview route backfills on disk then serves the campaign.

    After the successful backfill commit the on-disk Bundle_File is deleted
    best-effort (Req. 6.3).
    """
    bundle_path = _write_backfill_bundle(tmp_path)
    assert bundle_path.exists()  # precondition: the file is on disk
    resp = client.get(
        f"/api/stix/bases/{_BACKFILL_VERSION}/campaigns/shadowray/overview"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_key"] == "shadowray"
    assert [t["technique_id"] for t in body["techniques"]] == ["T1059"]
    assert [a["stix_id"] for a in body["actors"]] == [_BACKFILL_ACTOR_ID]
    # The successful commit deleted the on-disk Bundle_File (Req. 6.3).
    assert not bundle_path.exists()


def test_overview_no_row_no_file_422_bundle_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    """Neither a persisted row nor an on-disk file → ``bundle_indisponivel`` 422.

    The client fixture pins STIX file access to an empty ``tmp_path``, so no
    bundle is on disk and no version is persisted: with no file to backfill from
    and nothing persisted, the read maps to ``bundle_indisponivel`` 422 without
    any network egress (Req. 6.1, 9.4).
    """
    # Precondition: no Bundle_File on disk for this version.
    assert not (
        tmp_path / f"enterprise-attack-{_BACKFILL_VERSION}.json"
    ).exists()
    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/overview")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]


def test_campaign_overview_unknown_key_after_backfill_404(
    client: TestClient, tmp_path: Path
) -> None:
    """An unknown ``campaign_key`` after the backfill is 404 ``campanha_nao_encontrada``.

    The backfill persists the on-disk bundle, then the campaign lookup for a key
    that is not in the package maps to the 404 contract (Req. 9.5).
    """
    _write_backfill_bundle(tmp_path)
    resp = client.get(
        f"/api/stix/bases/{_BACKFILL_VERSION}/campaigns/does-not-exist/overview"
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "campanha_nao_encontrada"
    assert detail["mensagem"]


def test_overview_invalid_disk_bundle_422_bundle_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    """An invalid on-disk bundle during backfill → ``bundle_indisponivel`` 422.

    When the file exists but is not a valid STIX bundle, ``persist_from_disk``
    raises ``StixFileError`` (there is no valid bundle to serve) and the route
    maps it to ``bundle_indisponivel`` 422 (Req. 9.4). No DB row is written.
    """
    bundle_path = tmp_path / f"enterprise-attack-{_BACKFILL_VERSION}.json"
    bundle_path.write_text("not valid json at all", encoding="utf-8")
    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/overview")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]


def test_overview_backfill_db_write_failure_502_persistence_failure(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB write failure during backfill → ``falha_persistencia`` 502 (Req. 9.3).

    The on-disk bundle is valid, so the backfill reaches the DB write; a
    simulated persistence failure (raised from ``persist_from_disk``) must map to
    the ``falha_persistencia`` 502 contract rather than surfacing a 500.
    """
    _write_backfill_bundle(tmp_path)

    def _boom(self: object, version: str) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(
        stix_api.StixPersistenceService, "persist_from_disk", _boom
    )

    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/overview")
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "falha_persistencia"
    assert detail["mensagem"]

# ---------------------------------------------------------------------------
# Download POST — the single write/egress route (task 5.5). These exercise the
# only side-effecting route without any real network: a fake ``http_client``
# injected into ``StixService`` serves a valid bundle from memory (success
# contract), and an unknown version is rejected by the service before any disk
# or network access (``falha_download`` contract). Both pin ``data_dir`` to the
# client's ``tmp_path`` so the atomic write lands in the isolated directory.
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    """A minimal stand-in for the ``requests``-style response ``StixService`` uses.

    ``StixService._download_content`` calls ``raise_for_status()`` when present
    and otherwise checks ``status_code``, then reads ``text``. This fake serves a
    200 with the bundle text, so no real network egress happens.
    """

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpClient:
    """A fake HTTP client whose ``get`` always serves the same bundle text."""

    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, url: str, timeout: int | None = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(self._text)


def test_download_success_persists_and_reports_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``POST /download`` with a fake client serving a valid bundle is 200.

    The response carries ``downloaded=true`` and ``persisted=true`` and the
    version becomes persisted in the DB — the single write/egress route, driven
    by an injected fake ``http_client`` so there is no real network.

    # Feature: stix-package-persistence-and-overview
    Validates: Requirements 5.1, 5.2 (single surface), 1.1, 1.5 (download
    success contract), 8.1 (no-row precondition before the write).
    """
    bundle_text = json.dumps(_backfill_bundle())

    def _factory() -> StixService:
        return StixService(data_dir=tmp_path, http_client=_FakeHttpClient(bundle_text))

    monkeypatch.setattr(stix_api, "StixService", _factory)

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = _get_db_override
    try:
        local_client = TestClient(app)

        # Precondition: nothing persisted and nothing on disk yet.
        assert not (tmp_path / f"enterprise-attack-{_BACKFILL_VERSION}.json").exists()
        probe = session_factory()
        try:
            assert (
                probe.execute(
                    select(func.count())
                    .select_from(StixPackage)
                    .where(StixPackage.version == _BACKFILL_VERSION)
                ).scalar_one()
                == 0
            )
        finally:
            probe.close()

        resp = local_client.post(f"/api/stix/bases/{_BACKFILL_VERSION}/download")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == _BACKFILL_VERSION
        assert body["downloaded"] is True
        assert body["persisted"] is True

        # The write committed: the version is now persisted in the DB (Req. 1.1).
        verify = session_factory()
        try:
            assert (
                verify.execute(
                    select(func.count())
                    .select_from(StixPackage)
                    .where(StixPackage.version == _BACKFILL_VERSION)
                ).scalar_one()
                == 1
            )
        finally:
            verify.close()
    finally:
        app.dependency_overrides.clear()


def test_download_unknown_version_422_download_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``POST /download`` for an unknown version is 422 ``falha_download``.

    ``StixService`` rejects the unknown version before any disk or network
    access, so the route maps the typed ``StixDownloadError`` to the
    ``falha_download`` 422 contract and writes nothing to the DB.

    # Feature: stix-package-persistence-and-overview
    Validates: Requirements 9.1 (download error mapping), 5.1, 5.2.
    """

    def _factory() -> StixService:
        # A fake client that would fail loudly if ever called — it must not be,
        # since an unknown version is rejected before any fetch.
        return StixService(data_dir=tmp_path, http_client=_FakeHttpClient("{}"))

    monkeypatch.setattr(stix_api, "StixService", _factory)

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = _get_db_override
    try:
        local_client = TestClient(app)
        unknown_version = "0.0-unknown"
        assert unknown_version not in KNOWN_VERSIONS

        resp = local_client.post(f"/api/stix/bases/{unknown_version}/download")
        assert resp.status_code == 422, resp.text
        detail = resp.json()["detail"]
        assert detail["motivo"] == "falha_download"
        assert detail["mensagem"]

        # Nothing was written to the DB and no file landed on disk.
        assert not (
            tmp_path / f"enterprise-attack-{unknown_version}.json"
        ).exists()
        probe = session_factory()
        try:
            assert (
                probe.execute(
                    select(func.count()).select_from(StixPackage)
                ).scalar_one()
                == 0
            )
        finally:
            probe.close()
    finally:
        app.dependency_overrides.clear()


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


def test_get_analytics_backfills_from_disk_then_returns_200(
    client: TestClient,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
) -> None:
    """A version on disk but not persisted is backfilled, then analytics served.

    The analytics route finds no ``stix_packages`` row, sees the on-disk bundle,
    triggers the on-demand backfill (``persist_from_disk``, no network egress)
    and then responds 200 with the DB-backed analytics. After the request the
    version is persisted and the on-disk Bundle_File has been deleted best-effort
    following the successful commit (Req. 6.3).

    # Feature: stix-package-persistence-and-overview
    Validates: Requirements 12.7 (on-demand backfill for the analytics route).
    """
    bundle_path = _write_backfill_bundle(tmp_path)
    assert bundle_path.exists()  # precondition: the file is on disk

    # Precondition: nothing persisted yet.
    probe = session_factory()
    try:
        assert (
            probe.execute(
                select(func.count())
                .select_from(StixPackage)
                .where(StixPackage.version == _BACKFILL_VERSION)
            ).scalar_one()
            == 0
        )
    finally:
        probe.close()

    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/analytics")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == _BACKFILL_VERSION
    # The backfill bundle has one curated campaign (external_id C0045).
    assert body["curated_total"] == 1
    assert body["uncurated_total"] == 0
    assert [a["stix_id"] for a in body["top_actors"]] == [_BACKFILL_ACTOR_ID]
    assert body["top_actors"][0]["campaigns_total"] == 1
    assert [c["campaign_key"] for c in body["top_campaigns"]] == ["shadowray"]
    assert body["top_campaigns"][0]["techniques_total"] == 1
    assert body["tactics"] == [{"tactic": "execution", "techniques_total": 1}]
    assert [tech["technique_id"] for tech in body["top_techniques"]] == ["T1059"]
    assert body["top_techniques"][0]["campaigns_total"] == 1

    # The backfill made the version persisted (Req. 12.7).
    verify = session_factory()
    try:
        assert (
            verify.execute(
                select(func.count())
                .select_from(StixPackage)
                .where(StixPackage.version == _BACKFILL_VERSION)
            ).scalar_one()
            == 1
        )
    finally:
        verify.close()

    # The successful commit deleted the on-disk Bundle_File best-effort (Req. 6.3).
    assert not bundle_path.exists()


def test_get_analytics_no_row_no_file_422_bundle_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    """Neither a persisted row nor an on-disk file → ``bundle_indisponivel`` 422.

    The client fixture pins STIX file access to an empty ``tmp_path``, so no
    bundle is on disk and no version is persisted: with no file to backfill from
    and nothing persisted, the analytics route maps the typed
    ``StixPackageNotPersistedError`` to the ``bundle_indisponivel`` 422 contract
    (Req. 6.1).

    # Feature: stix-package-persistence-and-overview
    Validates: Requirements 12.7 (no row nor file → bundle_indisponivel 422).
    """
    # Precondition: no Bundle_File on disk for this version.
    assert not (
        tmp_path / f"enterprise-attack-{_BACKFILL_VERSION}.json"
    ).exists()
    resp = client.get(f"/api/stix/bases/{_BACKFILL_VERSION}/analytics")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]
