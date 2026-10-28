"""Unit tests for the Stage 3 ``AiDagGenerator`` with a faked LLM (task 10.5).

These example-based unit tests exercise :class:`AiDagGenerator`
(``app/services/ai/agents/dag_agent.py``) end to end with **fakes only** — no
network, no SEPINF endpoint, no database, no real LLM. A fake chat model whose
``invoke`` returns a scripted sequence of textual responses drives the agent's
parse / validate / assemble path and, critically, its one-repair contract
(Req. 2.1, 2.4, 14.3, 14.4):

1. **Fixed valid JSON → ``gerado_ia`` DAG.** A fake chat returning a single,
   well-formed ``{"nodes": [...], "edges": [...]}`` JSON object yields exactly one
   :class:`~app.services.dag.generator.ProducedDag` stamped
   ``origin=gerado_ia`` (:class:`~app.models.enums.DagOrigin.AI_GENERATED`), with
   the curation-compatible ``content`` shape
   (``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``). The LLM is
   consulted exactly once (no repair needed) — Req. 2.1, 2.4.

2. **Malformed → repair → valid.** The first ``invoke`` returns unparseable text
   and the second (the single permitted repair re-prompt) returns valid JSON. The
   agent recovers and returns one ``gerado_ia`` ``ProducedDag`` after **exactly
   two** invocations — Req. 14.3.

3. **Malformed → repair → still malformed.** Both ``invoke`` calls return
   unparseable text. After the one repair the agent raises
   :class:`~app.services.ai.agents.dag_agent.DagGenerationError` naming the failing
   node (``"dag"``), having invoked the LLM **exactly twice**, and returns / yields
   **no** (invalid) ``ProducedDag`` for persistence — Req. 14.4.

Isolation note: the agent is built with only a fake ``chat`` (no ``memory``, no
``attack_reader``), so experience retrieval and ATT&CK context degrade to empty
with no I/O. The fake chat exposes only the single surface the agent uses
(``chat.invoke(messages) -> reply`` with ``reply.content`` a string) and records
every call so the invocation count is asserted directly.

_Requirements: 2.1, 2.4, 14.3, 14.4_
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.enums import DagOrigin
from app.services.ai.agents.dag_agent import AiDagGenerator, DagGenerationError
from app.services.dag.generator import ProducedDag, StructuralModeling
from app.services.stage.stage_service import RelationshipElement, TechniqueElement

_SLUG = "ai-dag-faked"


# ---------------------------------------------------------------------------
# Fakes — a scripted chat model returning a sequence of textual responses
# ---------------------------------------------------------------------------


class _FakeReply:
    """A minimal chat reply exposing ``.content`` like a LangChain message."""

    def __init__(self, content: str) -> None:
        self.content = content


class _ScriptedChat:
    """Fake chat model whose ``invoke`` returns scripted responses in order.

    Mirrors the single surface the agent uses (``chat.invoke(messages) -> reply``
    with ``reply.content`` a string). It records every ``messages`` payload so a
    test can assert exactly how many times the agent consulted the LLM (the
    one-repair contract hinges on this count), and returns the next scripted
    textual response per call. Running past the script is a hard error so an
    unexpected extra invoke surfaces loudly instead of hanging.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[Any] = []

    def invoke(self, messages: Any) -> _FakeReply:
        self.calls.append(messages)
        if len(self.calls) > len(self._responses):
            raise AssertionError(
                f"chat.invoke called {len(self.calls)} times but only "
                f"{len(self._responses)} responses were scripted"
            )
        return _FakeReply(self._responses[len(self.calls) - 1])


def _modeling() -> StructuralModeling:
    """A two-technique Structural Modeling (Stage 1) the agent plans a DAG from."""
    return StructuralModeling(
        techniques=[
            TechniqueElement(
                technique_id="T1190",
                technique_name="Exploit Public-Facing Application",
                tactic="initial-access",
                node_id="n0",
            ),
            TechniqueElement(
                technique_id="T1059",
                technique_name="Command and Scripting Interpreter",
                tactic="execution",
                node_id="n1",
            ),
        ],
        relationships=[
            RelationshipElement(source="n0", target="n1"),
        ],
    )


def _valid_graph_json() -> str:
    """A well-formed ordered-DAG JSON object the agent can parse end to end."""
    return json.dumps(
        {
            "nodes": [
                {
                    "node_id": "n0",
                    "technique_id": "T1190",
                    "technique_name": "Exploit Public-Facing Application",
                    "tactic": "initial-access",
                },
                {
                    "node_id": "n1",
                    "technique_id": "T1059",
                    "technique_name": "Command and Scripting Interpreter",
                    "tactic": "execution",
                },
            ],
            "edges": [{"de": "n0", "para": "n1"}],
        }
    )


# ---------------------------------------------------------------------------
# 1. Fixed valid JSON -> one gerado_ia ProducedDag (Req. 2.1, 2.4)
# ---------------------------------------------------------------------------


def test_fixed_valid_json_produces_gerado_ia_dag() -> None:
    """A single valid JSON response yields one ``gerado_ia`` ``ProducedDag``.

    No repair is needed, so the LLM is consulted exactly once; the produced DAG
    carries ``origin=gerado_ia`` and the curation-compatible ``content`` shape
    (``structural_nodes`` + ``arestas`` with ``de``/``para`` keys) — Req. 2.1, 2.4.
    """
    chat = _ScriptedChat([_valid_graph_json()])

    agent = AiDagGenerator(chat=chat)
    produced = agent.generate(_SLUG, _modeling())

    # Origin stamping on the generator and the produced DAG (Req. 2.4).
    assert agent.origin() is DagOrigin.AI_GENERATED

    # Exactly one DAG produced, with the gerado_ia origin and identifier.
    assert isinstance(produced, list)
    assert len(produced) == 1
    dag = produced[0]
    assert isinstance(dag, ProducedDag)
    assert dag.origin is DagOrigin.AI_GENERATED
    assert dag.identifier == _SLUG

    # The LLM was consulted exactly once — no repair path taken.
    assert len(chat.calls) == 1

    # Curation-compatible content shape (structural_nodes + arestas de/para).
    content = dag.content
    assert set(content) == {"structural_nodes", "arestas"}
    node_ids = [n["node_id"] for n in content["structural_nodes"]]
    assert node_ids == ["n0", "n1"]
    assert content["arestas"] == [{"de": "n0", "para": "n1"}]

    # Derived parent/child relations are consistent with the single edge.
    by_id = {n["node_id"]: n for n in content["structural_nodes"]}
    assert by_id["n0"]["child_nodes"] == ["n1"]
    assert by_id["n1"]["parent_nodes"] == ["n0"]


# ---------------------------------------------------------------------------
# 2. Malformed -> one repair -> valid (Req. 14.3)
# ---------------------------------------------------------------------------


def test_malformed_then_valid_recovers_after_one_repair() -> None:
    """A malformed first response is repaired on the single re-prompt (Req. 14.3).

    The first ``invoke`` returns unparseable text; the second (the one permitted
    repair) returns valid JSON. The agent recovers and returns a single
    ``gerado_ia`` ``ProducedDag`` after **exactly two** invocations.
    """
    chat = _ScriptedChat(["not valid json at all {{{", _valid_graph_json()])

    agent = AiDagGenerator(chat=chat)
    produced = agent.generate(_SLUG, _modeling())

    # Exactly two LLM passes: the initial attempt plus the single repair.
    assert len(chat.calls) == 2

    assert len(produced) == 1
    assert produced[0].origin is DagOrigin.AI_GENERATED
    assert [n["node_id"] for n in produced[0].content["structural_nodes"]] == [
        "n0",
        "n1",
    ]


# ---------------------------------------------------------------------------
# 3. Malformed -> one repair -> still malformed (Req. 14.4)
# ---------------------------------------------------------------------------


def test_malformed_then_still_malformed_raises_node_error() -> None:
    """Still-malformed output after the one repair raises a ``dag`` node error.

    Both ``invoke`` calls return unparseable text. After the single permitted
    repair the agent raises :class:`DagGenerationError` naming the failing node
    (``"dag"``), having consulted the LLM **exactly twice**, and produces **no**
    (invalid) ``ProducedDag`` for persistence — Req. 14.4.
    """
    chat = _ScriptedChat(["garbage one {{{", "garbage two }}}"])

    agent = AiDagGenerator(chat=chat)
    with pytest.raises(DagGenerationError) as excinfo:
        agent.generate(_SLUG, _modeling())

    # The error names the failing node for the Run_Service's bookkeeping.
    assert excinfo.value.node == "dag"
    # A parse-failure cause is recorded (no silent None).
    assert excinfo.value.cause

    # Exactly two LLM passes: the initial attempt plus the single repair, no more.
    assert len(chat.calls) == 2


def test_empty_structural_nodes_after_repair_raises_and_persists_nothing() -> None:
    """A syntactically valid but empty node set is still a failure (Req. 14.4).

    Both responses parse as JSON but carry no nodes, so the final validation
    rejects them. The agent raises a ``dag`` node error after exactly two passes
    and returns no ``ProducedDag`` — nothing invalid reaches persistence.
    """
    empty_graph = json.dumps({"nodes": [], "edges": []})
    chat = _ScriptedChat([empty_graph, empty_graph])

    agent = AiDagGenerator(chat=chat)
    with pytest.raises(DagGenerationError) as excinfo:
        agent.generate(_SLUG, _modeling())

    assert excinfo.value.node == "dag"
    assert len(chat.calls) == 2
