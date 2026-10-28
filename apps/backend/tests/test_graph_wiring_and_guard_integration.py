"""Integration test for the StateGraph wiring and the max-iteration guard (task 14.5).

Exercises the fully assembled AI-curation graph produced by
:func:`app.services.ai.graph.build_graph` end-to-end at the structural level,
plus the two pure decision functions that the graph's conditional edge and its
entry node depend on:

* :func:`app.services.ai.graph.build_graph` — the graph assembly (task 14.2):
  all six nodes present, the linear edges wired in sequence, and the conditional
  edge after ``evaluate`` routing to the four targets ``dag`` /
  ``operationalize`` / ``environment`` / ``END`` (Req. 1.7, 3.1, 3.2, 3.3).
* :func:`app.services.ai.graph.route_after_eval` — the improvement-loop guard
  (task 14.3): ``finish`` once completing the current iteration would reach
  ``max_iterations`` (Req. 3.4/3.5, referenced here through the guard assertion
  the task calls out).
* :func:`app.services.ai.graph.orchestrate_node` — the entry node's
  default/invalid ``max_iterations`` handling (Req. 3.6, 3.7): default 3 when the
  configured value is absent, and default 3 **plus a warning** when it is
  non-integer or < 1.

A FAKE in-memory checkpointer (LangGraph's ``MemorySaver``) is used so the graph
compiles and its structure is inspectable entirely offline — no PostgreSQL, no
network, no Docker. The test only inspects the compiled graph's structure (via
``get_graph()``) and calls the pure decision/seed functions directly; it never
invokes the stage-agent nodes, so no LLM or lab driver is touched.

_Requirements: 1.7, 3.1, 3.2, 3.3, 3.6, 3.7_
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.services.ai import graph as graph_module
from app.services.ai.graph import build_graph, orchestrate_node, route_after_eval

# The design's node order (graph.py): orchestrate -> dag -> operationalize ->
# environment -> execute -> evaluate. The linear edges must wire exactly this
# sequence, each stage with a single outgoing edge to the next (Req. 3.1).
_NODE_ORDER: list[str] = [
    "orchestrate",
    "dag",
    "operationalize",
    "environment",
    "execute",
    "evaluate",
]

# The six stage/agent nodes the graph must declare (Req. 1.7 build, 3.1 wiring).
_EXPECTED_NODES = set(_NODE_ORDER)

# LangGraph's reserved sentinel node ids for START / END.
_START = "__start__"
_END = "__end__"

# The four conditional-edge targets after ``evaluate`` — the three refine stages
# and the loop terminus (``finish`` maps to LangGraph's END node) (Req. 3.2, 3.3).
_REFINE_TARGETS = {"dag", "operationalize", "environment"}


@pytest.fixture()
def compiled_graph():
    """Compile the real graph over a FAKE in-memory checkpointer.

    ``MemorySaver`` implements the same checkpointer contract ``build_graph``
    expects while keeping the whole test offline (no PostgreSQL). Compiling does
    not invoke any node, so this touches no LLM or lab driver.
    """
    return build_graph(checkpointer=MemorySaver())


@pytest.fixture()
def drawn_graph(compiled_graph):
    """The compiled app's inspectable structure (nodes + edges)."""
    return compiled_graph.get_graph()


# ===========================================================================
# build_graph: the graph is assembled on an explicit call (Req. 1.7) and all
# six nodes are present.
# ===========================================================================


def test_build_graph_returns_a_compiled_app(compiled_graph) -> None:
    """``build_graph`` returns a compiled, invocable LangGraph app (Req. 1.7).

    The graph is built only on the explicit ``build_graph`` call (importing the
    module performs no I/O); the returned object exposes the compiled-app surface
    the run service drives (``get_graph`` / ``invoke``).
    """
    assert compiled_graph is not None
    assert hasattr(compiled_graph, "get_graph")
    assert callable(compiled_graph.invoke)


def test_all_six_nodes_present(drawn_graph) -> None:
    """All six pipeline nodes are declared on the compiled graph (Req. 3.1).

    The drawn graph also carries LangGraph's reserved ``__start__`` / ``__end__``
    sentinels; the six real nodes must appear exactly, with no stage missing or
    extra stage node added.
    """
    node_ids = set(drawn_graph.nodes.keys())

    # Every expected stage node is present.
    assert _EXPECTED_NODES <= node_ids, (
        f"missing nodes: {_EXPECTED_NODES - node_ids}"
    )
    # The non-sentinel nodes are exactly the six expected ones.
    real_nodes = node_ids - {_START, _END}
    assert real_nodes == _EXPECTED_NODES, (
        f"unexpected node set: {real_nodes!r}"
    )


# ===========================================================================
# Linear edges wired in sequence (Req. 3.1): START -> orchestrate -> dag ->
# operationalize -> environment -> execute -> evaluate, each stage with exactly
# one outgoing (non-conditional) edge.
# ===========================================================================


def _linear_edges(drawn_graph) -> dict[str, str]:
    """Map ``source -> target`` for the unconditional (linear) edges only."""
    return {
        edge.source: edge.target
        for edge in drawn_graph.edges
        if not edge.conditional
    }


def test_linear_edges_wired_in_sequence(drawn_graph) -> None:
    """The linear edges form the exact START..evaluate chain (Req. 3.1).

    START enters ``orchestrate`` and each subsequent stage up to ``execute`` has
    exactly one outgoing linear edge to the next stage in order; ``evaluate`` has
    no *linear* successor (its only successors are the conditional loop edges).
    """
    linear = _linear_edges(drawn_graph)

    # START -> orchestrate, then each stage to the next up to execute -> evaluate.
    expected_chain = {_START: _NODE_ORDER[0]}
    for earlier, later in zip(_NODE_ORDER, _NODE_ORDER[1:]):
        expected_chain[earlier] = later

    assert linear == expected_chain, (
        f"linear edges deviate from the sequence: {linear!r}"
    )


def test_each_stage_has_exactly_one_outgoing_linear_edge(drawn_graph) -> None:
    """Every stage from orchestrate..execute has a single linear successor (Req. 3.1)."""
    outgoing_counts: dict[str, int] = {}
    for edge in drawn_graph.edges:
        if not edge.conditional:
            outgoing_counts[edge.source] = outgoing_counts.get(edge.source, 0) + 1

    # orchestrate, dag, operationalize, environment, execute each have exactly
    # one outgoing linear edge (evaluate's successors are all conditional).
    for stage in _NODE_ORDER[:-1]:
        assert outgoing_counts.get(stage) == 1, (
            f"{stage} must have exactly one outgoing linear edge, "
            f"got {outgoing_counts.get(stage)}"
        )


# ===========================================================================
# Conditional edge after evaluate routes to the four targets (Req. 3.2, 3.3):
# dag, operationalize, environment, and END (the "finish" key).
# ===========================================================================


def test_conditional_edges_fan_out_from_evaluate(drawn_graph) -> None:
    """The only conditional edges originate at ``evaluate`` (Req. 3.2, 3.3).

    The improvement loop is the single conditional branch in the graph; it hangs
    off ``evaluate`` and nowhere else.
    """
    conditional_sources = {
        edge.source for edge in drawn_graph.edges if edge.conditional
    }
    assert conditional_sources == {"evaluate"}


def test_conditional_edge_routes_to_four_targets(drawn_graph) -> None:
    """The conditional edge after evaluate reaches exactly the four targets.

    Three refine stages (``dag`` / ``operationalize`` / ``environment``) plus the
    loop terminus END — ``route_after_eval``'s ``finish`` key is mapped to END by
    ``build_graph`` (Req. 3.2, 3.3).
    """
    conditional_targets = {
        edge.target for edge in drawn_graph.edges if edge.conditional
    }

    assert conditional_targets == _REFINE_TARGETS | {_END}, (
        f"conditional targets deviate: {conditional_targets!r}"
    )
    # The loop terminus is present (the ``finish`` branch -> END).
    assert _END in conditional_targets
    # Each refine target is reachable from the conditional edge.
    assert _REFINE_TARGETS <= conditional_targets


# ===========================================================================
# route_after_eval: the max-iteration guard is enforced — ``finish`` once
# completing the current iteration would reach max_iterations (iteration + 1 >=
# max_iterations), regardless of the refine target the evaluator chose.
# ===========================================================================


def test_guard_finishes_when_current_iteration_reaches_max() -> None:
    """The guard returns ``finish`` when iteration + 1 >= max_iterations.

    With a failing verdict and a refine target set, the router still aborts the
    loop because completing the current (0-based) iteration reaches the bound:
    iteration 2 of max 3 -> 2 + 1 >= 3.
    """
    state = {
        "evaluation": {"verdict_success": False},
        "iteration": 2,
        "max_iterations": 3,
        "refine_stage": "dag",
    }
    assert route_after_eval(state) == "finish"


def test_guard_finishes_when_current_iteration_exceeds_max() -> None:
    """The guard also finishes when the count has run past the bound."""
    state = {
        "evaluation": {"verdict_success": False},
        "iteration": 5,
        "max_iterations": 3,
        "refine_stage": "operationalize",
    }
    assert route_after_eval(state) == "finish"


def test_guard_routes_to_refine_target_before_the_bound() -> None:
    """Below the bound and failing, the router honours the refine target.

    iteration 0 of max 3 -> 0 + 1 < 3, so the loop continues to the evaluator's
    chosen target rather than finishing (complements the guard assertion).
    """
    for target in _REFINE_TARGETS:
        state = {
            "evaluation": {"verdict_success": False},
            "iteration": 0,
            "max_iterations": 3,
            "refine_stage": target,
        }
        assert route_after_eval(state) == target


def test_passing_verdict_finishes_regardless_of_iteration() -> None:
    """A passing verdict finishes immediately, before the guard is consulted."""
    state = {
        "evaluation": {"verdict_success": True},
        "iteration": 0,
        "max_iterations": 3,
        "refine_stage": "dag",
    }
    assert route_after_eval(state) == "finish"


# ===========================================================================
# orchestrate_node: default/invalid max_iterations handling (Req. 3.6, 3.7).
# The node seeds ``max_iterations`` from configuration, defaulting to 3 and
# recording a warning on the history trace when the value is invalid/missing.
# ===========================================================================


class _FakeSettings:
    """Minimal stand-in exposing just the ``max_iterations`` the node reads."""

    def __init__(self, max_iterations) -> None:
        self.max_iterations = max_iterations
        # Keep retrieval_k valid so the (skipped, memory-less) retrieval path is
        # never the thing under test here.
        self.retrieval_k = 5


def _patch_settings(monkeypatch: pytest.MonkeyPatch, max_iterations) -> None:
    """Patch ``get_sepinf_settings`` (resolved lazily inside the node)."""
    import app.services.ai.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_sepinf_settings",
        lambda: _FakeSettings(max_iterations),
    )


def _orchestrate_trace(delta: dict) -> dict:
    """Extract the single orchestrate history trace from the node's delta."""
    history = delta["history"]
    assert len(history) == 1
    return history[0]


def test_orchestrate_seeds_configured_valid_max_iterations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid configured ``max_iterations`` is seeded verbatim, no warning (Req. 3.6)."""
    _patch_settings(monkeypatch, 7)

    delta = orchestrate_node({"campaign_id": "c1"}, memory=None, session=None)

    assert delta["max_iterations"] == 7
    assert "warning" not in _orchestrate_trace(delta)


def test_orchestrate_defaults_to_three_when_settings_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the config cannot be read at all, the node applies default 3 (Req. 3.6).

    Simulates an absent/unreadable configuration by making the settings accessor
    raise; the node must fall back to the default of 3 and record a warning
    (Req. 3.7).
    """
    import app.services.ai.config as config_module

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(config_module, "get_sepinf_settings", _boom)

    delta = orchestrate_node({"campaign_id": "c1"}, memory=None, session=None)

    assert delta["max_iterations"] == 3
    assert "warning" in _orchestrate_trace(delta)


@pytest.mark.parametrize(
    "invalid_value",
    [
        0,  # < 1
        -5,  # < 1
        "3",  # non-integer (string)
        3.0,  # non-integer (float)
        True,  # bool is an int subclass but must be rejected
        None,  # absent/None
    ],
)
def test_orchestrate_defaults_and_warns_on_invalid_max_iterations(
    monkeypatch: pytest.MonkeyPatch, invalid_value
) -> None:
    """Invalid/non-integer/<1 ``max_iterations`` -> default 3 + warning (Req. 3.7).

    Each invalid shape (less-than-1, non-integer type, bool, or absent) must be
    replaced with the default of 3 and recorded as a warning on the run trace so
    the operator sees the configuration value was overridden.
    """
    _patch_settings(monkeypatch, invalid_value)

    delta = orchestrate_node({"campaign_id": "c1"}, memory=None, session=None)

    assert delta["max_iterations"] == 3
    trace = _orchestrate_trace(delta)
    assert "warning" in trace, f"expected a warning for {invalid_value!r}"
    assert trace["max_iterations"] == 3


def test_orchestrate_default_matches_graph_module_constant() -> None:
    """The applied default is the module's documented guard default of 3 (Req. 3.6)."""
    assert graph_module._DEFAULT_MAX_ITERATIONS == 3
