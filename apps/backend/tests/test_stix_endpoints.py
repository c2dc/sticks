"""Endpoint tests for ``/api/stix/*`` (stream-stix-dashboard, task 5.3).

Builds a FastAPI ``TestClient`` over ``app.main:create_app`` (matching the style
of ``test_api_endpoints.py``) and exercises the STIX inspection endpoints that
are reachable **without any network egress**:

* ``GET /api/stix/bases`` — 200, bases ordered newest->oldest, all ``downloaded``
  false on a fresh (empty) data dir;
* ``GET /api/stix/bases/{version}/dashboard`` — 422 ``bundle_invalido`` for a
  base that is not downloaded;
* ``GET /api/stix/bases/{version}/campaigns`` — 422 ``bundle_invalido`` for a
  not-downloaded base;
* ``GET /api/stix/bases/{version}/adversaries`` — 422 ``bundle_invalido`` for a
  not-downloaded base.

Isolation
---------
The handlers in ``app.api.stix`` construct ``StixService()`` with the default
data dir (the real ``sticks/data/stix``, which may already hold a downloaded
base in the environment). To keep the tests deterministic and hermetic, the
``StixService`` symbol used by the handlers is monkeypatched to a factory that
pins ``data_dir`` to an empty ``tmp_path``. No real files or network are
touched; the download POST (the only egress) is intentionally not exercised
here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.stix as stix_api
from app.main import create_app
from app.services.stix.stix_service import (
    KNOWN_VERSIONS,
    StixService,
    _semantic_version_key,
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient whose STIX handlers use a StixService pinned to tmp_path.

    The real endpoints call ``StixService()`` with no args; we replace the
    ``StixService`` name in the ``app.api.stix`` module with a factory that
    injects ``data_dir=tmp_path`` so the listing/dashboard read from an empty,
    isolated directory (every base is 'not downloaded').
    """

    def _factory() -> StixService:
        return StixService(data_dir=tmp_path)

    monkeypatch.setattr(stix_api, "StixService", _factory)
    return TestClient(create_app())


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


def test_get_dashboard_base_not_downloaded_422_invalid_bundle(client: TestClient) -> None:
    """A dashboard request for a not-downloaded base is 422 ``bundle_invalido``
    (no partial counts)."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/dashboard")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_invalido"
    assert detail["mensagem"]
    # No counts leaked into the error body.
    assert "counts" not in resp.json()


def test_get_campaigns_base_not_downloaded_422_invalid_bundle(client: TestClient) -> None:
    """A campaigns request for a not-downloaded base is 422 ``bundle_invalido``."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/campaigns")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_invalido"
    assert detail["mensagem"]


def test_get_adversaries_base_not_downloaded_422_invalid_bundle(client: TestClient) -> None:
    """An actors request for a not-downloaded base is 422 ``bundle_invalido``."""
    version = KNOWN_VERSIONS[0]
    resp = client.get(f"/api/stix/bases/{version}/actors")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_invalido"
    assert detail["mensagem"]
