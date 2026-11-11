"""Testes de endpoint do Estágio 1 — Seleção da base STIX (task 2.3).

No esquema de SEIS Estágios, o Estágio 1 é a "Seleção da base STIX". A tarefa 2.3
adiciona/alinha os seus contratos REST (JSON pt-BR), todos reusando o
:class:`~app.services.stix.stage1_service.Stage1Service` (que, por sua vez,
delega ao ``StixService``):

* ``GET  /api/stix/bases`` — lista as versões conhecidas com ``downloaded`` /
  ``selectable`` (presença em disco checada ao vivo; nenhuma versão downloaded por
  padrão — Req. 5.4) **e** a flag ``curated`` por versão (``True`` apenas para a
  versão 18 — Req. 5.5, 5.6). A rota permanece no router ``stix`` (alinhada, não
  duplicada), mas passa a projetar ``curated`` via ``Stage1Service``.
* ``POST /api/stix/bases/{version}/download`` — o Estágio 1 garante o pacote da
  versão em disco: reusa o pacote quando presente (sem download de novo — Req. 5.3)
  e, quando ausente, aciona o **único egresso de rede** (valida ``version`` contra
  ``KNOWN_VERSIONS`` **antes** de tocar disco/rede — Req. 5.2). Falhas são
  mapeadas ao contrato ``falha_download``: 422 (versão desconhecida, rejeitada no
  pré-voo) / 502 (failure de rede/conteúdo/escrita — Req. 5.9).

Full isolation
----------------
Os handlers do router ``campaigns``/``stix`` constroem ``Stage1Service()`` (sem
injeção na rota), que resolve ``StixService()`` no módulo ``stage1_service``.
Monkeypatcheamos esse símbolo para uma fábrica que fixa ``data_dir`` num
``tmp_path`` isolated e injeta um ``http_client`` **falso** no único egresso de
rede, de modo que nenhum file real ou HTTP real seja tocado.

_Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_
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

import app.api.stix as stix_api
import app.services.stix.stage1_service as stage1_module
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.stix.stix_service import (
    CURATED_VERSION,
    KNOWN_VERSIONS,
    StixService,
)


# ---------------------------------------------------------------------------
# Fakes para o único egresso de rede (download).
# ---------------------------------------------------------------------------


def _bundle_curado() -> dict:
    """A minimal-but-valid STIX bundle (``campaign`` + a few typed objects)."""
    return {
        "type": "bundle",
        "objects": [
            {
                "type": "campaign",
                "id": "campaign--0000",
                "name": "ShadowRay",
                "external_references": [{"external_id": "C0045"}],
            },
            {"type": "attack-pattern", "id": "attack-pattern--1"},
            {"type": "intrusion-set", "id": "intrusion-set--1"},
        ],
    }


class _RespostaFake:
    """A minimal requests-like response for the injected ``http_client``."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


class _ClienteHttpFake:
    """Fake http client; returns a canned response (or raises) on GET."""

    def __init__(self, resposta: "_RespostaFake | Exception") -> None:
        self._resposta = resposta
        self.chamadas: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> "_RespostaFake":
        self.chamadas.append(url)
        if isinstance(self._resposta, Exception):
            raise self._resposta
        return self._resposta


def _pin_stage1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    http: "_ClienteHttpFake | None" = None,
) -> None:
    """Pin the ``Stage1Service``'s ``StixService`` to ``tmp_path`` (+ fake http).

    The route builds ``Stage1Service()`` → ``StixService()`` resolved in the
    ``stage1_service`` module; patching that symbol redirects every disk read
    (and the single network egress) into the isolated temp dir / fake client.
    """

    def _factory(*args: object, **kwargs: object) -> StixService:
        return StixService(data_dir=tmp_path, http_client=http)

    # The ``POST .../download`` and ``GET .../stage/1`` handlers (router campaigns)
    # build ``Stage1Service()`` → ``StixService()`` resolved in the
    # ``stage1_service`` module; ``GET /api/stix/bases`` (router stix) builds
    # ``Stage1Service(StixService())`` with the ``StixService`` symbol of the
    # ``app.api.stix`` module. Patch both so every route is isolated to tmp_path.
    monkeypatch.setattr(stage1_module, "StixService", _factory)
    monkeypatch.setattr(stix_api, "StixService", _factory)


# ---------------------------------------------------------------------------
# Ephemeral temp-file SQLite + app client (mirrors
# test_stage1_campaign_stix_endpoint.py): isolates the DB so write routes never
# touch the production SessionLocal / DEV Postgres.
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema, torn down afterward."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_stage1_bases_")
    db_path = Path(tmp_dir) / "stage1_bases.db"
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
    """Build a TestClient with the DB wired to the ephemeral SQLite factory.

    Registers ``app.dependency_overrides[get_db]`` so every write route uses the
    injected, ephemeral session — the production ``SessionLocal`` is never
    resolved, mirroring the sibling endpoint suites.
    """
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/stix/bases — listagem com downloaded/selectable/curated
# ---------------------------------------------------------------------------


def test_get_bases_projects_curated_flag_and_none_downloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``GET /api/stix/bases`` → 200 com a flag ``curated`` por versão (``True``
    só para a versão curated) e nenhuma downloaded num diretório vazio (Req. 5.4,
    5.5, 5.6)."""
    _pin_stage1(monkeypatch, tmp_path)
    client = _make_client(session_factory)

    resp = client.get("/api/stix/bases")

    assert resp.status_code == 200, resp.text
    bases = resp.json()["bases"]
    assert {b["version"] for b in bases} == set(KNOWN_VERSIONS)
    # Flag curated: exatamente a versão curated vem True.
    curadas = {b["version"] for b in bases if b["curated"]}
    assert curadas == {CURATED_VERSION}
    # Diretório vazio: nada downloaded por padrão.
    for base in bases:
        assert base["downloaded"] is False
        assert base["selectable"] is False
        assert base["file"] is None


# ---------------------------------------------------------------------------
# POST /api/stix/bases/{version}/download — download (único egresso de rede)
# ---------------------------------------------------------------------------


def test_post_download_downloads_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``POST /api/stix/bases/{version}/download`` ausente → 200, baixa via o único
    egresso de rede e marca ``downloaded=True`` (Req. 5.2)."""
    http = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    _pin_stage1(monkeypatch, tmp_path, http)
    client = _make_client(session_factory)

    resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"] == CURATED_VERSION
    assert body["downloaded"] is True
    # A superfície única responde ``persisted=True`` na conclusão com sucesso
    # (Req. 1.5, 5.5); o contrato não expõe mais o caminho do arquivo em disco.
    assert body["persisted"] is True
    # Houve exatamente um egresso de rede (o download).
    assert len(http.chamadas) == 1


def test_post_download_reuses_disk_no_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../download`` com o pacote já em disco → 200 e **nenhum** egresso de
    rede (reusa o disco — Req. 5.3)."""
    # Semeia o bundle em disco.
    base = StixService(data_dir=tmp_path).base_path(CURATED_VERSION)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps(_bundle_curado()), encoding="utf-8")

    http = _ClienteHttpFake(RuntimeError("não deveria haver rede"))
    _pin_stage1(monkeypatch, tmp_path, http)
    client = _make_client(session_factory)

    resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")

    assert resp.status_code == 200, resp.text
    assert resp.json()["downloaded"] is True
    assert http.chamadas == []  # disco reusado, sem rede


def test_post_download_unknown_version_422(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../download`` com versão desconhecida → 422 ``falha_download``,
    rejeitada antes de tocar disco/rede (Req. 5.2)."""
    http = _ClienteHttpFake(_RespostaFake("irrelevante"))
    _pin_stage1(monkeypatch, tmp_path, http)
    client = _make_client(session_factory)

    resp = client.post("/api/stix/bases/999.0/download")

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["motivo"] == "falha_download"
    # Pré-voo: nenhum egresso de rede para uma versão desconhecida.
    assert http.chamadas == []


def test_post_download_download_failure_502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """``POST .../download`` com failure de rede → 502 ``falha_download`` e nenhum
    file parcial em disco (Req. 5.9)."""
    http = _ClienteHttpFake(RuntimeError("rede caiu"))
    _pin_stage1(monkeypatch, tmp_path, http)
    client = _make_client(session_factory)

    resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["motivo"] == "falha_download"
    # Nenhum file parcial deixado em disco.
    assert not StixService(data_dir=tmp_path).base_path(CURATED_VERSION).exists()


# ===========================================================================
# Task 1 — Bug condition exploration test (Property 1).
#
# This sentinel test encodes the DB-isolation property. On UNFIXED code (no
# ``get_db`` override) the write route ``POST /api/stix/bases/{version}/download``
# resolves ``get_db`` to the production ``SessionLocal`` and persists the fixture
# bundle into the DEV database instead of an injected, ephemeral session — the
# Task 1 run on unfixed code documented that FAIL (it confirmed the leak).
#
# After the fix, this test exercises the FIXED code path: it builds its client
# through ``_make_client(session_factory)``, which registers
# ``app.dependency_overrides[get_db]`` over an ephemeral SQLite session. Per the
# design (``isBugCondition``), Property 1 holds when a write route is exercised
# on an app that HAS a ``get_db`` override pointing at an ephemeral session; the
# route must then use that injected session and production must receive no write.
# This test asserts that property, so it PASSES after the fix — and still FAILS
# if the override were removed from ``_make_client`` (the sentinel-used check
# below provides that guarantee).
#
# Disk/network stay isolated via ``_pin_stage1`` (StixService pinned to
# ``tmp_path`` + fake http client), so no real disk or network is touched.
#
# A sentinel session factory stands in for production: we point the production
# ``SessionLocal`` symbol (the one ``get_db`` resolves) at an observable
# ephemeral SQLite factory that records whether it was used. If the route used
# the production path, the sentinel records the write; if the route used an
# injected override, the sentinel stays untouched.
# ===========================================================================

import app.db.session as _db_session


class _SentinelSessionFactory:
    """An observable session factory standing in for the production one.

    It is backed by an ephemeral SQLite engine so no real DEV database is
    touched, and it records whether the route opened a session through it (the
    production path). A session opened here means the route did NOT use an
    injected override — i.e. the leak is present.
    """

    def __init__(self, inner: sessionmaker[Session]) -> None:
        self._inner = inner
        self.used = False

    def __call__(self) -> Session:
        self.used = True
        return self._inner()


def test_download_route_uses_injected_session_not_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
) -> None:
    """Property 1 — the write route SHALL use the injected session and production
    SHALL receive no write.

    Builds the client through the FIXED ``_make_client(session_factory)`` helper
    (which registers ``app.dependency_overrides[get_db]`` over an ephemeral
    SQLite session), seeds a bundle on disk, and exercises
    ``POST /api/stix/bases/{version}/download``. A sentinel factory stands in for
    production ``SessionLocal``. The assertion requires the app to override
    ``get_db`` (so the route uses the injected, ephemeral session) AND that the
    sentinel (production) session was never opened.

    EXPECTED on FIXED code: this PASSES — ``_make_client`` registers the
    ``get_db`` override, so the route resolves to the injected ephemeral session
    and the production sentinel is never opened. On UNFIXED code (if the override
    were removed from ``_make_client``) it FAILS, because the route would resolve
    ``get_db`` to the production ``SessionLocal`` and the sentinel would record
    the write — the Task 1 run on unfixed code already documented that FAIL.
    """
    # --- Build the observable sentinel ("production") session factory. --------
    sentinel_dir = tempfile.mkdtemp(prefix="pipeline_ui_bases_sentinel_")
    sentinel_db = Path(sentinel_dir) / "sentinel.db"
    sentinel_engine = create_db_engine(
        Settings(database_url=f"sqlite:///{sentinel_db.as_posix()}")
    )
    import_models()
    Base.metadata.create_all(bind=sentinel_engine)
    inner_factory: sessionmaker[Session] = sessionmaker(
        bind=sentinel_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    sentinel = _SentinelSessionFactory(inner_factory)

    # Point the production symbol that ``get_db`` resolves at the sentinel, so a
    # production-path resolution is observable without touching the real DEV DB.
    monkeypatch.setattr(_db_session, "SessionLocal", sentinel)

    try:
        # --- Isolate disk/network and seed the bundle on disk. ----------------
        http = _ClienteHttpFake(RuntimeError("não deveria haver rede"))
        _pin_stage1(monkeypatch, tmp_path, http)
        base = StixService(data_dir=tmp_path).base_path(CURATED_VERSION)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps(_bundle_curado()), encoding="utf-8")

        # --- Build the client through the FIXED helper (registers the get_db
        # override over the ephemeral SQLite session). ------------------------
        client = _make_client(session_factory)

        # --- Exercise the single write route. ---------------------------------
        resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")
        assert resp.status_code == 200, resp.text

        # --- Property 1: injected session used AND production received no write.
        # Equivalent, robust form from the design: the app overrides get_db.
        # ``TestClient`` exposes the underlying app via ``.app``.
        assert get_db in client.app.dependency_overrides, (
            "the download route resolved get_db to the production SessionLocal "
            "(no dependency override registered), so the fixture bundle was "
            "persisted through the production session"
        )
        # Core Property 1 check: without the override the route would open the
        # production sentinel session and ``sentinel.used`` would become True;
        # this assertion is what keeps the test meaningful (it still FAILS if the
        # override were removed).
        assert sentinel.used is False, (
            "the production (sentinel) SessionLocal was opened by the download "
            "route — the write leaked into the production session instead of an "
            "injected ephemeral one"
        )
    finally:
        sentinel_engine.dispose()
        if sentinel_db.exists():
            os.remove(sentinel_db)
        os.rmdir(sentinel_db.parent)


# ===========================================================================
# Task 2 — Preservation property tests (Property 2).
#
# These tests capture the BASELINE HTTP contract and disk isolation to preserve
# across the fix. Per the design's Preservation Requirements, the status codes
# and response bodies of ``GET /api/stix/bases`` and of the four
# ``POST .../download`` scenarios (200 download, 200 disk reuse with no network,
# 422 unknown version, 502 network failure) must stay identical, and the disk
# isolation provided by ``_pin_stage1`` must keep every disk read/write inside
# ``tmp_path`` and every egress on the fake http client.
#
# They are EXPECTED to PASS on UNFIXED code: the baseline they assert is exactly
# the behavior the current suite already exhibits. Because the bug condition
# requires a WRITE route resolving ``get_db`` to production, and these
# preservation checks assert only the externally observable HTTP contract and
# disk isolation (not the DB session binding), they hold both before and after
# the fix — that is what makes them a regression guard.
#
# The scenario set is driven by a property-based generator (Hypothesis
# ``sampled_from`` over the five named scenarios) so each scenario is exercised
# across many generated cases, giving a stronger preservation guarantee than a
# single example per scenario. Each scenario is a self-contained builder that
# isolates disk/network via ``_pin_stage1`` inside a fresh ``tmp_path`` and
# returns the response plus the fake http client for egress assertions.
# ===========================================================================

import shutil  # noqa: E402

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

# The five named HTTP scenarios that form the preservation domain. Each name is
# the key the property generator samples from; the builder below maps a name to
# its setup, exercise, and the expected (status_code, body-shape) contract.
_PRESERVATION_SCENARIOS: tuple[str, ...] = (
    "get_bases",
    "download_absent",
    "download_reuse_disk",
    "download_unknown_version",
    "download_network_failure",
)


def _isolated_client(data_dir: Path) -> TestClient:
    """Build a TestClient with an ephemeral SQLite ``get_db`` override.

    The engine lives in a file inside ``data_dir`` (the per-case temp directory
    that every caller tears down), so no write route resolves ``get_db`` to the
    production ``SessionLocal`` / DEV database. Status/body contracts are
    unchanged because the override only redirects the DB session binding.
    """
    db_path = data_dir / "preservation.db"
    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{db_path.as_posix()}")
    )
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
    return _make_client(factory)


def _build_scenario_client(
    scenario: str,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, "_ClienteHttpFake | None"]:
    """Isolate disk/network/DB for ``scenario`` and return (client, fake http).

    Mirrors the setup each original example test performs, but keyed by scenario
    name so the property generator can drive any of them. ``data_dir`` MUST be a
    per-case empty directory (the ``download_reuse_disk`` scenario seeds a bundle
    into it) so cases never share disk state. The client wires ``get_db`` to an
    ephemeral SQLite engine (DB isolation) in addition to the ``_pin_stage1``
    disk/network isolation. The returned fake http client (or ``None`` for the
    read-only ``get_bases`` scenario) lets the caller assert the network-egress
    contract that ``_pin_stage1`` guarantees.
    """
    if scenario == "get_bases":
        _pin_stage1(monkeypatch, data_dir)
        return _isolated_client(data_dir), None

    if scenario == "download_absent":
        http = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
        _pin_stage1(monkeypatch, data_dir, http)
        return _isolated_client(data_dir), http

    if scenario == "download_reuse_disk":
        base = StixService(data_dir=data_dir).base_path(CURATED_VERSION)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.write_text(json.dumps(_bundle_curado()), encoding="utf-8")
        http = _ClienteHttpFake(RuntimeError("não deveria haver rede"))
        _pin_stage1(monkeypatch, data_dir, http)
        return _isolated_client(data_dir), http

    if scenario == "download_unknown_version":
        http = _ClienteHttpFake(_RespostaFake("irrelevante"))
        _pin_stage1(monkeypatch, data_dir, http)
        return _isolated_client(data_dir), http

    if scenario == "download_network_failure":
        http = _ClienteHttpFake(RuntimeError("rede caiu"))
        _pin_stage1(monkeypatch, data_dir, http)
        return _isolated_client(data_dir), http

    raise AssertionError(f"unknown preservation scenario: {scenario}")


def _assert_scenario_contract(
    scenario: str,
    data_dir: Path,
    client: TestClient,
    http: "_ClienteHttpFake | None",
) -> None:
    """Assert the baseline HTTP contract + disk isolation for ``scenario``.

    This is the single source of truth the preservation property and the
    per-scenario unit tests share: identical status codes, response bodies, and
    network-egress expectations to the original example tests.
    """
    if scenario == "get_bases":
        resp = client.get("/api/stix/bases")
        assert resp.status_code == 200, resp.text
        bases = resp.json()["bases"]
        assert {b["version"] for b in bases} == set(KNOWN_VERSIONS)
        # Flag curated: exactly the curated version comes back True.
        curated = {b["version"] for b in bases if b["curated"]}
        assert curated == {CURATED_VERSION}
        # Empty directory: nothing downloaded/selectable, no file.
        for base in bases:
            assert base["downloaded"] is False
            assert base["selectable"] is False
            assert base["file"] is None
        return

    if scenario == "download_absent":
        assert http is not None
        resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == CURATED_VERSION
        assert body["downloaded"] is True
        assert body["persisted"] is True
        # Exactly one network egress (the download).
        assert len(http.chamadas) == 1
        return

    if scenario == "download_reuse_disk":
        assert http is not None
        resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")
        assert resp.status_code == 200, resp.text
        assert resp.json()["downloaded"] is True
        # Disk reused: no network egress at all.
        assert http.chamadas == []
        return

    if scenario == "download_unknown_version":
        assert http is not None
        resp = client.post("/api/stix/bases/999.0/download")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["motivo"] == "falha_download"
        # Pre-flight rejection: no network egress for an unknown version.
        assert http.chamadas == []
        return

    if scenario == "download_network_failure":
        assert http is not None
        resp = client.post(f"/api/stix/bases/{CURATED_VERSION}/download")
        assert resp.status_code == 502, resp.text
        assert resp.json()["detail"]["motivo"] == "falha_download"
        # No partial file left on disk after a failed download.
        assert not StixService(data_dir=data_dir).base_path(CURATED_VERSION).exists()
        return

    raise AssertionError(f"unknown preservation scenario: {scenario}")


@settings(max_examples=50, deadline=None)
@given(scenario=st.sampled_from(_PRESERVATION_SCENARIOS))
def test_http_contract_and_disk_isolation_preserved(scenario: str) -> None:
    """Property 2 — the HTTP contract and disk isolation SHALL be preserved.

    For every HTTP scenario in the suite's domain (``GET /api/stix/bases`` and
    the four ``POST .../download`` scenarios), the status code, response body,
    and network-egress isolation SHALL match the already-verified baseline. The
    property generator samples the scenario set so each case is exercised across
    many generated runs.

    Each generated case is fully self-isolated: a fresh temp data dir and a
    fresh ``MonkeyPatch`` context (both torn down afterward) stand in for the
    function-scoped ``tmp_path``/``monkeypatch`` fixtures, so disk and patch
    state never leak between the examples Hypothesis generates within one run.

    EXPECTED on UNFIXED code: this PASSES — it captures the baseline HTTP
    contract and disk isolation to preserve across the fix.

    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    data_dir = Path(tempfile.mkdtemp(prefix="pipeline_ui_bases_preservation_"))
    monkeypatch = pytest.MonkeyPatch()
    try:
        client, http = _build_scenario_client(scenario, data_dir, monkeypatch)
        _assert_scenario_contract(scenario, data_dir, client, http)
    finally:
        monkeypatch.undo()
        shutil.rmtree(data_dir, ignore_errors=True)


@pytest.mark.parametrize("scenario", _PRESERVATION_SCENARIOS)
def test_http_contract_and_disk_isolation_preserved_examples(
    scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Example (unit) coverage of each preservation scenario.

    Complements the property test with one explicit case per named scenario, so
    a failure pinpoints the exact scenario whose baseline contract regressed.

    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    client, http = _build_scenario_client(scenario, tmp_path, monkeypatch)
    _assert_scenario_contract(scenario, tmp_path, client, http)
