"""Testes de endpoint do Estágio 1 — Seleção da base STIX (task 2.3).

No esquema de SEIS Estágios, o Estágio 1 é a "Seleção da base STIX". A tarefa 2.3
adiciona/alinha os seus contratos REST (JSON pt-BR), todos reusando o
:class:`~app.services.stix.estagio1_service.Estagio1Service` (que, por sua vez,
delega ao ``StixService``):

* ``GET  /api/stix/bases`` — lista as versões conhecidas com ``baixada`` /
  ``selecionavel`` (presença em disco checada ao vivo; nenhuma versão baixada por
  padrão — Req. 5.4) **e** a flag ``curada`` por versão (``True`` apenas para a
  versão 18 — Req. 5.5, 5.6). A rota permanece no router ``stix`` (alinhada, não
  duplicada), mas passa a projetar ``curada`` via ``Estagio1Service``.
* ``POST /api/stix/bases/{versao}/baixar`` — o Estágio 1 garante o pacote da
  versão em disco: reusa o pacote quando presente (sem baixar de novo — Req. 5.3)
  e, quando ausente, aciona o **único egresso de rede** (valida ``versao`` contra
  ``VERSOES_CONHECIDAS`` **antes** de tocar disco/rede — Req. 5.2). Falhas são
  mapeadas ao contrato ``falha_download``: 422 (versão desconhecida, rejeitada no
  pré-voo) / 502 (falha de rede/conteúdo/escrita — Req. 5.9).

Isolamento total
----------------
Os handlers do router ``casos``/``stix`` constroem ``Estagio1Service()`` (sem
injeção na rota), que resolve ``StixService()`` no módulo ``estagio1_service``.
Monkeypatcheamos esse símbolo para uma fábrica que fixa ``data_dir`` num
``tmp_path`` isolado e injeta um ``http_client`` **falso** no único egresso de
rede, de modo que nenhum arquivo real ou HTTP real seja tocado.

_Requisitos: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8_
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.api.stix as stix_api
import app.services.stix.estagio1_service as estagio1_module
from app.main import create_app
from app.services.stix.stix_service import (
    VERSAO_CURADA,
    VERSOES_CONHECIDAS,
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

    def __init__(self, texto: str, status_code: int = 200) -> None:
        self.text = texto
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


def _pin_estagio1(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    http: "_ClienteHttpFake | None" = None,
) -> None:
    """Pin the ``Estagio1Service``'s ``StixService`` to ``tmp_path`` (+ fake http).

    The route builds ``Estagio1Service()`` → ``StixService()`` resolved in the
    ``estagio1_service`` module; patching that symbol redirects every disk read
    (and the single network egress) into the isolated temp dir / fake client.
    """

    def _factory(*args: object, **kwargs: object) -> StixService:
        return StixService(data_dir=tmp_path, http_client=http)

    # The ``POST .../baixar`` and ``GET .../estagio/1`` handlers (router casos)
    # build ``Estagio1Service()`` → ``StixService()`` resolved in the
    # ``estagio1_service`` module; ``GET /api/stix/bases`` (router stix) builds
    # ``Estagio1Service(StixService())`` with the ``StixService`` symbol of the
    # ``app.api.stix`` module. Patch both so every route is isolated to tmp_path.
    monkeypatch.setattr(estagio1_module, "StixService", _factory)
    monkeypatch.setattr(stix_api, "StixService", _factory)


# ---------------------------------------------------------------------------
# GET /api/stix/bases — listagem com baixada/selecionavel/curada
# ---------------------------------------------------------------------------


def test_get_bases_projeta_flag_curada_e_nenhuma_baixada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GET /api/stix/bases`` → 200 com a flag ``curada`` por versão (``True``
    só para a versão curada) e nenhuma baixada num diretório vazio (Req. 5.4,
    5.5, 5.6)."""
    _pin_estagio1(monkeypatch, tmp_path)
    client = TestClient(create_app())

    resp = client.get("/api/stix/bases")

    assert resp.status_code == 200, resp.text
    bases = resp.json()["bases"]
    assert {b["versao"] for b in bases} == set(VERSOES_CONHECIDAS)
    # Flag curada: exatamente a versão curada vem True.
    curadas = {b["versao"] for b in bases if b["curada"]}
    assert curadas == {VERSAO_CURADA}
    # Diretório vazio: nada baixado por padrão.
    for base in bases:
        assert base["baixada"] is False
        assert base["selecionavel"] is False
        assert base["arquivo"] is None


# ---------------------------------------------------------------------------
# POST /api/stix/bases/{versao}/baixar — download (único egresso de rede)
# ---------------------------------------------------------------------------


def test_post_baixar_baixa_quando_ausente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /api/stix/bases/{versao}/baixar`` ausente → 200, baixa via o único
    egresso de rede e marca ``baixada=True`` (Req. 5.2)."""
    http = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    _pin_estagio1(monkeypatch, tmp_path, http)
    client = TestClient(create_app())

    resp = client.post(f"/api/stix/bases/{VERSAO_CURADA}/baixar")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["versao"] == VERSAO_CURADA
    assert body["baixada"] is True
    assert body["arquivo"]
    # Houve exatamente um egresso de rede (o download).
    assert len(http.chamadas) == 1


def test_post_baixar_reusa_disco_sem_rede(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../baixar`` com o pacote já em disco → 200 e **nenhum** egresso de
    rede (reusa o disco — Req. 5.3)."""
    # Semeia o bundle em disco.
    base = StixService(data_dir=tmp_path).base_path(VERSAO_CURADA)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_text(json.dumps(_bundle_curado()), encoding="utf-8")

    http = _ClienteHttpFake(RuntimeError("não deveria haver rede"))
    _pin_estagio1(monkeypatch, tmp_path, http)
    client = TestClient(create_app())

    resp = client.post(f"/api/stix/bases/{VERSAO_CURADA}/baixar")

    assert resp.status_code == 200, resp.text
    assert resp.json()["baixada"] is True
    assert http.chamadas == []  # disco reusado, sem rede


def test_post_baixar_versao_desconhecida_422(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../baixar`` com versão desconhecida → 422 ``falha_download``,
    rejeitada antes de tocar disco/rede (Req. 5.2)."""
    http = _ClienteHttpFake(_RespostaFake("irrelevante"))
    _pin_estagio1(monkeypatch, tmp_path, http)
    client = TestClient(create_app())

    resp = client.post("/api/stix/bases/999.0/baixar")

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["motivo"] == "falha_download"
    # Pré-voo: nenhum egresso de rede para uma versão desconhecida.
    assert http.chamadas == []


def test_post_baixar_falha_download_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST .../baixar`` com falha de rede → 502 ``falha_download`` e nenhum
    arquivo parcial em disco (Req. 5.9)."""
    http = _ClienteHttpFake(RuntimeError("rede caiu"))
    _pin_estagio1(monkeypatch, tmp_path, http)
    client = TestClient(create_app())

    resp = client.post(f"/api/stix/bases/{VERSAO_CURADA}/baixar")

    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["motivo"] == "falha_download"
    # Nenhum arquivo parcial deixado em disco.
    assert not StixService(data_dir=tmp_path).base_path(VERSAO_CURADA).exists()
