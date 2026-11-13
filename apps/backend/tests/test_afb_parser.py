"""Deterministic tests for the .afb parser, the STIX round trip and cycle analysis.

Covers Properties 3, 4 and 5 of the agentic-only-flow-redesign spec with fixed
synthetic fixtures (no Hypothesis).

**Validates: Requirements 2.3, 2.4, 2.5, 2.6, 4.3**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.attack_flow.afb_parser import AfbParseError, parse_afb
from app.services.attack_flow.cycles import analyze_cycles, annotate_flow_cycles
from app.services.attack_flow.model import AttackFlow

FIXTURES = Path(__file__).parent / "fixtures" / "attack_flow"


def _uid(kind: str, n: int) -> str:
    return f"{kind}--00000000-0000-4000-8000-{n:012x}"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _is_acyclic(nodes: list[str], edges: list[tuple[str, str]]) -> bool:
    """Kahn's algorithm."""
    indegree = dict.fromkeys(nodes, 0)
    out: dict[str, list[str]] = {n: [] for n in nodes}
    for src, dst in edges:
        out[src].append(dst)
        indegree[dst] += 1
    ready = [n for n, d in indegree.items() if d == 0]
    seen = 0
    while ready:
        node = ready.pop()
        seen += 1
        for nxt in out[node]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    return seen == len(nodes)


# --------------------------------------------------------------- Property 4
def test_parse_acyclic_preserves_drawn_graph() -> None:
    flow = parse_afb(_load("acyclic.afb"), source_key="acyclic.afb")

    action_a = _uid("attack-action", 0x02)
    condition = _uid("attack-condition", 0x03)
    operator = _uid("attack-operator", 0x04)
    action_b = _uid("attack-action", 0x05)
    asset = _uid("attack-asset", 0x06)

    assert flow.flow_id == "attack-flow--00000000-0000-4000-8000-000000000001"
    assert flow.name == "Acyclic sample"
    assert flow.scope == "incident"
    assert [o.id for o in flow.objects] == [action_a, condition, operator, action_b, asset]

    # Control-flow edges equal the drawn lines; action -> asset is not an effect edge.
    assert flow.edges() == [(action_a, condition), (condition, operator), (operator, action_b)]
    by_id = {o.id: o for o in flow.objects}
    assert by_id[action_b].asset_refs == [asset]
    assert by_id[asset].properties == {"source_type": "file"}

    # Original technique and operator values are preserved.
    assert by_id[action_a].technique_id == "T1059"
    assert by_id[action_a].tactic_id == "TA0002"
    assert by_id[action_b].technique_id == "T1021.001"
    assert by_id[operator].operator == "OR"
    assert by_id[condition].description == "Host is reachable"

    # Only the node without incoming edges starts the flow.
    assert flow.start_refs == [action_a]


def test_parse_is_deterministic() -> None:
    first = parse_afb(_load("acyclic.afb"), source_key="a")
    second = parse_afb(_load("acyclic.afb"), source_key="b")
    assert first == second


def test_parse_cyclic_fixture_keeps_all_lines() -> None:
    flow = parse_afb(_load("cyclic.afb"), source_key="cyclic.afb")
    entry, retry, check = (_uid("attack-action", n) for n in (0x12, 0x13, 0x14))
    assert flow.edges() == [(entry, retry), (retry, check), (check, retry)]
    assert flow.start_refs == [entry]


# ------------------------------------------------------------ invalid input
def test_parse_invalid_fixture_raises() -> None:
    with pytest.raises(AfbParseError) as info:
        parse_afb(_load("invalid.afb"), source_key="invalid.afb")
    assert info.value.reason == "unsupported_schema"
    assert info.value.flow_file == "invalid.afb"


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(AfbParseError) as info:
        parse_afb(b"{not json", source_key="broken.afb")
    assert info.value.reason == "invalid_json"


def test_parse_rejects_unresolved_endpoint_and_bad_technique() -> None:
    document = json.loads(_load("acyclic.afb"))

    broken_line = json.loads(json.dumps(document))
    next(o for o in broken_line["objects"] if o["id"] == "dynamic_line")["target"] = "missing"
    with pytest.raises(AfbParseError) as info:
        parse_afb(json.dumps(broken_line).encode(), source_key="x")
    assert info.value.reason == "unresolved_line_endpoint"

    bad_technique = json.loads(json.dumps(document))
    action = next(o for o in bad_technique["objects"] if o["id"] == "action")
    action["properties"] = [["technique_id", "not-a-technique"]]
    with pytest.raises(AfbParseError) as info:
        parse_afb(json.dumps(bad_technique).encode(), source_key="x")
    assert info.value.reason == "invalid_technique_id"


# --------------------------------------------------------------- Property 3
@pytest.mark.parametrize("name", ["acyclic.afb", "cyclic.afb"])
def test_stix_round_trip_and_edge_derivation(name: str) -> None:
    flow = annotate_flow_cycles(parse_afb(_load(name), source_key=name))

    bundle = flow.to_stix_bundle()
    # JSON-serializable and restored losslessly.
    restored = AttackFlow.from_stix_bundle(json.loads(json.dumps(bundle)))
    assert restored == flow

    expected = {(o.id, ref) for o in flow.objects for ref in o.effect_refs}
    assert set(restored.edges()) == expected
    assert len(restored.edges()) == len(expected)
    # The bundle carries the edges only as effect_refs.
    stix_pairs = {
        (item["id"], ref) for item in bundle["objects"] for ref in item.get("effect_refs", [])
    }
    assert stix_pairs == expected


# --------------------------------------------------------------- Property 5
def test_analyze_cycles_acyclic_graph() -> None:
    report = analyze_cycles(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")], ["a"])
    assert report.components == []
    assert report.back_edges == []


def test_analyze_cycles_simple_cycle() -> None:
    nodes = ["a", "b", "c"]
    edges = [("a", "b"), ("b", "c"), ("c", "b")]
    report = analyze_cycles(nodes, edges, ["a"])
    assert report.components == [["b", "c"]]
    assert report.back_edges == [("c", "b")]
    assert set(report.back_edges) <= set(edges)

    execution = [e for e in edges if e not in set(report.back_edges)]
    assert _is_acyclic(nodes, execution)
    assert analyze_cycles(nodes, execution, ["a"]).components == []


def test_analyze_cycles_self_loop() -> None:
    nodes = ["a", "b"]
    edges = [("a", "b"), ("b", "b")]
    report = analyze_cycles(nodes, edges, ["a"])
    assert report.components == [["b"]]
    assert report.back_edges == [("b", "b")]
    execution = [e for e in edges if e not in set(report.back_edges)]
    assert _is_acyclic(nodes, execution)


def test_annotate_flow_cycles_builds_acyclic_execution_view() -> None:
    parsed = parse_afb(_load("cyclic.afb"), source_key="cyclic.afb")
    annotated = annotate_flow_cycles(parsed)
    retry, check = _uid("attack-action", 0x13), _uid("attack-action", 0x14)

    # The original is untouched; the copy carries the analysis.
    assert parsed.cycles == [] and parsed.back_edges == []
    assert annotated.cycles == [sorted([retry, check])]
    assert annotated.back_edges == [(check, retry)]
    assert set(annotated.execution_edges()) == set(annotated.edges()) - {(check, retry)}

    nodes = [o.id for o in annotated.objects]
    assert _is_acyclic(nodes, annotated.execution_edges())

    # Acyclic input stays untouched.
    clean = annotate_flow_cycles(parse_afb(_load("acyclic.afb"), source_key="a"))
    assert clean.cycles == [] and clean.back_edges == []
    assert clean.execution_edges() == clean.edges()
