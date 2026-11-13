"""Property test for checkpoint resumability (task 4.2).

# Feature: ai-curation-agents, Property 4: Runs are resumable from the last
# checkpoint — for ANY run with a ``thread_id``, resuming from the last persisted
# checkpoint restores the StateGraph state byte-for-byte equal to that checkpoint
# and resumes execution at the node *following* that checkpoint.

Validates: Requirements 4.2, 4.3

Why a fake checkpointer
-----------------------
Requirement 4.3 is a property of the LangGraph checkpoint/resume contract, not of
PostgreSQL: a run invoked with a stable ``thread_id`` snapshots its
:class:`~app.services.ai.state.AgentState` after every node, and resuming by that
same ``thread_id`` restores the last snapshot and continues at the next node. The
production :func:`app.services.ai.checkpointer.get_checkpointer` builds a
``PostgresSaver`` over the project's single PostgreSQL, but the *resumability*
guarantee lives entirely in the saver/graph protocol, which both ``PostgresSaver``
and the in-memory ``MemorySaver`` implement identically. The test therefore drives
the real :class:`AgentState` schema and the real LangGraph ``StateGraph`` through
an in-memory ``MemorySaver`` (a FAKE checkpointer), keeping the property offline,
deterministic and free of any live Postgres while still exercising the exact
checkpoint contract the production saver obeys (Requirements 4.2, 4.3).

How the property is driven
--------------------------
A linear graph mirrors the design's node order
(``orchestrate -> operationalize -> flow -> environment -> execute -> evaluate``);
each node mutates the shared ``AgentState`` so a checkpoint carries non-trivial
content. Hypothesis generates, per example:

* a non-empty, unique ``thread_id`` (Requirement 4.2 — the checkpoint thread key);
* an arbitrary seed ``AgentState`` (campaign id, iteration, max_iterations and the
  appended ``history`` the reducer accumulates); and
* an interrupt point: a node after which the run is paused, standing in for "the
  last persisted checkpoint" of a partially-completed run.

The run is invoked with ``interrupt_after=[node]`` so it halts right after that
node, persisting a checkpoint. The test then captures:

1. the serialized bytes of the persisted channel values (via the saver's own
   serializer), and the ``next`` node recorded in the checkpoint; and
2. resumes the run with ``invoke(None, config)`` — the LangGraph idiom for "resume
   from the last checkpoint under this ``thread_id``".

The property asserts:

* **Byte-for-byte restoration (Req. 4.3).** The state read back for the
  ``thread_id`` *before* resuming serializes to exactly the bytes persisted at the
  interrupt — the restored checkpoint reproduces the persisted state with no drift.
* **Resume at the following node (Req. 4.3).** The checkpoint's ``next`` is exactly
  the node that follows the interrupt node in the graph order, and after resuming
  the executed trace is precisely the suffix of nodes from that following node
  onward (the already-executed prefix is *not* re-run).
* **Thread-key fidelity (Req. 4.2).** Resumption happens under the same non-empty
  ``thread_id`` and the finished run's history equals the full node order.

Everything runs in-process with no network, Docker or Postgres.
"""

from __future__ import annotations

import itertools

from hypothesis import given, settings
from hypothesis import strategies as st
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.services.ai.state import AgentState

# The design's node order (graph.py): orchestrate -> operationalize -> flow ->
# environment -> execute -> evaluate. Mirrored here so the property exercises the
# real sequence a production run checkpoints through.
_NODE_ORDER: list[str] = [
    "orchestrate",
    "operationalize",
    "flow",
    "environment",
    "execute",
    "evaluate",
]


def _make_node(name: str):
    """Build a node that records its own name in the append-only ``history``.

    Each node appends a single trace entry through the real ``AgentState``
    ``history`` reducer (``Annotated[list[dict], add]``), so the accumulated
    ``history`` is exactly the ordered list of executed nodes — the observable
    signal for "which nodes ran before vs. after the checkpoint".
    """

    def _node(state: AgentState) -> dict:
        return {"history": [{"node": name, "iteration": state.get("iteration", 0)}]}

    return _node


def _build_graph(checkpointer):
    """Compile the linear AgentState graph over the given (fake) checkpointer."""
    graph = StateGraph(AgentState)
    for name in _NODE_ORDER:
        graph.add_node(name, _make_node(name))
    graph.add_edge(START, _NODE_ORDER[0])
    for earlier, later in itertools.pairwise(_NODE_ORDER):
        graph.add_edge(earlier, later)
    graph.add_edge(_NODE_ORDER[-1], END)
    return graph


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty thread ids (Requirement 4.2: the checkpoint thread key is non-empty).
_thread_ids = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=24,
)

# Interrupt after any node EXCEPT the last: there must be a "following node" at
# which to resume (Requirement 4.3).
_interrupt_nodes = st.sampled_from(_NODE_ORDER[:-1])


def _seed_states() -> st.SearchStrategy[dict]:
    """Arbitrary but schema-valid seed ``AgentState`` payloads."""
    return st.fixed_dictionaries(
        {
            "campaign_id": st.text(min_size=1, max_size=20),
            "iteration": st.integers(min_value=0, max_value=9),
            "max_iterations": st.integers(min_value=1, max_value=10),
        }
    )


def _following_node(interrupt_node: str) -> str:
    """The node that executes immediately after ``interrupt_node`` in the order."""
    return _NODE_ORDER[_NODE_ORDER.index(interrupt_node) + 1]


# ---------------------------------------------------------------------------
# Property 4: resume restores the last checkpoint byte-for-byte and continues at
# the following node (Requirements 4.2, 4.3).
#
# ``deadline=None``: compiling a fresh StateGraph and running it twice per example
# is comfortably fast but variable on a loaded machine; the property's isolation
# (a fresh MemorySaver + graph per example) is what matters, so the per-example
# deadline is lifted rather than risking flaky timing failures.
# ---------------------------------------------------------------------------
# Feature: ai-curation-agents, Property 4: Runs are resumable from the last
# checkpoint.
@settings(max_examples=40, deadline=None)
@given(
    thread_id=_thread_ids,
    seed=_seed_states(),
    interrupt_node=_interrupt_nodes,
)
def test_property4_resume_restores_last_checkpoint_and_continues(
    thread_id: str, seed: dict, interrupt_node: str
) -> None:
    """Resuming a run by ``thread_id`` restores the persisted state and continues.

    # Feature: ai-curation-agents, Property 4: Runs are resumable from the last
    # checkpoint.
    Validates: Requirements 4.2, 4.3
    """
    # FAKE checkpointer: the in-memory saver implements the same checkpoint/resume
    # protocol the production PostgresSaver does (Requirements 4.2, 4.3), so the
    # resumability property is exercised without any live PostgreSQL.
    checkpointer = MemorySaver()
    app = _build_graph(checkpointer).compile(
        checkpointer=checkpointer,
        interrupt_after=[interrupt_node],
    )

    # Requirement 4.2: a run is invoked under a stable, non-empty thread_id that is
    # the checkpoint thread key.
    assert thread_id != ""
    config = {"configurable": {"thread_id": thread_id}}

    # Run until the interrupt, persisting a checkpoint after ``interrupt_node``.
    app.invoke(dict(seed), config)

    # --- Capture the persisted checkpoint (the "last checkpoint"). ---
    # The raw checkpoint tuple carries the serialized channel values; the state
    # snapshot carries the resolved ``values`` and the ``next`` node(s) to run.
    persisted_tuple = checkpointer.get_tuple(config)
    assert persisted_tuple is not None, "a checkpoint must exist for the thread_id"
    persisted_snapshot = app.get_state(config)

    following = _following_node(interrupt_node)
    # The checkpoint records the node(s) to run next: exactly the following node
    # (Requirement 4.3 — resume at the node following the checkpoint).
    assert tuple(persisted_snapshot.next) == (following,)

    # Serialize the persisted channel values with the saver's own serializer so
    # "byte-for-byte equal" is asserted against the exact wire form the checkpoint
    # stores (Requirement 4.3).
    persisted_bytes = checkpointer.serde.dumps_typed(persisted_tuple.checkpoint["channel_values"])

    # Nodes executed up to and including the interrupt node: the prefix.
    executed_prefix = _NODE_ORDER[: _NODE_ORDER.index(interrupt_node) + 1]
    restored_history = [h["node"] for h in persisted_snapshot.values["history"]]
    assert restored_history == executed_prefix

    # --- Restore: read the state back for the thread_id (no new invocation). ---
    # Re-reading the checkpoint for the thread_id and serializing must reproduce
    # the SAME bytes — the restore is byte-for-byte faithful to what was persisted
    # (Requirement 4.3).
    reread_tuple = checkpointer.get_tuple(config)
    assert reread_tuple is not None
    reread_bytes = checkpointer.serde.dumps_typed(reread_tuple.checkpoint["channel_values"])
    assert reread_bytes == persisted_bytes, (
        "restored checkpoint is not byte-for-byte equal to the persisted one"
    )
    # The restored state continues to point at the following node.
    assert tuple(app.get_state(config).next) == (following,)

    # --- Resume from the last checkpoint under the same thread_id. ---
    # ``invoke(None, config)`` is LangGraph's resume idiom: no new input, continue
    # from the persisted checkpoint for this thread_id (Requirements 4.2, 4.3).
    final = app.invoke(None, config)

    final_history = [h["node"] for h in final["history"]]
    # The run completed the full node order exactly once, in order: the prefix was
    # NOT re-executed and the suffix ran starting at the following node.
    assert final_history == _NODE_ORDER
    # The suffix actually executed on resume is precisely the nodes from the
    # following node onward (Requirement 4.3).
    suffix = _NODE_ORDER[_NODE_ORDER.index(following) :]
    assert final_history[len(executed_prefix) :] == suffix

    # Non-history seed fields survived the checkpoint/resume unchanged.
    assert final["campaign_id"] == seed["campaign_id"]
    assert final["max_iterations"] == seed["max_iterations"]
