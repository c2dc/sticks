"""Contract tests of the Stage-1 endpoints' ``campaign_stix`` block (task 3.2).

In the SIX-stage scheme, Stage 1 became the **STIX base Selection**:
``GET /api/campaigns/{campaign}/stage/1`` delegates to ``Stage1Service`` and
returns the typed result ``Stage1ResultStix`` (bundle on disk + dashboard +
campaigns + verdict), accepting an optional ``version`` (default = curated
version). The structural modeling (historical DTO ``Stage1*``, whose Input is the
STIX ``campaign`` object) remains accessible **transitionally** at
``POST /api/campaigns/{campaign}/stage/1/execute`` until the 3.x tasks renumber it
to Stage 2.

These tests pin the HTTP contract:

* **GET (six stages)** — the STIX base Selection concludes when the bundle is on
  disk and the dashboard was built; an unknown version keeps the Stage not
  completed with the reported cause, with no network egress (Req. 5.7, 5.8, 5.9,
  5.10).
* **POST execute (transitional)** — the modeling Input **never breaks the Stage**
  (Correctness Property 1 — HTTP 200 in all scenarios) and **no Caldera adversary
  artifact leaks into the Input** (Property 3):

1. a valid ``version`` with the campaign in the bundle → ``found == true`` with the
   rich fields of the ``campaign`` object (name, stix_id, external_id, description,
   aliases, dates) and the base version.
2. no ``version`` → ``found == false`` and ``unavailable_reason ==
   "versao_ausente"`` (backward-compatible fallback, no HTTP error).
3. a valid ``version`` but the campaign absent from the bundle → ``found == false``
   and ``unavailable_reason == "campanha_nao_encontrada"``, still carrying the
   curated identifier (name + external_id from ``SLUG_TO_STIX``).

Both GET (read-only) and POST (execute) are covered where sensible.

Isolation
---------
Everything external is a throwaway:

* the database is an ephemeral temp-file SQLite wired into the app via the
  ``get_db`` dependency override (same pattern as ``test_api_endpoints.py``);
* the STIX base is read from a **pytest temp dir**: because the Stage-1
  endpoints construct ``StageService(db, case_service=...)`` directly (no stix
  injection point on the route), we monkeypatch the ``StixService`` symbol that
  ``StageService`` resolves at construction time so it points at ``tmp_path``.
  The real ``sticks/data/stix`` is never touched and there is no network egress.

The curated case fixtures already ship on disk (``shadowray`` is one of the 8
curated cases), so the slug is known to ``CaseService`` and the curated
identifier (name + ``external_id``) resolves from ``SLUG_TO_STIX`` on the
fallback paths.

_Requirements: 5.2, 5.4, 4.2 — Correctness Properties 1, 3_
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.services.stage.stage_service as stage_module
import app.services.stix.stage1_service as stage1_module
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.stix.stix_service import (
    SLUG_TO_STIX,
    KNOWN_VERSIONS,
    StixService,
)

# A curated slug that ships on disk and has an external_id in SLUG_TO_STIX.
_SLUG = "shadowray"
_EXTERNAL_ID = SLUG_TO_STIX[_SLUG]["referencias"][0]  # "C0045"
_NOME_CANONICO = SLUG_TO_STIX[_SLUG]["nome_canonico"]  # "ShadowRay"
# A valid, known base version — resolves to enterprise-attack-{version}.json.
_VERSION = KNOWN_VERSIONS[0]

# A curated adversary file path shape that must NEVER appear in the Entrada
# (Property 3 — no Caldera artifact leaks into the Stage-1 Entrada).
_CALDERA_FRAGMENT = "data/caldera_adversaries"


# ---------------------------------------------------------------------------
# Ephemeral temp-file SQLite + app client (mirrors test_api_endpoints.py).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema, torn down afterward."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage1_campaign_")
    db_path = Path(tmp_dir) / "stage1_campaign.db"
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
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a TestClient with the DB wired to the ephemeral SQLite factory."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a minimal-but-valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


@pytest.fixture()
def stix_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the StageService's StixService to a pytest temp data dir.

    The Stage-1 endpoints build ``StageService(db, case_service=...)`` directly,
    so there is no stix injection point on the route. ``StageService.__init__``
    falls back to ``StixService()`` resolved from the ``stage_service`` module;
    patching that symbol with a ``data_dir``-bound factory makes every bundle
    read happen inside ``tmp_path`` — the real ``sticks/data/stix`` is untouched
    and there is no network egress.
    """

    def _factory(*args: object, **kwargs: object) -> StixService:
        return StixService(data_dir=tmp_path)

    monkeypatch.setattr(stage_module, "StixService", _factory)
    return tmp_path


@pytest.fixture()
def stage1_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the six-stage ``GET .../stage/1`` handler to a temp data dir.

    In the six-stage scheme, ``GET /api/campaigns/{campaign}/stage/1`` delegates to
    :class:`~app.services.stix.stage1_service.Stage1Service`, que (sem
    route injection) builds a ``StixService()`` resolved in the module
    ``stage1_service``. Patching that symbol with a ``data_dir``-bound factory
    makes the bundle read + dashboard happen inside ``tmp_path`` — the real
    ``sticks/data/stix`` is untouched and there is no network egress (nenhum
    bundle em disco salvo os que o teste escreve).
    """

    def _factory(*args: object, **kwargs: object) -> StixService:
        return StixService(data_dir=tmp_path)

    monkeypatch.setattr(stage1_module, "StixService", _factory)
    return tmp_path


# A rich ``campaign`` object resolving to the ``shadowray`` slug (via C0045).
def _campaign_found() -> dict:
    return {
        "type": "campaign",
        "id": "campaign--c0045-0000-0000-0000-000000000000",
        "name": _NOME_CANONICO,
        "description": "ataques contra clusters Ray expostos.",
        "aliases": [_NOME_CANONICO, "Ray cluster abuse"],
        "first_seen": "2023-09-05T00:00:00.000Z",
        "last_seen": "2024-03-01T00:00:00.000Z",
        "external_references": [
            {"source_name": "mitre-attack", "external_id": _EXTERNAL_ID}
        ],
    }


def _assert_sem_artefato_caldera(input: dict) -> None:
    """Property 3 — no Caldera adversary path leaks into the STIX campaign block.

    The Stage-1 Entrada's STIX identity lives in ``campaign_stix`` — that block
    must never carry a Caldera adversary path (``data/caldera_adversaries/*.yml``).
    The legacy ``dag_file``/``stix_reference`` fields are deliberately preserved
    in the DTO for contract compatibility (per the design) and are dropped on the
    frontend side (tasks 5.1/7.1); they are therefore out of scope for this
    API-level guarantee.
    """
    block = input["campaign_stix"]
    serializado = json.dumps(block)
    assert _CALDERA_FRAGMENT not in serializado, block


# ===========================================================================
# 1) valid version WITH the campaign in the bundle → found=true + rich fields
# ===========================================================================


def test_get_stage1_valid_version_dashboard_completed(
    session_factory: sessionmaker[Session], stage1_data_dir: Path
) -> None:
    """``GET .../stage/1?version=`` com o bundle em disco → HTTP 200 e o
    Stage 1 result (STIX base Selection) ``completed=True`` with the
    bundle dashboard and the campaigns (six stages, Req. 5.7, 5.8, 5.10)."""
    campaign = _campaign_found()
    _write_bundle(
        StixService(data_dir=stage1_data_dir).base_path(_VERSION), [campaign]
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_SLUG}/stage/1", params={"version": _VERSION})

    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Verdict + Output of Stage 1 (bundle on disk + dashboard + campaigns).
    assert body["version"] == _VERSION
    assert body["completed"] is True
    assert body["bundle_on_disk"] is True
    assert body["error_message"] is None
    assert body["dashboard"] is not None
    assert body["dashboard"]["version"] == _VERSION
    # The written campaign is listed among the bundle's campaigns (Entrada do E2).
    nomes = {c["name"] for c in body["campaigns"]}
    assert _NOME_CANONICO in nomes


def test_post_stage1_execute_valid_version_campaign_found(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../stage/1/execute?version=`` com a campaign no bundle → HTTP
    200 and ``campaign_stix.found=true``; the structural execution follows (the
    Input is informative, Property 1)."""
    campaign = _campaign_found()
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSION), [campaign])

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/1/execute", params={"version": _VERSION}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == _SLUG
    block = body["input"]["campaign_stix"]
    assert block["found"] is True
    assert block["stix_id"] == campaign["id"]
    assert block["external_id"] == _EXTERNAL_ID
    assert block["base_version"] == _VERSION
    _assert_sem_artefato_caldera(body["input"])


# ===========================================================================
# 2) sem version → found=false / unavailable_reason="versao_ausente"
# ===========================================================================


def test_get_stage1_without_version_uses_curated_version(
    session_factory: sessionmaker[Session], stage1_data_dir: Path
) -> None:
    """``GET .../stage/1`` without ``version`` → HTTP 200 and Stage 1 starts from the
    curated version (``CURATED_VERSION``) as the stream default (six stages, Req.
    5.5, 5.6). With the curated bundle on disk, it concludes with no network egress."""
    from app.services.stix.stix_service import CURATED_VERSION

    # Curated bundle on disk → no download (no network); concludes Stage 1.
    _write_bundle(
        StixService(data_dir=stage1_data_dir).base_path(CURATED_VERSION),
        [_campaign_found()],
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/campaigns/{_SLUG}/stage/1")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == CURATED_VERSION
    assert body["curated"] is True
    assert body["completed"] is True
    assert body["bundle_on_disk"] is True
    assert body["dashboard"] is not None


def test_post_stage1_execute_without_version_fallback_version_missing(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../stage/1/execute`` sem ``version`` → HTTP 200 e o fallback
    ``versao_ausente`` (execution never breaks for a missing version)."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/campaigns/{_SLUG}/stage/1/execute")

    assert resp.status_code == 200, resp.text
    block = resp.json()["input"]["campaign_stix"]
    assert block["found"] is False
    assert block["unavailable_reason"] == "versao_ausente"
    assert block["base_version"] is None


# ===========================================================================
# 3) valid version WITHOUT the campaign in the bundle → campanha_nao_encontrada
# ===========================================================================


def test_get_stage1_unknown_version_does_not_complete_with_cause(
    session_factory: sessionmaker[Session], stage1_data_dir: Path
) -> None:
    """``GET .../stage/1?version=`` with an unknown version → HTTP 200 and
    Stage 1 **not completed** with the reported cause, with no network egress
    (``version`` is validated against ``KNOWN_VERSIONS`` before touching network —
    six stages, Req. 5.9). The service never raises: it returns the typed
    reportable result."""
    unknown_version = "999.0"

    client = _make_client(session_factory)
    resp = client.get(
        f"/api/campaigns/{_SLUG}/stage/1", params={"version": unknown_version}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == unknown_version
    assert body["completed"] is False
    assert body["bundle_on_disk"] is False
    assert body["dashboard"] is None
    assert body["campaigns"] == []
    assert body["error_message"]  # a causa reportada ao Pesquisador


def test_post_stage1_execute_valid_version_campaign_missing(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../stage/1/execute?version=`` with a readable base but WITHOUT the
    campaign → HTTP 200 and ``campanha_nao_encontrada`` (execution does not break)."""
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSION), [])

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/1/execute", params={"version": _VERSION}
    )

    assert resp.status_code == 200, resp.text
    block = resp.json()["input"]["campaign_stix"]
    assert block["found"] is False
    assert block["unavailable_reason"] == "campanha_nao_encontrada"
    assert block["base_version"] == _VERSION
    assert block["name"] == _NOME_CANONICO
    assert block["external_id"] == _EXTERNAL_ID


# ===========================================================================
# Property 1 — no HTTP error in ANY of the three scenarios (status 200 always).
# ===========================================================================


def test_stage1_never_breaks_http_in_any_scenario(
    session_factory: sessionmaker[Session],
    stix_data_dir: Path,
    stage1_data_dir: Path,
) -> None:
    """The Stage 1 route never breaks HTTP (always status 200).

    Covers the GET of the STIX base Selection (six stages) and the POST
    ``.../stage/1/execute`` (transitional modeling) in the relevant scenarios;
    todos devolvem 200, de modo que a tela sempre recebe um result tipado
    reportable (Req. 5.9) and the modeling Input never breaks the Stage."""
    from app.services.stix.stix_service import CURATED_VERSION

    base_get = StixService(data_dir=stage1_data_dir).base_path(_VERSION)
    base_curada = StixService(data_dir=stage1_data_dir).base_path(CURATED_VERSION)
    base_post = StixService(data_dir=stix_data_dir).base_path(_VERSION)
    client = _make_client(session_factory)

    # Scenario A: GET with a valid version and bundle on disk → 200.
    _write_bundle(base_get, [_campaign_found()])
    assert (
        client.get(
            f"/api/campaigns/{_SLUG}/stage/1", params={"version": _VERSION}
        ).status_code
        == 200
    )

    # Scenario B: GET without a version → uses the curated version; curated bundle on disk → 200.
    _write_bundle(base_curada, [_campaign_found()])
    assert client.get(f"/api/campaigns/{_SLUG}/stage/1").status_code == 200

    # Scenario C: GET with an unknown version → 200 (not completed, no network).
    assert (
        client.get(
            f"/api/campaigns/{_SLUG}/stage/1", params={"version": "999.0"}
        ).status_code
        == 200
    )

    # POST execute (transitional modeling): valid+campaign, no version, no campaign.
    _write_bundle(base_post, [_campaign_found()])
    assert (
        client.post(
            f"/api/campaigns/{_SLUG}/stage/1/execute", params={"version": _VERSION}
        ).status_code
        == 200
    )
    assert client.post(f"/api/campaigns/{_SLUG}/stage/1/execute").status_code == 200
    _write_bundle(base_post, [])
    assert (
        client.post(
            f"/api/campaigns/{_SLUG}/stage/1/execute", params={"version": _VERSION}
        ).status_code
        == 200
    )
