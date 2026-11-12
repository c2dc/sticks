"""Stage 4 ``AiOperationalizer`` — the ``gerado_ia`` operationalization agent.

This module hosts :class:`AiOperationalizer`, the AI implementation of the
Stage 4 :class:`~app.services.environment.operationalizer.OperationalizerByOrigin`
seam (task 7.2). It is the **command-semantics core** of the agent system and the
first shippable agent: it plugs into the *existing* per-origin seam with no change
to the seam signature, exactly like the curation implementation
(the former curated operationalizer).

What it does (Req. 2.2, 2.4, 2.5, 8.1):

* ``origin()`` stamps :attr:`TranslationSource.AI_GENERATED` (``"gerado_ia"``) on
  everything it produces — the ``ProducedOperationalization.origin`` and the
  derived ``EnvironmentParameters.origin``.
* ``operationalize(campaign, flow)`` walks the Stage 3 :class:`FlowView`, and for
  each node's ATT&CK technique **selects and parametrizes** concrete Atomic Red
  Team commands (via the ``tool_atomic_lookup`` command pool), assembles the two
  separate Stage 4 artifacts — the ordered Abilities (``Stage2Output``) and the
  derived :class:`EnvironmentParameters` — and returns them in a
  :class:`ProducedOperationalization` with ``origin=gerado_ia``.

Boundaries (safety, Req. 8.1):

The agent only **proposes**. Every command it emits remains subject to the
*unchanged* downstream containment check — this agent never executes anything,
never touches the host, and runs no shell. It self-checks each proposed command
with the containment parser (``tool_parse_command``) purely to avoid proposing a
command the downstream gate would reject; the gate itself stays authoritative.

Laziness (Req. 1.7):

The LLM chat model, the RAG :class:`ExperienceMemory` priming, the tool wrappers
and every heavy dependency are **injected lazily** — the constructor only stores
what it is handed, and each collaborator is resolved *inside* the methods, so
importing this module opens no socket, spawns no subprocess, and touches no
database. When a collaborator is not injected it is resolved from the default
lazy factories/wrappers on first use; the agent still degrades to a deterministic
selection when no LLM/RAG is available, so it remains usable (and testable)
without a live SEPINF endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Optional

from app.models.enums import AgentStage, TranslationSource
from app.services.environment.operationalizer import ProducedOperationalization

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from app.services.ai.memory import ExperienceMemory
    from app.services.ai.state import AgentState
    from app.services.flow.flow_view import FlowView
    from app.services.environment.operationalization import (
        EnvironmentParameters,
        TargetHost,
    )
    from app.services.emulation.views import Stage2Output

__all__ = ["AiOperationalizer"]


class AiOperationalizer:
    """Stage 4 agent satisfying :class:`OperationalizerByOrigin` structurally.

    Selects and parametrizes concrete commands from the Atomic Red Team command
    pool for each attack flow node, and derives the :class:`EnvironmentParameters`. The
    LLM chooses/adapts the atomic tests (primed by cross-campaign RAG experience);
    the command parser self-checks each proposal, and the project's
    service-inference heuristic is reused to derive the services to provision. It
    stamps ``gerado_ia`` on every produced artifact (Req. 2.4, 2.5).

    The agent only PROPOSES: emitted commands remain subject to the unchanged
    downstream containment check (Req. 8.1). This class exposes exactly
    ``origin()`` + ``operationalize()`` with the seam's signatures, so it is
    injectable in place of the curation operationalizer without any downstream
    change (structural duck typing — no explicit inheritance).
    """

    def __init__(
        self,
        *,
        chat: Any = None,
        memory: "ExperienceMemory | None" = None,
        atomic_lookup: "Callable[[str], list[dict]] | None" = None,
        parser: "Callable[[str], dict] | None" = None,
        trace_sink: "Optional[list[dict]]" = None,
    ) -> None:
        """Assemble the agent with lazily-injected collaborators.

        Nothing here performs I/O (Req. 1.7): the collaborators are only stored.
        Each one is resolved on first use inside :meth:`operationalize` — the
        tool wrappers from :mod:`app.services.ai.tools`, the LLM factory from
        :mod:`app.services.ai.llm` and the RAG :class:`ExperienceMemory` — so the
        agent can be constructed (and unit-tested with fakes) without a live
        backend.

        Args:
            chat: LangChain chat model used to select/adapt the atomic tests.
                When ``None`` the agent is not primed by the LLM and falls back to
                a deterministic selection of the command pool; it never builds a
                chat model at import time.
            memory: RAG :class:`ExperienceMemory` used to prime the proposal with
                known-good command shapes from *other* campaigns. Optional; when
                ``None`` the agent proposes without RAG priming.
            atomic_lookup: The Atomic Red Team command source — a callable
                ``technique_id -> list[dict]``. Defaults lazily to
                :func:`app.services.ai.tools.tool_atomic_lookup`.
            parser: The containment command parser used for self-checking a
                proposed command's destinations — a callable ``command -> dict``.
                Defaults lazily to :func:`app.services.ai.tools.tool_parse_command`.
        """
        self._chat = chat
        self._memory = memory
        self._atomic = atomic_lookup
        self._parser = parser
        # Optional Audit_Trace sink injected by the LangGraph ``node`` wrapper,
        # carried as an instance attribute — NOT an ``operationalize`` parameter —
        # so the ``@runtime_checkable`` ``OperationalizerByOrigin`` seam signature
        # (``operationalize(campaign, flow)``) is byte-for-byte unchanged (design
        # §A). When ``None`` no trace is captured and behavior is unchanged.
        self._trace_sink = trace_sink

    # -- Seam API -----------------------------------------------------------

    def origin(self) -> TranslationSource:
        """Stamp ``gerado_ia`` on the Stage 4 output (Req. 2.4, 2.5)."""
        return TranslationSource.AI_GENERATED

    def operationalize(
        self, campaign: str, flow: "FlowView"
    ) -> ProducedOperationalization:
        """Produce the two Stage 4 artifacts for ``campaign`` with ``origin=gerado_ia``.

        Walks the Stage 3 :class:`FlowView` and, per node, looks up the Atomic Red
        Team command pool for the node's technique, selects/parametrizes the
        commands (LLM-primed by RAG when available, deterministic otherwise), and
        assembles:

        1. the **ordered Abilities** (``Stage2Output`` with ``atomic_ordering``
           and ``translation_source=gerado_ia``); ``None`` when the attack flow yields no
           operationalizable technique (Req. 8.1), mirroring the curation seam's
           suppressed output; and
        2. the derived :class:`EnvironmentParameters` (hosts resolved from the
           proposed commands' internal destinations, services inferred by the
           reused heuristic, ``network_context`` notes), with the platform always
           ``CONTAINER``.

        The two artifacts come out in **separate** fields of
        :class:`ProducedOperationalization` (Req. 8.4). Every emitted command is
        still subject to the unchanged downstream containment check — this method
        only proposes (Req. 8.1).

        Audit_Trace capture (Req. 1.2, 1.3, 2.2, 2.3): when the node wrapper
        injected an ``trace_sink`` (a ``list[dict]``) into the constructor, the
        agent appends a single :class:`~app.services.ai.state.AgentTrace` dict
        capturing its LLM call sites — ``agent_input`` carrying the attack flow (the
        prior-stage artifact used as the modeling for this stage), the retrieved
        experience and the (absent) attack context / prior artifacts;
        ``processing`` carrying the per-node ``llm_prompt`` / raw ``llm_response``,
        the ``reasoning``, the ``tools_invoked`` and the (absent) repair fields.
        The sink is an injected instance attribute (NOT an ``operationalize``
        parameter) so the ``@runtime_checkable`` ``OperationalizerByOrigin`` seam
        signature is byte-for-byte unchanged; genuinely-absent elements are
        recorded with an explicit ``None`` sentinel the serialization layer maps to
        "not available".
        """
        experience = self._prime_experience(campaign, flow)

        # Per-node LLM prompt/response capture for the trace (populated by the
        # selection pass when an LLM is injected; stays empty on the deterministic
        # fallback so the trace honestly records "no LLM call").
        llm_calls: list[dict[str, Any]] = []
        selections = self._select_per_node(
            campaign, flow, experience, llm_calls=llm_calls
        )

        abilities = self._build_abilities(selections)
        parameters = self._derive_parameters(campaign, selections)

        if self._trace_sink is not None:
            tools_invoked = ["tool_atomic_lookup", "tool_parse_command"]
            if self._memory is not None:
                tools_invoked.insert(0, "experience_memory.retrieve")
            self._trace_sink.append(
                {
                    "stage": AgentStage.OPERATIONALIZATION.value,
                    "iteration": None,
                    "agent_input": {
                        "modeling": self._flow_summary(flow),
                        "retrieved_experience": experience,
                        "attack_context": None,
                        "prior_artifacts": {"flow": self._flow_summary(flow)},
                    },
                    "processing": {
                        "llm_prompt": [call["prompt"] for call in llm_calls]
                        if llm_calls
                        else None,
                        "llm_response": [call["response"] for call in llm_calls]
                        if llm_calls
                        else None,
                        "reasoning": None,
                        "tools_invoked": tools_invoked,
                        "repair_attempts": 0,
                        "repair_hints": None,
                    },
                }
            )

        return ProducedOperationalization(
            campaign_id=campaign,
            abilities=abilities,
            parameters=parameters,
            origin=self.origin(),
        )

    @staticmethod
    def _flow_summary(flow: "FlowView") -> dict[str, Any]:
        """A compact, JSON-native summary of the attack flow for the trace ``agent_input``."""
        nodes = []
        for node in getattr(flow, "nodes", []) or []:
            nodes.append(
                {
                    "node_id": getattr(node, "node_id", None),
                    "technique_id": getattr(node, "technique_id", None),
                    "technique_name": getattr(node, "technique_name", None),
                    "tactic": getattr(node, "tactic", None),
                }
            )
        return {
            "campaign_id": getattr(flow, "campaign_id", None),
            "name": getattr(flow, "name", None),
            "nodes": nodes,
        }

    # -- Collaborators resolved lazily --------------------------------------

    def _atomic_lookup(self) -> "Callable[[str], list[dict]]":
        """Resolve the Atomic Red Team command source, defaulting lazily."""
        if self._atomic is not None:
            return self._atomic
        from app.services.ai.tools import tool_atomic_lookup

        return tool_atomic_lookup

    def _parse_command(self) -> "Callable[[str], dict]":
        """Resolve the containment self-check parser, defaulting lazily."""
        if self._parser is not None:
            return self._parser
        from app.services.ai.tools import tool_parse_command

        return tool_parse_command

    def _prime_experience(
        self, campaign: str, flow: "FlowView"
    ) -> list[dict[str, Any]]:
        """Retrieve cross-campaign RAG experience to prime the proposal (Req. 2.2).

        Uses the injected :class:`ExperienceMemory` to fetch known-good command
        shapes from *other* campaigns. The memory already degrades to an empty set
        when unreachable, so a missing/failing memory simply yields no priming and
        the agent proposes without it.
        """
        if self._memory is None:
            return []
        query = self._experience_query(flow)
        try:
            return self._memory.retrieve(campaign_id=campaign, query=query)
        except Exception:  # noqa: BLE001 - priming is best-effort (Req. 5.7)
            return []

    @staticmethod
    def _experience_query(flow: "FlowView") -> str:
        """Build the RAG query text from the attack flow's techniques (traceable, compact)."""
        parts: list[str] = [getattr(flow, "name", "") or getattr(flow, "campaign_id", "")]
        for node in getattr(flow, "nodes", []) or []:
            technique_id = getattr(node, "technique_id", None)
            technique_name = getattr(node, "technique_name", None)
            if technique_id or technique_name:
                parts.append(f"{technique_id or ''} {technique_name or ''}".strip())
        return "\n".join(part for part in parts if part)

    # -- Per-node command selection/parametrization -------------------------

    def _select_per_node(
        self,
        campaign: str,
        flow: "FlowView",
        experience: list[dict[str, Any]],
        *,
        llm_calls: "Optional[list[dict[str, Any]]]" = None,
    ) -> list[dict[str, Any]]:
        """Select/parametrize one Ability worth of commands per attack flow node (Req. 2.2).

        For each node carrying a technique, fetches the Atomic Red Team command
        pool and picks the concrete commands to propose. When an LLM is injected
        it is asked to choose/adapt the atomic tests (primed by ``experience``);
        otherwise the agent falls back to a deterministic selection of the pool so
        it stays usable without a live endpoint. Each chosen command is
        self-checked with the containment parser (purely to avoid proposing what
        the downstream gate would reject — the gate stays authoritative, Req. 8.1).

        Returns one selection dict per operationalizable node, in attack flow order::

            {
                "node_id": str,
                "ability_id": str,
                "technique_id": str | None,
                "technique_name": str | None,
                "tactic": str | None,
                "commands": [{"name", "platform", "command"}...],
            }
        """
        lookup = self._atomic_lookup()
        selections: list[dict[str, Any]] = []

        for node in getattr(flow, "nodes", []) or []:
            technique_id = getattr(node, "technique_id", None)
            if not technique_id:
                # No technique to operationalize for this node (Req. 8.1).
                continue

            pool = lookup(technique_id) or []
            if not pool:
                continue

            chosen = self._choose_commands(
                node, pool, experience, llm_calls=llm_calls
            )
            if not chosen:
                continue

            # Self-check each proposed command (best-effort; the downstream
            # containment gate remains the authoritative check — Req. 8.1).
            self._self_check(chosen)

            selections.append(
                {
                    "node_id": getattr(node, "node_id", None)
                    or (technique_id or "node"),
                    "ability_id": self._ability_id_for(node, pool, len(selections)),
                    "technique_id": technique_id,
                    "technique_name": getattr(node, "technique_name", None)
                    or self._first_nonempty(pool, "technique_name"),
                    "tactic": getattr(node, "tactic", None)
                    or self._first_nonempty(pool, "tactic"),
                    "commands": chosen,
                }
            )

        return selections

    def _choose_commands(
        self,
        node: Any,
        pool: list[dict[str, Any]],
        experience: list[dict[str, Any]],
        *,
        llm_calls: "Optional[list[dict[str, Any]]]" = None,
    ) -> list[dict[str, Any]]:
        """Pick the concrete commands to propose for ``node`` from its ``pool``.

        With an LLM injected, delegates the selection/adaptation to it; on any LLM
        error (or when no LLM is injected) falls back to the deterministic default
        so the agent never hard-fails on priming alone. The returned commands are
        normalized to ``{"name", "platform", "command"}`` entries. When
        ``llm_calls`` is supplied the LLM prompt/response for this node is recorded
        into it for the Audit_Trace.
        """
        if self._chat is not None:
            try:
                chosen = self._choose_with_llm(
                    node, pool, experience, llm_calls=llm_calls
                )
                if chosen:
                    return chosen
            except Exception:  # noqa: BLE001 - degrade to deterministic selection
                pass
        return self._default_selection(pool)

    def _choose_with_llm(
        self,
        node: Any,
        pool: list[dict[str, Any]],
        experience: list[dict[str, Any]],
        *,
        llm_calls: "Optional[list[dict[str, Any]]]" = None,
    ) -> list[dict[str, Any]]:
        """Ask the injected LLM to select/adapt the atomic tests for ``node``.

        Builds a compact prompt from the technique, the candidate command pool and
        the retrieved cross-campaign experience, and invokes the chat model. The
        model is expected to return a JSON array of command strings; anything it
        returns is matched back against the pool (so the agent only proposes
        commands that exist in the Atomic Red Team source) and normalized. A reply
        that does not map onto the pool yields an empty selection, letting the
        caller fall back deterministically.
        """
        import json

        prompt = self._build_prompt(node, pool, experience)
        reply = self._chat.invoke(prompt)
        text = getattr(reply, "content", reply)
        if not isinstance(text, str):
            text = str(text)

        if llm_calls is not None:
            llm_calls.append(
                {
                    "node_id": getattr(node, "node_id", None)
                    or getattr(node, "technique_id", None),
                    "prompt": prompt,
                    "response": text,
                }
            )

        try:
            proposed = json.loads(text)
        except (ValueError, TypeError):
            return []
        if not isinstance(proposed, list):
            return []

        by_command = {entry.get("command"): entry for entry in pool}
        chosen: list[dict[str, Any]] = []
        for item in proposed:
            command = item if isinstance(item, str) else None
            if command and command in by_command:
                chosen.append(self._normalize_command(by_command[command]))
        return chosen

    @staticmethod
    def _build_prompt(
        node: Any,
        pool: list[dict[str, Any]],
        experience: list[dict[str, Any]],
    ) -> str:
        """Render the command-selection prompt (technique + pool + RAG experience)."""
        technique_id = getattr(node, "technique_id", None)
        technique_name = getattr(node, "technique_name", None)
        candidates = [entry.get("command") for entry in pool if entry.get("command")]
        hints = [hit.get("content") for hit in experience if hit.get("content")]
        return (
            "You operationalize one ATT&CK technique into contained lab commands.\n"
            f"Technique: {technique_id} {technique_name or ''}\n"
            "Candidate Atomic Red Team commands (choose a subset, verbatim):\n"
            + "\n".join(f"- {cmd}" for cmd in candidates)
            + "\nKnown-good shapes from past campaigns:\n"
            + "\n".join(f"- {hint}" for hint in hints)
            + "\nReturn a JSON array of the chosen command strings only."
        )

    @classmethod
    def _default_selection(cls, pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deterministic fallback: propose the first command per unique platform.

        Keeps the selection small and stable (one command per executor platform,
        in pool order) so the agent produces a usable proposal without an LLM.
        """
        chosen: list[dict[str, Any]] = []
        seen_platforms: set[Optional[str]] = set()
        for entry in pool:
            command = (entry.get("command") or "").strip()
            if not command:
                continue
            platform = entry.get("platform")
            if platform in seen_platforms:
                continue
            seen_platforms.add(platform)
            chosen.append(cls._normalize_command(entry))
        if not chosen and pool:
            # Every entry shared a platform with no command; fall back to the
            # first entry that carries a command.
            for entry in pool:
                if (entry.get("command") or "").strip():
                    chosen.append(cls._normalize_command(entry))
                    break
        return chosen

    @staticmethod
    def _normalize_command(entry: dict[str, Any]) -> dict[str, Any]:
        """Project an atomic-pool entry into the proposed-command shape."""
        return {
            "name": entry.get("name"),
            "platform": entry.get("platform"),
            "command": (entry.get("command") or "").strip(),
        }

    def _self_check(self, commands: list[dict[str, Any]]) -> None:
        """Self-check each proposed command through the containment parser.

        Annotates each command with the parser's verdict (destinations and their
        internal/external classification) so the proposal carries the same signal
        the downstream gate uses. This never blocks or executes anything — the
        unchanged containment gate remains the authoritative check (Req. 8.1); a
        parser error is tolerated so self-checking cannot break a proposal.
        """
        parse = self._parse_command()
        for command in commands:
            text = command.get("command") or ""
            try:
                command["parsed"] = parse(text)
            except Exception:  # noqa: BLE001 - self-check is advisory only
                command["parsed"] = None

    @staticmethod
    def _ability_id_for(node: Any, pool: list[dict[str, Any]], index: int) -> str:
        """Pick a stable ability id for a node's proposed Ability.

        Prefers the pool's source ability id (the Atomic Red Team source the
        commands came from); falls back to a deterministic id built from the
        node/technique so the ordering stays reproducible.
        """
        for entry in pool:
            ability_id = entry.get("ability_id")
            if ability_id:
                return str(ability_id)
        node_id = getattr(node, "node_id", None)
        technique_id = getattr(node, "technique_id", None)
        return str(node_id or technique_id or f"ability-{index}")

    @staticmethod
    def _first_nonempty(pool: list[dict[str, Any]], key: str) -> Optional[str]:
        """Return the first non-empty ``key`` value across the command pool."""
        for entry in pool:
            value = entry.get(key)
            if value:
                return value
        return None

    # -- Artifact assembly --------------------------------------------------

    def _build_abilities(
        self, selections: list[dict[str, Any]]
    ) -> Optional["Stage2Output"]:
        """Assemble the ordered Abilities (1st artifact) from the selections.

        Mirrors the curation ``Stage2Output`` shape — one
        ``Stage2AbilityView`` per operationalized node (its executors are the
        proposed commands), the ``atomic_ordering`` following attack flow order, and
        ``translation_source=gerado_ia``. Returns ``None`` when nothing was
        operationalizable (suppressed output — Req. 8.1).
        """
        if not selections:
            return None

        # Imported lazily so importing this module stays light.
        from app.services.emulation.views import (
            Stage2AbilityView,
            Stage2AdversaryView,
            Stage2ExecutorView,
            Stage2Output,
        )

        ability_views: list[Stage2AbilityView] = []
        ordering: list[str] = []
        for selection in selections:
            ability_id = selection["ability_id"]
            ordering.append(ability_id)
            ability_views.append(
                Stage2AbilityView(
                    ability_id=ability_id,
                    name=selection.get("technique_name"),
                    technique_id=selection.get("technique_id"),
                    technique_name=selection.get("technique_name"),
                    tactic=selection.get("tactic"),
                    description=None,
                    executors=[
                        Stage2ExecutorView(
                            name=command.get("name"),
                            platform=command.get("platform"),
                            command=command.get("command"),
                        )
                        for command in selection["commands"]
                    ],
                    translation_source=TranslationSource.AI_GENERATED,
                )
            )

        adversary_view = Stage2AdversaryView(
            id=f"adversary-{selections[0]['node_id']}",
            name="Adversário gerado por IA",
            description=(
                "Agrupamento de Abilities proposto pelo agente de IA a partir do "
                "attack flow (origem gerado_ia)."
            ),
            atomic_ordering=list(ordering),
            translation_source=TranslationSource.AI_GENERATED,
        )

        return Stage2Output(
            abilities=ability_views,
            ordering=ordering,
            adversary=adversary_view,
            translation_source=TranslationSource.AI_GENERATED,
            human_curation=False,
            has_abilities=True,
            has_adversary=True,
        )

    def _derive_parameters(
        self, campaign: str, selections: list[dict[str, Any]]
    ) -> "EnvironmentParameters":
        """Derive the :class:`EnvironmentParameters` (2nd artifact) from the proposal.

        Resolves target hosts from the proposed commands' **internal** destinations
        (via the containment parser + ``container_for_ip``), infers the services to
        provision by **reusing** the project's service-inference heuristic
        (:func:`app.services.environment.operationalizer._infer_services`), and
        collects no network notes (the attack flow view carries only the technique
        identity, not the curated per-node ``network_context``). The platform is
        always ``CONTAINER`` and the origin ``gerado_ia`` (Req. 2.4, 2.5, 17.1).
        """
        # Lazy imports (Req. 1.7): no containment/DTO deps at module import.
        from app.services.containment import (
            container_for_ip,
            is_internal_ip,
            parse_command,
        )
        from app.services.environment.operationalization import (
            EnvironmentParameters,
            EnvironmentPlatform,
            RequiredService,
            TargetHost,
        )
        from app.services.environment.operationalizer import _infer_services

        hosts: list["TargetHost"] = []
        hosts_by_key: dict[str, "TargetHost"] = {}
        services: list[RequiredService] = []
        seen_services: set[tuple[str, Optional[str]]] = set()

        for selection in selections:
            technique_id = selection.get("technique_id")
            commands = [
                command.get("command") or "" for command in selection["commands"]
            ]

            # Target hosts from the internal destinations of the proposed commands.
            for command_text in commands:
                if not command_text:
                    continue
                for dest in parse_command(command_text).destinations:
                    if not is_internal_ip(dest.value):
                        continue
                    key = dest.value
                    if key in hosts_by_key:
                        continue
                    host = TargetHost(
                        label=f"host_{chr(ord('a') + len(hosts))}"
                        if len(hosts) < 26
                        else f"host_{len(hosts)}",
                        address=dest.value,
                        container=container_for_ip(dest.value),
                    )
                    hosts.append(host)
                    hosts_by_key[key] = host

            # Services inferred from the proposed commands (reused heuristic).
            for service in _infer_services(commands, technique_id):
                key_s = (service.description, service.origin_technique)
                if key_s not in seen_services:
                    services.append(service)
                    seen_services.add(key_s)

        return EnvironmentParameters(
            campaign_id=campaign,
            platform=EnvironmentPlatform.CONTAINER,
            hosts=hosts,
            services=services,
            network_context=[],
            origin=self.origin(),
        )

# ---------------------------------------------------------------------------
# LangGraph node wrapper + persistence hook (task 7.3)
# ---------------------------------------------------------------------------
#
# The ``node`` below is the LangGraph node that drives the Stage 4 agent inside
# the compiled StateGraph (assembled later in task 14.2). It is a plain
# module-level function ``node(state) -> dict`` returning a *state delta* — the
# ``StateGraph`` merges the returned keys into the running ``AgentState`` — so it
# fits LangGraph's node contract without this module depending on the graph
# assembly (Req. 1.7).
#
# Persistence is deliberately **factored out** of the node (Req. 11.5): the node
# only produces the artifact and writes it into the state; the
# ``persist_stage_output`` helper is the Run_Service hook (tasks 15.x) that, given
# an injected ``Session`` and the owning ``agent_iterations`` row, writes the
# ``agent_stage_outputs`` row (``origin=gerado_ia``) within the service's atomic
# per-iteration transaction and links ``params_id`` to the persisted
# ``EnvironmentParameters`` row. Keeping it separate means the node stays pure of
# any DB session (and import-time DB deps) while the Run_Service owns the
# transaction boundary.

__all__ = ["AiOperationalizer", "node", "build_agent", "persist_stage_output"]


def build_agent(
    *,
    chat: Any = None,
    memory: "ExperienceMemory | None" = None,
    session: Any = None,
    trace_sink: "Optional[list[dict]]" = None,
) -> AiOperationalizer:
    """Build the Stage 4 :class:`AiOperationalizer`, resolving collaborators lazily.

    Resolves the LLM chat model (:func:`app.services.ai.llm.make_chat_model`) and
    the RAG :class:`~app.services.ai.memory.ExperienceMemory` on demand so that
    importing this module — and calling :func:`node` without a live backend —
    performs no I/O (Req. 1.7). Any collaborator that cannot be resolved (no
    SEPINF config, no DB session for the memory) is simply left unset: the agent
    then degrades to its deterministic selection and proposes without RAG priming,
    so the node stays usable and testable without the SEPINF endpoint.

    Args:
        chat: An explicit chat model; when ``None`` the SEPINF factory is tried
            lazily and any configuration/import failure degrades to ``None``.
        memory: An explicit :class:`ExperienceMemory`; when ``None`` it is built
            from ``session`` if one is provided, else left unset.
        session: A SQLAlchemy ``Session`` used only to construct the default
            :class:`ExperienceMemory` when ``memory`` is not injected.

    Returns:
        A ready :class:`AiOperationalizer` with whatever collaborators resolved.
    """
    resolved_chat = chat
    if resolved_chat is None:
        try:
            from app.services.ai.llm import make_chat_model

            resolved_chat = make_chat_model()
        except Exception:  # noqa: BLE001 - degrade to deterministic selection
            resolved_chat = None

    resolved_memory = memory
    if resolved_memory is None and session is not None:
        try:
            from app.services.ai.memory import ExperienceMemory

            resolved_memory = ExperienceMemory(session)
        except Exception:  # noqa: BLE001 - propose without RAG priming
            resolved_memory = None

    # Bind the Atomic Red Team command pool to the request-scoped session so the
    # pool is sourced from the DB-backed Command_Base, never from disk (task
    # 14.1, Req. 12.1, 12.3, 5.2). With no session the lookup is left unset and
    # the agent's own lazy default yields an empty pool (no disk read).
    resolved_atomic: "Callable[[str], list[dict]] | None" = None
    if session is not None:
        from app.services.ai.tools import tool_atomic_lookup

        def resolved_atomic(technique_id: str) -> list[dict]:  # type: ignore[misc]
            return tool_atomic_lookup(technique_id, session=session)

    return AiOperationalizer(
        chat=resolved_chat,
        memory=resolved_memory,
        atomic_lookup=resolved_atomic,
        trace_sink=trace_sink,
    )


def _techniques_from_state(state: "AgentState", session: Any = None) -> "FlowView":
    """Read the campaign's ATT&CK techniques from ``stix_campaign_techniques``.

    The operationalizer seam (``operationalize(campaign, flow)``) walks
    ``flow.nodes``, so the techniques are handed over as a :class:`FlowView` with
    one node per technique (``node_id`` = ``technique_id``) and no edges: the
    operationalization no longer depends on a generated flow (Req. 5.6).

    The campaign is resolved from ``state["campaign_id"]`` as its ``curated_slug``
    or ``stix_id`` in the most recently downloaded STIX package. Without a
    session, with an unknown campaign or with no techniques, an empty
    (non-curated, unavailable) :class:`FlowView` is returned so the node degrades
    to a suppressed operationalization instead of raising (Req. 8.1).
    """
    from app.services.flow.flow_view import FlowNodeView, FlowView

    campaign_id = state.get("campaign_id", "") or ""
    empty = FlowView(
        campaign_id=campaign_id,
        name=campaign_id,
        curated=False,
        can_advance=False,
        unavailable=True,
    )
    if session is None or not campaign_id:
        return empty

    from sqlalchemy import or_, select

    from app.models.domain import StixCampaign, StixCampaignTechnique, StixPackage

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
    if not rows:
        return empty

    return FlowView(
        campaign_id=campaign_id,
        name=campaign.name or campaign_id,
        curated=bool(campaign.curated),
        can_advance=True,
        nodes=[
            FlowNodeView(
                node_id=row.technique_id,
                technique_id=row.technique_id,
                technique_name=row.technique_name,
                tactic=row.tactic,
            )
            for row in rows
        ],
        edges=[],
    )

def node(state: "AgentState", *, session: Any = None) -> dict:
    """LangGraph node for Stage 4 — operationalize the campaign techniques.

    Builds the :class:`AiOperationalizer` with lazily-resolved collaborators
    (:func:`build_agent`), reads the campaign techniques from ``stix_campaign_techniques``,
    calls :meth:`AiOperationalizer.operationalize` and writes the serialized
    result into the returned **state delta** under
    ``artifacts.operationalization`` (``origin=gerado_ia`` — Req. 2.4). The node
    performs no persistence itself; the Run_Service drives
    :func:`persist_stage_output` to write the ``agent_stage_outputs`` row within
    its atomic per-iteration transaction (Req. 11.5).

    The returned dict preserves the rest of ``state["artifacts"]`` so the attack flow and
    any other stage output already present survive the merge. A ``history`` entry
    records the node's pass for the append-only trace.

    Args:
        state: The running :class:`~app.services.ai.state.AgentState`.

    Returns:
        A state delta: the updated ``artifacts`` (with ``operationalization``
        serialized) plus a one-element ``history`` trace.
    """
    campaign_id = state.get("campaign_id", "") or ""

    # Local trace sink injected into the agent: it appends its AgentTrace at its
    # LLM call sites; we stamp the current iteration and return it under the
    # append-only ``traces`` channel (design §A). The ``Annotated[list[dict], add]``
    # reducer merges it. The sink is passed through the constructor (not
    # ``operationalize``) so the seam signature is unchanged.
    sink: list[dict] = []
    # The session is threaded by ``build_graph`` as a bound kwarg so the agent's
    # Atomic Red Team command pool and RAG come from the DATABASE, never from disk
    # (task 14.1, Req. 12.1, 12.3, 5.2). A legacy ``state["_session"]`` channel is
    # honoured so direct callers/tests keep working.
    resolved_session = session if session is not None else state.get("_session")
    flow = _techniques_from_state(state, resolved_session)
    agent = build_agent(session=resolved_session, trace_sink=sink)
    produced = agent.operationalize(campaign_id, flow)

    iteration = state.get("iteration", 0)
    for trace in sink:
        if trace.get("iteration") is None:
            trace["iteration"] = iteration

    existing_artifacts = dict(state.get("artifacts") or {})
    existing_artifacts["operationalization"] = produced.model_dump(mode="json")

    return {
        "artifacts": existing_artifacts,
        "history": [
            {
                "node": "operationalize",
                "campaign_id": campaign_id,
                "origin": produced.origin.value,
                "has_abilities": produced.abilities is not None,
                "host_count": len(produced.parameters.hosts),
                "service_count": len(produced.parameters.services),
            }
        ],
        "traces": sink,
    }


def persist_stage_output(
    session: Any,
    iteration_id: int,
    produced: ProducedOperationalization,
    *,
    persist_parameters: bool = True,
    trace: Optional[dict] = None,
) -> Any:
    """Run_Service hook: persist the Stage 4 output as an ``agent_stage_outputs`` row.

    Factored out of :func:`node` so the ``AgentRunService`` (tasks 15.x) can drive
    it inside its atomic per-iteration transaction (Req. 11.5): the node stays
    free of any DB session, and this helper owns only the row assembly — it
    ``add``s and ``flush``es onto the **injected** ``session`` but never commits,
    leaving the transaction boundary to the caller.

    It writes one :class:`~app.models.domain.AgentStageOutput` for the
    operationalization stage with ``origin=gerado_ia`` (Req. 2.4) and the
    serialized :class:`ProducedOperationalization` as the JSON ``payload``. When
    ``persist_parameters`` is true it first persists the derived
    :class:`EnvironmentParameters` as an
    :class:`~app.models.domain.EnvironmentParametersRow` (``origin=gerado_ia``)
    and links its id via ``params_id`` — the ``params_id`` branch of the
    per-stage-output FK, exactly as the attack flow stage output links ``flow_id`` (design
    §agent_stage_outputs).

    Args:
        session: The injected SQLAlchemy ``Session`` (the Run_Service's unit of
            work). This helper only ``add``/``flush``es; it does not commit.
        iteration_id: The owning ``agent_iterations`` row id.
        produced: The Stage 4 artifact to persist.
        persist_parameters: When true, persist the derived
            :class:`EnvironmentParameters` and link ``params_id``; when false, the
            caller already holds a params row (or none applies) and only the
            stage-output row is written.
        trace: The matching :class:`~app.services.ai.state.AgentTrace` dict the
            Run_Service looked up from ``final_state["traces"]`` by
            ``(stage, iteration)``. When supplied, one 1:1 ``agent_stage_traces``
            row is written for the just-flushed stage output in the SAME flush,
            inside the Run_Service's existing SAVEPOINT (Req. 2.1, 2.5). Absent
            processing elements are carried with the explicit ``None`` sentinel.

    Returns:
        The persisted :class:`~app.models.domain.AgentStageOutput` instance
        (flushed, so its ``id`` and ``params_id`` are populated).
    """
    # Imported lazily so importing this module needs no DB/model deps (Req. 1.7).
    from app.models.domain import AgentStageOutput, EnvironmentParametersRow
    from app.models.enums import AgentStage, TranslationSource

    params_id: Optional[int] = None
    if persist_parameters:
        parameters = produced.parameters
        params_row = EnvironmentParametersRow(
            campaign_id=parameters.campaign_id,
            platform=parameters.platform,
            content=parameters.model_dump(mode="json"),
            origin=TranslationSource.AI_GENERATED,
            created_at=_utcnow(),
        )
        session.add(params_row)
        session.flush()
        params_id = params_row.id

    stage_output = AgentStageOutput(
        iteration_id=iteration_id,
        stage=AgentStage.OPERATIONALIZATION,
        origin=TranslationSource.AI_GENERATED,
        payload=produced.model_dump(mode="json"),
        params_id=params_id,
    )
    session.add(stage_output)
    session.flush()

    # Write the 1:1 Audit_Trace row for this stage output in the same flush, so
    # it lands inside the Run_Service's per-iteration SAVEPOINT (Req. 2.1, 2.5).
    from app.services.ai.agents._trace import persist_stage_trace

    persist_stage_trace(session, stage_output, trace)
    return stage_output


def _utcnow():
    """Naive-UTC timestamp for the persisted ``EnvironmentParameters`` row.

    Reuses the project's canonical ``_utcnow`` so the agent's rows carry the same
    naive-UTC convention as every other row; imported lazily to keep this module
    import free of DB deps (Req. 1.7).
    """
    from app.models.domain import _utcnow as _domain_utcnow

    return _domain_utcnow()
