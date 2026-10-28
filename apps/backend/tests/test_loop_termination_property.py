"""Property test for improvement-loop termination (task 14.4).

# Feature: ai-curation-agents, Property 3: The improvement loop terminates — for
# ANY run, the number of iterations is at most ``max_iterations`` and the
# conditional edge routes to ``END`` either when the verdict passes or when the
# max-iteration guard is hit. The loop NEVER runs forever.

Validates: Requirements 3.4, 3.5

Why fake evaluator verdicts + a fake checkpointer
-------------------------------------------------
Termination is a property of the loop's wiring — the conditional edge after
``evaluate`` plus the guard in
:func:`app.services.ai.graph.route_after_eval` — not of what any stage agent
actually computes. So this property drives the REAL router
(:func:`app.services.ai.graph.route_after_eval`) and the REAL
:class:`~app.services.ai.state.AgentState` schema through a compiled LangGraph
``StateGraph``, while standing in FAKE stage/evaluate nodes that:

* produce a FAKE evaluator verdict (randomly passing/failing per iteration), and
* increment the short-term-memory ``iteration`` counter once per loop pass.

The checkpointer is an in-memory ``MemorySaver`` (a FAKE checkpointer): it
implements the same checkpoint contract the production ``PostgresSaver`` obeys,
so the loop is exercised fully offline — no network, Docker or Postgres.

Why the fake nodes own the ``iteration`` increment
--------------------------------------------------
``route_after_eval`` reads ``state["iteration"]`` and fires the guard when
``iteration + 1 >= max_iterations``. In production ``orchestrate_node`` SEEDS
``iteration`` (default 0) but the stage/evaluate nodes as written do NOT bump it;
the Run_Service (task 15) advances the counter per loop pass. To exercise the
termination property end-to-end here — without depending on task 15 — a fake
node increments ``iteration`` exactly once per traversal (one loop pass), which
is precisely the quantity the guard bounds. The router under test is the real
one; only the per-pass counter advance and the verdict are faked.

How the property is driven
--------------------------
A compiled graph mirrors the loop: a ``refine`` stage node (the single
refinement target) → an ``evaluate`` node → a conditional edge via the real
``route_after_eval`` mapping ``{"dag","operationalize","environment"} → refine``
and ``"finish" → END``. Hypothesis generates, per example:

* ``max_iterations`` ∈ [1, 12] (a reasonable configured range; Req. 3.6 default
  is 3 but any valid bound must terminate); and
* an infinite-ish stream of FAKE verdicts (a finite list, cycled) so the loop is
  driven by arbitrary passing/failing sequences — including the adversarial
  all-failing stream that would run forever without the guard.

The property asserts, for every example:

* **Bounded iterations (Req. 3.4).** The number of loop passes actually executed
  is ``≥ 1`` and ``≤ max_iterations`` — the guard caps a would-be-infinite
  all-failing run at exactly ``max_iterations`` passes.
* **Always reaches END (Req. 3.5).** The run returns (it does not hang): the
  compiled graph drains to ``END`` via ``route_after_eval`` returning
  ``"finish"`` — either because a verdict passed or because the guard fired.
* **Router contract.** Every routing decision the real ``route_after_eval``
  produced is one of the four registered edge keys, and a ``"finish"`` is emitted
  exactly when the verdict passed OR the guard was hit.

Everything runs in-process with a LangGraph recursion ceiling set WELL ABOVE
``max_iterations`` so that, if termination were broken, the test would surface it
as a recursion error rather than hang — the ceiling can only be reached when the
guard fails to stop an all-failing loop.
"""

from __future__ import annotations

from itertools import count
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.services.ai.graph import route_after_eval
from app.services.ai.state import AgentState

# The exact pt-BR finish key route_after_eval returns to end the loop, and the
# three refine-target keys it may return to loop back. Literals, so the test
# never trusts the implementation to decide the expected routing vocabulary.
_FINISH = "finish"
_REFINE_TARGETS = ("dag", "operationalize", "environment")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A reasonable configured max_iterations range. Req. 3.6 defaults to 3, but the
# guard must terminate for ANY valid bound, so we sweep a small range including
# the degenerate bound of 1 (a single pass then stop).
_max_iterations = st.integers(min_value=1, max_value=12)

# A non-empty finite verdict stream, cycled by the fake evaluate node. Includes
# the adversarial all-False stream (never passes → only the guard can stop it)
# and mixed/true streams (an early pass finishes before the guard).
_verdict_streams = st.lists(st.booleans(), min_size=1, max_size=16)


# ---------------------------------------------------------------------------
# Fake graph: real router + real AgentState, fake stage/evaluate nodes.
# ---------------------------------------------------------------------------


def _build_terminating_graph(checkpointer, verdicts: list[bool]):
    """Compile a loop graph driven by the REAL ``route_after_eval``.

    ``refine`` is the single refinement-target stage node; it records a trace per
    loop pass. ``evaluate`` writes a FAKE verdict pulled (cycled) from
    ``verdicts`` into ``state["evaluation"]``, sets a refine target, and advances
    the short-term-memory ``iteration`` counter the guard reads, then the
    conditional edge calls the real router.

    ``iteration`` semantics mirror production EXACTLY (see
    :func:`app.services.ai.graph.route_after_eval`): the counter the router reads
    is the **0-based index of the iteration just evaluated** — 0 on the first
    pass, 1 on the second, and so on — so the guard
    ``iteration + 1 >= max_iterations`` makes the loop run exactly
    ``1..max_iterations`` passes. The evaluate node therefore writes
    ``iteration = <0-based pass index>`` (``orchestrate`` seeds 0; the pass index
    is the number of evaluations already done). The refine node does NOT touch the
    counter — in production the counter is advanced per loop pass by the
    Run_Service, which the evaluate node stands in for here.

    The router maps a refine target back to ``refine`` and ``"finish"`` to
    ``END``; a closure records each router decision so the test can assert the
    routing contract per pass.
    """
    stream = count()
    router_decisions: list[str] = []

    def _refine_node(state: AgentState) -> dict:
        # A loop pass through the single refinement target. The counter is owned
        # by the evaluate node (below) to keep the guard's 0-based "iteration just
        # evaluated" semantics exact; the refine node only records the pass.
        return {"history": [{"node": "refine", "iteration": state.get("iteration", 0)}]}

    def _evaluate_node(state: AgentState) -> dict:
        # A FAKE evaluator verdict (randomly passing/failing per iteration),
        # cycled from the generated stream so arbitrary verdict sequences drive
        # the loop — including the all-failing stream that only the guard stops.
        pass_index = next(stream)  # 0-based index of the iteration being evaluated
        verdict = verdicts[pass_index % len(verdicts)]
        return {
            # The router reads the 0-based index of the iteration just evaluated,
            # exactly as production does (orchestrate seeds 0; the Run_Service
            # advances it per pass). This keeps route_after_eval's guard faithful.
            "iteration": pass_index,
            "evaluation": {"verdict_success": verdict},
            # The refine target the router falls back to on a failing verdict.
            "refine_stage": "operationalize",
            "history": [{"node": "evaluate", "verdict": verdict}],
        }

    def _recording_router(state: AgentState) -> str:
        decision = route_after_eval(state)
        router_decisions.append(decision)
        return decision

    graph = StateGraph(AgentState)
    graph.add_node("refine", _refine_node)
    graph.add_node("evaluate", _evaluate_node)
    graph.add_edge(START, "refine")
    graph.add_edge("refine", "evaluate")
    graph.add_conditional_edges(
        "evaluate",
        _recording_router,
        {
            "dag": "refine",
            "operationalize": "refine",
            "environment": "refine",
            "finish": END,
        },
    )
    app = graph.compile(checkpointer=checkpointer)
    return app, router_decisions


# ---------------------------------------------------------------------------
# Property 3 — the improvement loop terminates (Requirements 3.4, 3.5).
#
# ``deadline=None``: compiling a fresh StateGraph and running it to completion per
# example is fast but variable on a loaded machine; the property's isolation (a
# fresh MemorySaver + graph per example) is what matters, so the per-example
# deadline is lifted rather than risking flaky timing failures.
# ---------------------------------------------------------------------------
# Feature: ai-curation-agents, Property 3: The improvement loop terminates.
@settings(max_examples=250, deadline=None)
@given(
    max_iterations=_max_iterations,
    verdicts=_verdict_streams,
    thread_suffix=st.integers(min_value=0, max_value=1_000_000),
)
def test_property3_improvement_loop_terminates(
    max_iterations: int, verdicts: list[bool], thread_suffix: int
) -> None:
    """For any verdict stream and bound, iterations ≤ max_iterations and END wins.

    # Feature: ai-curation-agents, Property 3: The improvement loop terminates.
    Validates: Requirements 3.4, 3.5
    """
    # FAKE checkpointer: the in-memory saver implements the same checkpoint
    # contract the production PostgresSaver obeys, so the loop runs fully offline.
    checkpointer = MemorySaver()
    app, router_decisions = _build_terminating_graph(checkpointer, verdicts)

    config: dict[str, Any] = {
        "configurable": {"thread_id": f"loop-term-{thread_suffix}"},
        # Recursion ceiling WELL ABOVE max_iterations: a correctly-guarded loop
        # finishes in ≤ max_iterations passes and never approaches this limit. A
        # broken guard on an all-failing stream would blow past it and surface a
        # recursion error here rather than hang the test — so reaching the ceiling
        # is itself a termination failure, not a false negative.
        "recursion_limit": 4 * max_iterations + 50,
    }

    # If termination were broken this call would raise a recursion error (never an
    # infinite hang), because the ceiling above is finite and > max_iterations.
    final = app.invoke(
        {
            "campaign_id": "loop-term-campaign",
            "iteration": 0,
            "max_iterations": max_iterations,
        },
        config,
    )

    # --- Always reaches END (Req. 3.5). ---
    # The run returned a terminal state. The LAST router decision must be the
    # finish key — the only path the conditional edge maps to END.
    assert router_decisions, "the router must have been consulted at least once"
    assert router_decisions[-1] == _FINISH, (
        "the loop must end by routing to END (finish), got "
        f"{router_decisions[-1]!r}"
    )

    # Every router decision is one of the four registered edge keys — the real
    # route_after_eval never returns anything outside the mapping.
    for decision in router_decisions:
        assert decision in {_FINISH, *_REFINE_TARGETS}, (
            f"router returned an unmapped key {decision!r}"
        )
    # Exactly one finish is emitted, and it is the terminal decision: once the
    # router says finish the loop ends, so finish cannot appear mid-stream.
    assert router_decisions.count(_FINISH) == 1
    assert _FINISH not in router_decisions[:-1]

    # --- Bounded iterations (Req. 3.4). ---
    # One loop pass == one evaluate-node execution == one router consultation, so
    # the number of iterations the run actually executed is exactly the number of
    # router decisions. (``final["iteration"]`` is the 0-based index of the LAST
    # iteration evaluated, i.e. passes - 1 — see the guard's semantics.)
    passes = len(router_decisions)
    assert final["iteration"] == passes - 1, (
        "the final iteration counter is the 0-based index of the last pass"
    )
    # At least one pass always runs (Req. 3.5: 1..max_iterations iterations) and
    # the guard caps the count at max_iterations even for an all-failing stream.
    assert 1 <= passes <= max_iterations, (
        f"iterations {passes} must be within 1..{max_iterations}"
    )

    # --- The guard fired exactly when it should (Req. 3.4, 3.5). ---
    # Independently reconstruct the verdict each candidate pass WOULD see (the
    # evaluate node cycles the stream), over the whole admissible range the guard
    # allows (1..max_iterations passes → 0-based indices 0..max_iterations-1).
    candidate_verdicts = [verdicts[i % len(verdicts)] for i in range(max_iterations)]
    first_pass_index = next(
        (i for i, v in enumerate(candidate_verdicts) if v), None
    )
    if first_pass_index is not None:
        # A verdict passes at 0-based index `first_pass_index` WITHIN the bound →
        # route_after_eval returns finish on that pass, so the loop runs exactly
        # first_pass_index + 1 passes (the earlier passes all failed).
        assert passes == first_pass_index + 1, (
            "a passing verdict must finish the loop on exactly that pass"
        )
        assert passes <= max_iterations
    else:
        # No verdict passes within the bound → only the guard stops the loop. The
        # guard (iteration + 1 >= max_iterations) first fires when iteration ==
        # max_iterations - 1, i.e. on the max_iterations-th pass, so the loop runs
        # exactly max_iterations passes — the upper bound of Req. 3.4.
        assert passes == max_iterations, (
            "an all-failing stream must be stopped by the guard at max_iterations"
        )
    # The verdict the final (finishing) pass saw must justify the finish: either
    # it passed, or the guard was hit (it was the max_iterations-th pass).
    final_verdict = candidate_verdicts[passes - 1]
    assert final_verdict or passes == max_iterations, (
        "finish must be justified by a passing verdict or the max-iteration guard"
    )

    # Non-iteration seed fields survived the loop unchanged.
    assert final["campaign_id"] == "loop-term-campaign"
    assert final["max_iterations"] == max_iterations
