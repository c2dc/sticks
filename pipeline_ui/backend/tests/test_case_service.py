"""Unit tests for the CaseService (task 3.3).

These example-based tests cover reading and parsing the curated ability/
adversary/DAG file sets, the clean "no adversary"/"no abilities" states, and
the per-case error isolation of the safe API (``try_load_case`` /
``load_all_cases_safe``).

Two data sources are used, both read-only with respect to real data:

1. **The real ShadowRay files** under ``sticks/data`` (read via a repo-root-
   relative path, exactly like ``test_subnet_validator.py`` does) — to prove the
   service parses a real curated case correctly (abilities, adversary,
   ``atomic_ordering`` and DAG).

2. **Programmatically-built temp fixtures** (pathlib + json) for every isolated
   scenario: a case with no adversary, a case with no abilities, a missing file
   and malformed JSON. The temp ``data_dir`` is injected into the service so no
   real data is ever mutated, and it is torn down after each test.

_Requisitos: 5.1, 5.6, 3.7, 3.8_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.case_service import (
    CURATED_CASES,
    CaseLoadErrorType,
    CaseService,
)


# ---------------------------------------------------------------------------
# Temp data_dir fixture helpers (programmatic fixtures, no real data touched)
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as UTF-8 JSON, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_valid_case(data_dir: Path, slug: str) -> None:
    """Seed a minimal but fully valid ability/adversary/DAG set for ``slug``.

    The payloads are intentionally tiny; they only need to parse cleanly so the
    case shows up as loaded and can act as a "healthy neighbour" while another
    case is broken/missing in the same catalog.
    """
    _write_json(
        data_dir / "api" / f"{slug}_dag-ability.json",
        [
            {
                "ability_id": f"{slug}-ab-1",
                "name": "T1190 - Exploit Public-Facing Application",
                "tactic": "initial-access",
                "technique_name": "Exploit Public-Facing Application",
                "technique_id": "T1190",
                "description": "seed ability",
                "executors": [
                    {
                        "name": "sh",
                        "platform": "linux",
                        "command": "curl http://172.21.0.20/x",
                    }
                ],
            }
        ],
    )
    _write_json(
        data_dir / "api" / f"{slug}_dag-adversary.json",
        {
            "id": f"{slug}-adv",
            "name": "Seed Adversary",
            "description": "seed",
            "atomic_ordering": [f"{slug}-ab-1"],
        },
    )
    _write_json(
        data_dir / "dag" / f"{slug}_dag.json",
        {
            "campaign_name": "Seed",
            "structural_nodes": [
                {
                    "node_id": "n0",
                    "node_index": 0,
                    "node_type": "structural",
                    "technique_id": "T1190",
                }
            ],
            "metadata": {"total_techniques": 1},
        },
    )


@pytest.fixture()
def temp_data_dir() -> Iterator[Path]:
    """Yield an isolated temp ``data_dir`` with ``api/`` and ``dag/`` subdirs.

    Cleaned up entirely afterward so no temp dirs leak. Callers seed whatever
    valid/broken fixtures each scenario needs.
    """
    tmp = Path(tempfile.mkdtemp(prefix="pipeline_ui_cases_"))
    (tmp / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "dag").mkdir(parents=True, exist_ok=True)
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# 1. Successful read of a real curated case (ShadowRay) via read-only fixture
# ---------------------------------------------------------------------------


def _real_data_dir() -> Path:
    """Locate the real ``sticks/data`` dir relative to the repo root.

    tests/ -> backend/ -> pipeline_ui/ -> repo root -> sticks/data (read-only),
    mirroring the strategy used by ``test_subnet_validator.py``.
    """
    here = Path(__file__).resolve()
    return here.parents[3] / "sticks" / "data"


def test_load_real_shadowray_case_parses_all_parts() -> None:
    """A real curated case parses abilities/adversary/atomic_ordering/dag.
    _Requisitos: 5.1_"""
    service = CaseService(data_dir=_real_data_dir())
    case = service.load_case("shadowray")

    # Metadata (Req. 5.1 registration).
    assert case.id == "shadowray"
    assert case.nome == "ShadowRay"

    # Abilities parsed with aligned field names.
    assert case.has_abilities is True
    assert len(case.abilities) >= 1
    first = case.abilities[0]
    assert first.ability_id == "0fa06c9c-fd66-52f1-b94a-83cb37bee900"
    assert first.technique_id == "T1190"
    assert first.executors and first.executors[0].command.startswith("curl")

    # Adversary + atomic_ordering (Req. 3.3, 3.5).
    assert case.has_adversary is True
    assert case.adversary is not None
    assert case.adversary.name == "ShadowRay"
    assert case.adversary.atomic_ordering[0] == "0fa06c9c-fd66-52f1-b94a-83cb37bee900"
    assert case.atomic_ordering == case.adversary.atomic_ordering

    # DAG parsed (campaign + structural nodes).
    assert case.dag is not None
    assert case.dag.campaign_name == "ShadowRay"
    assert len(case.dag.structural_nodes) >= 1
    assert case.dag.structural_nodes[0].technique_id == "T1190"


# ---------------------------------------------------------------------------
# 2. Case with NO adversary -> adversary is None, has_adversary is False
# ---------------------------------------------------------------------------


def test_case_without_adversary_file_is_clean_none(temp_data_dir: Path) -> None:
    """A missing adversary file yields ``adversary is None`` (not an error).
    _Requisitos: 3.8_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    # Remove the adversary file entirely -> "no adversary" state.
    (temp_data_dir / "api" / f"{slug}_dag-adversary.json").unlink()

    service = CaseService(data_dir=temp_data_dir)
    case = service.load_case(slug)

    assert case.adversary is None
    assert case.has_adversary is False
    assert case.atomic_ordering == []
    # Abilities are unaffected.
    assert case.has_abilities is True


def test_case_with_empty_adversary_object_is_clean_none(temp_data_dir: Path) -> None:
    """An empty/``{}`` adversary payload yields ``adversary is None``.
    _Requisitos: 3.8_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    _write_json(temp_data_dir / "api" / f"{slug}_dag-adversary.json", {})

    service = CaseService(data_dir=temp_data_dir)
    case = service.load_case(slug)

    assert case.adversary is None
    assert case.has_adversary is False


def test_case_with_null_adversary_is_clean_none(temp_data_dir: Path) -> None:
    """A JSON ``null`` adversary payload yields ``adversary is None``.
    _Requisitos: 3.8_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    _write_json(temp_data_dir / "api" / f"{slug}_dag-adversary.json", None)

    service = CaseService(data_dir=temp_data_dir)
    case = service.load_case(slug)

    assert case.adversary is None
    assert case.has_adversary is False


# ---------------------------------------------------------------------------
# 3. Case with NO abilities -> abilities == [], has_abilities is False
# ---------------------------------------------------------------------------


def test_case_with_empty_ability_list_has_no_abilities(temp_data_dir: Path) -> None:
    """An empty ability list yields ``abilities == []`` (not an error).
    _Requisitos: 3.7_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    _write_json(temp_data_dir / "api" / f"{slug}_dag-ability.json", [])

    service = CaseService(data_dir=temp_data_dir)
    case = service.load_case(slug)

    assert case.abilities == []
    assert case.has_abilities is False
    # Adversary is unaffected.
    assert case.has_adversary is True


def test_case_with_null_ability_list_has_no_abilities(temp_data_dir: Path) -> None:
    """A JSON ``null`` ability payload yields ``abilities == []``.
    _Requisitos: 3.7_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    _write_json(temp_data_dir / "api" / f"{slug}_dag-ability.json", None)

    service = CaseService(data_dir=temp_data_dir)
    case = service.load_case(slug)

    assert case.abilities == []
    assert case.has_abilities is False


# ---------------------------------------------------------------------------
# 4. MISSING file -> per-case MISSING_FILE error, other cases still load
# ---------------------------------------------------------------------------


def test_missing_ability_file_reports_missing_file_error(temp_data_dir: Path) -> None:
    """A missing ability file surfaces a per-case MISSING_FILE error.
    _Requisitos: 5.6_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    (temp_data_dir / "api" / f"{slug}_dag-ability.json").unlink()

    service = CaseService(data_dir=temp_data_dir)
    result = service.try_load_case(slug)

    assert result.ok is False
    assert result.case is None
    assert result.error is not None
    assert result.error.error_type is CaseLoadErrorType.MISSING_FILE
    assert result.error.is_missing_file is True
    assert result.error.slug == slug
    assert result.error.arquivo is not None


def test_missing_file_is_isolated_other_cases_still_load(temp_data_dir: Path) -> None:
    """One missing case lands in ``errors``; the rest remain in ``cases``.
    _Requisitos: 5.6_"""
    # Seed every curated case, then break exactly one by removing its DAG file.
    for slug in CURATED_CASES:
        _seed_valid_case(temp_data_dir, slug)
    broken = "shadowray"
    (temp_data_dir / "dag" / f"{broken}_dag.json").unlink()

    service = CaseService(data_dir=temp_data_dir)
    catalog = service.load_all_cases_safe()

    assert broken in catalog.errors
    assert catalog.errors[broken].error_type is CaseLoadErrorType.MISSING_FILE
    assert broken not in catalog.cases
    # Every other case loaded successfully.
    expected_ok = [s for s in CURATED_CASES if s != broken]
    assert sorted(catalog.loaded_slugs) == sorted(expected_ok)
    assert catalog.all_ok is False


# ---------------------------------------------------------------------------
# 5. MALFORMED JSON -> per-case MALFORMED_JSON error, isolated
# ---------------------------------------------------------------------------


def test_malformed_json_reports_malformed_error(temp_data_dir: Path) -> None:
    """Invalid JSON in a case file surfaces a per-case MALFORMED_JSON error.
    _Requisitos: 5.6, 3.7, 3.8_"""
    slug = "shadowray"
    _seed_valid_case(temp_data_dir, slug)
    # Corrupt the ability file with syntactically invalid JSON.
    (temp_data_dir / "api" / f"{slug}_dag-ability.json").write_text(
        "{ this is not valid json ,,, ]", encoding="utf-8"
    )

    service = CaseService(data_dir=temp_data_dir)
    result = service.try_load_case(slug)

    assert result.ok is False
    assert result.error is not None
    assert result.error.error_type is CaseLoadErrorType.MALFORMED_JSON
    assert result.error.is_malformed_json is True


def test_malformed_json_is_isolated_from_other_cases(temp_data_dir: Path) -> None:
    """A malformed case is recorded in ``errors`` without breaking the rest.
    _Requisitos: 5.6_"""
    for slug in CURATED_CASES:
        _seed_valid_case(temp_data_dir, slug)
    broken = "c0010"
    (temp_data_dir / "api" / f"{broken}_dag-adversary.json").write_text(
        "not-json{", encoding="utf-8"
    )

    service = CaseService(data_dir=temp_data_dir)
    catalog = service.load_all_cases_safe()

    assert broken in catalog.errors
    assert catalog.errors[broken].error_type is CaseLoadErrorType.MALFORMED_JSON
    assert broken not in catalog.cases
    # All the other cases loaded fine.
    for slug in CURATED_CASES:
        if slug != broken:
            assert slug in catalog.cases
    assert catalog.failed_slugs == [broken]
