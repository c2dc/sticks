"""Sanity checks of the curated-cases seed JSON files (no database access)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.seeds import loader, manifest

DATA_DIR = Path(__file__).resolve().parents[1] / "app" / "seeds" / "data"

KEPT_TOP_LEVEL_KEYS = {"campaign", "abilities", "adversary", "attack_flow"}
REMOVED_TOP_LEVEL_KEYS = {"environment_parameters", "provisioning_map"}
KEPT_FLOW_CONTENT_KEYS = {"campaign_name", "edges", "structural_nodes"}
REMOVED_FLOW_CONTENT_KEYS = {"capability_flow", "metadata", "campaign_file", "generated_at"}
KEPT_NODE_KEYS = {"node_id", "node_type", "technique_id", "technique_name", "tactic"}
REMOVED_NODE_KEYS = {
    "campaign_context",
    "network_context",
    "provides",
    "source_host_type",
    "target_host_type",
    "target_vuln_host_a",
    "target_vuln_host_b",
    "attacker_commands",
    "description",
    "parent_nodes",
    "child_nodes",
    "node_index",
    "ai_prompt_template",
}


def _cases() -> list[dict]:
    return loader.load_all()


def _abilities() -> list[dict]:
    return [a for case in _cases() for a in case["abilities"]]


def test_counts_match_the_curated_corpus() -> None:
    cases = _cases()
    assert len(cases) == 8
    assert len(_abilities()) == 108
    assert len([c["adversary"] for c in cases]) == 8
    assert len([c["attack_flow"] for c in cases]) == 8


def test_manifest_matches_the_files() -> None:
    cases = _cases()
    assert manifest.CAMPAIGN_SLUGS == tuple(sorted(p.stem for p in DATA_DIR.glob("*.json")))
    assert manifest.CAMPAIGN_SLUGS == loader.list_slugs()
    assert manifest.EXPECTED_COUNTS == {
        "campaigns": len(cases),
        "abilities": len(_abilities()),
        "adversaries": len(cases),
        "attack_flows": len(cases),
    }


def test_slugs_and_ids_are_unique_globally() -> None:
    cases = _cases()
    slugs = [c["campaign"]["id"] for c in cases]
    assert len(set(slugs)) == len(slugs)
    ability_ids = [a["ability_id"] for a in _abilities()]
    assert len(set(ability_ids)) == len(ability_ids)
    adversary_ids = [c["adversary"]["id"] for c in cases]
    assert len(set(adversary_ids)) == len(adversary_ids)


def test_references_resolve_globally_with_no_orphans() -> None:
    cases = _cases()
    slugs = {c["campaign"]["id"] for c in cases}
    for case in cases:
        assert case["adversary"]["campaign_id"] == case["campaign"]["id"]
        assert case["attack_flow"]["campaign_id"] == case["campaign"]["id"]
        assert {a["campaign_id"] for a in case["abilities"]} <= {case["campaign"]["id"]}
    assert slugs == set(manifest.CAMPAIGN_SLUGS)
    ability_ids = {a["ability_id"] for a in _abilities()}
    orphans = {
        ref
        for case in cases
        for ref in case["adversary"]["atomic_ordering"]
        if ref not in ability_ids
    }
    assert orphans == set()


def test_costaricto_references_a_shadowray_ability_without_orphaning() -> None:
    by_slug = {c["campaign"]["id"]: c for c in _cases()}
    shadowray_ids = {a["ability_id"] for a in by_slug["shadowray"]["abilities"]}
    assert "5c0eccee-eee4-5d88-b6e5-c83b25d44eaa" in shadowray_ids
    assert "5c0eccee-eee4-5d88-b6e5-c83b25d44eaa" in by_slug["costaricto"]["adversary"]["atomic_ordering"]


def test_attack_flows_have_the_lean_graph_content() -> None:
    flows = [c["attack_flow"] for c in _cases()]
    keys = [(f["campaign_id"], f["identifier"]) for f in flows]
    assert len(set(keys)) == len(keys)
    for flow in flows:
        assert flow["content"]["structural_nodes"]
        assert "arestas" not in flow["content"]
        assert set(flow["content"]) == KEPT_FLOW_CONTENT_KEYS
        assert not (set(flow["content"]) & REMOVED_FLOW_CONTENT_KEYS)


def test_seed_files_have_the_lean_top_level_format() -> None:
    for case in _cases():
        assert set(case) == KEPT_TOP_LEVEL_KEYS
        assert not (set(case) & REMOVED_TOP_LEVEL_KEYS)
        assert "curated" not in case["campaign"]
        assert "origin" not in case["attack_flow"]


def test_nodes_keep_only_the_live_fields() -> None:
    for case in _cases():
        for node in case["attack_flow"]["content"]["structural_nodes"]:
            assert KEPT_NODE_KEYS >= set(node)
            assert not (set(node) & REMOVED_NODE_KEYS)
            assert node["node_id"] and node["technique_id"]


def test_flow_edges_reference_existing_nodes() -> None:
    for case in _cases():
        content = case["attack_flow"]["content"]
        node_ids = {n["node_id"] for n in content["structural_nodes"]}
        assert content["edges"]
        for edge in content["edges"]:
            assert edge["from"] in node_ids and edge["to"] in node_ids


def test_seed_files_are_byte_stable_and_lf_only() -> None:
    for path in sorted(DATA_DIR.glob("*.json")):
        raw = path.read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
        data = json.loads(raw.decode("utf-8"))
        again = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        assert again.encode("utf-8") == raw

def test_loader_rejects_invalid_structure() -> None:
    good = json.loads((DATA_DIR / "c0010.json").read_text(encoding="utf-8"))
    del good["abilities"]
    with pytest.raises(loader.SeedDataError, match="abilities"):
        loader._validate("c0010", good)
    with pytest.raises(loader.SeedDataError, match="cannot read"):
        loader.load_case("does_not_exist")


def test_removed_fields_are_gone_from_the_models() -> None:
    from app.services.curated_case_models import CaseData, FlowData, FlowNode

    assert set(FlowNode.model_fields) == KEPT_NODE_KEYS
    assert set(FlowData.model_fields) == {"campaign_name", "structural_nodes"}
    assert not hasattr(CaseData, "has_adversary")
    # Legacy flow content that still carries the removed keys is tolerated
    # (extra keys are ignored on read).
    legacy = FlowNode.model_validate(
        {"node_id": "n1", "ai_prompt_template": "x", "provides": ["c2"], "node_index": 3}
    )
    assert legacy.node_id == "n1"
    assert not (set(legacy.model_dump()) & (REMOVED_NODE_KEYS | {"ai_prompt_template"}))
    old_flow = FlowData.model_validate(
        {"campaign_name": "x", "capability_flow": {}, "metadata": {}, "generated_at": "t"}
    )
    assert old_flow.campaign_name == "x"
    assert not (set(old_flow.model_dump()) & REMOVED_FLOW_CONTENT_KEYS)


# ---------------------------------------------------------------------------
# STIX binding declared by the seed (task 33.2)
# ---------------------------------------------------------------------------

REPORTED_STIX_IDS = {
    "apt41_dust": ("C0040", "campaign--add4d9de-1256-4166-83b8-57087288dced"),
    "c0010": ("C0010", "campaign--ab747e62-1bcb-479f-a26b-1cd39d413d81"),
    "c0026": ("C0026", "campaign--c89fa3ff-4773-4daf-8aec-d8f43f10116e"),
    "costaricto": ("C0004", "campaign--f9cc545e-b0ef-4b92-8884-a3a4427609f6"),
    "operation_midnighteclipse": ("C0048", "campaign--455b011a-2510-49ab-a96f-2f84d6a80189"),
    "outer_space": ("C0042", "campaign--a1f5df89-ec82-45ef-b229-4ca6a407dc4f"),
    "salesforce_data_exfiltration": ("C0059", "campaign--9cea8bec-07c5-422b-84b8-99d3128ce570"),
    "shadowray": ("C0045", "campaign--57899965-b171-48ed-bbb4-8faffa3d836a"),
}


def test_every_seed_declares_the_stix_version_and_campaign_key() -> None:
    for case in _cases():
        campaign = case["campaign"]
        slug = campaign["id"]
        assert campaign["stix_version"] == "18.0"
        assert campaign["stix_campaign_key"] == REPORTED_STIX_IDS[slug][0]
        assert campaign["stix_version"] == manifest.STIX_VERSION_BY_SLUG[slug]
        assert campaign["stix_campaign_key"] == manifest.STIX_CAMPAIGN_KEYS[slug]


def test_manifest_stix_binding_matches_the_files() -> None:
    cases = _cases()
    assert set(manifest.STIX_VERSION_BY_SLUG) == set(manifest.CAMPAIGN_SLUGS)
    assert set(manifest.STIX_CAMPAIGN_KEYS) == set(manifest.CAMPAIGN_SLUGS)
    assert manifest.REQUIRED_STIX_VERSIONS == tuple(
        sorted({c["campaign"]["stix_version"] for c in cases})
    )
    assert manifest.REQUIRED_STIX_VERSIONS == ("18.0",)
    assert manifest.CURATED_STIX_VERSION == "18.0"


def test_slug_to_stix_matches_the_seed_campaign_keys() -> None:
    from app.services.stix.stix_service import SLUG_TO_STIX

    assert set(SLUG_TO_STIX) == set(manifest.CAMPAIGN_SLUGS)
    for case in _cases():
        campaign = case["campaign"]
        assert SLUG_TO_STIX[campaign["id"]]["referencias"] == [campaign["stix_campaign_key"]]


def test_loader_rejects_an_invalid_stix_binding() -> None:
    good = json.loads((DATA_DIR / "c0010.json").read_text(encoding="utf-8"))
    for key, bad in (("stix_version", "18"), ("stix_version", "v18.0"), ("stix_campaign_key", "C45")):
        broken = json.loads(json.dumps(good))
        broken["campaign"][key] = bad
        with pytest.raises(loader.SeedDataError, match=key):
            loader._validate("c0010", broken)
    missing = json.loads(json.dumps(good))
    del missing["campaign"]["stix_campaign_key"]
    with pytest.raises(loader.SeedDataError, match="stix_campaign_key"):
        loader._validate("c0010", missing)


def test_stix_campaign_keys_are_unique_and_well_formed() -> None:
    import re

    keys = [case["campaign"]["stix_campaign_key"] for case in _cases()]
    assert len(keys) == 8
    assert len(set(keys)) == len(keys)
    assert all(re.fullmatch(r"C\d{4}", key) for key in keys)
    assert set(keys) == set(manifest.STIX_CAMPAIGN_KEYS.values())
