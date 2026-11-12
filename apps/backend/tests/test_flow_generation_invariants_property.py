"""Property test for the Stage 3 attack flow invariants (task 14.3).

# Feature: stix-db-single-source-ai-pipeline, Property 18: O attack flow proposto é acíclico, seus nós são técnicas da campanha e técnicas sem comando de respaldo viram lacunas, nunca comando inventado

Validates: Requirements 11.1, 11.3, 11.4, 12.4

This file fuses the former Properties 18 and 19 into a single property, and pins
two related guarantees as concrete (example-based) cases in the same module:

* **Property 18 (fused 18+19).** For an arbitrary campaign technique set and an
  arbitrary ``command_lookup`` predicate, the **real** :class:`FlowAgent`
  is driven with a faked LLM that returns a graph over a *subset* of the
  campaign techniques. The produced :class:`ProducedFlow` is asserted to satisfy
  the structural invariants the real agent assembles:

  1. the produced graph is **acyclic** (the agent rejects cycles during
     assembly, so anything returned is acyclic — Req. 11.1);
  2. the node technique set is a **subset** of the campaign techniques
     (the fake LLM only ever proposes campaign techniques — Req. 11.3);
  3. ``ProducedFlow.gaps`` equals **exactly** the set of campaign-technique nodes
     for which ``command_lookup`` returns ``False`` — gaps are derived by the
     real ``_collect_gaps`` from the injected predicate (Req. 11.4);
  4. **no node is assigned a command without backing** — the agent never invents
     a command; a backing-less technique becomes a gap, never a node-attached
     command (Req. 11.4, 12.4).

* **ex-Property 20 (concrete).** Every operationalized command references an
  **existing** attack flow node: the real :class:`AiOperationalizer` is driven with a
  faked LLM over the attack flow produced above, and each produced ability's technique
  is asserted to correspond to a node of that attack flow (Req. 5.3).

* **ex-Property 6 (concrete).** The ``Agent_Input`` composes identity + techniques
  from the ``Campaign_Overview`` (read through the agent's ``attack_reader``)
  with the items from ``ExperienceMemory.retrieve``: the captured
  ``agent_input`` dict carries both the ``attack_context`` from the overview and
  the ``retrieved_experience`` list (Req. 5.1, 10.6).

Isolation: everything runs on fakes — a scripted/computed fake chat model, an
injected ``command_lookup`` predicate, a fake ``ExperienceMemory`` exposing only
``retrieve`` and a fake ``attack_reader`` callable. No network, no SEPINF
endpoint, no real LLM. An ephemeral SQLite ``Session`` is stood up the way the
sibling AI property/unit tests do, even though the fused property drives the
agent through injected fakes rather than the database.

_Requirements: 11.1, 11.3, 11.4, 12.4, 5.1, 5.3, 10.6_
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Callable

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.enums import FlowOrigin, TranslationSource
from app.services.ai.agents.flow_agent import FlowAgent
from app.services.ai.agents.op_agent import AiOperationalizer
from app.services.flow.flow_view import FlowEdgeView, FlowNodeView, FlowView
from app.services.flow.generator import ProducedFlow, StructuralModeling
from app.services.flow.generator import RelationshipElement, TechniqueElement


# ---------------------------------------------------------------------------
# Ephemeral SQLite Session (same shape as the sibling AI property/unit tests).
# ---------------------------------------------------------------------------


def _make_session_factory() -> tuple[sessionmaker[Session], str]:
    """Create an ephemeral SQLite engine and return ``(factory, tmp_dir)``."""
    tmp_dir = tempfile.mkdtemp(prefix="flow_invariants_prop_")
    db_path = Path(tmp_dir) / "flow_invariants.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
    import_models()
    Base.metadata.create_all(bind=engine)

    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return factory, tmp_dir


# ---------------------------------------------------------------------------
# Fakes — a chat model computed from the drawn graph, no network / no real LLM.
# ---------------------------------------------------------------------------


class _FakeReply:
    """A minimal chat reply exposing ``.content`` like a LangChain message."""

    def __init__(self, content: str) -> None:
        self.content = content


class _GraphChat:
    """Fake chat model whose ``invoke`` always returns a fixed graph JSON.

    Mirrors the single surface the agents use (``chat.invoke(...) -> reply`` with
    ``reply.content`` a string). It returns the exact ordered-attack flow JSON object the
    agent's parser expects (``{"nodes": [...], "edges": [{"de", "para"}]}``), so
    the **real** :class:`FlowAgent` assembles/validates it end to end.
    """

    def __init__(self, nodes: list[dict], edges: list[dict]) -> None:
        self._payload = json.dumps({"nodes": nodes, "edges": edges})

    def invoke(self, _messages: Any) -> _FakeReply:
        return _FakeReply(self._payload)


# ---------------------------------------------------------------------------
# Hypothesis strategy — a campaign technique set + an acyclic subgraph over a
# subset of it + a per-technique command-backing predicate.
# ---------------------------------------------------------------------------


@st.composite
def _campaign_flow_and_backing(draw) -> dict[str, Any]:
    """Draw a campaign technique set, an acyclic node subset + edges, and backing.

    Returns a dict with:

    * ``techniques`` — the campaign's ATT&CK technique ids (the Stage 1 modeling).
    * ``nodes`` — the LLM graph nodes, a *subset* of ``techniques`` (so the
      subset invariant is exercised), each carrying a ``node_id``/``technique_id``.
    * ``edges`` — acyclic edges (strictly earlier -> later in node order) so the
      real agent never rejects the drawn graph as cyclic.
    * ``backing`` — a ``technique_id -> bool`` map driving the injected
      ``command_lookup`` predicate; the expected gaps are the node techniques
      mapping to ``False``.
    """
    n_tech = draw(st.integers(min_value=1, max_value=8))
    techniques = [f"T{1000 + i}" for i in range(n_tech)]

    # The graph uses a (possibly proper) subset of the campaign techniques, in
    # order, so "nodes subset of campaign techniques" is genuinely exercised.
    n_nodes = draw(st.integers(min_value=1, max_value=n_tech))
    node_techs = techniques[:n_nodes]

    nodes = [
        {
            "node_id": f"n{i}",
            "technique_id": tech,
            "technique_name": f"Technique {tech}",
            "tactic": f"tactic-{i}",
        }
        for i, tech in enumerate(node_techs)
    ]

    # Acyclic by construction: every edge goes from an earlier node to a later
    # node in the node ordering, so a topological order always exists.
    edges: list[dict] = []
    for i in range(n_nodes):
        for j in range(i + 1, n_nodes):
            if draw(st.booleans()):
                edges.append({"de": f"n{i}", "para": f"n{j}"})

    # Each campaign technique is independently "backed" or not.
    backing = {tech: draw(st.booleans()) for tech in techniques}

    return {
        "techniques": techniques,
        "nodes": nodes,
        "edges": edges,
        "backing": backing,
    }


def _modeling_for(techniques: list[str]) -> StructuralModeling:
    """Build a Stage 1 :class:`StructuralModeling` over the campaign techniques."""
    return StructuralModeling(
        techniques=[
            TechniqueElement(
                technique_id=tech,
                technique_name=f"Technique {tech}",
                tactic=f"tactic-{i}",
                node_id=f"n{i}",
            )
            for i, tech in enumerate(techniques)
        ],
        relationships=[],
    )


def _has_cycle(node_ids: set[str], edges: list[dict[str, str]]) -> bool:
    """Return ``True`` iff the directed graph has a cycle (Kahn's algorithm).

    An independent check (not the agent's own) so the acyclicity assertion is a
    genuine property check over the produced content, not a tautology.
    """
    indegree: dict[str, int] = {nid: 0 for nid in node_ids}
    adjacency: dict[str, list[str]] = {nid: [] for nid in node_ids}
    for edge in edges:
        adjacency[edge["de"]].append(edge["para"])
        indegree[edge["para"]] += 1

    queue = [nid for nid, deg in indegree.items() if deg == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for neighbour in adjacency[current]:
            indegree[neighbour] -= 1
            if indegree[neighbour] == 0:
                queue.append(neighbour)
    return visited != len(node_ids)


# ===========================================================================
# Property 18 (fused 18+19): acyclic + nodes ⊆ campaign techniques + gaps +
# never an invented command.
# ===========================================================================


# Feature: stix-db-single-source-ai-pipeline, Property 18: O attack flow proposto é acíclico, seus nós são técnicas da campanha e técnicas sem comando de respaldo viram lacunas, nunca comando inventado
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(case=_campaign_flow_and_backing())
def test_property18_flow_invariants_acyclic_subset_gaps_no_invented_command(
    case: dict[str, Any],
) -> None:
    """The produced attack flow is acyclic, node-subset, gaps == unbacked, never invents.

    Drives the REAL :class:`FlowAgent` with a faked LLM returning a graph
    over a subset of the campaign techniques and a controlled ``command_lookup``
    predicate, then asserts the four fused invariants on the single produced
    :class:`ProducedFlow`:

    (a) the graph is acyclic (Req. 11.1);
    (b) its node technique set ⊆ the campaign techniques (Req. 11.3);
    (c) ``gaps`` == exactly the node techniques whose ``command_lookup`` is
        ``False`` — derived by the real agent (Req. 11.4);
    (d) no node carries a command field — the agent attaches no command, let
        alone an invented one; unbacked techniques surface as gaps (Req. 11.4,
        12.4).

    Validates: Requirements 11.1, 11.3, 11.4, 12.4
    """
    techniques: list[str] = case["techniques"]
    nodes: list[dict] = case["nodes"]
    edges: list[dict] = case["edges"]
    backing: dict[str, bool] = case["backing"]

    campaign_techniques = set(techniques)
    node_techniques = [n["technique_id"] for n in nodes]

    # The injected Command_Base gap probe: True iff the technique is backed.
    command_lookup: Callable[[str], bool] = lambda technique_id: backing.get(
        technique_id, False
    )

    factory, _db_tmp = _make_session_factory()
    session = factory()
    try:
        chat = _GraphChat(nodes, edges)
        agent = FlowAgent(chat=chat, command_lookup=command_lookup)

        produced = agent.generate("ai-flow-invariants", _modeling_for(techniques))

        # Exactly one gerado_ia ProducedFlow is produced.
        assert isinstance(produced, list) and len(produced) == 1
        flow = produced[0]
        assert isinstance(flow, ProducedFlow)
        assert flow.origin is FlowOrigin.AI_GENERATED

        content = flow.content
        structural_nodes = content["structural_nodes"]
        arestas = content["arestas"]

        produced_node_ids = {n["node_id"] for n in structural_nodes}
        produced_node_techniques = [
            n["technique_id"] for n in structural_nodes if n.get("technique_id")
        ]

        # (a) Acyclic — verified independently of the agent's own cycle check.
        assert not _has_cycle(produced_node_ids, arestas)

        # (b) Node technique set ⊆ campaign techniques.
        assert set(produced_node_techniques) <= campaign_techniques

        # (c) gaps == exactly the node techniques with no backing command.
        expected_gaps = {
            tech for tech in node_techniques if not backing.get(tech, False)
        }
        assert set(flow.gaps) == expected_gaps
        # Every gap is a genuine campaign-technique node (never fabricated).
        assert set(flow.gaps) <= set(produced_node_techniques)
        # No duplicates: each gap technique recorded once.
        assert len(flow.gaps) == len(set(flow.gaps))

        # (d) No node is assigned a command — the agent never invents one; the
        # unbacked techniques are surfaced as gaps, not as node-attached commands.
        command_bearing_keys = {"command", "commands", "comando", "executors"}
        for node in structural_nodes:
            assert command_bearing_keys.isdisjoint(node.keys())
    finally:
        session.close()
        factory.kw["bind"].dispose()


# ===========================================================================
# ex-Property 20 (concrete): every operationalized command references an
# EXISTING attack flow node.
# ===========================================================================


def _flow_view_from_produced(campaign: str, produced: ProducedFlow) -> FlowView:
    """Build a :class:`FlowView` over the produced attack flow for the op agent to walk."""
    structural_nodes = produced.content["structural_nodes"]
    return FlowView(
        campaign_id=campaign,
        name="AI attack flow invariants",
        curated=False,
        can_advance=True,
        nodes=[
            FlowNodeView(
                node_id=node["node_id"],
                technique_id=node.get("technique_id"),
                technique_name=node.get("technique_name"),
                tactic=node.get("tactic"),
            )
            for node in structural_nodes
        ],
        edges=[
            FlowEdgeView(source=edge["de"], target=edge["para"])
            for edge in produced.content["arestas"]
        ],
    )


def test_ex_property20_operationalized_commands_reference_existing_flow_nodes() -> None:
    """Every operationalized ability's technique corresponds to an attack flow node.

    Produces an attack flow with the real :class:`FlowAgent` (faked LLM), builds the
    :class:`FlowView` from it, and drives the real :class:`AiOperationalizer` with
    a faked LLM over that attack flow. Each produced ability's technique is asserted to
    correspond to a node of the produced attack flow — an operationalized command can
    never reference a technique absent from the attack flow (Req. 5.3).
    """
    campaign = "ai-op-flow-nodes"
    nodes = [
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
    ]
    edges = [{"de": "n0", "para": "n1"}]

    flow_chat = _GraphChat(nodes, edges)
    flow_agent = FlowAgent(chat=flow_chat)
    produced = flow_agent.generate(
        campaign, _modeling_for(["T1190", "T1059"])
    )
    assert len(produced) == 1
    flow_view = _flow_view_from_produced(campaign, produced[0])
    flow_node_techniques = {
        node.technique_id for node in flow_view.nodes if node.technique_id
    }

    # A lab-internal IP so the host-derivation resolves without any I/O.
    internal_ip = "192.168.20.30"

    def _atomic_lookup(technique_id: str) -> list[dict]:
        pools: dict[str, list[dict]] = {
            "T1190": [
                {
                    "technique_id": "T1190",
                    "technique_name": "Exploit Public-Facing Application",
                    "ability_id": "ab-T1190",
                    "ability_name": "Exploit web app",
                    "tactic": "initial-access",
                    "name": "curl-probe",
                    "platform": "linux",
                    "command": f'curl "http://{internal_ip}:5000/?cmd=id"',
                }
            ],
            "T1059": [
                {
                    "technique_id": "T1059",
                    "technique_name": "Command and Scripting Interpreter",
                    "ability_id": "ab-T1059",
                    "ability_name": "Run script",
                    "tactic": "execution",
                    "name": "ssh-exec",
                    "platform": "linux",
                    "command": f"sshpass -p pw ssh root@{internal_ip} id",
                }
            ],
        }
        return pools.get((technique_id or "").strip(), [])

    # The op LLM chooses one pool command per technique (verbatim from the pool).
    class _ArrayChat:
        """Fake op chat returning a fixed JSON array of command strings."""

        def __init__(self, commands: list[str]) -> None:
            self._payload = json.dumps(commands)

        def invoke(self, _prompt: Any) -> _FakeReply:
            return _FakeReply(self._payload)

    op_chat = _ArrayChat(
        [
            f'curl "http://{internal_ip}:5000/?cmd=id"',
            f"sshpass -p pw ssh root@{internal_ip} id",
        ]
    )

    op_agent = AiOperationalizer(chat=op_chat, atomic_lookup=_atomic_lookup)
    result = op_agent.operationalize(campaign, flow_view)

    assert result.origin is TranslationSource.AI_GENERATED
    assert result.abilities is not None and result.abilities.has_abilities

    # Every produced ability's technique corresponds to an existing attack flow node.
    for ability in result.abilities.abilities:
        assert ability.technique_id in flow_node_techniques


# ===========================================================================
# ex-Property 6 (concrete): the Agent_Input composes identity+techniques from
# the Campaign_Overview with the items from ExperienceMemory.retrieve.
# ===========================================================================


class _FakeExperienceMemory:
    """Fake ``ExperienceMemory`` exposing only the ``retrieve`` surface used.

    Returns a fixed list of retrieval hits (shaped like the real
    :meth:`ExperienceMemory.retrieve` rows) so the agent's ``agent_input`` can be
    asserted to carry the retrieved experience.
    """

    def __init__(self, hits: list[dict[str, Any]]) -> None:
        self._hits = hits
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self, *, campaign_id: str, query: str, k: int = 5
    ) -> list[dict[str, Any]]:
        self.calls.append({"campaign_id": campaign_id, "query": query, "k": k})
        return list(self._hits)


def test_ex_property6_agent_input_composes_overview_and_retrieved_experience() -> None:
    """The captured ``agent_input`` carries the overview context + retrieved experience.

    Drives the real :class:`FlowAgent` with a faked LLM, an injected
    ``attack_reader`` modelling the DB-backed ``Campaign_Overview`` read, and a
    fake :class:`ExperienceMemory`; a ``trace_sink`` captures the single
    ``AgentTrace``. The ``agent_input`` dict is asserted to compose BOTH the
    ``attack_context`` from the overview AND the ``retrieved_experience`` list
    from ``ExperienceMemory.retrieve`` (Req. 5.1, 10.6).
    """
    campaign = "ai-agent-input-compose"
    techniques = ["T1190", "T1059"]

    nodes = [
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
    ]
    edges = [{"de": "n0", "para": "n1"}]
    chat = _GraphChat(nodes, edges)

    # The Campaign_Overview identity + techniques, as returned by the DB-backed
    # attack reader (tool_read_attack / the flow agent's attack_reader).
    overview = {
        "attack": {
            "name": "Operation Example",
            "campaign": campaign,
            "techniques": techniques,
        }
    }
    attack_reads: list[str] = []

    def _attack_reader(slug: str) -> dict:
        attack_reads.append(slug)
        return overview

    retrieved = [
        {
            "content": "Known-good shape for T1190 against a web app.",
            "metadata": {"verdict_success": True},
            "campaign_id": "other-campaign",
            "stage": "stage_3_flow",
            "outcome": "sucesso",
        },
        {
            "content": "T1059 script execution worked on linux targets.",
            "metadata": {"verdict_success": True},
            "campaign_id": "another-campaign",
            "stage": "stage_3_flow",
            "outcome": "sucesso",
        },
    ]
    memory = _FakeExperienceMemory(retrieved)

    trace_sink: list[dict] = []
    agent = FlowAgent(
        chat=chat,
        memory=memory,
        attack_reader=_attack_reader,
        trace_sink=trace_sink,
    )

    produced = agent.generate(campaign, _modeling_for(techniques))
    assert len(produced) == 1

    # The overview was read and the experience was retrieved.
    assert attack_reads == [campaign]
    assert len(memory.calls) == 1
    assert memory.calls[0]["campaign_id"] == campaign

    # Exactly one AgentTrace captured, carrying the composed Agent_Input.
    assert len(trace_sink) == 1
    agent_input = trace_sink[0]["agent_input"]

    # Composition: the attack_context from the Campaign_Overview is present with
    # the identity + techniques.
    assert agent_input["attack_context"] == overview
    assert agent_input["attack_context"]["attack"]["campaign"] == campaign
    assert agent_input["attack_context"]["attack"]["techniques"] == techniques

    # Composition: the retrieved_experience items from ExperienceMemory.retrieve
    # are carried alongside the overview context.
    assert agent_input["retrieved_experience"] == retrieved

    # The modeling (Stage 1 identity + techniques) is also carried.
    modeling_techniques = [
        t["technique_id"] for t in agent_input["modeling"]["techniques"]
    ]
    assert modeling_techniques == techniques
