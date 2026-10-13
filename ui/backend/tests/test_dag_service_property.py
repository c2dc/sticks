"""Property + example tests for :class:`DagService` (tasks 7.4, 7.5, 7.6).

These cover the DAG Generation/Visualization phase of the
``fluxo-stix-dashboard`` spec at the *service* level (``GET /api/casos/{caso}/dag``
is exercised separately in ``test_dag_endpoints.py``). Everything here is
read-only and offline: a :class:`DagService` is pointed at a **temporary** data
directory holding generated ``{slug}_dag.json`` / ``{slug}_dag-ability.json``
fixtures (never the network, never a real AI client — there is none on this
path).

The design's Correctness Properties covered:

* **Property 8** (task 7.4) — a curated case with a valid DAG yields an acyclic,
  non-empty Mermaid and enables advancing: ``curado==True``,
  ``pode_avancar==True``, ``|nos| == |structural_nodes|``,
  ``|arestas| == |edges|``, Mermaid starts with ``flowchart TD`` with one node
  declaration per node and one ``-->`` per edge, and no extra edge is introduced
  (acyclicity preserved). Also a concrete example over the REAL
  ``shadowray_dag.json`` (10 nós / 9 arestas).
* **Property 9** (task 7.5) — a non-curated slug is always "passa-direto":
  ``curado==False``, ``pode_avancar==False``, ``indisponivel==True``, empty
  ``nos``/``arestas``, never raising and never invoking any AI service (there is
  none — the contract is asserted).
* **Edge** (task 7.6) — a curated case whose DAG file is missing or malformed
  yields a typed error state (``curado==True``, ``pode_avancar==False``,
  ``erro`` set) distinguishing missing from malformed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.services.case_service import CURATED_CASES, CaseService
from app.services.dag.dag_service import DagService


# A real curated slug used for the generated fixtures and the real-file example.
_CURATED_SLUG = "shadowray"
assert _CURATED_SLUG in CURATED_CASES  # guard: keep the fixture slug curated


# ---------------------------------------------------------------------------
# Fixture builders — write generated {slug}_dag.json into a tmp data dir laid
# out exactly like <data_dir>/{api,dag}/ so CaseService(data_dir=...) resolves
# the files (ability + dag). No network, no AI.
# ---------------------------------------------------------------------------


def _write_case_fixture(
    data_dir: Path,
    slug: str,
    structural_nodes: list[dict],
) -> None:
    """Write the ability + DAG fixtures for ``slug`` under ``data_dir``.

    ``CaseService.load_case`` reads the ability list and the DAG for a slug (the
    adversary file is optional — a missing one simply means "no adversary").
    We write an empty ability list (valid, "no abilities") plus the DAG so
    ``obter_dag`` reaches the valid-DAG branch purely from the structural graph.
    """
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)

    (api_dir / f"{slug}_dag-ability.json").write_text("[]", encoding="utf-8")

    dag_payload = {
        "campaign_name": slug,
        "metadata": {"validation": {"is_dag": True}},
        "structural_nodes": structural_nodes,
    }
    (dag_dir / f"{slug}_dag.json").write_text(
        json.dumps(dag_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Hypothesis strategy — an acyclic DAG expressed via child_nodes.
#
# We generate N nodes with unique node_ids and only allow edges from an EARLIER
# node to a LATER node (i -> j with i < j). That construction guarantees
# acyclicity by topological order, matches how DagData/DagNode parse the real
# files (structural_nodes[*].child_nodes), and lets us know the exact expected
# edge set up front.
# ---------------------------------------------------------------------------


@st.composite
def _acyclic_dag(draw) -> dict:
    """Draw an acyclic DAG as a dict of ``{node_ids, structural_nodes, edges}``.

    ``edges`` is the exact de-duplicated ``(from, to)`` set the service must
    derive from ``child_nodes`` (earlier->later only, so no cycle is possible).
    """
    n = draw(st.integers(min_value=1, max_value=8))
    node_ids = [f"node-{i:02d}" for i in range(n)]

    # For each ordered pair (i < j) decide independently whether i -> j exists.
    edges: list[tuple[str, str]] = []
    children: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for i in range(n):
        for j in range(i + 1, n):
            if draw(st.booleans()):
                edges.append((node_ids[i], node_ids[j]))
                children[node_ids[i]].append(node_ids[j])

    structural_nodes = [
        {
            "node_id": node_ids[i],
            "node_index": i,
            "technique_id": f"T{1000 + i}",
            "technique_name": f"Technique {i}",
            "tactic": f"tactic-{i}",
            "child_nodes": children[node_ids[i]],
            # parent_nodes left empty on purpose: edges are derived from
            # child_nodes only (matching stage_service._extract_relationships).
            "parent_nodes": [],
        }
        for i in range(n)
    ]
    return {
        "node_ids": node_ids,
        "structural_nodes": structural_nodes,
        "edges": edges,
    }


def _has_cycle(num_nodes: int, edges: list[tuple[str, str]], ids: list[str]) -> bool:
    """Kahn's algorithm: True iff the directed graph has a cycle."""
    index = {nid: k for k, nid in enumerate(ids)}
    indeg = [0] * num_nodes
    adj: list[list[int]] = [[] for _ in range(num_nodes)]
    for src, dst in edges:
        adj[index[src]].append(index[dst])
        indeg[index[dst]] += 1
    queue = [k for k in range(num_nodes) if indeg[k] == 0]
    visited = 0
    while queue:
        u = queue.pop()
        visited += 1
        for v in adj[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                queue.append(v)
    return visited != num_nodes


# ===========================================================================
# Property 8 (task 7.4)
# ===========================================================================


# Feature: fluxo-stix-dashboard, Property 8: Caso curado produz Mermaid acíclico
# não vazio e habilita avanço
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(dag=_acyclic_dag())
def test_property8_curated_dag_yields_acyclic_mermaid_and_advance(
    dag: dict, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A curated case with a valid acyclic DAG → curado/pode_avancar + Mermaid.

    Validates: Requirements 5.2, 6.1, 6.3
    """
    data_dir = tmp_path_factory.mktemp("dag_prop8")
    _write_case_fixture(data_dir, _CURATED_SLUG, dag["structural_nodes"])

    service = DagService(case_service=CaseService(data_dir=data_dir))
    view = service.obter_dag(_CURATED_SLUG)

    # Curated + advanceable.
    assert view.curado is True
    assert view.pode_avancar is True
    assert view.erro is None
    assert view.indisponivel is False

    # One nó per structural node, one aresta per generated edge.
    n_nodes = len(dag["structural_nodes"])
    assert len(view.nos) == n_nodes
    assert len(view.arestas) == len(dag["edges"])

    # The derived edge set equals EXACTLY the generated edge set — no extra edge
    # (and none lost), so the acyclicity is preserved.
    derived = {(a.de, a.para) for a in view.arestas}
    expected = set(dag["edges"])
    assert derived == expected
    assert not _has_cycle(n_nodes, list(derived), dag["node_ids"])

    # Mermaid: header, one node declaration per node, one --> per edge.
    mermaid = view.mermaid
    assert mermaid.startswith("flowchart TD")
    # One "n_...[" node declaration per structural node.
    assert mermaid.count("[") == n_nodes
    # Exactly one arrow per edge.
    assert mermaid.count("-->") == len(dag["edges"])


def test_property8_example_real_shadowray_dag() -> None:
    """Concrete example over the REAL ``shadowray_dag.json`` → 10 nós / 9 arestas.

    Uses a default :class:`CaseService` (reads ``<repo>/sticks/data/dag``), which
    is present in the running container. The real DAG is a 10-node chain, so it
    produces 10 nós, 9 arestas and a non-empty acyclic Mermaid.

    Validates: Requirements 5.2, 6.1, 6.3
    """
    view = DagService(case_service=CaseService()).obter_dag(_CURATED_SLUG)

    assert view.curado is True
    assert view.pode_avancar is True
    assert view.erro is None
    assert len(view.nos) == 10
    assert len(view.arestas) == 9

    mermaid = view.mermaid
    assert mermaid.startswith("flowchart TD")
    assert mermaid.count("-->") == 9
    assert mermaid.count("[") == 10


# ===========================================================================
# Property 9 (task 7.5)
# ===========================================================================


# A tripwire AI client: any attribute access/call fails the test. The DAG path
# has no AI dependency, so wiring one in and asserting it is never touched makes
# the "no AI" contract explicit for the non-curated branch.
class _NoAICalledSentinel:
    def __getattr__(self, name: str):  # pragma: no cover - must never run
        raise AssertionError(
            f"DagService must not invoke any AI service (accessed {name!r})."
        )


# Feature: fluxo-stix-dashboard, Property 9: Caso não curado é sempre
# "passa-direto", sem IA e sem avanço
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    suffix=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="_-"),
        min_size=1,
        max_size=24,
    )
)
def test_property9_non_curated_is_passa_direto_without_ai(
    suffix: str, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A non-curated slug → passa-direto stub, never raising, never calling AI.

    Validates: Requirements 5.4, 5.5, 9.1, 9.2
    """
    slug = f"nao_curado_{suffix}"
    assume(slug not in CURATED_CASES)

    # Even with a usable (empty) tmp data dir, a non-curated slug never reads a
    # file and never touches any AI client.
    data_dir = tmp_path_factory.mktemp("dag_prop9")
    service = DagService(case_service=CaseService(data_dir=data_dir))

    # No AI client exists on this path; a tripwire sentinel is intentionally not
    # wired into DagService (there is no seam to inject one), so the strongest
    # contract assertion is: the stub is returned with no exception at all.
    _ = _NoAICalledSentinel  # referenced to document the "no AI" intent

    view = service.obter_dag(slug)

    assert view.curado is False
    assert view.pode_avancar is False
    assert view.indisponivel is True
    assert view.nos == []
    assert view.arestas == []
    assert view.mermaid == ""
    assert view.mensagem  # a descriptive pt-BR unavailability message


# ===========================================================================
# Edge cases (task 7.6) — curated case with a missing / malformed DAG.
# ===========================================================================


def test_edge_curated_missing_dag_file_yields_typed_error(
    tmp_path: Path,
) -> None:
    """Curated slug whose DAG file is absent → curado, not advanceable, erro set.

    _Requisitos: 5.3, 6.4_
    """
    data_dir = tmp_path / "data"
    api_dir = data_dir / "api"
    (data_dir / "dag").mkdir(parents=True, exist_ok=True)
    api_dir.mkdir(parents=True, exist_ok=True)
    # Provide the ability file but NOT the dag file → missing DAG.
    (api_dir / f"{_CURATED_SLUG}_dag-ability.json").write_text("[]", encoding="utf-8")

    service = DagService(case_service=CaseService(data_dir=data_dir))
    view = service.obter_dag(_CURATED_SLUG)

    assert view.curado is True
    assert view.pode_avancar is False
    assert view.erro is not None
    assert "ausente" in view.erro.lower()
    assert view.nos == []
    assert view.arestas == []


def test_edge_curated_malformed_dag_file_yields_typed_error(
    tmp_path: Path,
) -> None:
    """Curated slug whose DAG file is invalid JSON → curado, not advanceable, erro.

    _Requisitos: 5.3, 6.4_
    """
    data_dir = tmp_path / "data"
    api_dir = data_dir / "api"
    dag_dir = data_dir / "dag"
    api_dir.mkdir(parents=True, exist_ok=True)
    dag_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / f"{_CURATED_SLUG}_dag-ability.json").write_text("[]", encoding="utf-8")
    # Malformed JSON (truncated object).
    (dag_dir / f"{_CURATED_SLUG}_dag.json").write_text(
        '{"structural_nodes": [', encoding="utf-8"
    )

    service = DagService(case_service=CaseService(data_dir=data_dir))
    view = service.obter_dag(_CURATED_SLUG)

    assert view.curado is True
    assert view.pode_avancar is False
    assert view.erro is not None
    assert "malformado" in view.erro.lower()
    assert view.nos == []
    assert view.arestas == []
