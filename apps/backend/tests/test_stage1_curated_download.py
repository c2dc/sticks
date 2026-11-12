"""Example tests of the curated-version flag and the dashboard derivations.

Example (non property-based) tests that exercise
:class:`app.services.stix.stix_service.StixService` directly, complementing
``test_stix_service_property.py``:

* :data:`CURATED_VERSION` - derived from the seed manifest (``"18.0"``);
* ``dashboard`` - deterministic per-type counts, and the campaign listing with the
  ``curated`` flag resolved (Req. 5.7).

Bundles are built in memory; nothing touches the disk or the network.
"""

from __future__ import annotations

from app.services.stix.stix_service import (
    CURATED_VERSION,
    KNOWN_VERSIONS,
    StixService,
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


# ---------------------------------------------------------------------------
# CURATED_VERSION — derivada do seed via manifest (única fonte de verdade)
# ---------------------------------------------------------------------------


def test_curated_version_comes_from_the_seed_manifest() -> None:
    """``CURATED_VERSION`` é a versão declarada pelo seed (18.0), sem literal solto."""
    from app.seeds.manifest import CURATED_STIX_VERSION

    assert CURATED_VERSION == CURATED_STIX_VERSION == "18.0"
    assert CURATED_VERSION in KNOWN_VERSIONS


# ---------------------------------------------------------------------------
# dashboard — contagem por type + campaigns (Req. 5.7)
# ---------------------------------------------------------------------------


def test_dashboard_contagem_por_tipo_deterministica() -> None:
    """O dashboard traz os seis tipos exigidos, em ordem estável, com a contagem
    exata por type e descrições pt-BR não vazias (Req. 5.7)."""
    service = StixService()
    bundle = _bundle_curado()

    dashboard = service.dashboard(CURATED_VERSION, bundle)

    # Ordem estável e completa dos seis tipos exigidos.
    rotulos = [c.label for c in dashboard.counts]
    assert rotulos == [
        "campaigns",
        "techniques",
        "tactics",
        "actors",
        "malware",
        "relationships",
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


def test_dashboard_and_campaigns_list_curated() -> None:
    """A listagem de campaigns do bundle preserva a ordem e resolve a flag
    ``curated``/``curated_slug`` (ShadowRay curated, a outra não)."""
    service = StixService()
    bundle = _bundle_curado()

    campaigns = service.list_campaigns(bundle)

    assert [c.name for c in campaigns] == ["ShadowRay", "Any Campaign"]
    curated = campaigns[0]
    assert curated.curated is True
    assert curated.curated_slug == "shadowray"
    nao_curada = campaigns[1]
    assert nao_curada.curated is False
    assert nao_curada.curated_slug is None
