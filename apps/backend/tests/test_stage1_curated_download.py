"""Testes de exemplo da flag de versão curated e do download — Estágio 1 (task 2.4).

Testes de **exemplo** (não baseados em propriedade) que exercitam diretamente o
:class:`app.services.stix.stix_service.StixService`, complementando os testes de
propriedade em ``test_stix_service_property.py`` e os testes do serviço fino em
``test_stage1_service.py``:

* a flag de versão curated :func:`version_is_curated` — verdadeira só para
  :data:`CURATED_VERSION` (``"18.0"``) e falsa para qualquer outra versão
  (Req. 5.5);
* ``download_base`` nodes seus modos de failure ainda não cobertos por exemplo:
  conteúdo downloaded inválido (não é um bundle STIX) e failure de escrita em disco —
  ambos sem deixar file parcial (Req. 5.2, 5.9), além de um campaign de success e
  de erro HTTP para fechar o conjunto de exemplos;
* ``dashboard`` — contagem por type determinística e listagem de campaigns com a
  flag ``curated`` resolvida (Req. 5.7).

Full isolation: each test uses ``StixService(data_dir=tmp_path)`` (the
``sticks/data/stix`` real nunca é tocado) e um ``http_client`` **falso** para o
único egresso de rede — não há HTTP real nesta suíte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.stix.stix_service import (
    CURATED_VERSION,
    KNOWN_VERSIONS,
    StixDownloadError,
    StixService,
    version_is_curated,
)


# ---------------------------------------------------------------------------
# Helpers / fakes (sem rede real)
# ---------------------------------------------------------------------------


def _bundle_curado() -> dict:
    """Bundle STIX mínimo-mas-válido: 1 campaign curated + alguns tipos contáveis."""
    return {
        "type": "bundle",
        "objects": [
            {
                "type": "campaign",
                "id": "campaign--0000",
                "name": "ShadowRay",
                "external_references": [{"external_id": "C0045"}],
            },
            {
                "type": "campaign",
                "id": "campaign--0001",
                "name": "Any Campaign",
            },
            {"type": "attack-pattern", "id": "attack-pattern--1"},
            {"type": "attack-pattern", "id": "attack-pattern--2"},
            {"type": "attack-pattern", "id": "attack-pattern--3"},
            {"type": "intrusion-set", "id": "intrusion-set--1"},
            {"type": "identity", "id": "identity--ruido"},
        ],
    }


class _RespostaFake:
    """Resposta mínima no estilo ``requests`` para o ``http_client`` injetado."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


class _ClienteHttpFake:
    """Cliente HTTP falso: registra cada GET e devolve uma resposta canônica."""

    def __init__(self, resposta: _RespostaFake | Exception) -> None:
        self._resposta = resposta
        self.chamadas: list[str] = []

    def get(self, url: str, timeout: float | None = None) -> _RespostaFake:
        self.chamadas.append(url)
        if isinstance(self._resposta, Exception):
            raise self._resposta
        return self._resposta


# ---------------------------------------------------------------------------
# version_is_curated — verdadeira só para a versão curated (Req. 5.5)
# ---------------------------------------------------------------------------


def test_version_is_curated_true_for_18() -> None:
    """A versão curated (``CURATED_VERSION == "18.0"``) é a única marcada como curated."""
    assert CURATED_VERSION == "18.0"
    assert version_is_curated("18.0") is True
    assert version_is_curated(CURATED_VERSION) is True


@pytest.mark.parametrize(
    "version",
    [
        "19.1",
        "19.0",
        "17.1",
        "17.0",
        "9.0",
        "18",  # sem o ".0" NÃO é a versão curated (fonte única é "18.0")
        "18.1",
        "",
        "999.0",
    ],
)
def test_version_is_curated_false_for_others(version: str) -> None:
    """Qualquer versão diferente da curated (inclusive ``"18"`` sem o patch)
    retorna ``False`` — a fonte única é exatamente ``"18.0"``."""
    assert version_is_curated(version) is False


def test_version_is_curated_unique_among_known() -> None:
    """Exatamente uma das versões conhecidas é a curated."""
    curadas = [v for v in KNOWN_VERSIONS if version_is_curated(v)]
    assert curadas == [CURATED_VERSION]


# ---------------------------------------------------------------------------
# download_base — exemplos de success e de failure (Req. 5.2, 5.9)
# ---------------------------------------------------------------------------


def test_download_base_success_writes_and_relists(tmp_path: Path) -> None:
    """Um cliente devolvendo um bundle válido grava o file e devolve
    ``downloaded=True`` com a URL versionada; o conteúdo é re-lido por ``read_bundle``."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    info = service.download_base(CURATED_VERSION)

    assert info.downloaded is True
    assert info.selectable is True
    destino = service.base_path(CURATED_VERSION)
    assert destino.exists()
    assert info.file == str(destino)
    # Única chamada de rede, com a URL versionada e timeout finito positivo.
    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0].endswith(f"enterprise-attack-{CURATED_VERSION}.json")
    # Re-leitura confere o conteúdo persistido.
    lido = service.read_bundle(CURATED_VERSION)
    assert any(o.get("name") == "ShadowRay" for o in lido["objects"])
    # Nenhum file temporário sobra no diretório de dados.
    restos = [p.name for p in tmp_path.iterdir() if p.name != destino.name]
    assert restos == []


def test_download_base_unknown_version_refuses_no_disk_no_network(
    tmp_path: Path,
) -> None:
    """Uma versão fora de ``KNOWN_VERSIONS`` é recusada antes de tocar disco
    ou rede (sem injeção de caminho/URL)."""
    cliente = _ClienteHttpFake(_RespostaFake("nunca-usado"))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.download_base("999.0")

    assert cliente.chamadas == []
    assert not service.base_path("999.0").exists()


def test_download_base_http_error_no_file(tmp_path: Path) -> None:
    """Uma resposta HTTP não-2xx vira ``StixDownloadError`` e nada é gravado."""
    cliente = _ClienteHttpFake(_RespostaFake("not found", status_code=404))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.download_base(CURATED_VERSION)

    assert len(cliente.chamadas) == 1
    assert not service.base_path(CURATED_VERSION).exists()


def test_download_base_non_json_content_no_file(tmp_path: Path) -> None:
    """Conteúdo downloaded que não é JSON vira ``StixDownloadError`` e nada é gravado."""
    cliente = _ClienteHttpFake(_RespostaFake("isto nao e json {"))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.download_base(CURATED_VERSION)

    assert not service.base_path(CURATED_VERSION).exists()
    # Sem file parcial/temporário no diretório de dados.
    if tmp_path.exists():
        assert list(tmp_path.iterdir()) == []


def test_download_base_json_without_objects_no_file(tmp_path: Path) -> None:
    """JSON válido que NÃO é um bundle STIX (sem a lista ``objects``) é tratado
    como failure de download — nada é gravado (Req. 5.9)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps({"type": "bundle"})))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.download_base(CURATED_VERSION)

    assert not service.base_path(CURATED_VERSION).exists()
    if tmp_path.exists():
        assert list(tmp_path.iterdir()) == []


def test_download_base_write_failure_no_partial_file(tmp_path: Path) -> None:
    """Falha de escrita (diretório de dados é, na verdade, um file) vira
    ``StixDownloadError`` e nenhum file parcial é deixado (Req. 5.9).

    Mesmo com o conteúdo válido já downloaded, a gravação atômica não consegue criar
    o diretório de dados porque o caminho aponta para um file pré-existente, o
    que exercita o caminho de erro de escrita (``OSError``) de ``download_base``.
    """
    # ``data_dir`` aponta para um ARQUIVO, não um diretório: mkdir(parents=True)
    # failure com OSError, que ``download_base`` converte em StixDownloadError.
    caminho_arquivo = tmp_path / "stix_data_como_arquivo"
    caminho_arquivo.write_text("sou um file, nao um diretorio", encoding="utf-8")

    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    service = StixService(data_dir=caminho_arquivo, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.download_base(CURATED_VERSION)

    # O download chegou a ocorrer (conteúdo válido), mas a escrita falhou; o
    # file de destino não existe e o caminho continua sendo o file original.
    assert caminho_arquivo.is_file()
    assert not service.base_path(CURATED_VERSION).exists()


# ---------------------------------------------------------------------------
# dashboard — contagem por type + campaigns (Req. 5.7)
# ---------------------------------------------------------------------------


def _escrever_bundle(service: StixService, version: str, bundle: dict) -> None:
    caminho = service.base_path(version)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(bundle), encoding="utf-8")


def test_dashboard_contagem_por_tipo_deterministica(tmp_path: Path) -> None:
    """O dashboard traz os seis tipos exigidos, em ordem estável, com a contagem
    exata por type e descrições pt-BR não vazias (Req. 5.7)."""
    service = StixService(data_dir=tmp_path)
    _escrever_bundle(service, CURATED_VERSION, _bundle_curado())

    dashboard = service.dashboard(CURATED_VERSION)

    # Ordem estável e completa dos seis tipos exigidos.
    rotulos = [c.label for c in dashboard.counts]
    assert rotulos == [
        "campanhas",
        "tecnicas",
        "taticas",
        "intrusion_sets",
        "malware",
        "relacionamentos",
    ]
    por_tipo = {c.stix_type: c.count for c in dashboard.counts}
    assert por_tipo["campaign"] == 2
    assert por_tipo["attack-pattern"] == 3
    assert por_tipo["intrusion-set"] == 1
    # O type de ruído "identity" nunca entra nas counts exigidas.
    assert por_tipo["x-mitre-tactic"] == 0
    assert por_tipo["malware"] == 0
    assert por_tipo["relationship"] == 0
    # Toda contagem exposta tem descrição pt-BR não vazia.
    for contagem in dashboard.counts:
        assert contagem.description.strip() != ""


def test_dashboard_and_campaigns_list_curated(tmp_path: Path) -> None:
    """A listagem de campaigns do bundle preserva a ordem e resolve a flag
    ``curated``/``curated_slug`` (ShadowRay curated, a outra não)."""
    service = StixService(data_dir=tmp_path)
    _escrever_bundle(service, CURATED_VERSION, _bundle_curado())

    campaigns = service.list_campaigns(CURATED_VERSION)

    assert [c.name for c in campaigns] == ["ShadowRay", "Any Campaign"]
    curated = campaigns[0]
    assert curated.curated is True
    assert curated.curated_slug == "shadowray"
    nao_curada = campaigns[1]
    assert nao_curada.curated is False
    assert nao_curada.curated_slug is None
