"""Property-based + example tests for ``StixService.obter_campanha_por_slug``
(spec entrada-estagio1-campanha-stix, task 1.2).

Covers the resolution of a curated slug to the rich STIX ``campaign`` detail
exposed in the Stage 1 Entrada:

* resolution by ``external_id`` (e.g. ``C0045`` → ``shadowray``) and by
  canonical name;
* extraction of ``aliases`` / ``first_seen`` / ``last_seen`` / ``descricao``
  when present (and their omission when absent);
* ``None`` when no campaign in the base resolves to the slug;
* **Property 2** — the resolution is consistent with
  ``listar_campanhas`` / ``_mapear_campanha`` (the **same** ``stix_id``), so the
  Entrada never diverges from the campaigns screen.

Isolation: every test instantiates ``StixService(data_dir=tmp_path)`` so the
service reads only inside pytest/Hypothesis temp directories — the real
``sticks/data/stix`` is never touched and there is no network egress.

The oracles are computed independently of the method under test (from the
generated bundle and from ``SLUG_PARA_STIX`` directly), so the properties
genuinely cross-check the implementation rather than restating it.
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.stix.stix_service import (
    SLUG_PARA_STIX,
    VERSOES_CONHECIDAS,
    CampanhaStixDetalhada,
    StixService,
)

# A single shared iteration budget so every property runs >= 100 examples.
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

_VERSAO = VERSOES_CONHECIDAS[0]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a minimal-but-valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


# Curated samples that have an external_id reference, and all curated samples by
# canonical name (every slug has a nome_canonico).
_CURADAS_POR_ID = [
    (slug, mapa["referencias"][0])
    for slug, mapa in SLUG_PARA_STIX.items()
    if mapa.get("referencias")
]
_CURADAS_POR_NOME = [
    (slug, mapa["nome_canonico"]) for slug, mapa in SLUG_PARA_STIX.items()
]


# ---------------------------------------------------------------------------
# Example tests — resolution by external_id and by canonical name.
# ---------------------------------------------------------------------------


def test_resolve_por_external_id_c0045_shadowray(tmp_path: Path) -> None:
    """A campaign carrying external_id ``C0045`` resolves to the ``shadowray``
    slug and the rich detail reflects the campaign object's fields."""
    service = StixService(data_dir=tmp_path)
    campanha = {
        "type": "campaign",
        "id": "campaign--c0045-0000-0000-0000-000000000000",
        "name": "ShadowRay",
        "description": "ataques contra clusters Ray expostos.",
        "aliases": ["ShadowRay", "Ray cluster abuse"],
        "first_seen": "2023-09-05T00:00:00.000Z",
        "last_seen": "2024-03-01T00:00:00.000Z",
        "external_references": [
            {"source_name": "mitre-attack", "external_id": "C0045"}
        ],
    }
    _write_bundle(service.base_path(_VERSAO), [campanha])

    detalhe = service.obter_campanha_por_slug(_VERSAO, "shadowray")

    assert isinstance(detalhe, CampanhaStixDetalhada)
    assert detalhe.stix_id == campanha["id"]
    assert detalhe.nome == "ShadowRay"
    assert detalhe.external_id == "C0045"
    assert detalhe.descricao == campanha["description"]
    assert detalhe.aliases == ["ShadowRay", "Ray cluster abuse"]
    assert detalhe.first_seen == "2023-09-05T00:00:00.000Z"
    assert detalhe.last_seen == "2024-03-01T00:00:00.000Z"


def test_resolve_por_nome_canonico(tmp_path: Path) -> None:
    """A campaign with no external_id but the canonical name resolves by name.

    ``CostaRicto`` has no external_id in the object, yet its canonical name
    matches the curated mapping, so the slug resolves."""
    service = StixService(data_dir=tmp_path)
    campanha = {
        "type": "campaign",
        "id": _stix_id("campaign", 7),
        "name": "CostaRicto",
    }
    _write_bundle(service.base_path(_VERSAO), [campanha])

    detalhe = service.obter_campanha_por_slug(_VERSAO, "costaricto")

    assert isinstance(detalhe, CampanhaStixDetalhada)
    assert detalhe.stix_id == campanha["id"]
    assert detalhe.nome == "CostaRicto"
    # Fields absent in the object are omitted (None / empty).
    assert detalhe.external_id is None
    assert detalhe.descricao is None
    assert detalhe.aliases == []
    assert detalhe.first_seen is None
    assert detalhe.last_seen is None


def test_nome_canonico_case_insensitive(tmp_path: Path) -> None:
    """Canonical-name resolution is case-insensitive (reuses the shared rule)."""
    service = StixService(data_dir=tmp_path)
    campanha = {
        "type": "campaign",
        "id": _stix_id("campaign", 11),
        "name": "shadowray",  # lower-case variant of "ShadowRay"
    }
    _write_bundle(service.base_path(_VERSAO), [campanha])

    detalhe = service.obter_campanha_por_slug(_VERSAO, "shadowray")

    assert detalhe is not None
    assert detalhe.stix_id == campanha["id"]


def test_retorna_none_quando_ausente(tmp_path: Path) -> None:
    """No campaign resolving to the slug -> ``None`` (never a partial object)."""
    service = StixService(data_dir=tmp_path)
    # A bundle with a non-curated campaign and some noise, but nothing that maps
    # to "shadowray".
    _write_bundle(
        service.base_path(_VERSAO),
        [
            {
                "type": "campaign",
                "id": _stix_id("campaign", 1),
                "name": "Campanha Qualquer",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C9999"}
                ],
            },
            {"type": "intrusion-set", "id": _stix_id("intrusion-set", 2)},
            {"type": "attack-pattern", "id": _stix_id("attack-pattern", 3)},
        ],
    )

    assert service.obter_campanha_por_slug(_VERSAO, "shadowray") is None


def test_retorna_none_em_bundle_sem_campanhas(tmp_path: Path) -> None:
    """An empty bundle resolves to ``None`` for any slug."""
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(_VERSAO), [])

    for slug in SLUG_PARA_STIX:
        assert service.obter_campanha_por_slug(_VERSAO, slug) is None


def test_extrai_campos_ricos_quando_presentes(tmp_path: Path) -> None:
    """All rich fields are extracted when present; the first usable external_id
    wins when several references exist."""
    service = StixService(data_dir=tmp_path)
    campanha = {
        "type": "campaign",
        "id": _stix_id("campaign", 42),
        "name": "C0010",
        "description": "intrusão de longa duração.",
        "aliases": ["C0010", "", "Alias válido"],  # empties filtered out
        "first_seen": "2020-01-01T00:00:00.000Z",
        "last_seen": "2021-01-01T00:00:00.000Z",
        "external_references": [
            {"source_name": "capec"},  # no external_id -> skipped
            {"source_name": "mitre-attack", "external_id": "C0010"},
            {"source_name": "other", "external_id": "C0099"},
        ],
    }
    _write_bundle(service.base_path(_VERSAO), [campanha])

    detalhe = service.obter_campanha_por_slug(_VERSAO, "c0010")

    assert detalhe is not None
    assert detalhe.external_id == "C0010"  # first usable one
    assert detalhe.descricao == "intrusão de longa duração."
    assert detalhe.aliases == ["C0010", "Alias válido"]  # empty string dropped
    assert detalhe.first_seen == "2020-01-01T00:00:00.000Z"
    assert detalhe.last_seen == "2021-01-01T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Property 2 — resolution consistent with listar_campanhas / _mapear_campanha.
# ---------------------------------------------------------------------------


@st.composite
def _bundle_com_slug_curado(draw: st.DrawFn) -> tuple[list[dict], str, str]:
    """Generate a bundle that contains exactly one campaign resolving to a drawn
    curated slug (by external_id or canonical name), interleaved with noise and
    non-curated campaigns. Returns the objects, the target slug and the expected
    stix_id of the resolving campaign."""
    por_id = draw(st.booleans()) and bool(_CURADAS_POR_ID)
    if por_id:
        slug, external_id = draw(st.sampled_from(_CURADAS_POR_ID))
        # Vary casing to exercise case-insensitive matching.
        if draw(st.booleans()):
            external_id = external_id.lower()
        alvo = {
            "type": "campaign",
            "id": _stix_id("campaign", draw(st.integers(0, 9_999))),
            "name": "Nome Qualquer",
            "external_references": [
                {"source_name": "mitre-attack", "external_id": external_id}
            ],
        }
    else:
        slug, nome = draw(st.sampled_from(_CURADAS_POR_NOME))
        alvo = {
            "type": "campaign",
            "id": _stix_id("campaign", draw(st.integers(10_000, 19_999))),
            "name": nome,
        }
    alvo_id = alvo["id"]

    # Non-curated campaigns + noise objects, none of which resolves to a slug.
    ruido: list[dict] = [
        {
            "type": "campaign",
            "id": _stix_id("campaign", draw(st.integers(20_000, 29_999)) + i),
            "name": f"Campanha Nao Curada {i}",
            "external_references": [
                {"source_name": "mitre-attack", "external_id": f"C8{i:03d}"}
            ],
        }
        for i in range(draw(st.integers(0, 3)))
    ]
    ruido += [
        {"type": "intrusion-set", "id": _stix_id("intrusion-set", 50_000 + i)}
        for i in range(draw(st.integers(0, 2)))
    ]

    objetos = draw(st.permutations([alvo] + ruido))
    return list(objetos), slug, alvo_id


# Feature: entrada-estagio1-campanha-stix, Property 2: Resolução consistente com o endpoint de campanhas
@PROP_SETTINGS
@given(dados=_bundle_com_slug_curado())
def test_property2_resolucao_consistente_com_listar_campanhas(
    tmp_path: Path, dados: tuple[list[dict], str, str]
) -> None:
    """For a curated slug present in a base, ``obter_campanha_por_slug`` resolves
    to the **same** campaign (same ``stix_id``) that ``listar_campanhas`` /
    ``_mapear_campanha`` flags for that slug — the resolution rule is single and
    does not diverge between the Entrada and the campaigns screen."""
    objetos, slug, alvo_id = dados
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(_VERSAO), objetos)

    detalhe = service.obter_campanha_por_slug(_VERSAO, slug)

    # Independent oracle: the stix_id of the FIRST campaign that listar_campanhas
    # flags as curated with this slug (same bundle order as obter_campanha_por_slug).
    campanhas = service.listar_campanhas(_VERSAO)
    esperado = next(
        (c.stix_id for c in campanhas if c.curada and c.slug_curado == slug),
        None,
    )

    assert esperado is not None, "a campanha curada gerada deve ser listada"
    assert esperado == alvo_id
    assert detalhe is not None, "a Entrada deve resolver o mesmo slug curado"
    # Property 2: same campaign (same stix_id) in both views.
    assert detalhe.stix_id == esperado


# Feature: entrada-estagio1-campanha-stix, Property 2: slug ausente resolve para None nas duas visões
@PROP_SETTINGS
@given(
    slug=st.sampled_from(sorted(SLUG_PARA_STIX)),
    ruido=st.lists(
        st.sampled_from(["intrusion-set", "attack-pattern", "malware", "relationship"]),
        max_size=5,
    ),
)
def test_property2_slug_ausente_none_em_ambas_visoes(
    tmp_path: Path, slug: str, ruido: list[str]
) -> None:
    """When no campaign resolves to a slug, ``obter_campanha_por_slug`` returns
    ``None`` and ``listar_campanhas`` likewise flags no campaign with that slug —
    the two views stay consistent on absence too."""
    service = StixService(data_dir=tmp_path)
    objetos = [
        {"type": tipo, "id": _stix_id(tipo, i)} for i, tipo in enumerate(ruido)
    ]
    _write_bundle(service.base_path(_VERSAO), objetos)

    detalhe = service.obter_campanha_por_slug(_VERSAO, slug)
    campanhas = service.listar_campanhas(_VERSAO)

    assert detalhe is None
    assert all(not (c.curada and c.slug_curado == slug) for c in campanhas)
