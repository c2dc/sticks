"""Contract tests of the Stage-1 endpoints' ``campanha_stix`` block (task 3.2).

The *entrada conceitual* of Stage 1 (Modelagem Estrutural) is the STIX
``campaign`` object of the base selected in the flow. The read
(``GET /api/casos/{caso}/estagio/1``) and run
(``POST /api/casos/{caso}/estagio/1/executar``) endpoints both accept an optional
``versao`` query param and expose a ``campanha_stix`` block in the Entrada.

These tests pin the HTTP contract across the three scenarios the task calls out,
asserting that the Entrada **never breaks the Stage** (Correctness Property 1 —
HTTP 200 in every case, no HTTP error for a missing/absent version/campaign) and
that **no Caldera adversary artifact leaks into the Entrada** (Property 3):

1. ``versao`` válida com a campanha no bundle → ``encontrada == true`` com os
   campos ricos do objeto ``campaign`` (nome, stix_id, external_id, descrição,
   aliases, datas) e a versão da base.
2. sem ``versao`` → ``encontrada == false`` e ``motivo_indisponivel ==
   "versao_ausente"`` (fallback retrocompatível, sem erro HTTP).
3. ``versao`` válida mas a campanha ausente do bundle → ``encontrada == false``
   e ``motivo_indisponivel == "campanha_nao_encontrada"``, ainda carregando o
   identificador curado (nome + external_id de ``SLUG_PARA_STIX``).

Both GET (read-only) and POST (executar) are covered where sensible.

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
identifier (nome + ``external_id``) resolves from ``SLUG_PARA_STIX`` on the
fallback paths.

_Requisitos: 5.2, 5.4, 4.2 — Correctness Properties 1, 3_
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
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.services.stix.stix_service import (
    SLUG_PARA_STIX,
    VERSOES_CONHECIDAS,
    StixService,
)

# A curated slug that ships on disk and has an external_id in SLUG_PARA_STIX.
_SLUG = "shadowray"
_EXTERNAL_ID = SLUG_PARA_STIX[_SLUG]["referencias"][0]  # "C0045"
_NOME_CANONICO = SLUG_PARA_STIX[_SLUG]["nome_canonico"]  # "ShadowRay"
# A valid, known base version — resolves to enterprise-attack-{versao}.json.
_VERSAO = VERSOES_CONHECIDAS[0]

# A curated adversary file path shape that must NEVER appear in the Entrada
# (Property 3 — no Caldera artifact leaks into the Stage-1 Entrada).
_CALDERA_FRAGMENT = "data/caldera_adversaries"


# ---------------------------------------------------------------------------
# Ephemeral temp-file SQLite + app client (mirrors test_api_endpoints.py).
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema, torn down afterward."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_estagio1_campanha_")
    db_path = Path(tmp_dir) / "estagio1_campanha.db"
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


# A rich ``campaign`` object resolving to the ``shadowray`` slug (via C0045).
def _campanha_encontrada() -> dict:
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


def _assert_sem_artefato_caldera(entrada: dict) -> None:
    """Property 3 — no Caldera adversary path leaks into the STIX campaign block.

    The Stage-1 Entrada's STIX identity lives in ``campanha_stix`` — that block
    must never carry a Caldera adversary path (``data/caldera_adversaries/*.yml``).
    The legacy ``dag_file``/``stix_reference`` fields are deliberately preserved
    in the DTO for contract compatibility (per the design) and are dropped on the
    frontend side (tasks 5.1/7.1); they are therefore out of scope for this
    API-level guarantee.
    """
    bloco = entrada["campanha_stix"]
    serializado = json.dumps(bloco)
    assert _CALDERA_FRAGMENT not in serializado, bloco


# ===========================================================================
# 1) versao válida COM a campanha no bundle → encontrada=true + campos ricos
# ===========================================================================


def test_get_estagio1_versao_valida_campanha_encontrada(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``GET .../estagio/1?versao=`` com a campanha no bundle → HTTP 200 e
    ``campanha_stix.encontrada=true`` com os campos ricos (Property 1)."""
    campanha = _campanha_encontrada()
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSAO), [campanha])

    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/1", params={"versao": _VERSAO})

    assert resp.status_code == 200, resp.text
    entrada = resp.json()["entrada"]
    bloco = entrada["campanha_stix"]

    assert bloco["encontrada"] is True
    assert bloco["motivo_indisponivel"] is None
    assert bloco["versao_base"] == _VERSAO
    assert bloco["stix_id"] == campanha["id"]
    assert bloco["nome"] == _NOME_CANONICO
    assert bloco["external_id"] == _EXTERNAL_ID
    assert bloco["descricao"] == campanha["description"]
    assert bloco["aliases"] == [_NOME_CANONICO, "Ray cluster abuse"]
    assert bloco["first_seen"] == "2023-09-05T00:00:00.000Z"
    assert bloco["last_seen"] == "2024-03-01T00:00:00.000Z"
    _assert_sem_artefato_caldera(entrada)


def test_post_estagio1_executar_versao_valida_campanha_encontrada(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../estagio/1/executar?versao=`` com a campanha no bundle → HTTP
    200 e ``campanha_stix.encontrada=true``; a execução estrutural segue (a
    Entrada é informativa, Property 1)."""
    campanha = _campanha_encontrada()
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSAO), [campanha])

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["caso_id"] == _SLUG
    bloco = body["entrada"]["campanha_stix"]
    assert bloco["encontrada"] is True
    assert bloco["stix_id"] == campanha["id"]
    assert bloco["external_id"] == _EXTERNAL_ID
    assert bloco["versao_base"] == _VERSAO
    _assert_sem_artefato_caldera(body["entrada"])


# ===========================================================================
# 2) sem versao → encontrada=false / motivo_indisponivel="versao_ausente"
# ===========================================================================


def test_get_estagio1_sem_versao_fallback_versao_ausente(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``GET .../estagio/1`` sem ``versao`` → HTTP 200 (retrocompatível) e o
    fallback ``versao_ausente`` sem erro HTTP (Req. 5.4, Property 1)."""
    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/1")

    assert resp.status_code == 200, resp.text
    entrada = resp.json()["entrada"]
    bloco = entrada["campanha_stix"]

    assert bloco["encontrada"] is False
    assert bloco["motivo_indisponivel"] == "versao_ausente"
    assert bloco["versao_base"] is None
    # The curated identifier is still carried on the fallback path.
    assert bloco["nome"] == _NOME_CANONICO
    assert bloco["external_id"] == _EXTERNAL_ID
    _assert_sem_artefato_caldera(entrada)


def test_post_estagio1_executar_sem_versao_fallback_versao_ausente(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../estagio/1/executar`` sem ``versao`` → HTTP 200 e o fallback
    ``versao_ausente`` (a execução nunca quebra por falta de versão)."""
    client = _make_client(session_factory)
    resp = client.post(f"/api/casos/{_SLUG}/estagio/1/executar")

    assert resp.status_code == 200, resp.text
    bloco = resp.json()["entrada"]["campanha_stix"]
    assert bloco["encontrada"] is False
    assert bloco["motivo_indisponivel"] == "versao_ausente"
    assert bloco["versao_base"] is None


# ===========================================================================
# 3) versao válida SEM a campanha no bundle → campanha_nao_encontrada
# ===========================================================================


def test_get_estagio1_versao_valida_campanha_ausente(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``GET .../estagio/1?versao=`` com a base legível mas SEM a campanha →
    HTTP 200 e ``campanha_nao_encontrada`` + o identificador curado (Req. 4.2,
    Property 1)."""
    # A readable bundle whose only campaign does NOT resolve to ``shadowray``.
    _write_bundle(
        StixService(data_dir=stix_data_dir).base_path(_VERSAO),
        [
            {
                "type": "campaign",
                "id": "campaign--9999-0000-0000-0000-000000000000",
                "name": "Campanha Qualquer",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C9999"}
                ],
            }
        ],
    )

    client = _make_client(session_factory)
    resp = client.get(f"/api/casos/{_SLUG}/estagio/1", params={"versao": _VERSAO})

    assert resp.status_code == 200, resp.text
    entrada = resp.json()["entrada"]
    bloco = entrada["campanha_stix"]

    assert bloco["encontrada"] is False
    assert bloco["motivo_indisponivel"] == "campanha_nao_encontrada"
    assert bloco["versao_base"] == _VERSAO
    # Falls back to the curated identifier (nome + external_id de SLUG_PARA_STIX).
    assert bloco["nome"] == _NOME_CANONICO
    assert bloco["external_id"] == _EXTERNAL_ID
    # No rich fields when not found.
    assert bloco["stix_id"] is None
    assert bloco["descricao"] is None
    assert bloco["aliases"] == []
    _assert_sem_artefato_caldera(entrada)


def test_post_estagio1_executar_versao_valida_campanha_ausente(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """``POST .../estagio/1/executar?versao=`` com a base legível mas SEM a
    campanha → HTTP 200 e ``campanha_nao_encontrada`` (execução não quebra)."""
    _write_bundle(StixService(data_dir=stix_data_dir).base_path(_VERSAO), [])

    client = _make_client(session_factory)
    resp = client.post(
        f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
    )

    assert resp.status_code == 200, resp.text
    bloco = resp.json()["entrada"]["campanha_stix"]
    assert bloco["encontrada"] is False
    assert bloco["motivo_indisponivel"] == "campanha_nao_encontrada"
    assert bloco["versao_base"] == _VERSAO
    assert bloco["nome"] == _NOME_CANONICO
    assert bloco["external_id"] == _EXTERNAL_ID


# ===========================================================================
# Property 1 — no HTTP error in ANY of the three scenarios (status 200 always).
# ===========================================================================


def test_estagio1_nunca_quebra_http_em_nenhum_cenario(
    session_factory: sessionmaker[Session], stix_data_dir: Path
) -> None:
    """Across (versão válida+campanha, sem versão, versão válida sem campanha),
    both GET and POST return HTTP 200 — a Entrada nunca quebra a Modelagem."""
    base = StixService(data_dir=stix_data_dir).base_path(_VERSAO)
    client = _make_client(session_factory)

    # Scenario A: valid version + campaign present.
    _write_bundle(base, [_campanha_encontrada()])
    assert (
        client.get(f"/api/casos/{_SLUG}/estagio/1", params={"versao": _VERSAO}).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
        ).status_code
        == 200
    )

    # Scenario B: no version at all.
    assert client.get(f"/api/casos/{_SLUG}/estagio/1").status_code == 200
    assert client.post(f"/api/casos/{_SLUG}/estagio/1/executar").status_code == 200

    # Scenario C: valid version but the campaign absent from the bundle.
    _write_bundle(base, [])
    assert (
        client.get(f"/api/casos/{_SLUG}/estagio/1", params={"versao": _VERSAO}).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/casos/{_SLUG}/estagio/1/executar", params={"versao": _VERSAO}
        ).status_code
        == 200
    )
