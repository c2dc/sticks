"""Property-based test for StixService observable equivalence across locations.

Covers task 3.4 — Property 9 of the ``stix-bundle-cache-location`` spec. The
property-based test is tagged with the exact
``# Feature: stix-bundle-cache-location, Property 9: <text>`` comment required by
the plan and runs with Hypothesis at ``max_examples=100`` (minimum 100
iterations).

Isolation (Req. 5.1): the SAME generated valid STIX bundle is written into two
distinct ``data_dir`` directories under ``tmp_path`` and two ``StixService``
instances are built, one per directory. No real ``sticks`` cache and no network
are ever touched. The property asserts that the observable behaviour of
``list_bases`` (the ``downloaded``/``selectable`` flags per version),
``read_bundle`` (the parsed dict) and ``dashboard`` (the ``counts``) is identical
between the two locations — differing only in the path-embedding ``file`` field.
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.stix.stix_service import (
    ROTULO_TIPO,
    KNOWN_VERSIONS,
    StixService,
)

# Shared iteration budget so the property runs >= 100 examples. The
# ``function_scoped_fixture`` health check is suppressed because ``tmp_path`` is
# a per-test fixture reused across Hypothesis examples; each example writes into
# distinct child directories, so there is no cross-example leakage.
PROP_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# A KNOWN version so ``list_bases`` reports it as downloaded once materialised.
_VERSION = KNOWN_VERSIONS[0]

# The six STIX types the dashboard counts, plus a couple of noise types that
# must never leak into the required counts.
_REQUIRED_TYPES = list(ROTULO_TIPO.keys())
_NOISE_TYPES = ["identity", "course-of-action", "tool", "x-mitre-matrix"]


def _stix_id(prefix: str, n: int) -> str:
    return f"{prefix}--{n:08d}-0000-0000-0000-000000000000"


@st.composite
def _valid_bundle(draw: st.DrawFn) -> list[dict]:
    """Generate a valid STIX bundle's objects: a mix of required-type and noise
    objects, shuffled so ordering never influences the result."""
    objetos: list[dict] = []
    contador = 0
    for stix_type in _REQUIRED_TYPES + _NOISE_TYPES:
        for _ in range(draw(st.integers(min_value=0, max_value=4))):
            objetos.append(
                {"type": stix_type, "id": _stix_id(stix_type, contador)}
            )
            contador += 1
    return list(draw(st.permutations(objetos)))


def _write_bundle(path: Path, objects: list[dict]) -> None:
    """Write a minimal-but-valid STIX bundle (``{"objects": [...]}``) to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "bundle", "objects": objects}), encoding="utf-8"
    )


# Feature: stix-bundle-cache-location, Property 9: Equivalência do comportamento
# observável independente do local. Para qualquer bundle STIX válido escrito em
# dois diretórios de dados distintos, o resultado observável de list_bases,
# read_bundle e dashboard SHALL ser equivalente entre os dois locais a menos dos
# campos que embutem o caminho (file): os counts do dashboard, o dict retornado
# por read_bundle e os flags downloaded/selectable por versão SHALL ser idênticos.
# Validates: Requirements 3.1, 3.4
@PROP_SETTINGS
@given(objetos=_valid_bundle())
def test_property9_observable_equivalence_across_locations(
    tmp_path: Path, objetos: list[dict]
) -> None:
    # Two distinct data directories under tmp_path — different physical paths.
    dir_a = tmp_path / "loc_a"
    dir_b = tmp_path / "loc_b"

    service_a = StixService(data_dir=dir_a)
    service_b = StixService(data_dir=dir_b)

    # Different resolved locations (the only thing that varies between them).
    assert service_a.data_dir != service_b.data_dir

    # Write the SAME valid bundle into both locations for a KNOWN version.
    _write_bundle(service_a.base_path(_VERSION), objetos)
    _write_bundle(service_b.base_path(_VERSION), objetos)

    # (1) list_bases: downloaded/selectable flags per version identical, except
    #     the path-embedding ``file`` field. Both must report _VERSION as
    #     downloaded (and only that version, since nothing else was written).
    bases_a = {b.version: b for b in service_a.list_bases()}
    bases_b = {b.version: b for b in service_b.list_bases()}
    assert set(bases_a) == set(bases_b)
    for version in bases_a:
        base_a = bases_a[version]
        base_b = bases_b[version]
        assert base_a.version == base_b.version
        assert base_a.name == base_b.name
        assert base_a.downloaded == base_b.downloaded
        assert base_a.selectable == base_b.selectable
    assert bases_a[_VERSION].downloaded is True
    assert bases_a[_VERSION].selectable is True

    # (2) read_bundle: the parsed dict is identical between locations (the bundle
    #     content does not embed the path at all).
    assert service_a.read_bundle(_VERSION) == service_b.read_bundle(_VERSION)

    # (3) dashboard: version/name and the per-type counts are identical. The
    #     DashboardStix carries no path-embedding field, so full equality holds.
    dash_a = service_a.dashboard(_VERSION)
    dash_b = service_b.dashboard(_VERSION)
    assert dash_a.version == dash_b.version
    assert dash_a.name == dash_b.name
    counts_a = [(c.label, c.stix_type, c.count, c.description) for c in dash_a.counts]
    counts_b = [(c.label, c.stix_type, c.count, c.description) for c in dash_b.counts]
    assert counts_a == counts_b
