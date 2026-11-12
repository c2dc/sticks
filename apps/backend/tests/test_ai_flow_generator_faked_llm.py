"""Unit tests for the Stage 3 ``FlowAgent`` with a faked LLM (task 10.5).

These example-based unit tests exercise :class:`FlowAgent`
(``app/services/ai/agents/flow_agent.py``) end to end with **fakes only** — no
network, no SEPINF endpoint, no database, no real LLM. A fake chat model whose
``invoke`` returns a scripted sequence of textual responses drives the agent's
parse / validate / assemble path and, critically, its one-repair contract
(Req. 2.1, 2.4, 14.3, 14.4):

1. **Fixed valid JSON → ``gerado_ia`` attack flow.** A fake chat returning a single,
   well-formed ``{"nodes": [...], "edges": [...]}`` JSON object yields exactly one
   :class:`~app.services.flow.generator.ProducedFlow` stamped
   ``origin=gerado_ia`` (:class:`~app.models.enums.FlowOrigin.AI_GENERATED`), with
   the curation-compatible ``content`` shape
   (``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``). The LLM is
   consulted exactly once (no repair needed) — Req. 2.1, 2.4.

2. **Malformed → repair → valid.** The first ``invoke`` returns unparseable text
   and the second (the single permitted repair re-prompt) returns valid JSON. The
   agent recovers and returns one ``gerado_ia`` ``ProducedFlow`` after **exactly
   two** invocations — Req. 14.3.

3. **Malformed → repair → still malformed.** Both ``invoke`` calls return
   unparseable text. After the one repair the agent raises
   :class:`~app.services.ai.agents.flow_agent.FlowGenerationError` naming the failing
   node (``"flow"``), having invoked the LLM **exactly twice**, and returns / yields
   **no** (invalid) ``ProducedFlow`` for persistence — Req. 14.4.

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

from app.models.enums import FlowOrigin
from app.services.ai.agents.flow_agent import FlowAgent, FlowGenerationError
from app.services.attack_flow.model import AttackFlow, AttackFlowObject
from app.services.attack_flow.policy import FlowCandidate
from app.services.flow.generator import ProducedFlow, StructuralModeling
from app.services.flow.generator import RelationshipElement, TechniqueElement

_SLUG = "ai-flow-faked"


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
    """A two-technique Structural Modeling (Stage 1) the agent plans an attack flow from."""
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
    """A well-formed ordered-attack flow JSON object the agent can parse end to end."""
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
# 1. Fixed valid JSON -> one gerado_ia ProducedFlow (Req. 2.1, 2.4)
# ---------------------------------------------------------------------------


def test_fixed_valid_json_produces_gerado_ia_flow() -> None:
    """A single valid JSON response yields one ``gerado_ia`` ``ProducedFlow``.

    No repair is needed, so the LLM is consulted exactly once; the produced attack flow
    carries ``origin=gerado_ia`` and the curation-compatible ``content`` shape
    (``structural_nodes`` + ``arestas`` with ``de``/``para`` keys) — Req. 2.1, 2.4.
    """
    chat = _ScriptedChat([_valid_graph_json()])

    agent = FlowAgent(chat=chat)
    produced = agent.generate(_SLUG, _modeling())

    # Origin stamping on the generator and the produced attack flow (Req. 2.4).
    assert agent.origin() is FlowOrigin.AI_GENERATED

    # Exactly one attack flow produced, with the gerado_ia origin and identifier.
    assert isinstance(produced, list)
    assert len(produced) == 1
    flow = produced[0]
    assert isinstance(flow, ProducedFlow)
    assert flow.origin is FlowOrigin.AI_GENERATED
    assert flow.identifier == _SLUG

    # The LLM was consulted exactly once — no repair path taken.
    assert len(chat.calls) == 1

    # Curation-compatible content shape (structural_nodes + arestas de/para).
    content = flow.content
    # Attack Flow keys plus the legacy attack flow mirror.
    assert {"structural_nodes", "arestas", "objects", "flow_id"} <= set(content)
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
    ``gerado_ia`` ``ProducedFlow`` after **exactly two** invocations.
    """
    chat = _ScriptedChat(["not valid json at all {{{", _valid_graph_json()])

    agent = FlowAgent(chat=chat)
    produced = agent.generate(_SLUG, _modeling())

    # Exactly two LLM passes: the initial attempt plus the single repair.
    assert len(chat.calls) == 2

    assert len(produced) == 1
    assert produced[0].origin is FlowOrigin.AI_GENERATED
    assert [n["node_id"] for n in produced[0].content["structural_nodes"]] == [
        "n0",
        "n1",
    ]


# ---------------------------------------------------------------------------
# 3. Malformed -> one repair -> still malformed (Req. 14.4)
# ---------------------------------------------------------------------------


def test_malformed_then_still_malformed_raises_node_error() -> None:
    """Still-malformed output after the one repair raises a ``flow`` node error.

    Both ``invoke`` calls return unparseable text. After the single permitted
    repair the agent raises :class:`FlowGenerationError` naming the failing node
    (``"flow"``), having consulted the LLM **exactly twice**, and produces **no**
    (invalid) ``ProducedFlow`` for persistence — Req. 14.4.
    """
    chat = _ScriptedChat(["garbage one {{{", "garbage two }}}"])

    agent = FlowAgent(chat=chat)
    with pytest.raises(FlowGenerationError) as excinfo:
        agent.generate(_SLUG, _modeling())

    # The error names the failing node for the Run_Service's bookkeeping.
    assert excinfo.value.node == "flow"
    # A parse-failure cause is recorded (no silent None).
    assert excinfo.value.cause

    # Exactly two LLM passes: the initial attempt plus the single repair, no more.
    assert len(chat.calls) == 2


def test_empty_structural_nodes_after_repair_raises_and_persists_nothing() -> None:
    """A syntactically valid but empty node set is still a failure (Req. 14.4).

    Both responses parse as JSON but carry no nodes, so the final validation
    rejects them. The agent raises a ``flow`` node error after exactly two passes
    and returns no ``ProducedFlow`` — nothing invalid reaches persistence.
    """
    empty_graph = json.dumps({"nodes": [], "edges": []})
    chat = _ScriptedChat([empty_graph, empty_graph])

    agent = FlowAgent(chat=chat)
    with pytest.raises(FlowGenerationError) as excinfo:
        agent.generate(_SLUG, _modeling())

    assert excinfo.value.node == "flow"
    assert len(chat.calls) == 2


# ---------------------------------------------------------------------------
# 4. Three decision paths: exact / adapted / created (Req. 3.1-3.4)
# ---------------------------------------------------------------------------


class _FakeRetriever:
    def __init__(self, candidates: list[FlowCandidate], corpus: bool = True) -> None:
        self._candidates = candidates
        self._corpus = corpus

    def retrieve(self, **_kwargs: Any) -> list[FlowCandidate]:
        return list(self._candidates)

    def corpus_available(self) -> bool:
        return self._corpus


def _corpus_content(techniques: list[str]) -> dict:
    ids = [f"attack-action--{i}" for i in range(len(techniques))]
    objects = [
        AttackFlowObject(
            id=oid,
            type="attack-action",
            name=f"Doc {tech}",
            technique_id=tech,
            effect_refs=[ids[i + 1]] if i + 1 < len(ids) else [],
        )
        for i, (oid, tech) in enumerate(zip(ids, techniques))
    ]
    flow = AttackFlow(flow_id="attack-flow--doc", name="Doc", start_refs=ids[:1], objects=objects)
    return flow.model_dump(mode="json")


def _loader(techniques: list[str]):
    return lambda _flow_id: {"label": "Documented", "content": _corpus_content(techniques)}


def test_exact_path_copies_documented_flow_without_llm() -> None:
    chat = _ScriptedChat([])
    sink: list[dict] = []
    operationalization = {
        "abilities": {"abilities": [{"technique_id": "T1190", "executors": [{"id": 7, "command": "x"}]}]}
    }
    agent = FlowAgent(
        chat=chat,
        flow_retriever=_FakeRetriever([FlowCandidate(3, "Doc", frozenset({"T1190", "T1059"}))]),
        corpus_flow_loader=_loader(["T1190", "T1059"]),
        operationalization=operationalization,
        trace_sink=sink,
    )

    (produced,) = agent.generate(_SLUG, _modeling())

    assert chat.calls == []
    assert produced.provenance == {"kind": "exact", "references": [3], "similarity": 1.0}
    assert produced.content["provenance"]["kind"] == "exact"
    refs = {n["technique_id"]: n["command_ref"] for n in produced.content["structural_nodes"]}
    assert refs == {"T1190": 7, "T1059": None}
    assert produced.gaps == ["T1059"]
    assert sink[0]["processing"]["flow_decision"]["kind"] == "exact"


def test_exact_path_falls_back_to_adapted_when_flow_cannot_be_loaded() -> None:
    chat = _ScriptedChat([_valid_graph_json()])
    sink: list[dict] = []
    agent = FlowAgent(
        chat=chat,
        flow_retriever=_FakeRetriever([FlowCandidate(3, "Doc", frozenset({"T1190", "T1059"}))]),
        corpus_flow_loader=lambda _id: None,
        trace_sink=sink,
    )

    (produced,) = agent.generate(_SLUG, _modeling())

    assert len(chat.calls) == 1
    assert produced.provenance["kind"] == "adapted"
    assert sink[0]["processing"]["flow_decision"]["reason"] == "exact_flow_unavailable"


def test_adapted_path_calls_llm_with_references_and_records_provenance() -> None:
    chat = _ScriptedChat([_valid_graph_json()])
    agent = FlowAgent(
        chat=chat,
        flow_retriever=_FakeRetriever(
            [FlowCandidate(4, "Similar", frozenset({"T1190", "T1059", "T1078"}))]
        ),
        corpus_flow_loader=_loader(["T1190", "T1078", "T1059"]),
    )

    (produced,) = agent.generate(_SLUG, _modeling())

    assert len(chat.calls) == 1
    prompt = chat.calls[0][1]["content"]
    assert "Documented reference flows" in prompt
    assert produced.provenance["kind"] == "adapted"
    assert produced.provenance["references"] == [4]
    assert produced.provenance["similarity"] == pytest.approx(2 / 3)
    assert produced.content["provenance"]["references"] == [4]


def test_created_path_without_corpus_records_reason_and_empty_references() -> None:
    chat = _ScriptedChat([_valid_graph_json()])
    sink: list[dict] = []
    agent = FlowAgent(
        chat=chat, flow_retriever=_FakeRetriever([], corpus=False), trace_sink=sink
    )

    (produced,) = agent.generate(_SLUG, _modeling())

    assert produced.provenance == {"kind": "created", "references": [], "similarity": 0.0}
    assert sink[0]["processing"]["flow_decision"]["reason"] == "corpus_unavailable"


def test_created_path_with_corpus_but_no_similar_flow_has_no_reason() -> None:
    sink: list[dict] = []
    agent = FlowAgent(
        chat=_ScriptedChat([_valid_graph_json()]),
        flow_retriever=_FakeRetriever([], corpus=True),
        trace_sink=sink,
    )

    agent.generate(_SLUG, _modeling())

    assert sink[0]["processing"]["flow_decision"]["kind"] == "created"
    assert sink[0]["processing"]["flow_decision"]["reason"] is None


def test_retriever_failure_takes_created_path() -> None:
    class _Broken:
        def retrieve(self, **_kwargs: Any) -> list:
            raise RuntimeError("boom")

        def corpus_available(self) -> bool:
            return True

    agent = FlowAgent(chat=_ScriptedChat([_valid_graph_json()]), flow_retriever=_Broken())

    (produced,) = agent.generate(_SLUG, _modeling())

    assert produced.provenance["kind"] == "created"


# ---------------------------------------------------------------------------
# 5. Post-LLM validation: cycles are annotated, technique scope is enforced
# ---------------------------------------------------------------------------


def test_cyclic_llm_graph_is_annotated_without_a_second_llm_call() -> None:
    cyclic = json.loads(_valid_graph_json())
    cyclic["edges"].append({"de": "n1", "para": "n0"})
    chat = _ScriptedChat([json.dumps(cyclic)])
    sink: list[dict] = []

    (produced,) = FlowAgent(chat=chat, trace_sink=sink).generate(_SLUG, _modeling())

    assert len(chat.calls) == 1
    assert len(produced.content["cycles"]) == 1
    assert len(produced.content["back_edges"]) == 1
    assert sink[0]["processing"]["warnings"]


def test_technique_outside_campaign_triggers_the_single_repair() -> None:
    outside = json.loads(_valid_graph_json())
    outside["nodes"][1]["technique_id"] = "T9999"
    chat = _ScriptedChat([json.dumps(outside), _valid_graph_json()])

    (produced,) = FlowAgent(chat=chat).generate(_SLUG, _modeling())

    assert len(chat.calls) == 2
    assert {n["technique_id"] for n in produced.content["structural_nodes"]} == {
        "T1190",
        "T1059",
    }
