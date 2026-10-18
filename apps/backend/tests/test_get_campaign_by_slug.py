"""Property-based + example tests for ``StixService.get_campaign_by_slug``
(spec input-estagio1-campaign-stix, task 1.2).

Covers the resolution of a curated slug to the rich STIX ``campaign`` detail
exposed in the Stage 1 Entrada:

* resolution by ``external_id`` (e.g. ``C0045`` → ``shadowray``) and by
  canonical name;
* extraction of ``aliases`` / ``first_seen`` / ``last_seen`` / ``description``
  when present (and their omission when absent);
* ``None`` when no campaign in the base resolves to the slug;
* **Property 2** — the resolution is consistent with
  ``list_campaigns`` / ``_map_campaign`` (the **same** ``stix_id``), so the
  Entrada never diverges from the campaigns screen.

Isolation: every test instantiates ``StixService(data_dir=tmp_path)`` so the
service reads only inside pytest/Hypothesis temp directories — the real
``sticks/data/stix`` is never touched and there is no network egress.

The oracles are computed independently of the method under test (from the
generated bundle and from ``SLUG_TO_STIX`` directly), so the properties
genuinely cross-check the implementation rather than restating it.
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.stix.stix_service import (
    SLUG_TO_STIX,
    KNOWN_VERSIONS,
    DetailedCampaignStix,
    StixService,
)

# A single shared iteration budget so every property runs >= 100 examples.
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

_VERSION = KNOWN_VERSIONS[0]


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
    for slug, mapa in SLUG_TO_STIX.items()
    if mapa.get("referencias")
]
_CURADAS_POR_NOME = [
    (slug, mapa["nome_canonico"]) for slug, mapa in SLUG_TO_STIX.items()
]


# ---------------------------------------------------------------------------
# Example tests — resolution by external_id and by canonical name.
# ---------------------------------------------------------------------------


def test_resolve_por_external_id_c0045_shadowray(tmp_path: Path) -> None:
    """A campaign carrying external_id ``C0045`` resolves to the ``shadowray``
    slug and the rich detail reflects the campaign object's fields."""
    service = StixService(data_dir=tmp_path)
    campaign = {
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
    _write_bundle(service.base_path(_VERSION), [campaign])

    detail = service.get_campaign_by_slug(_VERSION, "shadowray")

    assert isinstance(detail, DetailedCampaignStix)
    assert detail.stix_id == campaign["id"]
    assert detail.name == "ShadowRay"
    assert detail.external_id == "C0045"
    assert detail.description == campaign["description"]
    assert detail.aliases == ["ShadowRay", "Ray cluster abuse"]
    assert detail.first_seen == "2023-09-05T00:00:00.000Z"
    assert detail.last_seen == "2024-03-01T00:00:00.000Z"


def test_resolve_por_nome_canonico(tmp_path: Path) -> None:
    """A campaign with no external_id but the canonical name resolves by name.

    ``CostaRicto`` has no external_id in the object, yet its canonical name
    matches the curated mapping, so the slug resolves."""
    service = StixService(data_dir=tmp_path)
    campaign = {
        "type": "campaign",
        "id": _stix_id("campaign", 7),
        "name": "CostaRicto",
    }
    _write_bundle(service.base_path(_VERSION), [campaign])

    detail = service.get_campaign_by_slug(_VERSION, "costaricto")

    assert isinstance(detail, DetailedCampaignStix)
    assert detail.stix_id == campaign["id"]
    assert detail.name == "CostaRicto"
    # Fields absent in the object are omitted (None / empty).
    assert detail.external_id is None
    assert detail.description is None
    assert detail.aliases == []
    assert detail.first_seen is None
    assert detail.last_seen is None


def test_nome_canonico_case_insensitive(tmp_path: Path) -> None:
    """Canonical-name resolution is case-insensitive (reuses the shared rule)."""
    service = StixService(data_dir=tmp_path)
    campaign = {
        "type": "campaign",
        "id": _stix_id("campaign", 11),
        "name": "shadowray",  # lower-case variant of "ShadowRay"
    }
    _write_bundle(service.base_path(_VERSION), [campaign])

    detail = service.get_campaign_by_slug(_VERSION, "shadowray")

    assert detail is not None
    assert detail.stix_id == campaign["id"]


def test_returns_none_when_absent(tmp_path: Path) -> None:
    """No campaign resolving to the slug -> ``None`` (never a partial object)."""
    service = StixService(data_dir=tmp_path)
    # A bundle with a non-curated campaign and some noise, but nothing that maps
    # to "shadowray".
    _write_bundle(
        service.base_path(_VERSION),
        [
            {
                "type": "campaign",
                "id": _stix_id("campaign", 1),
                "name": "Any Campaign",
                "external_references": [
                    {"source_name": "mitre-attack", "external_id": "C9999"}
                ],
            },
            {"type": "intrusion-set", "id": _stix_id("intrusion-set", 2)},
            {"type": "attack-pattern", "id": _stix_id("attack-pattern", 3)},
        ],
    )

    assert service.get_campaign_by_slug(_VERSION, "shadowray") is None


def test_returns_none_in_bundle_without_campaigns(tmp_path: Path) -> None:
    """An empty bundle resolves to ``None`` for any slug."""
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(_VERSION), [])

    for slug in SLUG_TO_STIX:
        assert service.get_campaign_by_slug(_VERSION, slug) is None


def test_extrai_campos_ricos_quando_presentes(tmp_path: Path) -> None:
    """All rich fields are extracted when present; the first usable external_id
    wins when several references exist."""
    service = StixService(data_dir=tmp_path)
    campaign = {
        "type": "campaign",
        "id": _stix_id("campaign", 42),
        "name": "C0010",
        "description": "long-duration intrusion.",
        "aliases": ["C0010", "", "Valid alias"],  # empties filtered out
        "first_seen": "2020-01-01T00:00:00.000Z",
        "last_seen": "2021-01-01T00:00:00.000Z",
        "external_references": [
            {"source_name": "capec"},  # no external_id -> skipped
            {"source_name": "mitre-attack", "external_id": "C0010"},
            {"source_name": "other", "external_id": "C0099"},
        ],
    }
    _write_bundle(service.base_path(_VERSION), [campaign])

    detail = service.get_campaign_by_slug(_VERSION, "c0010")

    assert detail is not None
    assert detail.external_id == "C0010"  # first usable one
    assert detail.description == "long-duration intrusion."
    assert detail.aliases == ["C0010", "Valid alias"]  # empty string dropped
    assert detail.first_seen == "2020-01-01T00:00:00.000Z"
    assert detail.last_seen == "2021-01-01T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Property 2 — resolution consistent with list_campaigns / _map_campaign.
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
        target = {
            "type": "campaign",
            "id": _stix_id("campaign", draw(st.integers(0, 9_999))),
            "name": "Nome Qualquer",
            "external_references": [
                {"source_name": "mitre-attack", "external_id": external_id}
            ],
        }
    else:
        slug, name = draw(st.sampled_from(_CURADAS_POR_NOME))
        target = {
            "type": "campaign",
            "id": _stix_id("campaign", draw(st.integers(10_000, 19_999))),
            "name": name,
        }
    alvo_id = target["id"]

    # Non-curated campaigns + noise objects, none of which resolves to a slug.
    ruido: list[dict] = [
        {
            "type": "campaign",
            "id": _stix_id("campaign", draw(st.integers(20_000, 29_999)) + i),
            "name": f"Non-Curated Campaign {i}",
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

    objects = draw(st.permutations([target] + ruido))
    return list(objects), slug, alvo_id


# Feature: input-estagio1-campaign-stix, Property 2: resolution consistent with the campaigns endpoint
@PROP_SETTINGS
@given(dados=_bundle_com_slug_curado())
def test_property2_resolution_consistent_with_list_campaigns(
    tmp_path: Path, dados: tuple[list[dict], str, str]
) -> None:
    """For a curated slug present in a base, ``get_campaign_by_slug`` resolves
    to the **same** campaign (same ``stix_id``) that ``list_campaigns`` /
    ``_map_campaign`` flags for that slug — the resolution rule is single and
    does not diverge between the Entrada and the campaigns screen."""
    objects, slug, alvo_id = dados
    service = StixService(data_dir=tmp_path)
    _write_bundle(service.base_path(_VERSION), objects)

    detail = service.get_campaign_by_slug(_VERSION, slug)

    # Independent oracle: the stix_id of the FIRST campaign that list_campaigns
    # flags as curated with this slug (same bundle order as get_campaign_by_slug).
    campaigns = service.list_campaigns(_VERSION)
    esperado = next(
        (c.stix_id for c in campaigns if c.curated and c.curated_slug == slug),
        None,
    )

    assert esperado is not None, "a campaign curated gerada deve ser listada"
    assert esperado == alvo_id
    assert detail is not None, "a Entrada deve resolver o mesmo slug curated"
    # Property 2: same campaign (same stix_id) in both views.
    assert detail.stix_id == esperado


# Feature: input-estagio1-campaign-stix, Property 2: absent slug resolves to None in both views
@PROP_SETTINGS
@given(
    slug=st.sampled_from(sorted(SLUG_TO_STIX)),
    ruido=st.lists(
        st.sampled_from(["intrusion-set", "attack-pattern", "malware", "relationship"]),
        max_size=5,
    ),
)
def test_property2_absent_slug_none_in_both_views(
    tmp_path: Path, slug: str, ruido: list[str]
) -> None:
    """When no campaign resolves to a slug, ``get_campaign_by_slug`` returns
    ``None`` and ``list_campaigns`` likewise flags no campaign with that slug —
    the two views stay consistent on absence too."""
    service = StixService(data_dir=tmp_path)
    objects = [
        {"type": type, "id": _stix_id(type, i)} for i, type in enumerate(ruido)
    ]
    _write_bundle(service.base_path(_VERSION), objects)

    detail = service.get_campaign_by_slug(_VERSION, slug)
    campaigns = service.list_campaigns(_VERSION)

    assert detail is None
    assert all(not (c.curated and c.curated_slug == slug) for c in campaigns)
