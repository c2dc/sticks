"""LangGraph assembly for the AI curation loop.

Will host ``build_graph()`` wiring the ``StateGraph`` nodes (orchestrate -> dag
-> operationalize -> environment -> execute -> evaluate), the linear edges, the
conditional edge after evaluation and the max-iteration guard (task 14.2).

Building the graph is deferred to an explicit ``build_graph()`` call; importing
this module performs no I/O (Requirement 1.7).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from app.services.ai.state import AgentState

logger = logging.getLogger(__name__)


# --- execute node (task 13.1) ---
#
# This section owns ONLY ``execute_node`` (and its private helpers). The
# ``build_graph`` assembly, ``orchestrate_node`` and ``route_after_eval`` belong
# to the Phase-4 graph tasks (14.x) and are intentionally NOT defined here, to
# keep this task's surface minimal and avoid collisions with those tasks.
#
# Stage 6 reuse (Req. 1.4, 8.1, 8.4, 9.3, 9.5):
#
# ``execute_node`` hands the AI-origin artifacts to the EXISTING, UNCHANGED
# Stage 6 :class:`~app.services.operation.runner.OperationRunner`. The runner is
# the single enforcement point of the design's emulation sequence — it runs, in
# order, the unchanged safety machinery:
#
#     containment  ->  isolation (read-only, fail-closed)  ->
#     preview/confirm  ->  execution  ->  1:1 audit  ->  report
#
# The agent executes NOTHING itself and never runs on the host (Req. 9.3, 9.5):
# it only adapts the serialized ``ProducedOperationalization`` (origin
# ``gerado_ia``) into the runner's inputs and reads back the Stage 6 report. No
# gate is modified, bypassed or re-implemented here — the agent proposes, the
# existing machinery disposes. Every command the AI produced is still subject to
# the same containment check and isolation gate the curated path passes through,
# and each executed command yields exactly one audit entry inside the runner.
#
# Laziness (Req. 1.7): every heavy dependency (the runner, the Caldera client,
# the audit logger, the DB session) is imported and constructed INSIDE the
# function, so importing this module opens no socket, spawns no subprocess and
# touches no database.


def execute_node(
    state: "AgentState",
    *,
    runner: Any = None,
    session: Any = None,
) -> dict:
    """Stage 6 node: run the EXISTING pipeline unchanged over the AI artifacts.

    Hands the current iteration's AI-origin artifacts (``state["artifacts"]``,
    stamped ``gerado_ia``) to the unchanged Stage 6
    :class:`~app.services.operation.runner.OperationRunner`, which enforces the
    full safety sequence (containment -> isolation -> preview/confirm ->
    execution -> 1:1 audit -> report). The agent itself executes nothing and
    never runs on the host (Req. 1.4, 8.1, 8.4, 9.3, 9.5).

    The node is pure plumbing: it adapts the serialized
    ``ProducedOperationalization`` into the runner-facing abilities + adversary,
    invokes the runner with ``confirmed=True`` (the explicit confirmation of an
    orchestrated run; the runner's own confirmation/containment/isolation gates
    remain authoritative), and returns the Stage 6 report as a state delta.

    A gate rejection (external destination, non-isolated container, missing
    confirmation) is NOT an error here: the runner returns a non-executed result
    which is serialized into the report so the evaluator (task 13.2) can treat it
    as a failed/blocked iteration eligible for refinement.

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; the AI
            artifacts are read from ``state["artifacts"]``.
        runner: Optional pre-built object exposing the ``OperationRunner.run``
            signature. Injected by tests/callers; when ``None`` a real
            ``OperationRunner`` is constructed lazily over ``session``.
        session: Optional SQLAlchemy ``Session`` used to build the runner and its
            audit logger when ``runner`` is not injected. When ``None`` a
            request-style session is opened lazily from the project's
            ``SessionLocal`` and closed before returning.

    Returns:
        A state delta ``{"report": <serialized Stage 6 report>}`` following the
        design (``return {"report": report.model_dump()}``), where the report is
        the runner's result serialized to a plain dict.
    """
    artifacts = state.get("artifacts") or {}
    campaign_id = state.get("campaign_id")

    operationalization = artifacts.get("operationalization") or {}
    abilities, adversary_id, adversary_payload = _runner_inputs(operationalization)

    # Build (or reuse) the UNCHANGED Stage 6 runner and invoke it. Everything is
    # resolved lazily so importing this module does no I/O (Req. 1.7).
    if runner is not None:
        result = runner.run(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id=adversary_id,
            confirmed=True,
            adversary_payload=adversary_payload,
        )
        return {"report": _report_to_dict(result)}

    owns_session = session is None
    if owns_session:
        from app.db.session import SessionLocal

        session = SessionLocal()
    try:
        from app.services.audit import AuditLogger
        from app.services.caldera import CalderaClient
        from app.services.operation.runner import OperationRunner

        caldera = CalderaClient()
        try:
            built = OperationRunner(
                session=session,
                caldera=caldera,
                audit_logger=AuditLogger(session),
            )
            result = built.run(
                campaign_id=campaign_id,
                abilities=abilities,
                adversary_id=adversary_id,
                confirmed=True,
                adversary_payload=adversary_payload,
            )
        finally:
            close = getattr(caldera, "close", None)
            if callable(close):
                close()
    finally:
        if owns_session:
            session.close()

    return {"report": _report_to_dict(result)}


# -- execute node helpers (task 13.1) -----------------------------------------


class _RunnerAbility:
    """Runner-facing view of an AI-proposed Ability with plain-dict executors.

    Mirrors the curated adapter in :mod:`app.services.ai`/``api.emulation``: the
    unchanged :class:`OperationRunner` reads ``ability_id`` / ``name`` /
    ``executors`` off each ability and builds the Caldera payload straight from
    ``executors`` (which must be JSON-serialisable). Reconstructed here from the
    serialized :class:`Stage2AbilityView` so the runner consumes an AI-origin
    ability exactly like a curated one.
    """

    __slots__ = (
        "ability_id",
        "name",
        "tactic",
        "technique_name",
        "technique_id",
        "description",
        "executors",
    )

    def __init__(self, view: dict[str, Any]) -> None:
        self.ability_id = view.get("ability_id")
        self.name = view.get("name")
        self.tactic = view.get("tactic")
        self.technique_name = view.get("technique_name")
        self.technique_id = view.get("technique_id")
        self.description = view.get("description")
        executors = view.get("executors") or []
        self.executors = tuple(
            {
                "name": executor.get("name"),
                "platform": executor.get("platform"),
                "command": executor.get("command"),
            }
            for executor in executors
            if isinstance(executor, dict)
        )


def _runner_inputs(
    operationalization: dict[str, Any],
) -> tuple[list["_RunnerAbility"], str, Optional[dict[str, Any]]]:
    """Adapt the serialized AI operationalization into the runner's ``run`` inputs.

    The AI Stage 4 artifact is a serialized
    :class:`~app.services.environment.operationalizer.ProducedOperationalization`
    whose ``abilities`` field is a serialized
    :class:`~app.services.stage.stage_service.Stage2Output`. From it we rebuild:

    * the ordered runner-facing abilities (one :class:`_RunnerAbility` per
      ``Stage2AbilityView``, in the adversary's ``atomic_ordering`` when present),
    * the ``adversary_id`` (the serialized adversary's id), and
    * the raw ``adversary_payload`` dict Caldera's v2 adversary endpoint expects
      (``adversary_id`` + ``name`` + ``description`` + ``atomic_ordering``),

    exactly as the curated emulation path builds them, so the unchanged runner
    treats the AI-origin artifacts identically. When the operationalization is
    suppressed/empty the abilities list is empty and the adversary id is ``""`` —
    the runner then has an empty chain, which the evaluator reads as a
    failed/blocked iteration.
    """
    stage2 = operationalization.get("abilities") or {}
    ability_views = stage2.get("abilities") or []
    adversary = stage2.get("adversary") or {}

    by_id: dict[str, _RunnerAbility] = {}
    order: list[str] = []
    for view in ability_views:
        if not isinstance(view, dict):
            continue
        ability = _RunnerAbility(view)
        if ability.ability_id is None:
            continue
        by_id[ability.ability_id] = ability
        order.append(ability.ability_id)

    # Prefer the adversary's atomic ordering; fall back to the Stage2 ordering,
    # then to discovery order, keeping only ids we actually have an ability for.
    ordering = (
        adversary.get("atomic_ordering")
        or stage2.get("ordering")
        or order
    )
    abilities = [by_id[aid] for aid in ordering if aid in by_id]
    if not abilities:
        abilities = [by_id[aid] for aid in order]

    adversary_id = adversary.get("id") or ""
    adversary_payload: Optional[dict[str, Any]] = None
    if adversary_id:
        adversary_payload = {
            "adversary_id": adversary_id,
            "name": adversary.get("name") or adversary_id,
            "description": adversary.get("description") or "",
            "atomic_ordering": list(ordering),
        }

    return abilities, adversary_id, adversary_payload


def _report_to_dict(result: Any) -> dict[str, Any]:
    """Serialize the Stage 6 runner result into a plain JSON-friendly dict.

    The design writes ``{"report": report.model_dump()}``; the unchanged
    :class:`~app.services.operation.runner.OperationRunResult` is a frozen
    dataclass rather than a pydantic model, so this helper accepts either shape:
    it uses ``model_dump()`` when present (a pydantic report / injected fake),
    falls back to :func:`dataclasses.asdict` for the real dataclass result, and
    finally to ``vars()``/identity for anything else. Enum values are reduced to
    their ``.value`` so the report stays checkpoint-serialisable.
    """
    model_dump = getattr(result, "model_dump", None)
    if callable(model_dump):
        return _normalize(model_dump())

    import dataclasses

    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return _normalize(dataclasses.asdict(result))

    if isinstance(result, dict):
        return _normalize(result)

    return _normalize(vars(result)) if hasattr(result, "__dict__") else {"report": result}


def _normalize(value: Any) -> Any:
    """Recursively reduce enums to their values so the report is serialisable."""
    import enum

    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value

# --- orchestrate node (task 14.1) ---
#
# This section owns ONLY ``orchestrate_node`` (and its private helpers). The
# ``build_graph`` assembly (task 14.2) and ``route_after_eval`` (task 14.3)
# belong to the sibling Phase-4 graph tasks and are intentionally NOT defined
# here, nor is ``execute_node`` (task 13.1, above) touched — this keeps the
# three graph tasks collision-free in the shared module.
#
# The orchestrator is the StateGraph entry node (design: ``orchestrate`` ->
# ``dag`` -> ... -> ``evaluate``). Before any stage agent runs it:
#
#   * loads the campaign context and retrieves cross-campaign experience from the
#     RAG :class:`~app.services.ai.memory.ExperienceMemory`, seeding an EMPTY
#     experience set when retrieval yields nothing or the memory is unreachable
#     (Req. 1.2, 1.3 — the memory already degrades to ``[]`` on unavailability);
#   * seeds the short-term-memory counters ``iteration`` (default 0) and
#     ``max_iterations`` from configuration (``SepinfSettings.max_iterations``,
#     default 3 per Req. 3.6), applying the default 3 and recording a warning when
#     the configured value is absent, non-integer, or < 1 (Req. 3.7).
#
# Laziness (Req. 1.7): the settings accessor, the ORM-backed memory and the DB
# session are all imported/constructed INSIDE the function, so importing this
# module opens no socket, spawns no subprocess and touches no database.

# Default max-iteration guard applied when configuration is absent/invalid
# (Req. 3.6, 3.7).
_DEFAULT_MAX_ITERATIONS = 3


def orchestrate_node(
    state: "AgentState",
    *,
    memory: Any = None,
    session: Any = None,
) -> dict:
    """Entry node: seed run state and retrieve cross-campaign experience.

    Loads the campaign context off the shared
    :class:`~app.services.ai.state.AgentState`, retrieves the cross-campaign
    experience that primes the stage-agent prompts, and seeds the loop counters
    (``iteration`` / ``max_iterations``) the conditional-edge guard reads.

    Experience retrieval (Req. 1.2, 1.3): the node asks the RAG
    :class:`~app.services.ai.memory.ExperienceMemory` for the top-``retrieval_k``
    similar curations of OTHER campaigns. When retrieval returns nothing, or the
    memory is unreachable, or no memory/session is available at all, the run
    continues with an EMPTY experience set so the stage agents can still execute
    without RAG priming — the memory itself already degrades to ``[]`` on
    unavailability, and this node additionally tolerates a missing memory/session.

    Iteration seeding (Req. 3.6, 3.7): ``max_iterations`` is read from
    configuration (:attr:`SepinfSettings.max_iterations`, default 3). When the
    configured value is absent, non-integer, or less than 1 the default of 3 is
    applied and a warning is recorded (both logged and carried on the returned
    ``history`` trace) noting the invalid/missing value was replaced. ``iteration``
    is seeded to its current value (default 0) so a resumed run keeps its progress.

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; the
            ``campaign_id`` is read from it and ``iteration``/``max_iterations``
            seeded onto the returned delta.
        memory: Optional pre-built :class:`ExperienceMemory`. Injected by
            tests/callers; when ``None`` one is built lazily from ``session`` (or
            ``state["_session"]``) if a session is available, else retrieval is
            skipped and an empty experience set is used.
        session: Optional SQLAlchemy ``Session`` used only to construct the
            default memory when ``memory`` is not injected.

    Returns:
        A state delta seeding ``retrieved_experience`` (possibly empty),
        ``iteration`` (default 0) and ``max_iterations`` (configured or the
        default 3), plus a one-element append-only ``history`` trace recording the
        seed and any max-iterations warning.
    """
    campaign_id = state.get("campaign_id", "") or ""

    experience = _retrieve_experience(state, campaign_id, memory=memory, session=session)
    max_iterations, warning = _resolve_max_iterations()
    iteration = state.get("iteration", 0)

    trace: dict[str, Any] = {
        "node": "orchestrate",
        "campaign_id": campaign_id,
        "retrieved_experience_count": len(experience),
        "iteration": iteration,
        "max_iterations": max_iterations,
    }
    if warning is not None:
        trace["warning"] = warning

    return {
        "retrieved_experience": experience,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "history": [trace],
    }


# -- orchestrate node helpers (task 14.1) -------------------------------------


def _retrieve_experience(
    state: "AgentState",
    campaign_id: str,
    *,
    memory: Any = None,
    session: Any = None,
) -> list[dict]:
    """Retrieve cross-campaign experience, degrading to an empty set (Req. 1.3).

    Resolves the RAG memory lazily: an injected ``memory`` is used as-is, else one
    is built from the injected ``session`` (or ``state["_session"]``). With no
    memory and no session the node simply proceeds without RAG priming. Any
    retrieval failure — an out-of-range ``k`` guard, an unreachable store, or a
    missing collaborator — yields an empty experience set so the stage agents can
    still execute (Req. 1.2, 1.3; the memory already swallows availability errors
    into ``[]``).
    """
    resolved_memory = memory
    if resolved_memory is None:
        resolved_session = session if session is not None else state.get("_session")
        if resolved_session is None:
            return []
        try:
            from app.services.ai.memory import ExperienceMemory

            resolved_memory = ExperienceMemory(resolved_session)
        except Exception as exc:  # noqa: BLE001 - never let RAG setup break the run
            logger.warning(
                "Orchestrator could not build the experience memory for campaign "
                "%s (%s: %s); continuing with an empty experience set.",
                campaign_id,
                type(exc).__name__,
                exc,
            )
            return []

    try:
        from app.services.ai.config import get_sepinf_settings

        retrieval_k = get_sepinf_settings().retrieval_k
    except Exception:  # noqa: BLE001 - fall back to the memory's own default k
        retrieval_k = 5

    try:
        hits = resolved_memory.retrieve(
            campaign_id=campaign_id,
            query=_context_query(state, campaign_id),
            k=retrieval_k,
        )
    except Exception as exc:  # noqa: BLE001 - degrade to an empty experience set
        logger.warning(
            "Orchestrator experience retrieval skipped for campaign %s (%s: %s); "
            "continuing with an empty experience set.",
            campaign_id,
            type(exc).__name__,
            exc,
        )
        return []

    return list(hits) if hits else []


def _resolve_max_iterations() -> tuple[int, Optional[str]]:
    """Resolve ``max_iterations`` from config, defaulting to 3 (Req. 3.6, 3.7).

    Reads :attr:`SepinfSettings.max_iterations` and returns it when it is a valid
    integer >= 1. When the value is absent, non-integer, or less than 1 — or the
    settings cannot be read at all — the default of 3 is returned together with a
    warning string recording that the invalid/missing value was replaced, which
    the caller both logs and carries on the run trace (Req. 3.7).
    """
    try:
        from app.services.ai.config import get_sepinf_settings

        configured: Any = get_sepinf_settings().max_iterations
    except Exception as exc:  # noqa: BLE001 - missing/unreadable config -> default
        warning = (
            f"max_iterations configuration could not be read ({type(exc).__name__}); "
            f"applying the default value of {_DEFAULT_MAX_ITERATIONS}."
        )
        logger.warning(warning)
        return _DEFAULT_MAX_ITERATIONS, warning

    # Reject booleans (a bool is an int subclass) and any non-integer value.
    if isinstance(configured, bool) or not isinstance(configured, int):
        warning = (
            f"configured max_iterations={configured!r} is absent or non-integer; "
            f"applying the default value of {_DEFAULT_MAX_ITERATIONS}."
        )
        logger.warning(warning)
        return _DEFAULT_MAX_ITERATIONS, warning

    if configured < 1:
        warning = (
            f"configured max_iterations={configured} is less than 1; applying the "
            f"default value of {_DEFAULT_MAX_ITERATIONS}."
        )
        logger.warning(warning)
        return _DEFAULT_MAX_ITERATIONS, warning

    return configured, None


def _context_query(state: "AgentState", campaign_id: str) -> str:
    """Build the RAG query that primes retrieval from the campaign context.

    Prefers an explicit campaign context/summary seeded on the state, falling back
    to a compact description built from the ``campaign_id`` so the query is never
    empty. Kept deliberately simple: the orchestrator only needs a stable text
    handle for the cross-campaign similarity search.
    """
    for key in ("context_query", "campaign_context", "campaign_summary"):
        value = state.get(key)  # type: ignore[call-overload]
        if isinstance(value, str) and value.strip():
            return value.strip()
    if campaign_id:
        return f"AI curation context for campaign {campaign_id}"
    return "AI curation context"


# --- build_graph (task 14.2) ---
#
# This section owns ONLY the ``build_graph`` assembly. It wires the ``StateGraph``
# nodes, the linear edges (START -> orchestrate -> dag -> operationalize ->
# environment -> execute -> evaluate) and the conditional edge after ``evaluate``
# (the improvement loop). It does NOT own the loop's routing logic: the
# max-iteration guard and the refine-target selection live in ``route_after_eval``
# (task 14.3), which this section does not implement.
#
# Forward reference to route_after_eval (Req. 3.1-3.3):
#
# The conditional edge needs a router callable. Rather than depend on 14.3 having
# landed, ``build_graph`` passes a thin dispatcher (``_route_after_eval``) that
# resolves the module-level ``route_after_eval`` name AT CALL TIME. When task 14.3
# defines ``route_after_eval`` in this module, the compiled graph picks it up with
# no change to ``build_graph``. Until then a minimal placeholder is used so the
# graph can be assembled and imported cleanly; invoking the placeholder raises a
# clear error pointing at task 14.3.
#
# Laziness (Req. 1.7): the stage-agent node callables and the LangGraph symbols
# are imported INSIDE ``build_graph`` so importing this module opens no socket,
# spawns no subprocess and touches no database.


def route_after_eval(state: "AgentState") -> str:
    """Conditional-edge router: the improvement loop's guard (Req. 3.2-3.5).

    Called after the ``evaluate`` node to decide the loop's next hop. Returns one
    of the four keys ``build_graph`` registered on the conditional edge —
    ``"dag"``, ``"operationalize"``, ``"environment"`` (re-enter that refine
    target) or ``"finish"`` (mapped to LangGraph's ``END``) — never raising and
    never returning anything else.

    Routing rules, in order:

    * **Passing verdict (Req. 3.2):** when the evaluator's
      ``evaluation["verdict_success"]`` is truthy the loop ends — return
      ``"finish"`` (``Run_Service`` marks the run COMPLETED).
    * **Max-iteration guard (Req. 3.4, 3.5):** otherwise, if completing the
      current iteration would reach ``max_iterations`` (i.e.
      ``iteration + 1 >= max_iterations``) the loop is aborted — return
      ``"finish"`` (``Run_Service`` marks the run ABORTED). This router only
      returns ``"finish"``; the COMPLETED-vs-ABORTED distinction is drawn by
      ``Run_Service`` (task 15) from the final evaluation. The ``+ 1`` counts the
      iteration just evaluated so the loop runs exactly ``1..max_iterations``
      times (``max_iterations`` defaults to 3 per task 14.1 / Req. 3.6).
    * **Refine (Req. 3.3):** otherwise route back to the evaluator's chosen
      refine target, ``state["refine_stage"]`` — one of ``"dag"`` /
      ``"operationalize"`` / ``"environment"`` — defaulting to
      ``"operationalize"`` when the evaluator left it unset.

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; reads
            ``evaluation``, ``iteration``, ``max_iterations`` and ``refine_stage``.

    Returns:
        One of ``"dag"``, ``"operationalize"``, ``"environment"`` or ``"finish"``.
    """
    evaluation = state.get("evaluation") or {}
    if evaluation.get("verdict_success"):
        return "finish"

    if state.get("iteration", 0) + 1 >= state.get("max_iterations", _DEFAULT_MAX_ITERATIONS):
        return "finish"  # guard: abort the loop (Req. 3.4, 3.5)

    return state.get("refine_stage") or "operationalize"


def _route_after_eval(state):
    """Dispatch to the module-level ``route_after_eval`` resolved at call time.

    Looking the router up by name here (rather than binding it when
    ``build_graph`` runs) means a later definition of ``route_after_eval`` — the
    full guard from task 14.3 — is honoured by an already-compiled graph without
    rebuilding it.

    Note: this dispatcher is intentionally left un-annotated. LangGraph inspects
    a branch callable's type hints to infer the conditional edge's input schema;
    an un-annotated dispatcher keeps that inference inert so the graph compiles
    regardless of whether ``AgentState`` is resolvable in this module's runtime
    namespace.
    """
    return globals()["route_after_eval"](state)


def build_graph(*, checkpointer):
    """Assemble and compile the AI-curation agent ``StateGraph`` (Req. 3.1-3.3).

    Wires the six nodes of the improvement loop and their edges, following the
    design:

        orchestrate -> dag -> operationalize -> environment -> execute -> evaluate

    Each of those linear stages has exactly one outgoing edge in sequence
    (START -> orchestrate, then each node to the next). After ``evaluate`` a
    conditional edge implements the loop: the router (task 14.3's
    ``route_after_eval``, resolved lazily here) returns one of ``"dag"``,
    ``"operationalize"``, ``"environment"`` to re-enter that stage, or ``"finish"``
    to end the run (mapped to LangGraph's ``END``).

    Laziness (Req. 1.7): the stage-agent node callables and the LangGraph symbols
    are imported INSIDE this function, so importing this module performs no I/O;
    the graph is built only on an explicit ``build_graph`` call.

    Args:
        checkpointer: The LangGraph checkpointer (e.g. the PostgreSQL saver from
            :mod:`app.services.ai.checkpointer`) used to persist the ``AgentState``
            after every node so a run is resumable.

    Returns:
        The compiled LangGraph application (``g.compile(checkpointer=checkpointer)``).
    """
    from langgraph.graph import END, START, StateGraph

    from app.services.ai.agents import dag_agent, env_agent, evaluator, op_agent
    from app.services.ai.state import AgentState

    g = StateGraph(AgentState)

    g.add_node("orchestrate", orchestrate_node)  # seeds state + RAG retrieval
    g.add_node("dag", dag_agent.node)            # Stage 3 agent
    g.add_node("operationalize", op_agent.node)  # Stage 4 agent
    g.add_node("environment", env_agent.node)    # Stage 5 agent
    g.add_node("execute", execute_node)          # Stage 6 via existing OperationRunner
    g.add_node("evaluate", evaluator.node)       # reads report, writes decision

    # Linear edges: each stage has exactly one outgoing edge in sequence.
    g.add_edge(START, "orchestrate")
    g.add_edge("orchestrate", "dag")
    g.add_edge("dag", "operationalize")
    g.add_edge("operationalize", "environment")
    g.add_edge("environment", "execute")
    g.add_edge("execute", "evaluate")

    # Conditional edge after evaluate: the improvement loop. The router is
    # resolved lazily (task 14.3 owns its guard logic); the mapping routes a
    # refine-target back to its stage node or ends the run.
    g.add_conditional_edges(
        "evaluate",
        _route_after_eval,
        {
            "dag": "dag",
            "operationalize": "operationalize",
            "environment": "environment",
            "finish": END,
        },
    )

    return g.compile(checkpointer=checkpointer)
