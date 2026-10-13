"""Endpoint tests for ``/api/stix/*`` (fluxo-stix-dashboard, task 5.3).

Builds a FastAPI ``TestClient`` over ``app.main:create_app`` (matching the style
of ``test_api_endpoints.py``) and exercises the STIX inspection endpoints that
are reachable **without any network egress**:

* ``GET /api/stix/bases`` — 200, bases ordered newest->oldest, all ``baixada``
  false on a fresh (empty) data dir;
* ``GET /api/stix/bases/{versao}/dashboard`` — 422 ``bundle_invalido`` for a
  base that is not downloaded;
* ``GET /api/stix/bases/{versao}/campanhas`` — 422 ``bundle_invalido`` for a
  not-downloaded base;
* ``GET /api/stix/bases/{versao}/atores`` — 422 ``bundle_invalido`` for a
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
    VERSOES_CONHECIDAS,
    StixService,
    _chave_versao_semantica,
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


def test_get_bases_200_ordenadas_e_todas_nao_baixadas(client: TestClient) -> None:
    """``GET /api/stix/bases`` returns 200 with every known base, ordered
    newest->oldest, all ``baixada``/``selecionavel`` false on a fresh dir."""
    resp = client.get("/api/stix/bases")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    bases = body["bases"]
    assert {b["versao"] for b in bases} == set(VERSOES_CONHECIDAS)

    # Strictly descending by semantic version (newest first).
    keys = [_chave_versao_semantica(b["versao"]) for b in bases]
    for anterior, proximo in zip(keys, keys[1:]):
        assert anterior > proximo, f"ordem decrescente violada: {anterior} !> {proximo}"

    # Fresh data dir: nothing downloaded.
    for base in bases:
        assert base["baixada"] is False
        assert base["selecionavel"] is False
        assert base["arquivo"] is None


def test_get_dashboard_base_nao_baixada_422_bundle_invalido(client: TestClient) -> None:
    """A dashboard request for a not-downloaded base is 422 ``bundle_invalido``
    (no partial counts)."""
    versao = VERSOES_CONHECIDAS[0]
    resp = client.get(f"/api/stix/bases/{versao}/dashboard")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_invalido"
    assert detail["mensagem"]
    # No counts leaked into the error body.
    assert "contagens" not in resp.json()


def test_get_campanhas_base_nao_baixada_422_bundle_invalido(client: TestClient) -> None:
    """A campaigns request for a not-downloaded base is 422 ``bundle_invalido``."""
    versao = VERSOES_CONHECIDAS[0]
    resp = client.get(f"/api/stix/bases/{versao}/campanhas")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_invalido"
    assert detail["mensagem"]


def test_get_atores_base_nao_baixada_422_bundle_invalido(client: TestClient) -> None:
    """An actors request for a not-downloaded base is 422 ``bundle_invalido``."""
    versao = VERSOES_CONHECIDAS[0]
    resp = client.get(f"/api/stix/bases/{versao}/atores")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["motivo"] == "bundle_invalido"
    assert detail["mensagem"]
