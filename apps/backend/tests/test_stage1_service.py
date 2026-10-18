"""Testes de exemplo do serviço fino do Estágio 1 — Seleção da base STIX (task 2.2).

Cobrem a orquestração de :class:`app.services.stix.stage1_service.Stage1Service`
que reusa o :class:`app.services.stix.stix_service.StixService`:

* ``list_bases`` projeta a flag ``curated`` e o state de disco (Req. 5.4, 5.5);
* ``download_base`` baixa só quando ausente e reusa o disco quando presente, com o
  download sendo o único egresso de rede (Req. 5.2, 5.3);
* ``complete`` conclui o Estágio só quando o bundle está em disco e o dashboard
  foi montado (Req. 5.8, 5.10) e, em failure de download/leitura, mantém o Estágio
  não concluído com a causa reportada e sem file parcial (Req. 5.9);
* a Saída (dashboard + campaigns) alimenta o Estágio 2 (Req. 2.2).

Full isolation: each test uses ``StixService(data_dir=tmp_path)`` (never touches the
``sticks/data/stix`` real) e um ``http_client`` **falso** para o único egresso de
rede — não há HTTP real nesta suíte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.stix.stage1_service import (
    BaseStixListada,
    Stage1Service,
    Stage1ResultStix,
)
from app.services.stix.stix_service import (
    CURATED_VERSION,
    KNOWN_VERSIONS,
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

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
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
# list_bases — flag curated + state de disco
# ---------------------------------------------------------------------------


def test_list_bases_projects_curated_flag_and_disk(tmp_path: Path) -> None:
    """Each listed base carries ``curated`` (True only for 18.0) and no base is
    downloaded by default (Req. 5.4, 5.5, 5.6)."""
    service = Stage1Service(StixService(data_dir=tmp_path))

    bases = service.list_bases()

    assert all(isinstance(b, BaseStixListada) for b in bases)
    assert {b.version for b in bases} == set(KNOWN_VERSIONS)
    # Exactly one curated version (18.0), and nothing downloaded by default.
    curadas = [b.version for b in bases if b.curated]
    assert curadas == [CURATED_VERSION]
    assert all(not b.downloaded and not b.selectable for b in bases)


def test_list_bases_reflects_bundle_on_disk(tmp_path: Path) -> None:
    """A materialised bundle flips ``downloaded``/``selectable`` for that version."""
    stix = StixService(data_dir=tmp_path)
    _escrever_bundle(stix.base_path(CURATED_VERSION))
    service = Stage1Service(stix)

    base = next(b for b in service.list_bases() if b.version == CURATED_VERSION)

    assert base.downloaded and base.selectable
    assert base.file == str(stix.base_path(CURATED_VERSION))
    assert base.curated is True


# ---------------------------------------------------------------------------
# download_base — reuso do disco vs único egresso de rede
# ---------------------------------------------------------------------------


def test_download_base_reuses_disk_no_network(tmp_path: Path) -> None:
    """When the bundle is already on disk, no network egress happens (Req. 5.3)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    _escrever_bundle(stix.base_path(CURATED_VERSION))
    service = Stage1Service(stix)

    info = service.download_base(CURATED_VERSION)

    assert info.downloaded and info.selectable
    assert cliente.chamadas == []  # reused disk — the only egress never ran


def test_download_base_downloads_when_absent(tmp_path: Path) -> None:
    """When absent, the single network egress runs and writes the bundle (Req. 5.2)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Stage1Service(stix)

    info = service.download_base(CURATED_VERSION)

    assert info.downloaded and info.selectable
    assert len(cliente.chamadas) == 1
    assert stix.base_path(CURATED_VERSION).exists()


def test_download_base_unknown_version_refuses_no_disk(tmp_path: Path) -> None:
    """An unknown version is rejected before touching disk/network (Req. 5.2)."""
    cliente = _ClienteHttpFake(_RespostaFake("irrelevante"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Stage1Service(stix)

    with pytest.raises(StixDownloadError):
        service.download_base("999.0")

    assert cliente.chamadas == []
    assert not stix.base_path("999.0").exists()


# ---------------------------------------------------------------------------
# complete — verdict de conclusão + Saída
# ---------------------------------------------------------------------------


def test_complete_success_builds_dashboard_and_campaigns(tmp_path: Path) -> None:
    """Success: bundle on disk + dashboard assembled → completed; the Saída
    (dashboard + campaigns) feeds Stage 2 (Req. 5.8, 5.10, 2.2)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Stage1Service(stix)

    result = service.complete(CURATED_VERSION)

    assert isinstance(result, Stage1ResultStix)
    assert result.completed is True
    assert result.bundle_on_disk is True
    assert result.curated is True
    assert result.error_message is None
    assert result.dashboard is not None
    # Dashboard carries the six required per-type counts.
    por_tipo = {c.stix_type: c.count for c in result.dashboard.counts}
    assert por_tipo["attack-pattern"] == 2
    assert por_tipo["campaign"] == 1
    # Saída → Entrada do Estágio 2: the curated campaign is listed and flagged.
    assert len(result.campaigns) == 1
    assert result.campaigns[0].curated is True
    assert result.campaigns[0].curated_slug == "shadowray"


def test_complete_reuses_bundle_on_disk_no_network(tmp_path: Path) -> None:
    """A pre-existing bundle concludes Stage 1 without any network egress."""
    cliente = _ClienteHttpFake(_RespostaFake("nunca-usado"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    _escrever_bundle(stix.base_path(CURATED_VERSION))
    service = Stage1Service(stix)

    result = service.complete(CURATED_VERSION)

    assert result.completed is True
    assert cliente.chamadas == []


def test_complete_download_failure_does_not_complete_no_partial_file(tmp_path: Path) -> None:
    """Download failure: Stage not concluded, cause reported, no partial file
    on disk (Req. 5.9)."""
    cliente = _ClienteHttpFake(RuntimeError("rede caiu"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Stage1Service(stix)

    result = service.complete(CURATED_VERSION)

    assert result.completed is False
    assert result.bundle_on_disk is False
    assert result.dashboard is None
    assert result.campaigns == []
    assert result.error_message is not None and result.error_message != ""
    assert not stix.base_path(CURATED_VERSION).exists()  # no partial file


def test_complete_unknown_version_does_not_complete_with_cause(tmp_path: Path) -> None:
    """An unknown version keeps the Stage not concluded with a reported cause,
    without touching disk or network (Req. 5.9)."""
    cliente = _ClienteHttpFake(_RespostaFake("irrelevante"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Stage1Service(stix)

    result = service.complete("999.0")

    assert result.completed is False
    assert result.error_message is not None
    assert cliente.chamadas == []


def test_complete_invalid_content_does_not_complete(tmp_path: Path) -> None:
    """Downloaded content that is not a STIX bundle fails the download, keeping
    the Stage not concluded with no partial file (Req. 5.9)."""
    cliente = _ClienteHttpFake(_RespostaFake("isto nao e json"))
    stix = StixService(data_dir=tmp_path, http_client=cliente)
    service = Stage1Service(stix)

    result = service.complete(CURATED_VERSION)

    assert result.completed is False
    assert result.dashboard is None
    assert not stix.base_path(CURATED_VERSION).exists()
