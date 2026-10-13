"""Property-based + example tests for StixService (fluxo-stix-dashboard).

Covers the optional test sub-tasks 1.3, 1.4, 2.3, 2.4, 2.5, 3.3, 3.4, 3.5 and
4.2 of the ``fluxo-stix-dashboard`` spec. Every property-based test is tagged
with the exact ``# Feature: fluxo-stix-dashboard, Property N: <text>`` comment
required by the plan and runs with Hypothesis at ``max_examples=100`` (minimum
100 iterations).

Isolation (no real files / no real network):

* Every test instantiates ``StixService(data_dir=tmp_path)`` so the service
  reads/writes only inside Hypothesis/pytest temp directories — the real
  ``sticks/data/stix`` is never touched.
* The single network egress (``baixar_base``) is exercised with an injected
  **fake** ``http_client`` — there is no real HTTP in this suite.

The oracles are computed independently of the service under test (counting from
the generated bundle directly), so the properties genuinely cross-check the
implementation rather than restating it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.stix.stix_service import (
    DESCRICOES_TIPO,
    ROTULO_TIPO,
    SLUG_PARA_STIX,
    VERSOES_CONHECIDAS,
    StixDownloadError,
    StixFileError,
    StixLoadErrorType,
    StixService,
    _chave_versao_semantica,
)

# A single shared iteration budget so every property runs >= 100 examples.
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# Shared helpers / strategies
# ---------------------------------------------------------------------------


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a minimal-but-valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


# The six STIX types the dashboard must count, plus a couple of noise types that
# must never leak into the required counts.
_REQUIRED_TYPES = list(ROTULO_TIPO.keys())
_NOISE_TYPES = ["identity", "course-of-action", "tool", "x-mitre-matrix"]


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


# ---------------------------------------------------------------------------
# Task 1.3 — Property 1: bases ordered newest -> oldest.
# ---------------------------------------------------------------------------

# A strategy over non-empty subsets of the known versions, in arbitrary order,
# so the sort key is checked across many inputs rather than only the real list.
_versao_lists = st.lists(
    st.sampled_from(VERSOES_CONHECIDAS), min_size=1, max_size=len(VERSOES_CONHECIDAS)
).map(lambda xs: list(dict.fromkeys(xs)))  # dedupe, preserve order


# Feature: fluxo-stix-dashboard, Property 1: Bases sempre ordenadas da mais nova para a mais antiga
@PROP_SETTINGS
@given(versoes=_versao_lists)
def test_property1_bases_ordenadas_mais_nova_primeiro(
    tmp_path: Path, versoes: list[str]
) -> None:
    """The semantic sort key is monotonic (strictly) descending, and the real
    ``listar_bases()`` output is sorted newest->oldest by that same key."""
    # (a) The sort key applied to any generated subset yields a non-increasing
    #     sequence of numeric tuples when sorted descending.
    chaves = sorted(
        (_chave_versao_semantica(v) for v in versoes), reverse=True
    )
    for anterior, proximo in zip(chaves, chaves[1:]):
        assert anterior >= proximo

    # (b) The real listing (over an isolated empty data dir) is strictly
    #     descending by semantic version — no two known versions tie.
    service = StixService(data_dir=tmp_path)
    bases = service.listar_bases()
    assert len(bases) == len(VERSOES_CONHECIDAS)
    keys = [_chave_versao_semantica(b.versao) for b in bases]
    for anterior, proximo in zip(keys, keys[1:]):
        assert anterior > proximo, (
            f"esperava ordem estritamente decrescente, {anterior} !> {proximo}"
        )


# ---------------------------------------------------------------------------
# Task 1.4 — Property 2: baixada/selecionavel reflect disk presence exactly.
# ---------------------------------------------------------------------------


# Feature: fluxo-stix-dashboard, Property 2: "Baixada" e "selecionavel" refletem exatamente a presenca em disco
@PROP_SETTINGS
@given(
    presentes=st.lists(
        st.sampled_from(VERSOES_CONHECIDAS),
        max_size=len(VERSOES_CONHECIDAS),
    ).map(lambda xs: set(xs))
)
def test_property2_baixada_selecionavel_refletem_disco(
    tmp_path: Path, presentes: set[str]
) -> None:
    """For a generated subset of versions materialised as files in ``tmp_path``,
    each base's ``baixada`` equals the file's existence, ``selecionavel ==
    baixada`` and ``arquivo`` is set iff ``baixada``."""
    service = StixService(data_dir=tmp_path)

    # Hypothesis reuses the function-scoped tmp_path across examples, so clear
    # any base files left by a previous example before materialising this one.
    tmp_path.mkdir(parents=True, exist_ok=True)
    for stale in tmp_path.glob("enterprise-attack-*.json"):
        stale.unlink()

    # Materialise exactly the chosen subset as minimal valid bundles.
    for versao in presentes:
        _write_bundle(service.base_path(versao), [])

    bases = service.listar_bases()
    assert {b.versao for b in bases} == set(VERSOES_CONHECIDAS)

    for base in bases:
        esperado_baixada = base.versao in presentes
        assert base.baixada is esperado_baixada
        assert base.selecionavel == base.baixada
        if base.baixada:
            assert base.arquivo == str(service.base_path(base.versao))
        else:
            assert base.arquivo is None


# ---------------------------------------------------------------------------
# Tasks 2.3 / 2.4 — bundle strategy + dashboard count & description properties.
# ---------------------------------------------------------------------------


@st.composite
def _bundle_de_contagens(draw: st.DrawFn) -> tuple[list[dict], dict[str, int]]:
    """Generate a bundle with a known number of objects per required type (plus
    noise), returning both the objects and the oracle count per required type."""
    objetos: list[dict] = []
    oraculo: dict[str, int] = {}
    contador = 0

    for stix_type in _REQUIRED_TYPES:
        n = draw(st.integers(min_value=0, max_value=5))
        oraculo[stix_type] = n
        for _ in range(n):
            objetos.append({"type": stix_type, "id": _stix_id(stix_type, contador)})
            contador += 1

    # Noise objects of types that are not counted by the dashboard.
    for noise in _NOISE_TYPES:
        for _ in range(draw(st.integers(min_value=0, max_value=4))):
            objetos.append({"type": noise, "id": _stix_id(noise, contador)})
            contador += 1

    # Shuffle so ordering never influences the counts.
    objetos = draw(st.permutations(objetos))
    return list(objetos), oraculo


# Feature: fluxo-stix-dashboard, Property 3: Contagem por tipo igual ao numero de objetos daquele tipo no bundle
@PROP_SETTINGS
@given(dados=_bundle_de_contagens())
def test_property3_contagens_iguais_aos_objetos(
    tmp_path: Path, dados: tuple[list[dict], dict[str, int]]
) -> None:
    """Each ``ContagemTipo.quantidade`` equals the number of generated objects
    of that STIX type; noise types never inflate a required count."""
    objetos, oraculo = dados
    versao = VERSOES_CONHECIDAS[0]
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(versao), objetos)

    dashboard = service.dashboard(versao)
    por_tipo = {c.stix_type: c.quantidade for c in dashboard.contagens}

    # Every required type appears exactly once and matches the oracle.
    assert set(por_tipo) == set(_REQUIRED_TYPES)
    for stix_type, esperado in oraculo.items():
        assert por_tipo[stix_type] == esperado, (
            f"{stix_type}: esperava {esperado}, obteve {por_tipo[stix_type]}"
        )


# Feature: fluxo-stix-dashboard, Property 4: Todo tipo contabilizado tem descricao nao vazia
@PROP_SETTINGS
@given(dados=_bundle_de_contagens())
def test_property4_toda_descricao_nao_vazia(
    tmp_path: Path, dados: tuple[list[dict], dict[str, int]]
) -> None:
    """Every ``ContagemTipo`` in any produced dashboard has a non-empty pt-BR
    description drawn from DESCRICOES_TIPO."""
    objetos, _ = dados
    versao = VERSOES_CONHECIDAS[0]
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(versao), objetos)

    dashboard = service.dashboard(versao)
    assert dashboard.contagens, "o dashboard deve sempre trazer os tipos exigidos"
    for contagem in dashboard.contagens:
        assert contagem.descricao, f"descricao vazia para {contagem.stix_type}"
        assert contagem.descricao.strip() != ""
        assert contagem.descricao == DESCRICOES_TIPO[contagem.stix_type]


# ---------------------------------------------------------------------------
# Task 2.5 — example/edge: invalid bundles raise typed StixFileError.
# ---------------------------------------------------------------------------


def test_bundle_invalido_erros_tipados(tmp_path: Path) -> None:
    """Malformed JSON, valid-JSON-without-objects and a missing file each raise
    a typed StixFileError with the right error_type — for both ler_bundle and
    dashboard, which must never return partial counts."""
    service = StixService(data_dir=tmp_path)

    # (a) Malformed JSON -> MALFORMED_JSON.
    v_mal = VERSOES_CONHECIDAS[0]
    service.base_path(v_mal).parent.mkdir(parents=True, exist_ok=True)
    service.base_path(v_mal).write_text("{not valid json", encoding="utf-8")
    with pytest.raises(StixFileError) as exc_mal:
        service.ler_bundle(v_mal)
    assert exc_mal.value.error_type is StixLoadErrorType.MALFORMED_JSON
    with pytest.raises(StixFileError) as exc_mal_dash:
        service.dashboard(v_mal)
    assert exc_mal_dash.value.error_type is StixLoadErrorType.MALFORMED_JSON

    # (b) Valid JSON but no "objects" list -> INVALID_STRUCTURE.
    v_inv = VERSOES_CONHECIDAS[1]
    service.base_path(v_inv).write_text(
        json.dumps({"type": "bundle"}), encoding="utf-8"
    )
    with pytest.raises(StixFileError) as exc_inv:
        service.ler_bundle(v_inv)
    assert exc_inv.value.error_type is StixLoadErrorType.INVALID_STRUCTURE
    with pytest.raises(StixFileError) as exc_inv_dash:
        service.dashboard(v_inv)
    assert exc_inv_dash.value.error_type is StixLoadErrorType.INVALID_STRUCTURE

    # (c) Missing file -> MISSING_FILE.
    v_missing = VERSOES_CONHECIDAS[2]
    with pytest.raises(StixFileError) as exc_missing:
        service.ler_bundle(v_missing)
    assert exc_missing.value.error_type is StixLoadErrorType.MISSING_FILE
    with pytest.raises(StixFileError) as exc_missing_dash:
        service.dashboard(v_missing)
    assert exc_missing_dash.value.error_type is StixLoadErrorType.MISSING_FILE


# ---------------------------------------------------------------------------
# Task 3.3 — Property 5: listar_campanhas == campaign objects, in order.
# ---------------------------------------------------------------------------


@st.composite
def _bundle_com_campanhas(draw: st.DrawFn) -> tuple[list[dict], list[str]]:
    """Generate a bundle mixing campaign objects (tracked, in order) with noise
    objects. Returns the objects and the ordered list of campaign stix ids."""
    objetos: list[dict] = []
    ids_campanhas: list[str] = []
    n = draw(st.integers(min_value=0, max_value=8))
    contador = 0
    for i in range(n):
        cid = _stix_id("campaign", contador)
        contador += 1
        objetos.append(
            {"type": "campaign", "id": cid, "name": f"Campanha {i}"}
        )
        ids_campanhas.append(cid)

    # Interleave noise objects (never campaigns).
    for noise in _NOISE_TYPES + ["intrusion-set", "relationship"]:
        for _ in range(draw(st.integers(min_value=0, max_value=3))):
            objetos.append({"type": noise, "id": _stix_id(noise, contador)})
            contador += 1

    # Keep campaigns' relative order but shuffle positions among all objects by
    # a stable interleave: insert noise at random points while preserving the
    # campaign subsequence order.
    campanhas = [o for o in objetos if o["type"] == "campaign"]
    resto = [o for o in objetos if o["type"] != "campaign"]
    resultado: list[dict] = []
    ci = ri = 0
    while ci < len(campanhas) or ri < len(resto):
        escolher_campanha = draw(st.booleans())
        if ci < len(campanhas) and (escolher_campanha or ri >= len(resto)):
            resultado.append(campanhas[ci])
            ci += 1
        elif ri < len(resto):
            resultado.append(resto[ri])
            ri += 1
    return resultado, ids_campanhas


# Feature: fluxo-stix-dashboard, Property 5: Lista de campanhas igual aos objetos campaign do bundle
@PROP_SETTINGS
@given(dados=_bundle_com_campanhas())
def test_property5_lista_campanhas_igual_aos_objetos(
    tmp_path: Path, dados: tuple[list[dict], list[str]]
) -> None:
    """``listar_campanhas`` returns exactly the campaign-typed objects, in the
    same order, none lost or duplicated."""
    objetos, ids_esperados = dados
    versao = VERSOES_CONHECIDAS[0]
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(versao), objetos)

    campanhas = service.listar_campanhas(versao)
    ids_obtidos = [c.stix_id for c in campanhas]
    assert ids_obtidos == ids_esperados


# ---------------------------------------------------------------------------
# Task 3.4 — Property 6: actor -> campaigns grouping from attributed-to.
# ---------------------------------------------------------------------------


@st.composite
def _bundle_atribuicoes(draw: st.DrawFn) -> dict:
    """Generate intrusion-sets, campaigns and attributed-to relationships.

    Returns a dict with the generated ``objects`` and the independently-computed
    oracle mapping (actor id -> set of campaign ids, plus the unattributed set).
    """
    n_atores = draw(st.integers(min_value=0, max_value=4))
    n_campanhas = draw(st.integers(min_value=0, max_value=6))

    atores = [_stix_id("intrusion-set", i) for i in range(n_atores)]
    campanhas = [_stix_id("campaign", 100 + i) for i in range(n_campanhas)]

    objetos: list[dict] = []
    for i, aid in enumerate(atores):
        objetos.append({"type": "intrusion-set", "id": aid, "name": f"Ator {i}"})
    for i, cid in enumerate(campanhas):
        objetos.append({"type": "campaign", "id": cid, "name": f"Camp {i}"})

    # Oracle: campaign -> set of actors it is attributed to.
    atribuicao: dict[str, set[str]] = {aid: set() for aid in atores}
    atribuidas: set[str] = set()
    rel_contador = 0
    if atores and campanhas:
        n_rels = draw(st.integers(min_value=0, max_value=10))
        for _ in range(n_rels):
            cid = draw(st.sampled_from(campanhas))
            aid = draw(st.sampled_from(atores))
            objetos.append(
                {
                    "type": "relationship",
                    "id": _stix_id("relationship", 200 + rel_contador),
                    "relationship_type": "attributed-to",
                    "source_ref": cid,
                    "target_ref": aid,
                }
            )
            rel_contador += 1
            atribuicao[aid].add(cid)
            atribuidas.add(cid)

    nao_atribuidas = {c for c in campanhas if c not in atribuidas}
    objetos = list(draw(st.permutations(objetos)))
    return {
        "objects": objetos,
        "atores": atores,
        "atribuicao": atribuicao,
        "nao_atribuidas": nao_atribuidas,
    }


# Feature: fluxo-stix-dashboard, Property 6: Agrupamento Ator -> Campanhas reflete os relationships
@PROP_SETTINGS
@given(dados=_bundle_atribuicoes())
def test_property6_agrupamento_ator_campanhas(tmp_path: Path, dados: dict) -> None:
    """Each campaign appears under exactly the actors it is attributed to (and
    unattributed campaigns under the synthetic 'sem-ator' group), none lost or
    duplicated."""
    versao = VERSOES_CONHECIDAS[0]
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(versao), dados["objects"])

    atores = service.listar_atores(versao)
    obtido: dict[str, set[str]] = {}
    for ator in atores:
        ids = [c.stix_id for c in ator.campanhas]
        # No campaign duplicated under the same actor.
        assert len(ids) == len(set(ids)), f"campanha duplicada sob {ator.stix_id}"
        obtido[ator.stix_id] = set(ids)

    # Every real actor's campaign set equals the oracle.
    for aid, esperado in dados["atribuicao"].items():
        assert obtido.get(aid, set()) == esperado

    # Unattributed campaigns live under the synthetic group iff any exist.
    sem_ator = service.ATOR_SEM_ASSOCIACAO_ID
    if dados["nao_atribuidas"]:
        assert obtido.get(sem_ator, set()) == dados["nao_atribuidas"]
    else:
        assert sem_ator not in obtido


# ---------------------------------------------------------------------------
# Task 3.5 — Property 7: curada flag matches the curated mapping exactly.
# ---------------------------------------------------------------------------

# Known curated (slug, campaign spec) samples: by external_id and by name.
_CURADAS_POR_ID = [
    (slug, mapa["referencias"][0])
    for slug, mapa in SLUG_PARA_STIX.items()
    if mapa.get("referencias")
]
_CURADAS_POR_NOME = [
    (slug, mapa["nome_canonico"]) for slug, mapa in SLUG_PARA_STIX.items()
]

# External ids / names that are guaranteed NOT to match any curated case.
_NAO_CURADAS_IDS = ["C9999", "C0000", "T1055", "G0001"]
_NAO_CURADAS_NOMES = ["Campanha Qualquer", "Operation Nowhere", "Zzz Unknown"]


@st.composite
def _campanha_curada_ou_nao(draw: st.DrawFn) -> tuple[dict, str | None]:
    """Generate one campaign object plus the oracle slug (or None if not curated)."""
    modo = draw(st.sampled_from(["id", "nome", "nao_id", "nao_nome"]))
    contador = draw(st.integers(min_value=0, max_value=10_000))
    cid = _stix_id("campaign", contador)

    if modo == "id":
        slug, external_id = draw(st.sampled_from(_CURADAS_POR_ID))
        # Vary casing to exercise case-insensitive matching.
        if draw(st.booleans()):
            external_id = external_id.lower()
        obj = {
            "type": "campaign",
            "id": cid,
            "name": draw(st.sampled_from(_NAO_CURADAS_NOMES)),
            "external_references": [{"source_name": "mitre", "external_id": external_id}],
        }
        return obj, slug

    if modo == "nome":
        slug, nome = draw(st.sampled_from(_CURADAS_POR_NOME))
        obj = {"type": "campaign", "id": cid, "name": nome}
        return obj, slug

    if modo == "nao_id":
        obj = {
            "type": "campaign",
            "id": cid,
            "name": draw(st.sampled_from(_NAO_CURADAS_NOMES)),
            "external_references": [
                {"source_name": "mitre", "external_id": draw(st.sampled_from(_NAO_CURADAS_IDS))}
            ],
        }
        return obj, None

    # nao_nome
    obj = {
        "type": "campaign",
        "id": cid,
        "name": draw(st.sampled_from(_NAO_CURADAS_NOMES)),
    }
    return obj, None


# Feature: fluxo-stix-dashboard, Property 7: Flag "curada" corresponde exatamente ao mapeamento de casos curados
@PROP_SETTINGS
@given(itens=st.lists(_campanha_curada_ou_nao(), min_size=1, max_size=8))
def test_property7_flag_curada_corresponde_ao_mapeamento(
    tmp_path: Path, itens: list[tuple[dict, str | None]]
) -> None:
    """``curada``/``slug_curado`` is set iff the campaign matches the curated
    mapping (by external_id or canonical name); otherwise curada is False and
    slug_curado is None."""
    objetos = [obj for obj, _ in itens]
    oraculos = [slug for _, slug in itens]
    versao = VERSOES_CONHECIDAS[0]
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(versao), objetos)

    campanhas = service.listar_campanhas(versao)
    assert len(campanhas) == len(itens)
    for campanha, slug_esperado in zip(campanhas, oraculos):
        if slug_esperado is None:
            assert campanha.curada is False
            assert campanha.slug_curado is None
        else:
            assert campanha.curada is True
            assert campanha.slug_curado == slug_esperado


# ---------------------------------------------------------------------------
# Task 4.2 — example/edge: download via MOCKED http_client (no real network).
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for an HTTP response: ``.text`` + ``.raise_for_status``."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if not (200 <= self.status_code < 300):
            raise RuntimeError(f"HTTP {self.status_code}")


class _OkClient:
    """Fake client whose ``get`` returns a successful bundle response.

    Records the URL and timeout it was called with so the test can assert the
    versioned URL and the finite timeout.
    """

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[tuple[str, object]] = []

    def get(self, url: str, timeout=None):  # noqa: ANN001 - test double
        self.calls.append((url, timeout))
        return _FakeResponse(self._body, status_code=200)


class _RaisingClient:
    """Fake client whose ``get`` always raises (network failure)."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, timeout=None):  # noqa: ANN001 - test double
        self.calls += 1
        raise RuntimeError("conexao recusada (simulado)")


class _HttpErrorClient:
    """Fake client returning a non-2xx response (raise_for_status fails)."""

    def get(self, url: str, timeout=None):  # noqa: ANN001 - test double
        return _FakeResponse("not found", status_code=404)


def test_download_sucesso_escreve_atomico(tmp_path: Path) -> None:
    """A mocked client returning a valid bundle: ``baixar_base`` writes the file
    into tmp_path and returns baixada=True with the versioned URL requested."""
    versao = VERSOES_CONHECIDAS[0]
    bundle = json.dumps({"type": "bundle", "objects": [{"type": "campaign", "id": "x"}]})
    client = _OkClient(bundle)
    service = StixService(data_dir=tmp_path, http_client=client)

    info = service.baixar_base(versao)

    assert info.baixada is True
    assert info.selecionavel is True
    destino = service.base_path(versao)
    assert destino.exists()
    assert info.arquivo == str(destino)
    # Content persisted is the downloaded bundle, re-readable by ler_bundle.
    lido = service.ler_bundle(versao)
    assert lido["objects"] == [{"type": "campaign", "id": "x"}]
    # The client was called once with the versioned filename and a finite timeout.
    assert len(client.calls) == 1
    url, timeout = client.calls[0]
    assert url.endswith(f"enterprise-attack-{versao}.json")
    assert isinstance(timeout, (int, float)) and timeout > 0
    # No leftover temp files in the data dir.
    restos = [p.name for p in tmp_path.iterdir() if p.name != destino.name]
    assert restos == [], f"nenhum arquivo temporario deve sobrar: {restos}"


def test_download_falha_rede_sem_arquivo_parcial(tmp_path: Path) -> None:
    """A failing client (raises) -> StixDownloadError and NO file left behind."""
    versao = VERSOES_CONHECIDAS[0]
    client = _RaisingClient()
    service = StixService(data_dir=tmp_path, http_client=client)

    with pytest.raises(StixDownloadError):
        service.baixar_base(versao)
    assert client.calls == 1
    assert not service.base_path(versao).exists()
    # Directory holds no partial/temp artifacts.
    if tmp_path.exists():
        assert list(tmp_path.iterdir()) == []


def test_download_status_nao_ok_sem_arquivo(tmp_path: Path) -> None:
    """A non-2xx response -> StixDownloadError and no file written."""
    versao = VERSOES_CONHECIDAS[0]
    service = StixService(data_dir=tmp_path, http_client=_HttpErrorClient())

    with pytest.raises(StixDownloadError):
        service.baixar_base(versao)
    assert not service.base_path(versao).exists()


def test_download_versao_desconhecida_sem_disco_nem_rede(tmp_path: Path) -> None:
    """An unknown version is rejected before any disk/network access."""
    client = _RaisingClient()
    service = StixService(data_dir=tmp_path, http_client=client)

    with pytest.raises(StixDownloadError):
        service.baixar_base("999.999")
    # The client was never contacted and nothing was written.
    assert client.calls == 0
    if tmp_path.exists():
        assert list(tmp_path.iterdir()) == []
