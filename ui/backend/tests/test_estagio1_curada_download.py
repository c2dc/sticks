"""Testes de exemplo da flag de versão curada e do download — Estágio 1 (task 2.4).

Testes de **exemplo** (não baseados em propriedade) que exercitam diretamente o
:class:`app.services.stix.stix_service.StixService`, complementando os testes de
propriedade em ``test_stix_service_property.py`` e os testes do serviço fino em
``test_estagio1_service.py``:

* a flag de versão curada :func:`versao_eh_curada` — verdadeira só para
  :data:`VERSAO_CURADA` (``"18.0"``) e falsa para qualquer outra versão
  (Req. 5.5);
* ``baixar_base`` nos seus modos de falha ainda não cobertos por exemplo:
  conteúdo baixado inválido (não é um bundle STIX) e falha de escrita em disco —
  ambos sem deixar arquivo parcial (Req. 5.2, 5.9), além de um caso de sucesso e
  de erro HTTP para fechar o conjunto de exemplos;
* ``dashboard`` — contagem por tipo determinística e listagem de campanhas com a
  flag ``curada`` resolvida (Req. 5.7).

Isolamento total: cada teste usa ``StixService(data_dir=tmp_path)`` (o
``sticks/data/stix`` real nunca é tocado) e um ``http_client`` **falso** para o
único egresso de rede — não há HTTP real nesta suíte.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.stix.stix_service import (
    VERSAO_CURADA,
    VERSOES_CONHECIDAS,
    StixDownloadError,
    StixService,
    versao_eh_curada,
)


# ---------------------------------------------------------------------------
# Helpers / fakes (sem rede real)
# ---------------------------------------------------------------------------


def _bundle_curado() -> dict:
    """Bundle STIX mínimo-mas-válido: 1 campanha curada + alguns tipos contáveis."""
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
                "name": "Campanha Qualquer",
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

    def __init__(self, texto: str, status_code: int = 200) -> None:
        self.text = texto
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
# versao_eh_curada — verdadeira só para a versão curada (Req. 5.5)
# ---------------------------------------------------------------------------


def test_versao_eh_curada_verdadeira_para_18() -> None:
    """A versão curada (``VERSAO_CURADA == "18.0"``) é a única marcada como curada."""
    assert VERSAO_CURADA == "18.0"
    assert versao_eh_curada("18.0") is True
    assert versao_eh_curada(VERSAO_CURADA) is True


@pytest.mark.parametrize(
    "versao",
    [
        "19.1",
        "19.0",
        "17.1",
        "17.0",
        "9.0",
        "18",  # sem o ".0" NÃO é a versão curada (fonte única é "18.0")
        "18.1",
        "",
        "999.0",
    ],
)
def test_versao_eh_curada_falsa_para_demais(versao: str) -> None:
    """Qualquer versão diferente da curada (inclusive ``"18"`` sem o patch)
    retorna ``False`` — a fonte única é exatamente ``"18.0"``."""
    assert versao_eh_curada(versao) is False


def test_versao_eh_curada_unica_entre_as_conhecidas() -> None:
    """Exatamente uma das versões conhecidas é a curada."""
    curadas = [v for v in VERSOES_CONHECIDAS if versao_eh_curada(v)]
    assert curadas == [VERSAO_CURADA]


# ---------------------------------------------------------------------------
# baixar_base — exemplos de sucesso e de falha (Req. 5.2, 5.9)
# ---------------------------------------------------------------------------


def test_baixar_base_sucesso_escreve_e_relistura(tmp_path: Path) -> None:
    """Um cliente devolvendo um bundle válido grava o arquivo e devolve
    ``baixada=True`` com a URL versionada; o conteúdo é re-lido por ``ler_bundle``."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    info = service.baixar_base(VERSAO_CURADA)

    assert info.baixada is True
    assert info.selecionavel is True
    destino = service.base_path(VERSAO_CURADA)
    assert destino.exists()
    assert info.arquivo == str(destino)
    # Única chamada de rede, com a URL versionada e timeout finito positivo.
    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0].endswith(f"enterprise-attack-{VERSAO_CURADA}.json")
    # Re-leitura confere o conteúdo persistido.
    lido = service.ler_bundle(VERSAO_CURADA)
    assert any(o.get("name") == "ShadowRay" for o in lido["objects"])
    # Nenhum arquivo temporário sobra no diretório de dados.
    restos = [p.name for p in tmp_path.iterdir() if p.name != destino.name]
    assert restos == []


def test_baixar_base_versao_desconhecida_recusa_sem_disco_nem_rede(
    tmp_path: Path,
) -> None:
    """Uma versão fora de ``VERSOES_CONHECIDAS`` é recusada antes de tocar disco
    ou rede (sem injeção de caminho/URL)."""
    cliente = _ClienteHttpFake(_RespostaFake("nunca-usado"))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.baixar_base("999.0")

    assert cliente.chamadas == []
    assert not service.base_path("999.0").exists()


def test_baixar_base_erro_http_sem_arquivo(tmp_path: Path) -> None:
    """Uma resposta HTTP não-2xx vira ``StixDownloadError`` e nada é gravado."""
    cliente = _ClienteHttpFake(_RespostaFake("not found", status_code=404))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.baixar_base(VERSAO_CURADA)

    assert len(cliente.chamadas) == 1
    assert not service.base_path(VERSAO_CURADA).exists()


def test_baixar_base_conteudo_nao_json_sem_arquivo(tmp_path: Path) -> None:
    """Conteúdo baixado que não é JSON vira ``StixDownloadError`` e nada é gravado."""
    cliente = _ClienteHttpFake(_RespostaFake("isto nao e json {"))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.baixar_base(VERSAO_CURADA)

    assert not service.base_path(VERSAO_CURADA).exists()
    # Sem arquivo parcial/temporário no diretório de dados.
    if tmp_path.exists():
        assert list(tmp_path.iterdir()) == []


def test_baixar_base_conteudo_json_sem_objects_sem_arquivo(tmp_path: Path) -> None:
    """JSON válido que NÃO é um bundle STIX (sem a lista ``objects``) é tratado
    como falha de download — nada é gravado (Req. 5.9)."""
    cliente = _ClienteHttpFake(_RespostaFake(json.dumps({"type": "bundle"})))
    service = StixService(data_dir=tmp_path, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.baixar_base(VERSAO_CURADA)

    assert not service.base_path(VERSAO_CURADA).exists()
    if tmp_path.exists():
        assert list(tmp_path.iterdir()) == []


def test_baixar_base_falha_escrita_sem_arquivo_parcial(tmp_path: Path) -> None:
    """Falha de escrita (diretório de dados é, na verdade, um arquivo) vira
    ``StixDownloadError`` e nenhum arquivo parcial é deixado (Req. 5.9).

    Mesmo com o conteúdo válido já baixado, a gravação atômica não consegue criar
    o diretório de dados porque o caminho aponta para um arquivo pré-existente, o
    que exercita o caminho de erro de escrita (``OSError``) de ``baixar_base``.
    """
    # ``data_dir`` aponta para um ARQUIVO, não um diretório: mkdir(parents=True)
    # falha com OSError, que ``baixar_base`` converte em StixDownloadError.
    caminho_arquivo = tmp_path / "stix_data_como_arquivo"
    caminho_arquivo.write_text("sou um arquivo, nao um diretorio", encoding="utf-8")

    cliente = _ClienteHttpFake(_RespostaFake(json.dumps(_bundle_curado())))
    service = StixService(data_dir=caminho_arquivo, http_client=cliente)

    with pytest.raises(StixDownloadError):
        service.baixar_base(VERSAO_CURADA)

    # O download chegou a ocorrer (conteúdo válido), mas a escrita falhou; o
    # arquivo de destino não existe e o caminho continua sendo o arquivo original.
    assert caminho_arquivo.is_file()
    assert not service.base_path(VERSAO_CURADA).exists()


# ---------------------------------------------------------------------------
# dashboard — contagem por tipo + campanhas (Req. 5.7)
# ---------------------------------------------------------------------------


def _escrever_bundle(service: StixService, versao: str, bundle: dict) -> None:
    caminho = service.base_path(versao)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(bundle), encoding="utf-8")


def test_dashboard_contagem_por_tipo_deterministica(tmp_path: Path) -> None:
    """O dashboard traz os seis tipos exigidos, em ordem estável, com a contagem
    exata por tipo e descrições pt-BR não vazias (Req. 5.7)."""
    service = StixService(data_dir=tmp_path)
    _escrever_bundle(service, VERSAO_CURADA, _bundle_curado())

    dashboard = service.dashboard(VERSAO_CURADA)

    # Ordem estável e completa dos seis tipos exigidos.
    rotulos = [c.tipo for c in dashboard.contagens]
    assert rotulos == [
        "campanhas",
        "tecnicas",
        "taticas",
        "intrusion_sets",
        "malware",
        "relacionamentos",
    ]
    por_tipo = {c.stix_type: c.quantidade for c in dashboard.contagens}
    assert por_tipo["campaign"] == 2
    assert por_tipo["attack-pattern"] == 3
    assert por_tipo["intrusion-set"] == 1
    # O tipo de ruído "identity" nunca entra nas contagens exigidas.
    assert por_tipo["x-mitre-tactic"] == 0
    assert por_tipo["malware"] == 0
    assert por_tipo["relationship"] == 0
    # Toda contagem exposta tem descrição pt-BR não vazia.
    for contagem in dashboard.contagens:
        assert contagem.descricao.strip() != ""


def test_dashboard_e_campanhas_listam_curada(tmp_path: Path) -> None:
    """A listagem de campanhas do bundle preserva a ordem e resolve a flag
    ``curada``/``slug_curado`` (ShadowRay curada, a outra não)."""
    service = StixService(data_dir=tmp_path)
    _escrever_bundle(service, VERSAO_CURADA, _bundle_curado())

    campanhas = service.listar_campanhas(VERSAO_CURADA)

    assert [c.nome for c in campanhas] == ["ShadowRay", "Campanha Qualquer"]
    curada = campanhas[0]
    assert curada.curada is True
    assert curada.slug_curado == "shadowray"
    nao_curada = campanhas[1]
    assert nao_curada.curada is False
    assert nao_curada.slug_curado is None
