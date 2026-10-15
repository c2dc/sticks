"""Testes de exemplo do serviço fino do Estágio 1 — Seleção da base STIX (task 2.2).

Cobrem a orquestração de :class:`app.services.stix.estagio1_service.Estagio1Service`
que reusa o :class:`app.services.stix.stix_service.StixService`:

* ``listar_bases`` projeta a flag ``curada`` e o estado de disco (Req. 5.4, 5.5);
* ``baixar_base`` baixa só quando ausente e reusa o disco quando presente, com o
  download sendo o único egresso de rede (Req. 5.2, 5.3);
* ``concluir`` conclui o Estágio só quando o bundle está em disco e o dashboard
  foi montado (Req. 5.8, 5.10) e, em falha de download/leitura, mantém o Estágio
  não concluído com a causa reportada e sem arquivo parcial (Req. 5.9);
* a Saída (dashboard + campanhas) alimenta o Estágio 2 (Req. 2.2).

Isolamento total: cada teste usa ``StixService(data_dir=tmp_path)`` (nunca toca o
``sticks/data/stix`` real) e um ``http_client`` **falso** para o único egresso de
rede — não há HTTP real nesta suíte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.stix.estagio1_service import (
    BaseStixListada,
    Estagio1Service,
    ResultadoEstagio1,
)
from app.services.stix.stix_service import (
    VERSAO_CURADA,
    VERSOES_CONHECIDAS,
    StixDownloadError,
    StixService,
)


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _bundle_curado() -> dict:
    """A minimal-but-valid STIX bundle with one curated campaign + a few types."""
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
            {"type": "attack-pattern", "id": "attack-pattern--2"},
            {"type": "intrusion-set", "id": "intrusion-set--1"},
        ],
    }


def _escrever_bundle(path: Path, bundle: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle or _bundle_curado()), encoding="utf-8")


class _RespostaFake:
    """A minimal requests-like response for the injected ``http_client``."""

    def __init__(self, texto: str, status_code: int = 200) -> None:
        self.text = texto
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


class _ClienteHttpFake:
    """Fake http client recording every GET; returns a canned response."""

    def __init__(self, resposta: _RespostaFake | Exception) -> None:
        self._resposta = resposta
        self.chamadas: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> _RespostaFake:
        self.chamadas.append(url)
        if isinstance(self._resposta, Exception):
            raise self._resposta
        return self._resposta


# ---------------------------------------------------------------------------
# listar_bases — flag curada + estado de disco
# ---------------------------------------------------------------------------


def test_listar_bases_projeta_flag_curada_e_disco(tmp_path: Path) -> None:
    """Each listed base carries ``curada`` (True only for 18.0) and no base is
    downloaded by default (Req. 5.4, 5.5, 5.6)."""
    service = Estagio1Service(StixService(data_dir=tmp_path))

    bases = service.listar_bases()

    assert all(isinstance(b, BaseStixListada) for b in bases)
    assert {b.versao for b in bases} == set(VERSOES_CONHECIDAS)
    # Exactly one curated version (18.0), and nothing downloaded by default.
    curadas = [b.versao for b in bases if b.curada]
    assert curadas == [VERSAO_CURADA]
    assert all(not b.baixada and not b.selecionavel for b in bases)


def test_listar_bases_reflete_bundle_em_disco(tmp_path: Path) -> None:
    """A materialised bundle flips ``baixada``/``selecionavel`` for that version."""
    stix = StixService(data_dir=tmp_path)
    _escrever_bundle(stix.base_path(VERSAO_CURADA))
    service = Estagio1Service(stix)

    base = next(b for b in service.listar_bases() if b.versao == VERSAO_CURADA)

    assert base.baixada and base.selecionavel
    assert base.arquivo == str(stix.base_path(VERSAO_CURADA))
    assert base.curada is True


# ---------------------------------------------------------------------------
# baixar_base — reuso do disco vs único egresso de rede
# ---------------------------------------------------------------------------


def test_baixar_base_reusa_disco_sem_rede(tmp_path: Path) -> None:
    """When the bundle is already on disk, no network egress happens (Req. 5.3)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    _escrever_bundle(stix.base_path(VERSAO_CURADA))
    service = Estagio1Service(stix)

    info = service.baixar_base(VERSAO_CURADA)

    assert info.baixada and info.selecionavel
    assert cliente.chamadas == []  # reused disk — the only egress never ran


def test_baixar_base_baixa_quando_ausente(tmp_path: Path) -> None:
    """When absent, the single network egress runs and writes the bundle (Req. 5.2)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Estagio1Service(stix)

    info = service.baixar_base(VERSAO_CURADA)

    assert info.baixada and info.selecionavel
    assert len(cliente.chamadas) == 1
    assert stix.base_path(VERSAO_CURADA).exists()


def test_baixar_base_versao_desconhecida_recusa_sem_disco(tmp_path: Path) -> None:
    """An unknown version is rejected before touching disk/network (Req. 5.2)."""
    cliente = _ClienteHttpFake(_RespostaFake("irrelevante"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Estagio1Service(stix)

    with pytest.raises(StixDownloadError):
        service.baixar_base("999.0")

    assert cliente.chamadas == []
    assert not stix.base_path("999.0").exists()


# ---------------------------------------------------------------------------
# concluir — veredito de conclusão + Saída
# ---------------------------------------------------------------------------


def test_concluir_sucesso_monta_dashboard_e_campanhas(tmp_path: Path) -> None:
    """Success: bundle on disk + dashboard assembled → concluido; the Saída
    (dashboard + campaigns) feeds Stage 2 (Req. 5.8, 5.10, 2.2)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Estagio1Service(stix)

    resultado = service.concluir(VERSAO_CURADA)

    assert isinstance(resultado, ResultadoEstagio1)
    assert resultado.concluido is True
    assert resultado.bundle_em_disco is True
    assert resultado.curada is True
    assert resultado.mensagem_erro is None
    assert resultado.dashboard is not None
    # Dashboard carries the six required per-type counts.
    por_tipo = {c.stix_type: c.quantidade for c in resultado.dashboard.contagens}
    assert por_tipo["attack-pattern"] == 2
    assert por_tipo["campaign"] == 1
    # Saída → Entrada do Estágio 2: the curated campaign is listed and flagged.
    assert len(resultado.campanhas) == 1
    assert resultado.campanhas[0].curada is True
    assert resultado.campanhas[0].slug_curado == "shadowray"


def test_concluir_reusa_bundle_em_disco_sem_rede(tmp_path: Path) -> None:
    """A pre-existing bundle concludes Stage 1 without any network egress."""
    cliente = _ClienteHttpFake(_RespostaFake("nunca-usado"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    _escrever_bundle(stix.base_path(VERSAO_CURADA))
    service = Estagio1Service(stix)

    resultado = service.concluir(VERSAO_CURADA)

    assert resultado.concluido is True
    assert cliente.chamadas == []


def test_concluir_falha_download_nao_conclui_sem_arquivo_parcial(tmp_path: Path) -> None:
    """Download failure: Stage not concluded, cause reported, no partial file
    on disk (Req. 5.9)."""
    cliente = _ClienteHttpFake(RuntimeError("rede caiu"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Estagio1Service(stix)

    resultado = service.concluir(VERSAO_CURADA)

    assert resultado.concluido is False
    assert resultado.bundle_em_disco is False
    assert resultado.dashboard is None
    assert resultado.campanhas == []
    assert resultado.mensagem_erro is not None and resultado.mensagem_erro != ""
    assert not stix.base_path(VERSAO_CURADA).exists()  # no partial file


def test_concluir_versao_desconhecida_nao_conclui_com_causa(tmp_path: Path) -> None:
    """An unknown version keeps the Stage not concluded with a reported cause,
    without touching disk or network (Req. 5.9)."""
    cliente = _ClienteHttpFake(_RespostaFake("irrelevante"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Estagio1Service(stix)

    resultado = service.concluir("999.0")

    assert resultado.concluido is False
    assert resultado.mensagem_erro is not None
    assert cliente.chamadas == []


def test_concluir_conteudo_invalido_nao_conclui(tmp_path: Path) -> None:
    """Downloaded content that is not a STIX bundle fails the download, keeping
    the Stage not concluded with no partial file (Req. 5.9)."""
    cliente = _ClienteHttpFake(_RespostaFake("isto nao e json"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Estagio1Service(stix)

    resultado = service.concluir(VERSAO_CURADA)

    assert resultado.concluido is False
    assert resultado.dashboard is None
    assert not stix.base_path(VERSAO_CURADA).exists()
