"""Stage 3 DAG agent: the ``AiDagGenerator`` implementation of the ``DagGenerator`` seam.

Hosts :class:`AiDagGenerator`, the AI-origin counterpart of
:class:`~app.services.dag.generator.CurationDagGenerator`. It plugs into the
exact same ``@runtime_checkable`` :class:`~app.services.dag.generator.DagGenerator`
Protocol (``origin()`` + ``generate(campaign, modeling)``) so ``DagService`` can
inject it in place of the curation generator with no downstream change
(Requirements 2.1, 2.7).

Unlike curation — which ignores ``modeling`` because the curated graph already
exists on disk — this agent **uses** the Structural Modeling (Stage 1): it builds
a prompt from the modeling plus the cross-campaign experience retrieved from the
RAG memory, asks the LLM for an **ordered technique graph**, validates and (on
malformed output) repairs the structure **exactly once**, and returns a single
:class:`~app.services.dag.generator.ProducedDag` stamped
``origin = DagOrigin.AI_GENERATED`` (``"gerado_ia"`` — Requirements 2.4, 2.5). The
produced ``content`` mirrors the curation format
(``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``) so the persisted
DAG is byte-compatible with the existing ``Dag`` entity and no schema migration is
needed.

Error handling follows Requirement 14.3/14.4: a single repair attempt is made when
the LLM output cannot be parsed into a valid DAG; if the output is still malformed
after that one repair, a :class:`DagGenerationError` is raised identifying the
failing node and the parse-failure cause, and **no** (invalid) ``ProducedDag`` is
returned for persistence.

Laziness (Requirement 1.7): the chat model, the experience memory and the
ATT&CK/STIX reader are all **injected** and resolved lazily; importing this module
opens no network connection, builds no LLM client, and touches no database. The
``make_chat_model`` import used by the (separate) LangGraph node wrapper — task
10.3 — is intentionally **not** performed here.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable, Optional

from app.models.enums import DagOrigin
from app.services.dag.generator import ProducedDag, StructuralModeling

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from sqlalchemy.orm import Session

    from app.models.domain import AgentStageOutput
    from app.services.ai.memory import ExperienceMemory
    from app.services.ai.state import AgentState

logger = logging.getLogger(__name__)

__all__ = [
    "AiDagGenerator",
    "DagGenerationError",
    "node",
    "persist_dag_stage_output",
]

# The agent asks the LLM for a graph, then performs AT MOST this many additional
# repair attempts on malformed output. Requirement 14.3 fixes it at exactly one
# repair: the initial attempt plus one repair == 2 total LLM passes.
_MAX_REPAIR_ATTEMPTS = 1

# Default cross-campaign experience fan-in for the RAG priming (bounded by the
# memory's own 1..100 guard).
_DEFAULT_RETRIEVAL_K = 5


class DagGenerationError(RuntimeError):
    """Raised when the LLM output is still unparseable after the one repair.

    Signals Requirement 14.4: after the single permitted repair attempt the LLM
    output still cannot be parsed into a valid DAG, so the failing node and the
    parse-failure cause are recorded on the exception and **no** invalid
    ``ProducedDag`` is produced for persistence. The ``node`` attribute names the
    failing agent node (``"dag"``) for the Run_Service's failure bookkeeping.
    """

    def __init__(self, message: str, *, node: str = "dag", cause: Optional[str] = None) -> None:
        super().__init__(message)
        self.node = node
        self.cause = cause


class AiDagGenerator:
    """AI implementation of the :class:`DagGenerator` seam (Requirements 2.1, 2.4, 2.5).

    Satisfies the ``@runtime_checkable`` ``DagGenerator`` Protocol structurally
    (``origin()`` + ``generate()``), with no explicit inheritance — exactly the
    point of the seam, so it is injectable wherever ``CurationDagGenerator`` is
    (Requirement 2.7).

    Collaborators are injected and resolved lazily so constructing the generator
    (and importing this module) performs no I/O (Requirement 1.7):

    * ``chat`` — a LangChain-style chat model (the lazily built
      :func:`app.services.ai.llm.make_chat_model` result) exposing
      ``invoke(messages)``; the node wrapper (task 10.3) supplies it.
    * ``memory`` — an :class:`~app.services.ai.memory.ExperienceMemory` for the
      cross-campaign RAG priming; when absent the agent proceeds with an empty
      experience set (graceful degrade, Requirement 5.7).
    * ``attack_reader`` — the read-only ATT&CK/STIX context tool
      (:func:`app.services.ai.tools.tool_read_attack`); optional extra context.
    """

    def __init__(
        self,
        *,
        chat: Any = None,
        memory: "Optional[ExperienceMemory]" = None,
        attack_reader: Optional[Callable[..., dict]] = None,
        retrieval_k: int = _DEFAULT_RETRIEVAL_K,
    ) -> None:
        self._chat = chat
        self._memory = memory
        self._attack = attack_reader
        self._retrieval_k = retrieval_k

    # ----- seam ---------------------------------------------------------------

    def origin(self) -> DagOrigin:
        """The origin stamped on produced DAGs: always ``gerado_ia`` (Req. 2.4, 2.5)."""
        return DagOrigin.AI_GENERATED

    def generate(self, campaign: str, modeling: StructuralModeling) -> list[ProducedDag]:
        """Produce the AI DAG for ``campaign`` from the Structural Modeling.

        Unlike :class:`CurationDagGenerator` (which ignores ``modeling``), this
        agent **uses** it: it builds a prompt from the modeling plus the retrieved
        cross-campaign experience, asks the LLM for an ordered technique graph,
        validates/repairs the structure exactly once, and returns a single
        :class:`ProducedDag` with ``origin = gerado_ia`` (Requirements 2.1, 2.4,
        2.5). The ``content`` mirrors the curation format so ``DagService``
        persists it as an AI-origin ``Dag`` with no schema change.

        Repair contract (Requirements 14.3, 14.4): if the first LLM response
        cannot be parsed into a valid DAG, exactly **one** repair attempt is made
        (re-prompting the model with the parse error). If the output is still
        invalid after that single repair, a :class:`DagGenerationError` is raised
        naming the failing node (``"dag"``) and the parse-failure cause, and no
        (invalid) ``ProducedDag`` is returned.
        """
        if self._chat is None:
            raise DagGenerationError(
                "AiDagGenerator requires a chat model to generate a DAG; none was "
                "injected.",
                node="dag",
                cause="missing_chat_model",
            )

        experience = self._retrieve_experience(campaign, modeling)
        attack_context = self._read_attack_context(campaign)

        # First attempt, then at most one repair that re-prompts with the parse
        # error (Requirement 14.3). ``last_cause`` carries the most recent
        # parse-failure cause into the node error if the repair also fails.
        last_cause: Optional[str] = None
        repair_hint: Optional[str] = None
        for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
            messages = self._build_messages(
                campaign,
                modeling,
                experience,
                attack_context,
                repair_hint=repair_hint,
            )
            raw = self._invoke_chat(messages)
            try:
                graph = self._parse_graph(raw)
                content = self._assemble_content(graph, modeling)
                self._validate_content(content)
            except _DagParseError as exc:
                last_cause = str(exc)
                # Feed the parse error back into the single repair pass so the
                # model can correct the malformed structure (Requirement 14.3).
                repair_hint = str(exc)
                logger.warning(
                    "AiDagGenerator parse/validation failed for campaign %s on "
                    "attempt %d/%d (%s).",
                    campaign,
                    attempt + 1,
                    _MAX_REPAIR_ATTEMPTS + 1,
                    exc,
                )
                continue

            label = self._resolve_label(graph, attack_context)
            return [
                ProducedDag(
                    origin=DagOrigin.AI_GENERATED,
                    identifier=campaign,
                    label=label,
                    content=content,
                )
            ]

        # Still malformed after the one permitted repair: record a node error and
        # do NOT return an invalid ProducedDag (Requirement 14.4).
        raise DagGenerationError(
            f"AiDagGenerator could not parse a valid DAG for campaign {campaign} "
            f"after one repair attempt.",
            node="dag",
            cause=last_cause,
        )

    # ----- collaborators ------------------------------------------------------

    def _retrieve_experience(
        self, campaign: str, modeling: StructuralModeling
    ) -> list[dict[str, Any]]:
        """Retrieve cross-campaign experience to prime the prompt (RAG).

        Degrades gracefully: with no injected memory (or on any retrieval
        unavailability, which the memory itself already swallows) the agent
        proceeds with an empty experience set (Requirement 5.7).
        """
        if self._memory is None:
            return []
        query = self._modeling_summary(modeling)
        try:
            return self._memory.retrieve(
                campaign_id=campaign, query=query, k=self._retrieval_k
            )
        except Exception as exc:  # noqa: BLE001 - never let RAG priming break generation
            logger.warning(
                "AiDagGenerator experience retrieval skipped for campaign %s "
                "(%s: %s); continuing with an empty experience set.",
                campaign,
                type(exc).__name__,
                exc,
            )
            return []

    def _read_attack_context(self, campaign: str) -> Optional[dict]:
        """Read the optional ATT&CK/STIX context via the injected reader tool.

        Tolerant: no reader injected, or any reader failure, yields ``None`` so
        the missing context never breaks generation.
        """
        if self._attack is None:
            return None
        try:
            return self._attack(campaign)
        except Exception as exc:  # noqa: BLE001 - optional context, never fatal
            logger.warning(
                "AiDagGenerator ATT&CK context read skipped for campaign %s "
                "(%s: %s).",
                campaign,
                type(exc).__name__,
                exc,
            )
            return None

    def _invoke_chat(self, messages: list[dict[str, str]]) -> str:
        """Invoke the injected chat model and return its textual content.

        Supports both the LangChain message-object return (``.content``) and a
        plain-string return, so a faked chat model in tests needs only to return
        text.
        """
        response = self._chat.invoke(messages)
        content = getattr(response, "content", response)
        if isinstance(content, list):
            # Some chat models return a list of content parts; join the text ones.
            parts = [
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            ]
            return "".join(parts)
        return content if isinstance(content, str) else str(content)

    # ----- prompt assembly ----------------------------------------------------

    def _build_messages(
        self,
        campaign: str,
        modeling: StructuralModeling,
        experience: list[dict[str, Any]],
        attack_context: Optional[dict],
        *,
        repair_hint: Optional[str] = None,
    ) -> list[dict[str, str]]:
        """Build the chat messages: a system contract + the modeling/RAG prompt.

        On a repair pass ``repair_hint`` carries the previous parse error so the
        model is explicitly told what to correct (Requirement 14.3).
        """
        system = (
            "You are the Stage 3 DAG planner for an adversary-emulation pipeline. "
            "Given the Structural Modeling (ATT&CK techniques and their "
            "relationships) and optional past experience, produce an ORDERED "
            "directed acyclic graph of techniques. Respond with ONLY a JSON object "
            'of the exact shape {"nodes": [{"node_id": str, "technique_id": str, '
            '"technique_name": str|null, "tactic": str|null}], "edges": '
            '[{"de": str, "para": str}]}. "de"/"para" reference node_id values. '
            "Do not include any prose, explanation or markdown fences."
        )
        user = self._build_prompt(campaign, modeling, experience, attack_context)
        if repair_hint:
            user = (
                f"{user}\n\nThe previous response could not be parsed into a valid "
                f"DAG. Error: {repair_hint}. Return a corrected JSON object with "
                "the exact shape described, and nothing else."
            )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _build_prompt(
        self,
        campaign: str,
        modeling: StructuralModeling,
        experience: list[dict[str, Any]],
        attack_context: Optional[dict],
    ) -> str:
        """Render the user prompt from the modeling + retrieved experience."""
        techniques = [
            {
                "node_id": t.node_id or t.technique_id,
                "technique_id": t.technique_id,
                "technique_name": t.technique_name,
                "tactic": t.tactic,
            }
            for t in modeling.techniques
        ]
        relationships = [
            {"source": r.source, "target": r.target} for r in modeling.relationships
        ]
        lines = [
            f"Campaign: {campaign}",
            "Structural Modeling techniques (Stage 1):",
            json.dumps(techniques, ensure_ascii=False),
            "Structural Modeling relationships (Stage 1):",
            json.dumps(relationships, ensure_ascii=False),
        ]
        if experience:
            snippets = [
                {"content": hit.get("content"), "outcome": hit.get("outcome")}
                for hit in experience
            ]
            lines.append("Relevant experience from OTHER campaigns:")
            lines.append(json.dumps(snippets, ensure_ascii=False))
        if attack_context and attack_context.get("attack"):
            lines.append("ATT&CK/STIX context (read-only):")
            lines.append(json.dumps(attack_context["attack"], ensure_ascii=False))
        lines.append(
            "Produce the ordered technique DAG as the JSON object described. "
            "Prefer the modeling's techniques and relationships; refine ordering "
            "and fill gaps using the experience and ATT&CK context."
        )
        return "\n".join(lines)

    @staticmethod
    def _modeling_summary(modeling: StructuralModeling) -> str:
        """A compact text summary of the modeling used as the RAG query."""
        ids = [t.technique_id for t in modeling.techniques if t.technique_id]
        return "DAG planning for techniques: " + ", ".join(ids) if ids else (
            "DAG planning for an empty technique set"
        )

    # ----- parsing / validation / assembly ------------------------------------

    @staticmethod
    def _parse_graph(raw: str) -> dict[str, Any]:
        """Parse the LLM text into a ``{"nodes": [...], "edges": [...]}`` dict.

        Tolerant of a surrounding markdown code fence; strict about the top-level
        shape. Raises :class:`_DagParseError` on any malformed output so the
        single repair path (Requirement 14.3) can re-prompt.
        """
        text = (raw or "").strip()
        if not text:
            raise _DagParseError("empty LLM response")

        # Strip a leading/trailing ```json ... ``` fence if present.
        if text.startswith("```"):
            text = text.strip("`")
            # drop a leading language tag like "json\n"
            newline = text.find("\n")
            if newline != -1 and " " not in text[:newline]:
                text = text[newline + 1 :]
            text = text.strip()

        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise _DagParseError(f"response is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise _DagParseError("top-level JSON must be an object")
        nodes = data.get("nodes")
        edges = data.get("edges", [])
        if not isinstance(nodes, list):
            raise _DagParseError("'nodes' must be a list")
        if not isinstance(edges, list):
            raise _DagParseError("'edges' must be a list")
        return {"nodes": nodes, "edges": edges}

    def _assemble_content(
        self, graph: dict[str, Any], modeling: StructuralModeling
    ) -> dict:
        """Assemble the persistable ``content`` in the curation-compatible format.

        Produces ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}`` —
        the exact shape of :meth:`CurationDagGenerator._assemble_content` and the
        ``GET /api/campaigns/{campaign}/dag`` contract — so the persisted DAG is
        schema-compatible regardless of origin. ``parent_nodes``/``child_nodes``
        are derived from the edges so a consumer sees the same structural shape as
        a curated DAG. Raises :class:`_DagParseError` on structurally invalid
        node/edge entries.
        """
        raw_nodes = graph["nodes"]
        raw_edges = graph["edges"]

        structural_nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        for entry in raw_nodes:
            if not isinstance(entry, dict):
                raise _DagParseError("each node must be an object")
            node_id = entry.get("node_id") or entry.get("technique_id")
            if not isinstance(node_id, str) or not node_id.strip():
                raise _DagParseError("each node must carry a non-empty 'node_id'")
            node_id = node_id.strip()
            if node_id in node_ids:
                raise _DagParseError(f"duplicate node_id {node_id!r}")
            node_ids.add(node_id)
            structural_nodes.append(
                {
                    "node_id": node_id,
                    "technique_id": entry.get("technique_id"),
                    "technique_name": entry.get("technique_name"),
                    "tactic": entry.get("tactic"),
                    "parent_nodes": [],
                    "child_nodes": [],
                }
            )

        node_by_id = {n["node_id"]: n for n in structural_nodes}
        edges: list[dict[str, str]] = []
        seen_edges: set[tuple[str, str]] = set()
        for entry in raw_edges:
            if not isinstance(entry, dict):
                raise _DagParseError("each edge must be an object")
            src = entry.get("de") or entry.get("source") or entry.get("from")
            dst = entry.get("para") or entry.get("target") or entry.get("to")
            if not isinstance(src, str) or not isinstance(dst, str):
                raise _DagParseError("each edge must have string 'de'/'para'")
            src, dst = src.strip(), dst.strip()
            if src not in node_by_id or dst not in node_by_id:
                raise _DagParseError(
                    f"edge {src!r}->{dst!r} references an unknown node_id"
                )
            if src == dst:
                raise _DagParseError(f"self-loop edge on node {src!r} is not a DAG")
            if (src, dst) in seen_edges:
                continue
            seen_edges.add((src, dst))
            edges.append({"de": src, "para": dst})
            node_by_id[src]["child_nodes"].append(dst)
            node_by_id[dst]["parent_nodes"].append(src)

        # Reject cycles: the graph must be acyclic (a DAG).
        if self._has_cycle(node_ids, edges):
            raise _DagParseError("graph contains a cycle; a DAG must be acyclic")

        return {"structural_nodes": structural_nodes, "arestas": edges}

    @staticmethod
    def _has_cycle(node_ids: set[str], edges: list[dict[str, str]]) -> bool:
        """Return ``True`` iff the directed graph has a cycle (Kahn's algorithm)."""
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

    @staticmethod
    def _validate_content(content: dict) -> None:
        """Final guard that the assembled content is a non-empty, valid DAG.

        A valid produced DAG must carry at least one structural node; an empty
        node set means the model returned nothing usable (Requirement 14.4).
        """
        if not content.get("structural_nodes"):
            raise _DagParseError("the produced DAG has no structural nodes")

    @staticmethod
    def _resolve_label(graph: dict[str, Any], attack_context: Optional[dict]) -> Optional[str]:
        """Best-effort readable label for the produced DAG (optional field)."""
        if attack_context:
            attack = attack_context.get("attack")
            if isinstance(attack, dict):
                name = attack.get("name") or attack.get("campaign")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None


class _DagParseError(ValueError):
    """Internal: a single parse/validation failure of one LLM response.

    Not part of the public API — it drives the one-repair loop inside
    :meth:`AiDagGenerator.generate`. A still-failing repair is surfaced to callers
    as the public :class:`DagGenerationError` (Requirement 14.4).
    """


# ---------------------------------------------------------------------------
# LangGraph node wrapper + persistence hook (task 10.3)
# ---------------------------------------------------------------------------
#
# ``node`` is the Stage 3 :class:`AiDagGenerator` wrapped as a LangGraph node: it
# builds the generator with a lazily-resolved chat model and (optional) RAG
# memory, reads the Structural Modeling (Stage 1) out of the shared
# :class:`~app.services.ai.state.AgentState`, runs :meth:`AiDagGenerator.generate`
# and returns the ``artifacts.dag`` state delta the design prescribes
# (Requirement 2.4). It performs NO persistence and opens NO database — the graph
# nodes only mutate the checkpointed state.
#
# Persisting the AI-origin DAG and the ``agent_stage_outputs`` bookkeeping row is
# a SEPARATE, Session-bound concern owned by the ``AgentRunService`` (tasks 15.x),
# which is the only layer that holds the injected ``Session`` and the enclosing
# ``agent_iterations`` row. It is factored here as the standalone
# :func:`persist_dag_stage_output` helper so the Run_Service can drive it after a
# node produces ``artifacts.dag`` (Requirement 11.5), without this module ever
# importing the graph runtime or opening a connection at import time
# (Requirement 1.7).


def _modeling_from_state(state: "AgentState") -> StructuralModeling:
    """Reconstruct the :class:`StructuralModeling` (Stage 1) from the agent state.

    The DAG (Stage 3) is derived from the Structural Modeling (Stage 1), so the
    node reads the modeling the orchestrator seeded into the shared state. The
    modeling may already be a :class:`StructuralModeling`, a serialized ``dict``
    (``{"techniques": [...], "relationships": [...]}``, e.g. a
    ``StructuralModeling.model_dump()`` round-tripped through the checkpointer),
    or absent — in which case an empty modeling is used so generation can still
    proceed (and fail cleanly if the model returns nothing usable).
    """
    modeling = state.get("modeling")
    if isinstance(modeling, StructuralModeling):
        return modeling
    if isinstance(modeling, dict):
        try:
            return StructuralModeling.model_validate(modeling)
        except Exception as exc:  # noqa: BLE001 - tolerate a malformed snapshot
            logger.warning(
                "Could not parse the Structural Modeling from state (%s: %s); "
                "proceeding with an empty modeling.",
                type(exc).__name__,
                exc,
            )
    return StructuralModeling()


def node(state: "AgentState") -> dict:
    """LangGraph Stage 3 node: produce the AI DAG and write ``artifacts.dag``.

    Builds an :class:`AiDagGenerator` with a lazily-resolved SEPINF chat model
    (:func:`app.services.ai.llm.make_chat_model`) and the RAG
    :class:`~app.services.ai.memory.ExperienceMemory` when one was seeded on the
    state, reads the Structural Modeling (Stage 1) via
    :func:`_modeling_from_state`, calls :meth:`AiDagGenerator.generate` for the
    ``campaign_id``, and returns the state delta the design prescribes
    (Requirement 2.4)::

        {"artifacts": {**state.get("artifacts", {}),
                       "dag": produced[0].model_dump() if produced else None}}

    The chat model and memory are resolved **inside** the node so importing this
    module performs no I/O (Requirement 1.7). Persistence of the AI-origin ``Dag``
    row and the ``agent_stage_outputs`` bookkeeping is intentionally **not** done
    here — it is the Session-bound Run_Service's job, driven through
    :func:`persist_dag_stage_output` once this node has produced ``artifacts.dag``
    (Requirement 11.5).
    """
    # Lazy import so importing this module requires neither the LangChain client
    # nor the config/settings chain (Requirement 1.7).
    from app.services.ai.llm import make_chat_model

    chat = make_chat_model()
    memory = state.get("memory")
    generator = AiDagGenerator(chat=chat, memory=memory)

    modeling = _modeling_from_state(state)
    produced = generator.generate(state["campaign_id"], modeling)

    return {
        "artifacts": {
            **state.get("artifacts", {}),
            "dag": produced[0].model_dump() if produced else None,
        }
    }


def persist_dag_stage_output(
    session: "Session",
    *,
    iteration_id: int,
    campaign_id: str,
    produced: ProducedDag,
) -> "AgentStageOutput":
    """Persist a produced AI DAG as an ``agent_stage_outputs`` row (Requirement 11.5).

    This is the Session-bound persistence hook the ``AgentRunService`` (tasks
    15.x) drives after the Stage 3 :func:`node` has produced ``artifacts.dag``.
    It is factored out of the LangGraph node so the graph runtime stays free of
    database access and the Run_Service — the only layer that holds the injected
    ``Session`` and the enclosing ``agent_iterations`` row — owns the write.

    Two linked writes, in order:

    1. Persist the AI-origin ``Dag`` row so the DAG is stored exactly like a
       curated one (same portable ``content`` format, same ``Dag`` entity, no
       schema migration — the ``DagOrigin`` CHECK already admits ``gerado_ia``).
       The persistence mirrors ``DagService.sync_curated_dags``: the ``Case`` row
       is upserted first (via ``DagService._upsert_case_row``) to satisfy the
       ``dags.campaign_id`` FK, then the ``Dag`` row is reconciled **idempotently**
       on ``(campaign_id, origin, identifier)`` — an equivalent AI-origin DAG is
       reused rather than duplicated (Req. 4.5).
    2. Insert the ``agent_stage_outputs`` row for ``iteration_id`` with
       ``stage = estagio_3_dag``, ``origin = gerado_ia``, the serialized
       ``payload`` (``produced.model_dump()``) and ``dag_id`` linking the
       AI-origin ``Dag`` row from step 1.

    Both rows are added and flushed (so ``id``/``dag_id`` are assigned) but **not**
    committed — the transaction boundary belongs to the Run_Service, which commits
    the whole iteration atomically. Returns the persisted
    :class:`~app.models.domain.AgentStageOutput`.
    """
    # Lazy imports keep module import free of ORM/model/service import cost and
    # any circular-import risk (Requirement 1.7).
    from sqlalchemy import select

    from app.models.domain import AgentStageOutput, Dag as DagModel
    from app.models.enums import AgentStage, TranslationSource
    from app.services.dag.dag_service import DagService, _utcnow

    # Persist the AI-origin Dag row via DagService, mirroring sync_curated_dags:
    # ensure the Case row exists (FK), then reconcile idempotently on
    # (campaign_id, origin, identifier) so re-running a stage does not duplicate.
    dag_service = DagService(session=session)
    dag_service._upsert_case_row(campaign_id, produced.label)

    dag_id = (
        session.execute(
            select(DagModel.id)
            .where(DagModel.campaign_id == campaign_id)
            .where(DagModel.origin == produced.origin)
            .where(DagModel.identifier == produced.identifier)
        )
        .scalars()
        .first()
    )
    if dag_id is None:
        dag_row = DagModel(
            campaign_id=campaign_id,
            origin=produced.origin,
            identifier=produced.identifier,
            label=produced.label,
            content=produced.content,
            created_at=_utcnow(),
        )
        session.add(dag_row)
        session.flush()
        dag_id = dag_row.id

    row = AgentStageOutput(
        iteration_id=iteration_id,
        stage=AgentStage.DAG,
        origin=TranslationSource.AI_GENERATED,
        payload=produced.model_dump(),
        dag_id=dag_id,
    )
    session.add(row)
    session.flush()
    return row
