"""Deterministic example tests for ``download_base`` HTTP error mapping and the
best-effort bundle deletion at the download endpoint (task 4.3).

These are *example* (not property) tests: they pin one concrete scenario each
for the full ``download_base`` error contract and the ``_read_with_backfill``
unavailable path, plus the best-effort-deletion guarantee that a failing
``unlink`` after a successful commit still yields ``persisted=true`` from the
endpoint.

Cases covered
-------------
* ``download_base`` with an **unknown version** → ``falha_download`` 422: the
  service rejects the version before touching disk or network (Req. 7.4).
* ``download_base`` with a **network failure** → ``falha_download`` 502: a fake
  ``http_client`` raises on ``get``, so the typed ``StixDownloadError`` for a
  known version maps to the 502 upstream-failure contract (Req. 7.1).
* ``download_base`` with a **DB write failure** → ``falha_persistencia`` 502: the
  download succeeds but the persistence write raises, so the endpoint maps the
  generic error (distinct from ``StixDownloadError``) to the 502 persistence
  contract (Req. 7.1 companion — the persistence failure branch).
* ``_read_with_backfill`` with **no file and no persisted row** →
  ``bundle_indisponivel`` 422: a read for a version that is neither persisted nor
  on disk maps to the unavailable contract without any network egress
  (Req. 5.3, 6.1).
* ``download_base`` where **``unlink`` fails after the commit** → the endpoint
  still returns ``persisted=true``: the best-effort deletion swallows the
  ``OSError`` and never reverts the persistence (Req. 2.3).

Isolation
---------
Mirrors ``test_stix_endpoints.py``: the ``StixService`` symbol used by the
handlers is monkeypatched to a factory that pins ``data_dir`` to an empty
``tmp_path`` (and, where a download is exercised, injects a fake ``http_client``
so no real network is touched), and the ``get_db`` dependency is overridden onto
an ephemeral temp-file SQLite built straight from the ORM metadata. Every case
is deterministic and hermetic.
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
from app.models.domain import StixPackage
from app.services.stix.stix_service import KNOWN_VERSIONS, StixService

# A known version so the StixService path/naming matches production.
_VERSION = KNOWN_VERSIONS[0]
_CAMPAIGN_ID = "campaign--00000000-0000-0000-0000-000000000001"
_PATTERN_ID = "attack-pattern--00000000-0000-0000-0000-00000000000a"
_ACTOR_ID = "intrusion-set--00000000-0000-0000-0000-0000000000f1"


# ---------------------------------------------------------------------------
# Fixtures (ephemeral SQLite from the ORM; mirrors the sibling route tests).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + ORM schema (removed on exit)."""
    tmp_dir = tempfile.mkdtemp(prefix="stix_download_errors_")
    db_path = Path(tmp_dir) / "stix_download_errors.db"
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


def _valid_bundle() -> dict:
    """One concrete, valid STIX bundle (a curated campaign, a technique, an actor)."""
    return {
        "type": "bundle",
        "id": "bundle--00000000-0000-0000-0000-000000000000",
        "spec_version": "2.1",
        "objects": [
            {
                "type": "campaign",
                "id": _CAMPAIGN_ID,
                "name": "ShadowRay",
                "description": "A curated campaign for the download test.",
                "aliases": ["Shadow", "Ray"],
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C0045"}
                ],
            },
            {
                "type": "attack-pattern",
                "id": _PATTERN_ID,
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
                "type": "intrusion-set",
                "id": _ACTOR_ID,
                "name": "APT-Example",
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000011",
                "relationship_type": "uses",
                "source_ref": _CAMPAIGN_ID,
                "target_ref": _PATTERN_ID,
            },
            {
                "type": "relationship",
                "id": "relationship--00000000-0000-0000-0000-000000000013",
                "relationship_type": "attributed-to",
                "source_ref": _CAMPAIGN_ID,
                "target_ref": _ACTOR_ID,
            },
        ],
    }


# ---------------------------------------------------------------------------
# Fake HTTP clients — the single network egress, served from memory or failing.
# ---------------------------------------------------------------------------


class _FakeHttpResponse:
    """Minimal ``requests``-style response ``StixService._download_content`` uses."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


class _ServingHttpClient:
    """A fake HTTP client whose ``get`` always serves the same bundle text."""

    def __init__(self, text: str) -> None:
        self._text = text

    def get(self, url: str, timeout: int | None = None) -> _FakeHttpResponse:
        return _FakeHttpResponse(self._text)


class _FailingHttpClient:
    """A fake HTTP client whose ``get`` always raises (a network failure)."""

    def get(self, url: str, timeout: int | None = None) -> _FakeHttpResponse:
        raise ConnectionError("simulated network failure reaching the MITRE repo")


class _MustNotFetchHttpClient:
    """A fake HTTP client that fails loudly if ever called.

    Used by the unknown-version case, where the fetch must never happen (the
    version is rejected before any disk/network access).
    """

    def get(self, url: str, timeout: int | None = None) -> _FakeHttpResponse:  # pragma: no cover - must not run
        raise AssertionError("network egress must not happen for an unknown version")


# ---------------------------------------------------------------------------
# Client builder — pins data_dir to tmp_path and overrides get_db.
# ---------------------------------------------------------------------------


def _build_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    http_client: object | None,
) -> TestClient:
    """A TestClient with hermetic STIX file access and an ephemeral SQLite DB."""

    def _factory() -> StixService:
        if http_client is None:
            return StixService(data_dir=tmp_path)
        return StixService(data_dir=tmp_path, http_client=http_client)

    monkeypatch.setattr(stix_api, "StixService", _factory)

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# download_base error mapping
# ---------------------------------------------------------------------------


def test_download_unknown_version_maps_to_download_failure_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """An unknown version maps to ``falha_download`` 422 and writes nothing.

    ``StixService`` rejects the version before any disk or network access, so the
    endpoint maps the typed ``StixDownloadError`` to the 422 contract (Req. 7.4).
    """
    client = _build_client(
        tmp_path, monkeypatch, session_factory, _MustNotFetchHttpClient()
    )
    unknown_version = "0.0-unknown"
    assert unknown_version not in KNOWN_VERSIONS

    resp = client.post(f"/api/stix/bases/{unknown_version}/download")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "falha_download"
    assert detail["mensagem"]

    # Nothing written to the DB and no file landed on disk.
    assert not (tmp_path / f"enterprise-attack-{unknown_version}.json").exists()
    probe = session_factory()
    try:
        assert probe.execute(select(func.count()).select_from(StixPackage)).scalar_one() == 0
    finally:
        probe.close()


def test_download_network_failure_maps_to_download_failure_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """A network failure on a known version maps to ``falha_download`` 502.

    The fake ``http_client`` raises on ``get``, so ``StixService.download_base``
    wraps it in a typed ``StixDownloadError`` for a KNOWN version; the endpoint
    maps it to the 502 upstream-failure contract and writes nothing (Req. 7.1).
    """
    client = _build_client(
        tmp_path, monkeypatch, session_factory, _FailingHttpClient()
    )

    resp = client.post(f"/api/stix/bases/{_VERSION}/download")
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "falha_download"
    assert detail["mensagem"]

    # No file landed on disk and nothing was persisted.
    assert not (tmp_path / f"enterprise-attack-{_VERSION}.json").exists()
    probe = session_factory()
    try:
        assert probe.execute(select(func.count()).select_from(StixPackage)).scalar_one() == 0
    finally:
        probe.close()


def test_download_db_write_failure_maps_to_persistence_failure_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """A DB write failure after a successful download maps to ``falha_persistencia`` 502.

    The download succeeds (fake client serves a valid bundle), but the
    persistence write raises a generic error; the endpoint catches it distinctly
    from ``StixDownloadError`` and maps it to the 502 persistence contract.
    """
    bundle_text = json.dumps(_valid_bundle())
    client = _build_client(
        tmp_path, monkeypatch, session_factory, _ServingHttpClient(bundle_text)
    )

    def _boom(self: object, version: str) -> None:
        raise RuntimeError("simulated DB write failure")

    monkeypatch.setattr(
        stix_api.StixPersistenceService, "download_and_persist", _boom
    )

    resp = client.post(f"/api/stix/bases/{_VERSION}/download")
    assert resp.status_code == 502, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "falha_persistencia"
    assert detail["mensagem"]


# ---------------------------------------------------------------------------
# _read_with_backfill unavailable path
# ---------------------------------------------------------------------------


def test_read_without_file_and_not_persisted_maps_to_bundle_unavailable_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """A read for a version with neither a file nor a persisted row → 422.

    ``_read_with_backfill`` finds no ``stix_packages`` row and no on-disk bundle
    (data_dir pinned to an empty ``tmp_path``), so it maps to
    ``bundle_indisponivel`` 422 without any network egress (Req. 5.3, 6.1).
    """
    client = _build_client(tmp_path, monkeypatch, session_factory, None)

    # Precondition: no Bundle_File on disk for this version.
    assert not (tmp_path / f"enterprise-attack-{_VERSION}.json").exists()

    resp = client.get(f"/api/stix/bases/{_VERSION}/overview")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_indisponivel"
    assert detail["mensagem"]


# ---------------------------------------------------------------------------
# Best-effort deletion at the endpoint
# ---------------------------------------------------------------------------


def test_download_still_persisted_when_unlink_fails_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``unlink`` failing after the commit still yields ``persisted=true``.

    The download and persistence commit succeed; the best-effort deletion of the
    on-disk Bundle_File then raises ``OSError``, which ``_delete_bundle_file``
    swallows. The endpoint must still respond 200 with ``persisted=true`` and the
    version must remain persisted — a deletion failure never reverts the
    persistence (Req. 2.3).
    """
    bundle_text = json.dumps(_valid_bundle())
    client = _build_client(
        tmp_path, monkeypatch, session_factory, _ServingHttpClient(bundle_text)
    )

    # Make the best-effort deletion fail: Path.unlink raises OSError. The commit
    # has already happened by the time _delete_bundle_file calls unlink, so this
    # exercises the post-commit best-effort swallow (Req. 2.3).
    real_unlink = Path.unlink

    def _failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == f"enterprise-attack-{_VERSION}.json":
            raise OSError("simulated unlink failure after commit")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    resp = client.post(f"/api/stix/bases/{_VERSION}/download")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == _VERSION
    assert body["downloaded"] is True
    assert body["persisted"] is True

    # The persistence committed despite the deletion failure: the row is present.
    verify = session_factory()
    try:
        assert (
            verify.execute(
                select(func.count())
                .select_from(StixPackage)
                .where(StixPackage.version == _VERSION)
            ).scalar_one()
            == 1
        )
    finally:
        verify.close()

    # The deletion failed, so the on-disk file is still there — proving the
    # deletion was attempted and its failure did not break the committed state.
    assert (tmp_path / f"enterprise-attack-{_VERSION}.json").exists()
