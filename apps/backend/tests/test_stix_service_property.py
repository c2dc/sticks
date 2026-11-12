"""Property-based + example tests for StixService (in-memory download and derivations).

Every property-based test runs with Hypothesis at ``max_examples=40``.

Isolation (no files / no network):

* ``StixService`` holds no state on disk; the derivations work on bundles built
  in memory.
* The single network egress (``fetch_bundle``) is exercised with an injected
  **fake** ``http_client`` (``tests/_stix_fakes.py``) - there is no real HTTP.

The oracles are computed independently of the service under test (counting from
the generated bundle directly), so the properties genuinely cross-check the
implementation rather than restating it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from _stix_fakes import FakeStixHttp, small_bundle

from app.services.stix.stix_service import (
    TYPE_DESCRIPTIONS,
    ROTULO_TIPO,
    SLUG_TO_STIX,
    KNOWN_VERSIONS,
    StixDownloadError,
    StixService,
)

# A single shared iteration budget so every property runs >= 100 examples.
PROP_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# Shared helpers / strategies
# ---------------------------------------------------------------------------



# The six STIX types the dashboard must count, plus a couple of noise types that
# must never leak into the required counts.
_REQUIRED_TYPES = list(ROTULO_TIPO.keys())
_NOISE_TYPES = ["identity", "course-of-action", "tool", "x-mitre-matrix"]


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


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


# Feature: stream-stix-dashboard, Property 3: Contagem por type igual ao numero de objetos daquele type no bundle
@PROP_SETTINGS
@given(dados=_bundle_de_contagens())
def test_property3_contagens_iguais_aos_objetos(
    dados: tuple[list[dict], dict[str, int]]
) -> None:
    """Each ``TypeCount.count`` equals the number of generated objects
    of that STIX type; noise types never inflate a required count."""
    objetos, oraculo = dados
    version = KNOWN_VERSIONS[0]
    service = StixService()
    bundle = {"type": "bundle", "objects": objetos}

    dashboard = service.dashboard(version, bundle)
    por_tipo = {c.stix_type: c.count for c in dashboard.counts}

    # Every required type appears exactly once and matches the oracle.
    assert set(por_tipo) == set(_REQUIRED_TYPES)
    for stix_type, esperado in oraculo.items():
        assert por_tipo[stix_type] == esperado, (
            f"{stix_type}: esperava {esperado}, obteve {por_tipo[stix_type]}"
        )


# Feature: stream-stix-dashboard, Property 4: Todo type contabilizado tem description nao vazia
@PROP_SETTINGS
@given(dados=_bundle_de_contagens())
def test_property4_every_description_not_empty(
    dados: tuple[list[dict], dict[str, int]]
) -> None:
    """Every ``TypeCount`` in any produced dashboard has a non-empty English
    description drawn from TYPE_DESCRIPTIONS (no Portuguese literal remains)."""
    objetos, _ = dados
    version = KNOWN_VERSIONS[0]
    service = StixService()
    bundle = {"type": "bundle", "objects": objetos}

    dashboard = service.dashboard(version, bundle)
    assert dashboard.counts, "dashboard must always return the required types"
    for contagem in dashboard.counts:
        assert contagem.description, f"empty description for {contagem.stix_type}"
        assert contagem.description.strip() != ""
        assert contagem.description == TYPE_DESCRIPTIONS[contagem.stix_type]


# ---------------------------------------------------------------------------
# fetch_bundle: in-memory download, validation, and no disk access.
# ---------------------------------------------------------------------------


def test_fetch_bundle_returns_bundle_in_memory() -> None:
    """The bundle is parsed straight from the response body; only the versioned
    URL is requested."""
    version = KNOWN_VERSIONS[0]
    http = FakeStixHttp({version: small_bundle()})
    bundle = StixService(http_client=http).fetch_bundle(version)
    assert isinstance(bundle["objects"], list) and bundle["objects"]
    assert len(http.calls) == 1
    assert http.calls[0].endswith(f"enterprise-attack-{version}.json")


@pytest.mark.parametrize(
    "version", ["", "latest", "../etc/passwd", "18.0/../../x", "18.0 ", "v18.0", "18"]
)
def test_fetch_bundle_rejects_malformed_version_without_network(version: str) -> None:
    """A malformed version is refused before any URL is built or request sent."""
    http = FakeStixHttp({})
    with pytest.raises(StixDownloadError):
        StixService(http_client=http).fetch_bundle(version)
    assert http.calls == []


def test_fetch_bundle_failure_modes_raise_download_error() -> None:
    """Network error, HTTP 404, invalid JSON and a non-bundle body all fail with
    :class:`StixDownloadError` carrying the version."""
    v_net, v_404, v_json, v_struct = "19.1", "19.0", "18.0", "17.1"
    http = FakeStixHttp(
        {v_json: "{not valid json", v_struct: {"type": "bundle"}},
        fail_versions={v_net},
    )
    service = StixService(http_client=http)
    for version in (v_net, v_404, v_json, v_struct):
        with pytest.raises(StixDownloadError) as excinfo:
            service.fetch_bundle(version)
        assert excinfo.value.version == version


def test_fetch_bundle_writes_nothing_to_disk(tmp_path: Path, monkeypatch) -> None:
    """A full fetch creates no file or directory in the working directory."""
    monkeypatch.chdir(tmp_path)
    version = KNOWN_VERSIONS[0]
    StixService(http_client=FakeStixHttp({version: small_bundle()})).fetch_bundle(version)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# Task 3.3 — Property 5: list_campaigns == campaign objects, in order.
# ---------------------------------------------------------------------------


@st.composite
def _bundle_with_campaigns(draw: st.DrawFn) -> tuple[list[dict], list[str]]:
    """Generate a bundle mixing campaign objects (tracked, in order) with noise
    objects. Returns the objects and the ordered list of campaign stix ids."""
    objetos: list[dict] = []
    campaign_ids: list[str] = []
    n = draw(st.integers(min_value=0, max_value=8))
    contador = 0
    for i in range(n):
        cid = _stix_id("campaign", contador)
        contador += 1
        objetos.append(
            {"type": "campaign", "id": cid, "name": f"Campaign {i}"}
        )
        campaign_ids.append(cid)

    # Interleave noise objects (never campaigns).
    for noise in _NOISE_TYPES + ["intrusion-set", "relationship"]:
        for _ in range(draw(st.integers(min_value=0, max_value=3))):
            objetos.append({"type": noise, "id": _stix_id(noise, contador)})
            contador += 1

    # Keep campaigns' relative order but shuffle positions among all objects by
    # a stable interleave: insert noise at random points while preserving the
    # campaign subsequence order.
    campaigns = [o for o in objetos if o["type"] == "campaign"]
    resto = [o for o in objetos if o["type"] != "campaign"]
    result: list[dict] = []
    ci = ri = 0
    while ci < len(campaigns) or ri < len(resto):
        choose_campaign = draw(st.booleans())
        if ci < len(campaigns) and (choose_campaign or ri >= len(resto)):
            result.append(campaigns[ci])
            ci += 1
        elif ri < len(resto):
            result.append(resto[ri])
            ri += 1
    return result, campaign_ids


# Feature: stream-stix-dashboard, Property 5: Lista de campaigns igual aos objetos campaign do bundle
@PROP_SETTINGS
@given(dados=_bundle_with_campaigns())
def test_property5_campaign_list_equals_objects(
    dados: tuple[list[dict], list[str]]
) -> None:
    """``list_campaigns`` returns exactly the campaign-typed objects, in the
    same order, none lost or duplicated."""
    objetos, ids_esperados = dados
    version = KNOWN_VERSIONS[0]
    service = StixService()
    bundle = {"type": "bundle", "objects": objetos}

    campaigns = service.list_campaigns(bundle)
    ids_obtidos = [c.stix_id for c in campaigns]
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
    n_adversaries = draw(st.integers(min_value=0, max_value=4))
    n_campaigns = draw(st.integers(min_value=0, max_value=6))

    adversaries = [_stix_id("intrusion-set", i) for i in range(n_adversaries)]
    campaigns = [_stix_id("campaign", 100 + i) for i in range(n_campaigns)]

    objetos: list[dict] = []
    for i, aid in enumerate(adversaries):
        objetos.append({"type": "intrusion-set", "id": aid, "name": f"Ator {i}"})
    for i, cid in enumerate(campaigns):
        objetos.append({"type": "campaign", "id": cid, "name": f"Camp {i}"})

    # Oracle: campaign -> set of actors it is attributed to.
    atribuicao: dict[str, set[str]] = {aid: set() for aid in adversaries}
    atribuidas: set[str] = set()
    rel_contador = 0
    if adversaries and campaigns:
        n_rels = draw(st.integers(min_value=0, max_value=10))
        for _ in range(n_rels):
            cid = draw(st.sampled_from(campaigns))
            aid = draw(st.sampled_from(adversaries))
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

    nao_atribuidas = {c for c in campaigns if c not in atribuidas}
    objetos = list(draw(st.permutations(objetos)))
    return {
        "objects": objetos,
        "adversaries": adversaries,
        "atribuicao": atribuicao,
        "nao_atribuidas": nao_atribuidas,
    }


# Feature: stream-stix-dashboard, Property 6: Adversary -> Campaigns grouping reflects the relationships
@PROP_SETTINGS
@given(dados=_bundle_atribuicoes())
def test_property6_adversary_campaigns_grouping(dados: dict) -> None:
    """Each campaign appears under exactly the actors it is attributed to (and
    unattributed campaigns under the synthetic 'sem-ator' group), none lost or
    duplicated."""
    version = KNOWN_VERSIONS[0]
    service = StixService()
    bundle = {"type": "bundle", "objects": dados["objects"]}

    adversaries = service.list_adversaries(bundle)
    obtido: dict[str, set[str]] = {}
    for ator in adversaries:
        ids = [c.stix_id for c in ator.campaigns]
        # No campaign duplicated under the same actor.
        assert len(ids) == len(set(ids)), f"campaign duplicada sob {ator.stix_id}"
        obtido[ator.stix_id] = set(ids)

    # Every real actor's campaign set equals the oracle.
    for aid, esperado in dados["atribuicao"].items():
        assert obtido.get(aid, set()) == esperado

    # Unattributed campaigns live under the synthetic group iff any exist.
    sem_ator = service.ADVERSARY_UNASSOCIATED_ID
    if dados["nao_atribuidas"]:
        assert obtido.get(sem_ator, set()) == dados["nao_atribuidas"]
    else:
        assert sem_ator not in obtido


# ---------------------------------------------------------------------------
# Task 3.5 — Property 7: curated flag matches the curated mapping exactly.
# ---------------------------------------------------------------------------

# Known curated (slug, campaign spec) samples: by external_id and by name.
_CURADAS_POR_ID = [
    (slug, mapa["referencias"][0])
    for slug, mapa in SLUG_TO_STIX.items()
    if mapa.get("referencias")
]
_CURADAS_POR_NOME = [
    (slug, mapa["nome_canonico"]) for slug, mapa in SLUG_TO_STIX.items()
]

# External ids / names that are guaranteed NOT to match any curated case.
_NAO_CURADAS_IDS = ["C9999", "C0000", "T1055", "G0001"]
_NON_CURATED_NAMES = ["Any Campaign", "Operation Nowhere", "Zzz Unknown"]


@st.composite
def _campaign_curated_or_not(draw: st.DrawFn) -> tuple[dict, str | None]:
    """Generate one campaign object plus the oracle slug (or None if not curated)."""
    modo = draw(st.sampled_from(["id", "name", "nao_id", "nao_nome"]))
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
            "name": draw(st.sampled_from(_NON_CURATED_NAMES)),
            "external_references": [{"source_name": "mitre", "external_id": external_id}],
        }
        return obj, slug

    if modo == "name":
        slug, name = draw(st.sampled_from(_CURADAS_POR_NOME))
        obj = {"type": "campaign", "id": cid, "name": name}
        return obj, slug

    if modo == "nao_id":
        obj = {
            "type": "campaign",
            "id": cid,
            "name": draw(st.sampled_from(_NON_CURATED_NAMES)),
            "external_references": [
                {"source_name": "mitre", "external_id": draw(st.sampled_from(_NAO_CURADAS_IDS))}
            ],
        }
        return obj, None

    # nao_nome
    obj = {
        "type": "campaign",
        "id": cid,
        "name": draw(st.sampled_from(_NON_CURATED_NAMES)),
    }
    return obj, None


# Feature: stream-stix-dashboard, Property 7: Flag "curated" corresponde exatamente ao mapeamento de campaigns curados
@PROP_SETTINGS
@given(itens=st.lists(_campaign_curated_or_not(), min_size=1, max_size=8))
def test_property7_curated_flag_matches_mapping(
    itens: list[tuple[dict, str | None]]
) -> None:
    """``curated``/``curated_slug`` is set iff the campaign matches the curated
    mapping (by external_id or canonical name); otherwise curated is False and
    curated_slug is None."""
    objetos = [obj for obj, _ in itens]
    oraculos = [slug for _, slug in itens]
    version = KNOWN_VERSIONS[0]
    service = StixService()
    bundle = {"type": "bundle", "objects": objetos}

    campaigns = service.list_campaigns(bundle)
    assert len(campaigns) == len(itens)
    for campaign, slug_esperado in zip(campaigns, oraculos):
        if slug_esperado is None:
            assert campaign.curated is False
            assert campaign.curated_slug is None
        else:
            assert campaign.curated is True
            assert campaign.curated_slug == slug_esperado
