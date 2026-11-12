"""Flow agent: the ``FlowAgent`` implementation of the ``FlowGenerator`` seam.

Hosts :class:`FlowAgent`, the AI-origin counterpart of
 the former curated generator. It plugs into the
exact same ``@runtime_checkable`` :class:`~app.services.flow.generator.FlowGenerator`
Protocol (``origin()`` + ``generate(campaign, modeling)``) so the run service can
inject it in place of the curation generator with no downstream change
(Requirements 2.1, 2.7).

Unlike curation — which ignores ``modeling`` because the curated graph already
exists on disk — this agent **uses** the Structural Modeling (Stage 1): it builds
a prompt from the modeling plus the cross-campaign experience retrieved from the
RAG memory, asks the LLM for an **ordered technique graph**, validates and (on
malformed output) repairs the structure **exactly once**, and returns a single
:class:`~app.services.flow.generator.ProducedFlow` stamped
``origin = FlowOrigin.AI_GENERATED`` (``"gerado_ia"`` — Requirements 2.4, 2.5). The
produced ``content`` mirrors the curation format
(``{"structural_nodes": [...], "arestas": [{"de", "para"}]}``) so the persisted
Attack flow is byte-compatible with the existing ``AttackFlowRow`` entity and no schema migration is
needed.

Error handling follows Requirement 14.3/14.4: a single repair attempt is made when
the LLM output cannot be parsed into a valid attack flow; if the output is still malformed
after that one repair, a :class:`FlowGenerationError` is raised identifying the
failing node and the parse-failure cause, and **no** (invalid) ``ProducedFlow`` is
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
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Callable, Optional

from app.models.enums import AgentStage, FlowOrigin, FlowProvenance
from app.services.attack_flow.cycles import analyze_cycles, annotate_flow_cycles
from app.services.attack_flow.model import (
    AttackFlow,
    AttackFlowObject,
    FlowProvenanceRecord,
)
from app.services.attack_flow.policy import FlowDecision, classify_flow_path
from app.services.flow.generator import ProducedFlow, StructuralModeling

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from sqlalchemy.orm import Session

    from app.models.domain import AgentStageOutput
    from app.services.ai.memory import ExperienceMemory
    from app.services.ai.state import AgentState

logger = logging.getLogger(__name__)

__all__ = [
    "FlowAgent",
    "FlowGenerationError",
    "node",
    "persist_flow_stage_output",
]

# The agent asks the LLM for a graph, then performs AT MOST this many additional
# repair attempts on malformed output. Requirement 14.3 fixes it at exactly one
# repair: the initial attempt plus one repair == 2 total LLM passes.
_MAX_REPAIR_ATTEMPTS = 1

# Default cross-campaign experience fan-in for the RAG priming (bounded by the
# memory's own 1..100 guard).
_DEFAULT_RETRIEVAL_K = 5


class FlowGenerationError(RuntimeError):
    """Raised when the LLM output is still unparseable after the one repair.

    Signals Requirement 14.4: after the single permitted repair attempt the LLM
    output still cannot be parsed into a valid attack flow, so the failing node and the
    parse-failure cause are recorded on the exception and **no** invalid
    ``ProducedFlow`` is produced for persistence. The ``node`` attribute names the
    failing agent node (``"flow"``) for the Run_Service's failure bookkeeping.
    """

    def __init__(self, message: str, *, node: str = "flow", cause: Optional[str] = None) -> None:
        super().__init__(message)
        self.node = node
        self.cause = cause


class FlowAgent:
    """AI implementation of the :class:`FlowGenerator` seam (Requirements 2.1, 2.4, 2.5).

    Satisfies the ``@runtime_checkable`` ``FlowGenerator`` Protocol structurally
    (``origin()`` + ``generate()``), with no explicit inheritance — exactly the
    point of the seam, so it is injectable wherever a generator is
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
        command_lookup: Optional[Callable[[str], bool]] = None,
        retrieval_k: int = _DEFAULT_RETRIEVAL_K,
        trace_sink: "Optional[list[dict]]" = None,
        flow_retriever: Any = None,
        corpus_flow_loader: Optional[Callable[[int], Optional[dict]]] = None,
        operationalization: Optional[dict] = None,
        command_ref_resolver: Optional[Callable[[str, Optional[str]], Optional[int]]] = None,
        campaign_name: Optional[str] = None,
        campaign_aliases: Sequence[str] = (),
        campaign_description: str = "",
    ) -> None:
        # Three-path decision collaborators (all optional, all lazy):
        # * ``flow_retriever`` — a ``FlowRetriever`` (``retrieve`` +
        #   ``corpus_available``); without it the corpus counts as unavailable
        #   and the ``created`` path is taken.
        # * ``corpus_flow_loader`` — ``flow_id -> {"label", "content"}`` of a
        #   documented flow, used by the ``exact`` copy and the reference
        #   summaries of the ``adapted`` prompt.
        # * ``operationalization`` — the ``artifacts.operationalization`` dict;
        #   it decides which actions have a command and feeds ``command_ref``.
        # * ``command_ref_resolver`` — ``(technique_id, command) ->
        #   technique_commands.id`` to turn an operationalized command into the
        #   ``command_ref`` stored on the action.
        self._flow_retriever = flow_retriever
        self._corpus_flow_loader = corpus_flow_loader
        self._operationalization = operationalization
        self._command_ref_resolver = command_ref_resolver
        self._campaign_name = campaign_name
        self._campaign_aliases = tuple(campaign_aliases)
        self._campaign_description = campaign_description
        self._chat = chat
        self._memory = memory
        self._attack = attack_reader
        # Command_Base gap probe: a ``technique_id -> bool`` callable backed by
        # ``CommandBase.has_command_for`` (task 14.2, Req. 11.4). A node technique
        # for which this returns ``False`` has no backing command in the
        # Command_Base (neither an exact ``technique_commands`` mapping nor a
        # similarity suggestion) and becomes a gap on ``ProducedFlow.gaps`` instead
        # of the agent inventing an unsupported command. When ``None`` (no session
        # was threaded in, or a direct caller/test injected nothing) the gap probe
        # is skipped and no gaps are recorded — behaviour degrades cleanly.
        self._command_lookup = command_lookup
        self._retrieval_k = retrieval_k
        # Optional Audit_Trace sink injected by the LangGraph ``node`` wrapper. It
        # is carried as an instance attribute — NOT a ``generate`` parameter — so
        # the ``@runtime_checkable`` ``FlowGenerator`` seam signature
        # (``generate(campaign, modeling)``) is byte-for-byte unchanged (design
        # §A). When ``None`` (curation fallback / direct seam callers) no trace is
        # captured and behavior is exactly as before.
        self._trace_sink = trace_sink

    # ----- seam ---------------------------------------------------------------

    def origin(self) -> FlowOrigin:
        """The origin stamped on produced attack flows: always ``gerado_ia`` (Req. 2.4, 2.5)."""
        return FlowOrigin.AI_GENERATED

    def generate(self, campaign: str, modeling: StructuralModeling) -> list[ProducedFlow]:
        """Produce the AI attack flow for ``campaign`` from the Structural Modeling.

        Unlike a fixed generator (which would ignore ``modeling``), this
        agent **uses** it: it builds a prompt from the modeling plus the retrieved
        cross-campaign experience, asks the LLM for an ordered technique graph,
        validates/repairs the structure exactly once, and returns a single
        :class:`ProducedFlow` with ``origin = gerado_ia`` (Requirements 2.1, 2.4,
        2.5). The ``content`` mirrors the legacy content format so the flow readers
        persists it as an AI-origin ``AttackFlowRow`` with no schema change.

        Repair contract (Requirements 14.3, 14.4): if the first LLM response
        cannot be parsed into a valid attack flow, exactly **one** repair attempt is made
        (re-prompting the model with the parse error). If the output is still
        invalid after that single repair, a :class:`FlowGenerationError` is raised
        naming the failing node (``"flow"``) and the parse-failure cause, and no
        (invalid) ``ProducedFlow`` is returned.

        Audit_Trace capture (Requirements 1.2, 1.3, 2.2, 2.3): when the node
        wrapper injected an ``trace_sink`` (a ``list[dict]``) into the constructor,
        the agent appends a single :class:`~app.services.ai.state.AgentTrace` dict
        at its LLM call site — ``agent_input`` carrying the modeling, retrieved
        experience, attack context and (absent) prior artifacts; ``processing``
        carrying the built ``llm_prompt``, the raw ``llm_response``, the
        ``reasoning``, the ``tools_invoked``, the ``repair_attempts`` count and the
        ``repair_hints`` fed into the repair pass. The sink is an injected instance
        attribute (NOT a ``generate`` parameter) so the ``@runtime_checkable``
        ``FlowGenerator`` seam signature is byte-for-byte unchanged (curation and
        direct callers inject nothing and behave exactly as before). Genuinely-
        absent processing elements are recorded with an explicit ``None`` sentinel
        the serialization layer maps to "not available".
        """
        techniques = self._campaign_techniques(modeling)
        decision, reason, _candidates = self._decide(campaign, modeling, techniques)
        warnings: list[str] = []

        # ``exact``: copy the documented flow without calling the LLM. If the
        # flow cannot be loaded the decision is demoted to ``adapted`` so the
        # recorded provenance stays truthful.
        corpus_flow: Optional[dict] = None
        if decision.kind is FlowProvenance.EXACT:
            corpus_flow = self._load_corpus_flow(decision.references[0])
            if corpus_flow is None:
                decision = FlowDecision(
                    FlowProvenance.ADAPTED, decision.references, decision.similarity
                )
                reason = "exact_flow_unavailable"

        provenance = {
            "kind": decision.kind.value,
            "references": list(decision.references),
            "similarity": decision.similarity,
        }
        decision_trace: dict[str, Any] = {**provenance, "reason": reason}

        if corpus_flow is not None:
            produced = self._produce_exact(
                campaign, corpus_flow, provenance, warnings
            )
            self._emit_trace(
                modeling=modeling,
                experience=[],
                attack_context=None,
                messages=None,
                raw=None,
                tools_invoked=["flow_retriever.retrieve", "corpus_flow_loader"],
                repair_attempts=0,
                repair_hints=[],
                decision=decision_trace,
                warnings=warnings,
            )
            return [produced]

        if self._chat is None:
            raise FlowGenerationError(
                "FlowAgent requires a chat model to generate an attack flow; none was "
                "injected.",
                node="flow",
                cause="missing_chat_model",
            )

        experience = self._retrieve_experience(campaign, modeling)
        attack_context = self._read_attack_context(campaign)
        references = self._reference_summaries(decision.references)
        allowed = frozenset(techniques)

        # First attempt, then at most one repair that re-prompts with the parse
        # error (Requirement 14.3). ``last_cause`` carries the most recent
        # parse-failure cause into the node error if the repair also fails.
        last_cause: Optional[str] = None
        repair_hint: Optional[str] = None
        # Trace accumulators (appended to the sink at the end of the call, so the
        # single AgentTrace reflects every LLM pass incl. the repair).
        last_messages: Optional[list[dict[str, str]]] = None
        last_raw: Optional[str] = None
        repair_hints: list[str] = []
        repair_attempts = 0
        tools_invoked: list[str] = ["experience_memory.retrieve"]
        if attack_context is not None:
            tools_invoked.append("tool_read_attack")
        try:
            for attempt in range(_MAX_REPAIR_ATTEMPTS + 1):
                messages = self._build_messages(
                    campaign,
                    modeling,
                    experience,
                    attack_context,
                    repair_hint=repair_hint,
                    references=references,
                )
                last_messages = messages
                raw = self._invoke_chat(messages)
                last_raw = raw
                try:
                    graph = self._parse_graph(raw)
                    content = self._assemble_content(
                        graph, modeling, campaign=campaign,
                        allowed_techniques=allowed, provenance=provenance,
                        warnings=warnings,
                    )
                    self._validate_content(content)
                except _FlowParseError as exc:
                    last_cause = str(exc)
                    repair_attempts += 1
                    # Feed the parse error back into the single repair pass so the
                    # model can correct the malformed structure (Requirement 14.3).
                    repair_hint = str(exc)
                    repair_hints.append(str(exc))
                    logger.warning(
                        "FlowAgent parse/validation failed for campaign %s on "
                        "attempt %d/%d (%s).",
                        campaign,
                        attempt + 1,
                        _MAX_REPAIR_ATTEMPTS + 1,
                        exc,
                    )
                    continue

                label = self._resolve_label(graph, attack_context)
                # Flag the campaign techniques whose nodes made it into the
                # graph but have NO backing command: they become gaps rather
                # than an invented, unsupported command (Req. 11.4).
                gaps = self._collect_gaps(content)
                return [
                    ProducedFlow(
                        origin=FlowOrigin.AI_GENERATED,
                        identifier=campaign,
                        label=label,
                        content=content,
                        gaps=gaps,
                        provenance=provenance,
                    )
                ]

            # Still malformed after the one permitted repair: record a node error
            # and do NOT return an invalid ProducedFlow (Requirement 14.4).
            raise FlowGenerationError(
                f"FlowAgent could not parse a valid attack flow for campaign {campaign} "
                f"after one repair attempt.",
                node="flow",
                cause=last_cause,
            )
        finally:
            # Append the AgentTrace capturing the input + processing, whether the
            # call returned an attack flow or raised — so ``_fail_run`` finds the partial
            # trace captured up to the failure (design §A "Failure capture").
            self._emit_trace(
                modeling=modeling,
                experience=experience,
                attack_context=attack_context,
                messages=last_messages,
                raw=last_raw,
                tools_invoked=tools_invoked,
                repair_attempts=repair_attempts,
                repair_hints=repair_hints,
                decision=decision_trace,
                warnings=warnings,
            )

    # ----- three-path decision ------------------------------------------------

    @staticmethod
    def _campaign_techniques(modeling: StructuralModeling) -> list[str]:
        """Campaign technique ids from the modeling, deduplicated, in order."""
        seen: dict[str, None] = {}
        for technique in modeling.techniques:
            if technique.technique_id:
                seen.setdefault(technique.technique_id, None)
        return list(seen)

    def _decide(
        self, campaign: str, modeling: StructuralModeling, techniques: list[str]
    ) -> tuple[FlowDecision, Optional[str], list[Any]]:
        """Retrieve candidates and classify the path (no LLM involved).

        Returns ``(decision, reason, candidates)``. ``reason`` is
        ``"corpus_unavailable"`` when there are no candidates because no
        documented flow has been ingested (or no retriever is wired), so that
        case is distinguishable from "no similar flow".
        """
        candidates: list[Any] = []
        corpus_ok = False
        if self._flow_retriever is not None:
            try:
                candidates = self._flow_retriever.retrieve(
                    campaign_techniques=techniques,
                    campaign_description=self._campaign_description
                    or self._modeling_summary(modeling),
                )
                corpus_ok = bool(candidates) or self._flow_retriever.corpus_available()
            except Exception as exc:  # noqa: BLE001 - never let retrieval break generation
                logger.warning(
                    "FlowAgent corpus retrieval skipped for campaign %s (%s: %s); "
                    "taking the 'created' path.",
                    campaign,
                    type(exc).__name__,
                    exc,
                )
                candidates = []
        reason = None if corpus_ok else "corpus_unavailable"
        decision = classify_flow_path(
            self._campaign_name or campaign,
            self._campaign_aliases,
            frozenset(techniques),
            candidates,
        )
        return decision, reason, candidates

    def _load_corpus_flow(self, flow_id: int) -> Optional[dict]:
        """Load ``{"label", "content"}`` of a documented flow; ``None`` if absent."""
        if self._corpus_flow_loader is None:
            return None
        try:
            loaded = self._corpus_flow_loader(flow_id)
        except Exception as exc:  # noqa: BLE001 - degrade to the LLM path
            logger.warning(
                "FlowAgent could not load corpus flow %s (%s: %s).",
                flow_id,
                type(exc).__name__,
                exc,
            )
            return None
        if not loaded or not isinstance(loaded.get("content"), dict):
            return None
        return loaded

    def _reference_summaries(self, flow_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Compact summaries of the referenced flows for the ``adapted`` prompt."""
        summaries: list[dict[str, Any]] = []
        for flow_id in flow_ids:
            loaded = self._load_corpus_flow(flow_id)
            if loaded is None:
                continue
            try:
                flow = AttackFlow.model_validate(loaded["content"])
            except Exception:  # noqa: BLE001 - skip an unreadable reference
                continue
            by_id = {obj.id: obj for obj in flow.objects}
            actions = [
                {"technique_id": obj.technique_id, "name": obj.name}
                for obj in flow.objects
                if obj.type == "attack-action"
            ]
            edges = [
                {"de": by_id[src].technique_id or src, "para": by_id[dst].technique_id or dst}
                for src, dst in flow.execution_edges()
                if src in by_id and dst in by_id
                and by_id[src].type == "attack-action"
                and by_id[dst].type == "attack-action"
            ]
            summaries.append(
                {
                    "flow_id": flow_id,
                    "name": flow.name,
                    "actions": actions,
                    "edges": edges,
                }
            )
        return summaries

    # ----- command index / refs ----------------------------------------------

    def _command_index(self) -> Optional[tuple[set[str], dict[str, Optional[int]]]]:
        """Techniques backed by the operationalization, and their ``command_ref``.

        ``None`` when no operationalization artifact was supplied (the caller
        then falls back to the Command_Base probe).
        """
        op = self._operationalization
        if op is None:
            return None
        abilities = (op.get("abilities") or {}).get("abilities") or []
        backed: set[str] = set()
        refs: dict[str, Optional[int]] = {}
        for ability in abilities:
            technique_id = ability.get("technique_id")
            if not isinstance(technique_id, str) or not technique_id:
                continue
            for executor in ability.get("executors") or []:
                command = executor.get("command")
                direct = executor.get("id")
                if not (isinstance(command, str) and command.strip()) and not isinstance(
                    direct, int
                ):
                    continue
                backed.add(technique_id)
                if refs.get(technique_id) is not None:
                    continue
                ref: Optional[int] = direct if isinstance(direct, int) else None
                if ref is None and self._command_ref_resolver is not None:
                    try:
                        ref = self._command_ref_resolver(technique_id, command)
                    except Exception as exc:  # noqa: BLE001 - ref is best-effort
                        logger.warning(
                            "FlowAgent command_ref lookup failed for %s (%s: %s).",
                            technique_id,
                            type(exc).__name__,
                            exc,
                        )
                refs[technique_id] = ref
        return backed, refs

    def _apply_command_refs(self, flow: AttackFlow) -> AttackFlow:
        """Fill ``command_ref`` on every action from the operationalization."""
        index = self._command_index()
        refs = index[1] if index is not None else {}
        objects = [
            obj.model_copy(update={"command_ref": refs.get(obj.technique_id or "")})
            if obj.type == "attack-action"
            else obj
            for obj in flow.objects
        ]
        return flow.model_copy(update={"objects": objects})

    # ----- exact path ---------------------------------------------------------

    def _produce_exact(
        self,
        campaign: str,
        corpus_flow: dict,
        provenance: dict,
        warnings: list[str],
    ) -> ProducedFlow:
        """Copy the documented flow (no LLM) and fill ``command_ref`` per action."""
        flow = AttackFlow.model_validate(corpus_flow["content"])
        flow = flow.model_copy(
            update={
                "provenance": FlowProvenanceRecord(**provenance),
                "flow_id": f"attack-flow--{uuid.uuid5(uuid.NAMESPACE_URL, f'sticks:{campaign}:exact')}",
            }
        )
        flow = annotate_flow_cycles(flow)
        flow = self._apply_command_refs(flow)
        if flow.cycles:
            warnings.append(
                f"documented flow has cycles {flow.cycles}; back edges "
                f"{flow.back_edges} are excluded from execution"
            )
        content = self._flow_content(flow)
        return ProducedFlow(
            origin=FlowOrigin.AI_GENERATED,
            identifier=campaign,
            label=corpus_flow.get("label") or flow.name or None,
            content=content,
            gaps=self._collect_gaps(content),
            provenance=provenance,
        )

    def _emit_trace(
        self,
        *,
        modeling: StructuralModeling,
        experience: list[dict[str, Any]],
        attack_context: Optional[dict],
        messages: Optional[list[dict[str, str]]],
        raw: Optional[str],
        tools_invoked: list[str],
        repair_attempts: int,
        repair_hints: list[str],
        decision: dict[str, Any],
        warnings: list[str],
    ) -> None:
        """Append the single AgentTrace of this call to the injected sink."""
        if self._trace_sink is None:
            return
        self._trace_sink.append(
            {
                "stage": AgentStage.FLOW.value,
                "iteration": None,
                "agent_input": {
                    "modeling": modeling.model_dump(mode="json"),
                    "retrieved_experience": experience,
                    "attack_context": attack_context,
                    "prior_artifacts": None,
                },
                "processing": {
                    "llm_prompt": messages,
                    "llm_response": raw,
                    "reasoning": None,
                    "tools_invoked": tools_invoked,
                    "repair_attempts": repair_attempts,
                    "repair_hints": repair_hints or None,
                    "flow_decision": decision,
                    "warnings": list(warnings) or None,
                },
            }
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
                "FlowAgent experience retrieval skipped for campaign %s "
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
                "FlowAgent ATT&CK context read skipped for campaign %s "
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
        references: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, str]]:
        """Build the chat messages: a system contract + the modeling/RAG prompt.

        On a repair pass ``repair_hint`` carries the previous parse error so the
        model is explicitly told what to correct (Requirement 14.3).
        """
        system = (
            "You are the Stage 3 attack flow planner for an adversary-emulation pipeline. "
            "Given the Structural Modeling (ATT&CK techniques and their "
            "relationships) and optional past experience, produce an ORDERED "
            "directed acyclic graph of techniques. Respond with ONLY a JSON object "
            'of the exact shape {"nodes": [{"node_id": str, "technique_id": str, '
            '"technique_name": str|null, "tactic": str|null}], "edges": '
            '[{"de": str, "para": str}]}. "de"/"para" reference node_id values. '
            "Do not include any prose, explanation or markdown fences."
        )
        user = self._build_prompt(
            campaign, modeling, experience, attack_context, references=references
        )
        if repair_hint:
            user = (
                f"{user}\n\nThe previous response could not be parsed into a valid "
                f"attack flow. Error: {repair_hint}. Return a corrected JSON object with "
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
        *,
        references: Optional[list[dict[str, Any]]] = None,
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
        index = self._command_index()
        if index is not None:
            lines.append("Campaign techniques that have a command (operationalization):")
            lines.append(json.dumps(sorted(index[0]), ensure_ascii=False))
        allowed = self._campaign_techniques(modeling)
        if allowed:
            lines.append(
                "Use ONLY these campaign technique ids in the nodes: "
                + json.dumps(allowed, ensure_ascii=False)
            )
        if references:
            lines.append(
                "Documented reference flows (adapt their ordering to this campaign):"
            )
            lines.append(json.dumps(references, ensure_ascii=False))
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
            "Produce the ordered technique attack flow as the JSON object described. "
            "Prefer the modeling's techniques and relationships; refine ordering "
            "and fill gaps using the experience and ATT&CK context."
        )
        return "\n".join(lines)

    @staticmethod
    def _modeling_summary(modeling: StructuralModeling) -> str:
        """A compact text summary of the modeling used as the RAG query."""
        ids = [t.technique_id for t in modeling.techniques if t.technique_id]
        return "attack flow planning for techniques: " + ", ".join(ids) if ids else (
            "attack flow planning for an empty technique set"
        )

    # ----- parsing / validation / assembly ------------------------------------

    @staticmethod
    def _parse_graph(raw: str) -> dict[str, Any]:
        """Parse the LLM text into a ``{"nodes": [...], "edges": [...]}`` dict.

        Tolerant of a surrounding markdown code fence; strict about the top-level
        shape. Raises :class:`_FlowParseError` on any malformed output so the
        single repair path (Requirement 14.3) can re-prompt.
        """
        text = (raw or "").strip()
        if not text:
            raise _FlowParseError("empty LLM response")

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
            raise _FlowParseError(f"response is not valid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise _FlowParseError("top-level JSON must be an object")
        nodes = data.get("nodes")
        edges = data.get("edges", [])
        if not isinstance(nodes, list):
            raise _FlowParseError("'nodes' must be a list")
        if not isinstance(edges, list):
            raise _FlowParseError("'edges' must be a list")
        return {"nodes": nodes, "edges": edges}

    def _assemble_content(
        self,
        graph: dict[str, Any],
        modeling: StructuralModeling,
        *,
        campaign: str = "",
        allowed_techniques: frozenset[str] = frozenset(),
        provenance: Optional[dict] = None,
        warnings: Optional[list[str]] = None,
    ) -> dict:
        """Assemble the persistable ``content`` in the curation-compatible format.

        Produces ``{"structural_nodes": [...], "arestas": [{"de", "para"}]}`` —
        the exact shape of the legacy curated content assembly and the
        ``GET /api/campaigns/{campaign}/flows/{flow_id}`` contract — so the persisted attack flow is
        schema-compatible regardless of origin. ``parent_nodes``/``child_nodes``
        are derived from the edges so a consumer sees the same structural shape as
        a curated attack flow. Raises :class:`_FlowParseError` on structurally invalid
        node/edge entries.
        """
        raw_nodes = graph["nodes"]
        raw_edges = graph["edges"]

        structural_nodes: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        for entry in raw_nodes:
            if not isinstance(entry, dict):
                raise _FlowParseError("each node must be an object")
            node_id = entry.get("node_id") or entry.get("technique_id")
            if not isinstance(node_id, str) or not node_id.strip():
                raise _FlowParseError("each node must carry a non-empty 'node_id'")
            node_id = node_id.strip()
            technique = entry.get("technique_id")
            if (
                allowed_techniques
                and isinstance(technique, str)
                and technique not in allowed_techniques
            ):
                raise _FlowParseError(
                    f"technique {technique!r} is not one of the campaign techniques"
                )
            if node_id in node_ids:
                raise _FlowParseError(f"duplicate node_id {node_id!r}")
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
                raise _FlowParseError("each edge must be an object")
            src = entry.get("de") or entry.get("source") or entry.get("from")
            dst = entry.get("para") or entry.get("target") or entry.get("to")
            if not isinstance(src, str) or not isinstance(dst, str):
                raise _FlowParseError("each edge must have string 'de'/'para'")
            src, dst = src.strip(), dst.strip()
            if src not in node_by_id or dst not in node_by_id:
                raise _FlowParseError(
                    f"edge {src!r}->{dst!r} references an unknown node_id"
                )
            if (src, dst) in seen_edges:
                continue
            seen_edges.add((src, dst))
            edges.append({"de": src, "para": dst})
            node_by_id[src]["child_nodes"].append(dst)
            node_by_id[dst]["parent_nodes"].append(src)

        # Post-LLM validation: analyze cycles instead of rejecting the graph.
        # Back edges are kept in the flow (``back_edges``) but excluded from the
        # acyclic execution view; a warning goes to the trace and no second LLM
        # call is made because of a cycle.
        object_ids = {
            n["node_id"]: self._object_id(campaign, n["node_id"])
            for n in structural_nodes
        }
        objects = [
            AttackFlowObject(
                id=object_ids[n["node_id"]],
                type="attack-action",
                name=n["technique_name"],
                technique_id=n["technique_id"] if isinstance(n["technique_id"], str) else None,
                tactic_id=n["tactic"] if isinstance(n["tactic"], str) else None,
                effect_refs=[object_ids[child] for child in n["child_nodes"]],
            )
            for n in structural_nodes
        ]
        start_refs = [
            object_ids[n["node_id"]] for n in structural_nodes if not n["parent_nodes"]
        ]
        if not start_refs and structural_nodes:
            start_refs = [object_ids[structural_nodes[0]["node_id"]]]
        flow = AttackFlow(
            flow_id=f"attack-flow--{uuid.uuid5(uuid.NAMESPACE_URL, f'sticks:{campaign}')}",
            name=campaign,
            start_refs=start_refs,
            objects=objects,
            provenance=FlowProvenanceRecord(**provenance) if provenance else None,
        )
        flow = annotate_flow_cycles(flow)
        flow = self._apply_command_refs(flow)
        if flow.cycles and warnings is not None:
            warnings.append(
                f"generated flow has cycles {flow.cycles}; back edges "
                f"{flow.back_edges} are excluded from execution"
            )
        reverse_ids = {oid: nid for nid, oid in object_ids.items()}
        return self._flow_content(flow, node_ids=reverse_ids)

    @staticmethod
    def _object_id(campaign: str, node_id: str) -> str:
        key = "sticks:" + campaign + ":" + node_id
        return "attack-action--" + str(uuid.uuid5(uuid.NAMESPACE_URL, key))

    @staticmethod
    def _flow_content(
        flow: AttackFlow, *, node_ids: Optional[dict[str, str]] = None
    ) -> dict:
        """Persistable ``content``: the Attack Flow plus the legacy structural mirror.

        The Attack Flow keys (``flow_id``, ``objects``, ``cycles``,
        ``back_edges``, ...) are the source of truth. ``structural_nodes`` and
        ``arestas`` mirror the actions and the action-to-action edges of the
        acyclic execution view (conditions and operators are traversed), so
        legacy readers keep working.
        """
        node_ids = node_ids or {}
        by_id = {obj.id: obj for obj in flow.objects}
        adjacency: dict[str, list[str]] = {obj.id: [] for obj in flow.objects}
        for src, dst in flow.execution_edges():
            if src in adjacency and dst in by_id:
                adjacency[src].append(dst)

        def action_targets(start: str) -> list[str]:
            found: list[str] = []
            seen: set[str] = {start}
            stack = list(reversed(adjacency[start]))
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                if by_id[current].type == "attack-action":
                    found.append(current)
                else:
                    stack.extend(reversed(adjacency[current]))
            return found

        actions = [obj for obj in flow.objects if obj.type == "attack-action"]
        legacy_id = {obj.id: node_ids.get(obj.id, obj.id) for obj in actions}
        nodes = {
            obj.id: {
                "node_id": legacy_id[obj.id],
                "technique_id": obj.technique_id,
                "technique_name": obj.name,
                "tactic": obj.tactic_id,
                "command_ref": obj.command_ref,
                "parent_nodes": [],
                "child_nodes": [],
            }
            for obj in actions
        }
        arestas: list[dict[str, str]] = []
        for obj in actions:
            for target in action_targets(obj.id):
                arestas.append({"de": legacy_id[obj.id], "para": legacy_id[target]})
                nodes[obj.id]["child_nodes"].append(legacy_id[target])
                nodes[target]["parent_nodes"].append(legacy_id[obj.id])

        content = flow.model_dump(mode="json")
        content["structural_nodes"] = [nodes[obj.id] for obj in actions]
        content["arestas"] = arestas
        return content

    @staticmethod
    def _validate_content(content: dict) -> None:
        """Final guard that the assembled content is a non-empty, valid attack flow.

        A valid produced attack flow must carry at least one structural node; an empty
        node set means the model returned nothing usable (Requirement 14.4).
        """
        if not content.get("structural_nodes"):
            raise _FlowParseError("the produced attack flow has no structural nodes")

    def _collect_gaps(self, content: dict) -> list[str]:
        """Return the node techniques with no backing command in the Command_Base.

        Walks the assembled ``structural_nodes`` and, for each node carrying a
        ``technique_id``, consults the injected ``has_command_for`` probe
        (:meth:`app.services.command_base.CommandBase.has_command_for`). A
        technique the Command_Base cannot back — no exact ``technique_commands``
        mapping and no similarity suggestion — is recorded as a gap so the attack flow
        agent surfaces the lacuna instead of inventing an unsupported command
        (Req. 11.4). Each gap technique is listed once, in first-seen node order.

        When no probe was injected (no session threaded in, or a direct caller/
        test), the gap check is skipped and an empty list is returned so the
        agent degrades cleanly without a Command_Base.
        """
        # The operationalization output, when supplied, is the authority on which
        # techniques have a command; otherwise fall back to the Command_Base probe.
        index = self._command_index()
        if index is None and self._command_lookup is None:
            return []

        gaps: list[str] = []
        seen: set[str] = set()
        for node in content.get("structural_nodes", []):
            technique_id = node.get("technique_id")
            if not isinstance(technique_id, str) or not technique_id.strip():
                continue
            if index is not None:
                technique_id = technique_id.strip()
                if technique_id not in seen:
                    seen.add(technique_id)
                    if technique_id not in index[0]:
                        gaps.append(technique_id)
                continue
            technique_id = technique_id.strip()
            if technique_id in seen:
                continue
            seen.add(technique_id)
            try:
                backed = self._command_lookup(technique_id)
            except Exception as exc:  # noqa: BLE001 - never let the probe break generation
                logger.warning(
                    "FlowAgent Command_Base gap probe failed for technique "
                    "%s (%s: %s); treating it as backed to avoid a false gap.",
                    technique_id,
                    type(exc).__name__,
                    exc,
                )
                continue
            if not backed:
                gaps.append(technique_id)
        return gaps

    @staticmethod
    def _resolve_label(graph: dict[str, Any], attack_context: Optional[dict]) -> Optional[str]:
        """Best-effort readable label for the produced attack flow (optional field)."""
        if attack_context:
            attack = attack_context.get("attack")
            if isinstance(attack, dict):
                name = attack.get("name") or attack.get("campaign")
                if isinstance(name, str) and name.strip():
                    return name.strip()
        return None


class _FlowParseError(ValueError):
    """Internal: a single parse/validation failure of one LLM response.

    Not part of the public API — it drives the one-repair loop inside
    :meth:`FlowAgent.generate`. A still-failing repair is surfaced to callers
    as the public :class:`FlowGenerationError` (Requirement 14.4).
    """


# ---------------------------------------------------------------------------
# LangGraph node wrapper + persistence hook (task 10.3)
# ---------------------------------------------------------------------------
#
# ``node`` is the Stage 3 :class:`FlowAgent` wrapped as a LangGraph node: it
# builds the generator with a lazily-resolved chat model and (optional) RAG
# memory, reads the Structural Modeling (Stage 1) out of the shared
# :class:`~app.services.ai.state.AgentState`, runs :meth:`FlowAgent.generate`
# and returns the ``artifacts.flow`` state delta the design prescribes
# (Requirement 2.4). It performs NO persistence and opens NO database — the graph
# nodes only mutate the checkpointed state.
#
# Persisting the AI-origin attack flow and the ``agent_stage_outputs`` bookkeeping row is
# a SEPARATE, Session-bound concern owned by the ``AgentRunService`` (tasks 15.x),
# which is the only layer that holds the injected ``Session`` and the enclosing
# ``agent_iterations`` row. It is factored here as the standalone
# :func:`persist_flow_stage_output` helper so the Run_Service can drive it after a
# node produces ``artifacts.flow`` (Requirement 11.5), without this module ever
# importing the graph runtime or opening a connection at import time
# (Requirement 1.7).


def _modeling_from_state(state: "AgentState") -> StructuralModeling:
    """Reconstruct the :class:`StructuralModeling` (Stage 1) from the agent state.

    The attack flow (Stage 3) is derived from the Structural Modeling (Stage 1), so the
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


def _attack_reader_for_session(
    session: Any,
) -> Optional[Callable[..., dict]]:
    """Build the DB-sourced ATT&CK base-context reader bound to ``session``.

    The attack flow agent's Agent_Input base context (identity/techniques/actors) comes
    from the ``Campaign_Overview`` in the DATABASE, never from disk (Req. 5.1,
    5.2, 5.4). Given the request-scoped ``Session`` the Run_Service threaded onto
    the node, this returns a thin reader that calls
    :func:`app.services.ai.tools.tool_read_attack` bound to that session (and the
    curated base version). With no session it returns ``None`` so
    :meth:`FlowAgent._read_attack_context` degrades to an empty base context
    WITHOUT reading disk.
    """
    if session is None:
        return None

    from app.services.ai.tools import tool_read_attack

    def _reader(campaign: str) -> dict:
        return tool_read_attack(campaign, session=session)

    return _reader


def _command_lookup_for_session(
    session: Any,
) -> Optional[Callable[[str], bool]]:
    """Build the Command_Base gap probe (``has_command_for``) bound to ``session``.

    The attack flow agent flags a campaign technique as a gap when the Command_Base can
    back it with no concrete command (Req. 11.4). Given the request-scoped
    ``Session`` the Run_Service threaded onto the node, this returns a thin
    ``technique_id -> bool`` callable delegating to
    :meth:`app.services.command_base.CommandBase.has_command_for` — the same
    DB-sourced Command_Base the operationalization agent resolves commands from
    (``technique_commands`` relational mapping + pgvector similarity fallback),
    never disk (Req. 11.2, 12.1, 12.3). With no session it returns ``None`` so the
    gap probe is skipped and no gaps are recorded (no disk read).
    """
    if session is None:
        return None

    from app.services.command_base import CommandBase

    command_base = CommandBase(session)

    def _probe(technique_id: str) -> bool:
        return command_base.has_command_for(technique_id)

    return _probe


def _campaign_profile(session: Any, campaign_id: str) -> dict[str, Any]:
    """Name, aliases, description and techniques of the campaign (database).

    Resolves the campaign as ``curated_slug`` or ``stix_id`` in the most recent
    STIX package. Without a session or a match it returns empty values so the
    agent still runs (``created`` path, no technique restriction).
    """
    empty: dict[str, Any] = {
        "name": None,
        "aliases": [],
        "description": "",
        "techniques": [],
    }
    if session is None or not campaign_id:
        return empty
    try:
        from sqlalchemy import or_, select

        from app.models.domain import (
            StixCampaign,
            StixCampaignTechnique,
            StixPackage,
        )
        from app.services.flow.generator import TechniqueElement

        campaign = session.execute(
            select(StixCampaign)
            .join(StixPackage, StixPackage.id == StixCampaign.package_id)
            .where(
                or_(
                    StixCampaign.curated_slug == campaign_id,
                    StixCampaign.stix_id == campaign_id,
                )
            )
            .order_by(StixPackage.downloaded_at.desc(), StixPackage.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if campaign is None:
            return empty
        rows = (
            session.execute(
                select(StixCampaignTechnique)
                .where(StixCampaignTechnique.campaign_id == campaign.id)
                .order_by(StixCampaignTechnique.id)
            )
            .scalars()
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - profile is optional context
        logger.warning(
            "Flow node could not read the campaign profile (%s: %s).",
            type(exc).__name__,
            exc,
        )
        return empty
    aliases = campaign.aliases if isinstance(campaign.aliases, list) else []
    return {
        "name": campaign.name,
        "aliases": [a for a in aliases if isinstance(a, str)],
        "description": campaign.description or "",
        "techniques": [
            TechniqueElement(
                technique_id=row.technique_id,
                technique_name=row.technique_name,
                tactic=row.tactic,
                node_id=row.technique_id,
            )
            for row in rows
        ],
    }


def _flow_retriever_for_session(session: Any, memory: Any) -> Any:
    """``FlowRetriever`` bound to ``session`` (``None`` without a session)."""
    if session is None:
        return None
    from app.services.attack_flow.retriever import FlowRetriever

    return FlowRetriever(session, memory=memory)


def _corpus_flow_loader_for_session(
    session: Any,
) -> Optional[Callable[[int], Optional[dict]]]:
    """``flow_id -> {"label", "content"}`` over ``attack_flows`` (corpus only)."""
    if session is None:
        return None

    def _load(flow_id: int) -> Optional[dict]:
        from app.models.domain import AttackFlowRow
        from app.models.enums import FlowOrigin as _Origin

        row = session.get(AttackFlowRow, flow_id)
        if row is None or row.origin != _Origin.MITRE_CORPUS:
            return None
        return {"label": row.label, "content": row.content}

    return _load


def _command_ref_resolver_for_session(
    session: Any,
) -> Optional[Callable[[str, Optional[str]], Optional[int]]]:
    """``(technique_id, command) -> technique_commands.id`` over the database."""
    if session is None:
        return None

    def _resolve(technique_id: str, command: Optional[str]) -> Optional[int]:
        from sqlalchemy import select

        from app.models.domain import TechniqueCommand

        stmt = select(TechniqueCommand.id).where(
            TechniqueCommand.technique_id == technique_id
        )
        if command:
            stmt = stmt.where(TechniqueCommand.command == command)
        row = session.execute(stmt.order_by(TechniqueCommand.id).limit(1)).first()
        return int(row[0]) if row is not None else None

    return _resolve


def node(state: "AgentState", *, session: Any = None) -> dict:
    """LangGraph Stage 3 node: produce the AI attack flow and write ``artifacts.flow``.

    Builds an :class:`FlowAgent` with a lazily-resolved SEPINF chat model
    (:func:`app.services.ai.llm.make_chat_model`) and the RAG
    :class:`~app.services.ai.memory.ExperienceMemory` when one was seeded on the
    state, reads the Structural Modeling (Stage 1) via
    :func:`_modeling_from_state`, calls :meth:`FlowAgent.generate` for the
    ``campaign_id``, and returns the state delta the design prescribes
    (Requirement 2.4)::

        {"artifacts": {**state.get("artifacts", {}),
                       "flow": produced[0].model_dump() if produced else None}}

    The chat model and memory are resolved **inside** the node so importing this
    module performs no I/O (Requirement 1.7). Persistence of the AI-origin ``AttackFlowRow``
    row and the ``agent_stage_outputs`` bookkeeping is intentionally **not** done
    here — it is the Session-bound Run_Service's job, driven through
    :func:`persist_flow_stage_output` once this node has produced ``artifacts.flow``
    (Requirement 11.5).
    """
    # Lazy import so importing this module requires neither the LangChain client
    # nor the config/settings chain (Requirement 1.7).
    from app.services.ai.llm import make_chat_model

    chat = make_chat_model()

    # Resolve the request-scoped session the Run_Service threaded into this node
    # (bound kwarg via ``build_graph``), honouring a legacy ``state["_session"]``
    # channel for direct callers/tests.
    resolved_session = session if session is not None else state.get("_session")

    # The RAG memory primes the Agent_Input with items retrieved by similarity
    # (Req. 5.3). Prefer an explicit ``state["memory"]``; otherwise build one over
    # the resolved session so similarity retrieval engages in a real run. With no
    # memory and no session the agent proceeds with an empty experience set.
    memory = state.get("memory")
    if memory is None and resolved_session is not None:
        try:
            from app.services.ai.memory import ExperienceMemory

            memory = ExperienceMemory(resolved_session)
        except Exception as exc:  # noqa: BLE001 - never let RAG setup break the run
            logger.warning(
                "attack flow node could not build the experience memory (%s: %s); "
                "continuing with an empty experience set.",
                type(exc).__name__,
                exc,
            )
            memory = None

    # Build the DB-sourced ATT&CK base-context reader bound to the request-scoped
    # session the Run_Service threaded into this node (task 14.1). The reader
    # resolves the campaign's identity/techniques/actors from the Campaign_Overview
    # (database) for ANY of the 52 campaigns — curated or not — and NEVER reads
    # the legacy CLI data directory on disk (Req. 5.1, 5.2, 5.4). With no session the base
    # context degrades to empty (no disk read).
    attack_reader = _attack_reader_for_session(resolved_session)

    # Build the Command_Base gap probe bound to the request-scoped session the
    # Run_Service threaded into this node (task 14.2). For each node technique the
    # generator consults ``CommandBase.has_command_for`` to decide whether the
    # technique is backed by a concrete command (exact ``technique_commands``
    # mapping or similarity suggestion) or must be recorded as a gap on
    # ``ProducedFlow.gaps`` instead of inventing a command (Req. 11.4). With no
    # session the probe is left unset and no gaps are recorded (no disk read).
    command_lookup = _command_lookup_for_session(resolved_session)

    # Local trace sink injected into the generator: the agent appends its
    # AgentTrace at the LLM call site; we stamp the current iteration and return it
    # under the append-only ``traces`` channel (design §A). The
    # ``Annotated[list[dict], add]`` reducer merges it. The sink is passed through
    # the constructor (not ``generate``) so the seam signature is unchanged.
    sink: list[dict] = []

    # Three-path decision collaborators, bound to the request-scoped session.
    profile = _campaign_profile(resolved_session, state.get("campaign_id", ""))
    flow_retriever = _flow_retriever_for_session(resolved_session, memory)
    generator = FlowAgent(
        chat=chat,
        memory=memory,
        attack_reader=attack_reader,
        command_lookup=command_lookup,
        trace_sink=sink,
        flow_retriever=flow_retriever,
        corpus_flow_loader=_corpus_flow_loader_for_session(resolved_session),
        operationalization=(state.get("artifacts") or {}).get("operationalization"),
        command_ref_resolver=_command_ref_resolver_for_session(resolved_session),
        campaign_name=profile["name"],
        campaign_aliases=profile["aliases"],
        campaign_description=profile["description"],
    )

    modeling = _modeling_from_state(state)
    if not modeling.techniques and profile["techniques"]:
        # The flow phase runs after operationalization and no longer receives a
        # Stage 1 modeling: the campaign techniques come from the database.
        modeling = StructuralModeling(techniques=profile["techniques"])
    produced = generator.generate(state["campaign_id"], modeling)

    iteration = state.get("iteration", 0)
    for trace in sink:
        if trace.get("iteration") is None:
            trace["iteration"] = iteration

    return {
        "artifacts": {
            **state.get("artifacts", {}),
            "flow": produced[0].model_dump() if produced else None,
        },
        "traces": sink,
    }


def persist_flow_stage_output(
    session: "Session",
    *,
    iteration_id: int,
    campaign_id: str,
    produced: ProducedFlow,
    trace: "Optional[dict]" = None,
) -> "AgentStageOutput":
    """Persist a produced AI attack flow as an ``agent_stage_outputs`` row (Requirement 11.5).

    This is the Session-bound persistence hook the ``AgentRunService`` (tasks
    15.x) drives after the Stage 3 :func:`node` has produced ``artifacts.flow``.
    It is factored out of the LangGraph node so the graph runtime stays free of
    database access and the Run_Service — the only layer that holds the injected
    ``Session`` and the enclosing ``agent_iterations`` row — owns the write.

    Two linked writes, in order:

    1. Persist the AI-origin ``AttackFlowRow`` row so the attack flow is stored exactly like a
       curated one (same portable ``content`` format, same ``AttackFlowRow`` entity, no
       schema migration — the ``FlowOrigin`` CHECK already admits ``gerado_ia``).
       The ``Case`` row
       is upserted first (via ``upsert_case_row``) to satisfy the
       ``attack_flows.campaign_id`` FK, then the ``AttackFlowRow`` row is reconciled **idempotently**
       on ``(campaign_id, origin, identifier)`` — an equivalent AI-origin attack flow is
       reused rather than duplicated (Req. 4.5).
    2. Insert the ``agent_stage_outputs`` row for ``iteration_id`` with
       ``stage = flow``, ``origin = gerado_ia``, the serialized
       ``payload`` (``produced.model_dump()``) and ``flow_id`` linking the
       AI-origin ``AttackFlowRow`` row from step 1.

    Both rows are added and flushed (so ``id``/``flow_id`` are assigned) but **not**
    committed — the transaction boundary belongs to the Run_Service, which commits
    the whole iteration atomically. Returns the persisted
    :class:`~app.models.domain.AgentStageOutput`.

    Audit_Trace (Req. 2.1, 2.5): when ``trace`` is supplied — the matching
    :class:`~app.services.ai.state.AgentTrace` dict the Run_Service looked up from
    ``final_state["traces"]`` by ``(stage, iteration)`` — one 1:1
    ``agent_stage_traces`` row is written for the just-flushed stage output in the
    SAME flush (via :func:`app.services.ai.agents._trace.persist_stage_trace`),
    inside the Run_Service's existing SAVEPOINT. Absent processing elements are
    carried with the explicit ``None`` sentinel.
    """
    # Lazy imports keep module import free of ORM/model/service import cost and
    # any circular-import risk (Requirement 1.7).
    from sqlalchemy import select

    from app.models.domain import AgentStageOutput, AttackFlowRow
    from app.models.enums import AgentStage, TranslationSource
    from app.services.curated_case_reader import upsert_case_row, utcnow as _utcnow

    # Persist the AI-origin AttackFlowRow row:
    # ensure the Case row exists (FK), then reconcile idempotently on
    # (campaign_id, origin, identifier) so re-running a stage does not duplicate.
    upsert_case_row(session, campaign_id, produced.label)

    flow_id = (
        session.execute(
            select(AttackFlowRow.id)
            .where(AttackFlowRow.campaign_id == campaign_id)
            .where(AttackFlowRow.origin == produced.origin)
            .where(AttackFlowRow.identifier == produced.identifier)
        )
        .scalars()
        .first()
    )
    if flow_id is None:
        flow_row = AttackFlowRow(
            campaign_id=campaign_id,
            origin=produced.origin,
            identifier=produced.identifier,
            label=produced.label,
            content=produced.content,
            provenance=produced.provenance,
            has_cycles=bool(produced.content.get("cycles")),
            created_at=_utcnow(),
        )
        session.add(flow_row)
        session.flush()
        flow_id = flow_row.id

    row = AgentStageOutput(
        iteration_id=iteration_id,
        stage=AgentStage.FLOW,
        origin=TranslationSource.AI_GENERATED,
        payload=produced.model_dump(),
        flow_id=flow_id,
    )
    session.add(row)
    session.flush()

    # Write the 1:1 Audit_Trace row for this stage output in the same flush, so
    # it lands inside the Run_Service's per-iteration SAVEPOINT (Req. 2.1, 2.5).
    from app.services.ai.agents._trace import persist_stage_trace

    persist_stage_trace(session, row, trace)
    return row
