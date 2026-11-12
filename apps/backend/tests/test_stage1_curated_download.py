"""Example tests of the curated-version flag and the dashboard derivations.

Example (non property-based) tests that exercise
:class:`app.services.stix.stix_service.StixService` directly, complementing
``test_stix_service_property.py``:

* the curated-version flag :func:`version_is_curated` - true only for
  :data:`CURATED_VERSION` (``"18.0"``) (Req. 5.5);
* ``dashboard`` - deterministic per-type counts, and the campaign listing with the
  ``curated`` flag resolved (Req. 5.7).

Bundles are built in memory; nothing touches the disk or the network.
"""

from __future__ import annotations

import pytest

from app.services.stix.stix_service import (
    CURATED_VERSION,
    KNOWN_VERSIONS,
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
