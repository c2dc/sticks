"""LangGraph assembly for the AI curation loop.

Will host ``build_graph()`` wiring the ``StateGraph`` nodes (orchestrate -> operationalize -> flow
-> operationalize -> environment -> execute -> evaluate), the linear edges, the
conditional edge after evaluation and the max-iteration guard (task 14.2).

Building the graph is deferred to an explicit ``build_graph()`` call; importing
this module performs no I/O (Requirement 1.7).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

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
# Execution phase reuse (Req. 1.4, 8.1, 8.4, 9.3, 9.5):
#
# ``execute_node`` hands the AI-origin artifacts to the EXISTING, UNCHANGED
# Execution phase :class:`~app.services.operation.runner.OperationRunner`. The runner is
# the single enforcement point of the design's emulation sequence — it runs, in
# order, the unchanged safety machinery:
#
#     containment  ->  isolation (read-only, fail-closed)  ->
#     preview/confirm  ->  execution  ->  1:1 audit  ->  report
#
# The agent executes NOTHING itself and never runs on the host (Req. 9.3, 9.5):
# it only adapts the serialized ``ProducedOperationalization`` (origin
# ``gerado_ia``) into the runner's inputs and reads back the Execution phase report. No
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
    state: AgentState,
    *,
    runner: Any = None,
    session: Any = None,
) -> dict:
    """Execution phase node: run the EXISTING pipeline unchanged over the AI artifacts.

    Hands the current iteration's AI-origin artifacts (``state["artifacts"]``,
    stamped ``gerado_ia``) to the unchanged Execution phase
    :class:`~app.services.operation.runner.OperationRunner`, which enforces the
    full safety sequence (containment -> isolation -> preview/confirm ->
    execution -> 1:1 audit -> report). The agent itself executes nothing and
    never runs on the host (Req. 1.4, 8.1, 8.4, 9.3, 9.5).

    The node is pure plumbing: it adapts the serialized
    ``ProducedOperationalization`` into the runner-facing abilities + adversary,
    invokes the runner with ``confirmed=True`` (the explicit confirmation of an
    orchestrated run; the runner's own confirmation/containment/isolation gates
    remain authoritative), and returns the Execution phase report as a state delta.

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
        A state delta ``{"report": <serialized Execution phase report>, "execution_result":
        <structured execution result>}``. ``report`` is the runner's result
        serialized to a plain dict (as the design sketches,
        ``report.model_dump()``); ``execution_result`` is the explicit,
        decoupled view the ``evaluation`` phase receives — the runner verdict and
        the real containment/isolation state read off the LIVE result object
        before it is flattened (task 4.1, Req. 2.1, 2.2). Keeping the two
        separate makes ``execution`` produce the result and ``evaluation`` (not
        the execute step) render the verdict from it.
    """
    artifacts = state.get("artifacts") or {}
    campaign_id = state.get("campaign_id")

    operationalization = artifacts.get("operationalization") or {}
    abilities, adversary_id, adversary_payload = _runner_inputs(operationalization)

    # Build (or reuse) the UNCHANGED Execution phase runner and invoke it. Everything is
    # resolved lazily so importing this module does no I/O (Req. 1.7).
    if runner is not None:
        # Materialize the AI-generated abilities into ``abilities`` before the
        # runner inserts any ``AbilityResult`` so the parent rows exist in the
        # SAME session, satisfying ``resultados_ability_ability_id_fkey``
        # (Req. 2.1, 2.2). No-op/idempotent for already-present rows (Req. 3.1).
        _materialize_ai_abilities(session, campaign_id, abilities)
        result = runner.run(
            campaign_id=campaign_id,
            abilities=abilities,
            adversary_id=adversary_id,
            confirmed=True,
            adversary_payload=adversary_payload,
        )
        return {
            "report": _report_to_dict(result),
            "execution_result": _execution_result(result),
        }

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
            # Upsert the AI-generated abilities on the SAME session the runner
            # uses, before any ``AbilityResult`` insert, so the FK is satisfied
            # (Req. 2.1, 2.2); idempotent for already-present rows (Req. 3.1).
            _materialize_ai_abilities(session, campaign_id, abilities)
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

    return {
        "report": _report_to_dict(result),
        "execution_result": _execution_result(result),
    }


# -- execute node helpers (task 13.1) -----------------------------------------


class _RunnerAbility:
    """Runner-facing view of an AI-proposed Ability with plain-dict executors.

    Mirrors the curated adapter that used to live in the emulation API: the
    unchanged :class:`OperationRunner` reads ``ability_id`` / ``name`` /
    ``executors`` off each ability and builds the Caldera payload straight from
    ``executors`` (which must be JSON-serialisable). Reconstructed here from the
    serialized :class:`OperationalizationAbilityView` so the runner consumes an AI-origin
    ability exactly like a curated one.
    """

    __slots__ = (
        "ability_id",
        "description",
        "executors",
        "name",
        "tactic",
        "technique_id",
        "technique_name",
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


def _materialize_ai_abilities(
    session: Any,
    campaign_id: Any,
    abilities: list[_RunnerAbility],
) -> None:
    """Upsert the AI-generated abilities into ``abilities`` before Execution phase writes.

    Mirrors the curated upsert in
    the former curated phase persistence (~lines
    2037-2045): for each runner-facing ability, fetch the existing
    :class:`Ability` row by ``ability_id`` or create one, then set the metadata
    the AI artifact already carries (``campaign_id``, ``name``, ``tactic``,
    ``technique_name``, ``technique_id``, ``description``, ``executors``) and
    stamp ``translation_source = AI_GENERATED``. The upsert runs in the SAME
    session the Execution phase :class:`OperationRunner` uses, so the parent rows are
    visible before the runner's ``AbilityResult`` insert, satisfying
    ``resultados_ability_ability_id_fkey`` (Req. 2.1, 2.2).

    The upsert is idempotent (keyed by ``ability_id`` via ``session.get``): a
    MISSING row is created with its full AI metadata and stamped
    ``AI_GENERATED``, while an ALREADY-PRESENT row is left untouched — metadata
    and ``translation_source`` are set ONLY on creation. This preserves the
    provenance (and identity) of a row that already exists, so a curated
    (``HUMAN_CURATION``) row flowing through the same seam keeps its provenance
    and a re-run of an AI iteration is a no-op rather than a stomp (Req. 3.1).
    Abilities with no ``ability_id`` are skipped, mirroring the runner's own
    guard. The ORM models are imported lazily so importing this module performs
    no I/O (Req. 1.7).

    The campaign (``campaigns.id``) parent is guaranteed to exist before this
    runs: the ``POST /api/campaigns/{campaign}/ai/run`` handler materializes the
    ``campaigns`` row via
    :meth:`~app.services.campaign_materializer.CampaignMaterializer.materialize`
    (an idempotent upsert from the STIX) right after the guard and BEFORE the run
    starts, for ANY of the 52 campaigns — curated or not. This node therefore
    RELIES on the parent row already existing and does not create it, so setting
    ``campaign_id`` here never violates the ``abilities.campaign_id ->
    campaigns.id`` FK (Req. 2.4, 2.5, 2.6). The session is flushed so the parent
    ``abilities`` rows are written before the child result insert, mirroring the
    curated path's explicit ``session.flush()`` ordering.
    """
    if session is None or not abilities:
        return

    from app.models.domain import Ability as AbilityModel
    from app.models.enums import TranslationSource

    materialized = False
    for ability in abilities:
        ability_id = getattr(ability, "ability_id", None)
        if not ability_id:
            continue  # mirror the runner's own guard: skip entries with no id

        db_ability = session.get(AbilityModel, ability_id)
        if db_ability is not None:
            # Already-present row: leave it as-is. Overwriting metadata or
            # ``translation_source`` here would stomp the provenance of a row
            # another path owns (e.g. a curated ``HUMAN_CURATION`` row) and
            # break the idempotent-no-op preservation guarantee (Req. 3.1).
            continue

        # Missing row: materialize the AI-generated ability with full metadata
        # so the parent exists before the runner's ``AbilityResult`` insert.
        db_ability = AbilityModel(ability_id=ability_id)
        session.add(db_ability)
        db_ability.campaign_id = campaign_id
        db_ability.name = ability.name
        db_ability.tactic = ability.tactic
        db_ability.technique_name = ability.technique_name
        db_ability.technique_id = ability.technique_id
        db_ability.description = ability.description
        # ``_RunnerAbility.executors`` already carries plain, JSON-serialisable
        # dicts, so no ``model_dump`` is needed as on the curated path.
        db_ability.executors = list(ability.executors)
        db_ability.translation_source = TranslationSource.AI_GENERATED
        materialized = True

    # Flush so the parent ``abilities`` rows are visible before the runner's
    # ``AbilityResult`` insert checks the foreign key, within the same unit of
    # work (mirrors the curated path's explicit ``session.flush()`` ordering).
    if materialized:
        session.flush()


def _runner_inputs(
    operationalization: dict[str, Any],
) -> tuple[list[_RunnerAbility], str, dict[str, Any] | None]:
    """Adapt the serialized AI operationalization into the runner's ``run`` inputs.

    The AI Operationalization phase artifact is a serialized
    :class:`~app.services.environment.operationalizer.ProducedOperationalization`
    whose ``abilities`` field is a serialized
    :class:`~app.services.emulation.views.OperationalizationOutput`. From it we rebuild:

    * the ordered runner-facing abilities (one :class:`_RunnerAbility` per
      ``OperationalizationAbilityView``, in the adversary's ``atomic_ordering`` when present),
    * the ``adversary_id`` (the serialized adversary's id), and
    * the raw ``adversary_payload`` dict Caldera's v2 adversary endpoint expects
      (``adversary_id`` + ``name`` + ``description`` + ``atomic_ordering``),

    exactly as the curated emulation path builds them, so the unchanged runner
    treats the AI-origin artifacts identically. When the operationalization is
    suppressed/empty the abilities list is empty and the adversary id is ``""`` —
    the runner then has an empty chain, which the evaluator reads as a
    failed/blocked iteration.
    """
    operationalization_abilities = operationalization.get("abilities") or {}
    ability_views = operationalization_abilities.get("abilities") or []
    adversary = operationalization_abilities.get("adversary") or {}

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

    # Prefer the adversary's atomic ordering; fall back to the operationalization ordering,
    # then to discovery order, keeping only ids we actually have an ability for.
    ordering = (
        adversary.get("atomic_ordering") or operationalization_abilities.get("ordering") or order
    )
    abilities = [by_id[aid] for aid in ordering if aid in by_id]
    if not abilities:
        abilities = [by_id[aid] for aid in order]

    adversary_id = adversary.get("id") or ""
    adversary_payload: dict[str, Any] | None = None
    if adversary_id:
        adversary_payload = {
            "adversary_id": adversary_id,
            "name": adversary.get("name") or adversary_id,
            "description": adversary.get("description") or "",
            "atomic_ordering": list(ordering),
        }

    return abilities, adversary_id, adversary_payload


def _report_to_dict(result: Any) -> dict[str, Any]:
    """Serialize the Execution phase runner result into a plain JSON-friendly dict.

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


def _execution_result(result: Any) -> dict[str, Any]:
    """Build the structured execution result the ``evaluation`` phase receives.

    This is the decoupling seam of task 4.1 (Req. 2.1, 2.2): the ``execution``
    phase reads the LIVE Execution phase runner result and hands the ``evaluation`` phase
    the real outcome — the runner ``verdict`` (its ``RunOutcome`` value), whether
    the Operation actually executed, and the real containment/isolation
    verdicts — instead of coupling the verdict to the execute step or forcing the
    evaluator to re-derive it from the raw runner internals.

    The containment/isolation booleans are read HERE, from the live result,
    because they are ``@property`` aggregates (``OperationContainmentReport.allowed``
    / ``IsolationResult.passed``) that ``dataclasses.asdict`` drops when the report
    is flattened for the checkpoint — so the execution phase is the one place they
    are still available to record for the evaluator (Req. 2.2). A fake/injected
    result that already carries plain-dict containment/isolation (tests) is
    honoured as-is.

    The returned dict is JSON-friendly (enums reduced to their values) so it is
    checkpoint-serialisable like the report.

    Args:
        result: The Execution phase runner result (a real ``OperationRunResult`` or an
            injected fake exposing ``outcome`` / ``started`` / ``containment`` /
            ``isolation`` / ``total_success`` / ``total_failure`` / ``message``).

    Returns:
        A plain dict following :class:`~app.services.ai.state.ExecutionResult`.
    """
    verdict = _enum_value(getattr(result, "outcome", None))
    total_success = _to_int(getattr(result, "total_success", 0))
    total_failure = _to_int(getattr(result, "total_failure", 0))

    # "concluida" (COMPLETED) with at least one success and no failure is the
    # single passing verdict — the same rule the evaluator applies to the report
    # when no explicit flag is present, kept in sync here so the two agree.
    executed = bool(getattr(result, "started", False))
    verdict_success = verdict == "concluida" and total_success > 0 and total_failure == 0

    return {
        "verdict": verdict,
        "verdict_success": verdict_success,
        "executed": executed,
        "containment_passed": _verdict_flag(getattr(result, "containment", None), "allowed"),
        "isolation_passed": _verdict_flag(getattr(result, "isolation", None), "passed"),
        "total_success": total_success,
        "total_failure": total_failure,
        "message": str(getattr(result, "message", "") or ""),
    }


def _verdict_flag(verdict: Any, attribute: str) -> bool | None:
    """Read a containment/isolation pass flag off the live verdict (or a fake).

    Prefers the verdict object's ``@property`` aggregate (``allowed`` for
    containment, ``passed`` for isolation); falls back to the same key on a
    plain-dict verdict (an injected fake). Returns ``None`` when no pre-flight
    verdict is present, so the evaluator can tell "not evaluated" from
    "evaluated and failed".
    """
    if verdict is None:
        return None
    value = getattr(verdict, attribute, None)
    if value is None and isinstance(verdict, dict):
        value = verdict.get(attribute)
    return None if value is None else bool(value)


def _enum_value(value: Any) -> Any:
    """Reduce an enum member to its ``.value``; pass other values through."""
    return getattr(value, "value", value)


def _to_int(value: Any) -> int:
    """Best-effort coercion to ``int`` (0 on failure)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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
# ``operationalize`` -> ``flow`` -> ...  -> ``evaluate``). Before any phase agent runs it:
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
    state: AgentState,
    *,
    memory: Any = None,
    session: Any = None,
) -> dict:
    """Entry node: seed run state and retrieve cross-campaign experience.

    Loads the campaign context off the shared
    :class:`~app.services.ai.state.AgentState`, retrieves the cross-campaign
    experience that primes the phase-agent prompts, and seeds the loop counters
    (``iteration`` / ``max_iterations``) the conditional-edge guard reads.

    Experience retrieval (Req. 1.2, 1.3): the node asks the RAG
    :class:`~app.services.ai.memory.ExperienceMemory` for the top-``retrieval_k``
    similar curations of OTHER campaigns. When retrieval returns nothing, or the
    memory is unreachable, or no memory/session is available at all, the run
    continues with an EMPTY experience set so the phase agents can still execute
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
    state: AgentState,
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
    missing collaborator — yields an empty experience set so the phase agents can
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
        except Exception as exc:  # noqa: BLE001 -- never let RAG setup break the run
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
    except Exception:  # noqa: BLE001 -- fall back to the memory's own default k
        retrieval_k = 5

    try:
        hits = resolved_memory.retrieve(
            campaign_id=campaign_id,
            query=_context_query(state, campaign_id),
            k=retrieval_k,
        )
    except Exception as exc:  # noqa: BLE001 -- degrade to an empty experience set
        logger.warning(
            "Orchestrator experience retrieval skipped for campaign %s (%s: %s); "
            "continuing with an empty experience set.",
            campaign_id,
            type(exc).__name__,
            exc,
        )
        return []

    return list(hits) if hits else []


def _resolve_max_iterations() -> tuple[int, str | None]:
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
    except Exception as exc:  # noqa: BLE001 -- missing/unreadable config -> default
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


def _context_query(state: AgentState, campaign_id: str) -> str:
    """Build the RAG query that primes retrieval from the campaign context.

    Prefers an explicit ``context_query`` / ``campaign_summary`` seeded on the
    state, falling back to a compact description built from the ``campaign_id`` so
    the query is never empty. Kept deliberately simple: the orchestrator only needs
    a stable text handle for the cross-campaign similarity search.
    """
    for key in ("context_query", "campaign_summary"):
        value = state.get(key)  # type: ignore[call-overload] -- state is a TypedDict and the key is a runtime string from a fixed tuple
        if isinstance(value, str) and value.strip():
            return value.strip()
    if campaign_id:
        return f"AI curation context for campaign {campaign_id}"
    return "AI curation context"


# --- build_graph (task 14.2) ---
#
# This section owns ONLY the ``build_graph`` assembly. It wires the ``StateGraph``
# nodes, the linear edges (START -> orchestrate -> operationalize -> flow ->
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
# Laziness (Req. 1.7): the phase-agent node callables and the LangGraph symbols
# are imported INSIDE ``build_graph`` so importing this module opens no socket,
# spawns no subprocess and touches no database.


def route_after_eval(state: AgentState) -> str:
    """Conditional-edge router: the improvement loop's guard (Req. 3.2-3.5).

    Called after the ``evaluate`` node to decide the loop's next hop. Returns one
    of the four keys ``build_graph`` registered on the conditional edge —
    ``"flow"``, ``"operationalize"``, ``"environment"`` (re-enter that refine
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
    * **Refine (Req. 3.3, 15.2-15.5):** otherwise route back to the evaluator's
      chosen refine target, ``state["refine_phase"]``, normalized to one of the
      three short edge keys ``"flow"`` / ``"operationalize"`` / ``"environment"``
      so the conditional edge re-enters the matching phase agent (``"flow"`` ->
      Flow_Agent, ``"operationalize"`` -> Operationalization_Agent,
      ``"environment"`` -> Environment_Generation). The evaluator writes its
      choice as an :class:`~app.models.enums.AgentPhase` *value* — the semantic,
      number-free phase names (``flow`` / ``operationalization`` /
      ``environment``) — which is mapped here to its short edge key; a short key
      is already canonical and passes through unchanged. When the evaluator left
      the target unset the router defaults to ``"operationalize"`` (the
      command-semantics core, the most common refine target).

    Args:
        state: The shared :class:`~app.services.ai.state.AgentState`; reads
            ``evaluation``, ``iteration``, ``max_iterations`` and ``refine_phase``.

    Returns:
        One of ``"flow"``, ``"operationalize"``, ``"environment"`` or ``"finish"``.
    """
    evaluation = state.get("evaluation") or {}
    if evaluation.get("verdict_success"):
        return "finish"

    if state.get("iteration", 0) + 1 >= state.get("max_iterations", _DEFAULT_MAX_ITERATIONS):
        return "finish"  # guard: abort the loop (Req. 3.4, 3.5)

    return _refine_edge_key(state.get("refine_phase"))


# Map the evaluator's refine target — an ``AgentPhase`` value OR an already-short
# edge key — onto the short conditional-edge key ``build_graph`` registered, so
# exactly one of the three phase agents is re-entered per refining iteration
# (Req. 15.2-15.5). Kept as literals so this module needs no enum import at
# call time (Req. 1.7); the values mirror ``app.models.enums.AgentPhase``'s
# semantic, number-free phase values (``flow`` / ``operationalization`` /
# ``environment``).
_REFINE_PHASE_EDGE_KEYS = {
    # Evaluator-written AgentPhase values (semantic, number-free).
    "flow": "flow",
    "operationalization": "operationalize",
    "environment": "environment",
    # Already-canonical short edge keys (idempotent pass-through).
    "operationalize": "operationalize",
}


def _refine_edge_key(refine_phase: Any) -> str:
    """Normalize ``refine_phase`` to a short conditional-edge key (Req. 15.2-15.5).

    The evaluator's ``_diagnose_failing_phase`` chooses exactly one refine target
    and writes it onto ``state["refine_phase"]`` as an
    :class:`~app.models.enums.AgentPhase` *value*; this maps that value (or an
    already-short key, for robustness) onto the single short edge key the
    conditional edge registered, so the loop re-enters the right phase agent —
    ``"flow"`` -> Flow_Agent (15.3), ``"operationalize"`` ->
    Operationalization_Agent (15.4), ``"environment"`` -> Environment_Generation
    (15.5). An unset/unrecognised target defaults to ``"operationalize"``.
    """
    key = _REFINE_PHASE_EDGE_KEYS.get(refine_phase) if refine_phase else None
    return key or "operationalize"


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


def build_graph(*, checkpointer, session: Any = None):
    """Assemble and compile the AI-curation agent ``StateGraph`` (Req. 3.1-3.3).

    Wires the six nodes of the improvement loop and their edges, following the
    design:

        orchestrate -> operationalize -> flow -> environment -> execute -> evaluate

    Each of those linear phases has exactly one outgoing edge in sequence
    (START -> orchestrate, then each node to the next). After ``evaluate`` a
    conditional edge implements the loop: the router (task 14.3's
    ``route_after_eval``, resolved lazily here) returns one of ``"flow"``,
    ``"operationalize"``, ``"environment"`` to re-enter that phase, or ``"finish"``
    to end the run (mapped to LangGraph's ``END``).

    Laziness (Req. 1.7): the phase-agent node callables and the LangGraph symbols
    are imported INSIDE this function, so importing this module performs no I/O;
    the graph is built only on an explicit ``build_graph`` call.

    Args:
        checkpointer: The LangGraph checkpointer (e.g. the PostgreSQL saver from
            :mod:`app.services.ai.checkpointer`) used to persist the ``AgentState``
            after every node so a run is resumable.
        session: The request-scoped SQLAlchemy ``Session`` the Run_Service owns.
            It is bound into the phase nodes (as a keyword argument via
            :func:`functools.partial`) so the agents assemble their Agent_Input
            from the DATABASE — ``Campaign_Overview`` and the pgvector RAG — and
            NEVER from the legacy CLI data directory on disk (task 14.1, Req. 5.1, 5.2,
            5.4). Binding it here (rather than carrying it on the ``AgentState``)
            keeps the live, non-serializable ``Session`` out of the checkpointed
            state while still reaching every node. When ``None`` the nodes resolve
            no session and the DB-sourced context degrades to empty (no disk read).

    Returns:
        The compiled LangGraph application (``g.compile(checkpointer=checkpointer)``).
    """
    from functools import partial

    from langgraph.graph import END, START, StateGraph

    from app.services.ai.agents import env_agent, evaluator, flow_agent, op_agent
    from app.services.ai.state import AgentState

    g = StateGraph(AgentState)

    # Bind the request-scoped Session into the nodes that assemble the Agent_Input
    # from the database (task 14.1). ``functools.partial`` carries it as a bound
    # kwarg so it never lands on the checkpointed ``AgentState`` (a live Session
    # is not serializable), while still reaching the flow/op agents and the
    # orchestrator/evaluator that build the DB-sourced context and RAG.
    g.add_node(
        "orchestrate", partial(orchestrate_node, session=session)
    )  # seeds state + RAG retrieval (DB)
    g.add_node("flow", partial(flow_agent.node, session=session))  # flow agent (DB)
    g.add_node(
        "operationalize", partial(op_agent.node, session=session)
    )  # Operationalization phase agent (DB)
    g.add_node("environment", env_agent.node)  # Environment phase agent
    g.add_node(
        "execute", partial(execute_node, session=session)
    )  # Execution phase via existing OperationRunner
    g.add_node("evaluate", evaluator.node)  # reads report, writes decision

    # Linear edges: each phase has exactly one outgoing edge in sequence.
    g.add_edge(START, "orchestrate")
    g.add_edge("orchestrate", "operationalize")
    g.add_edge("operationalize", "flow")
    g.add_edge("flow", "environment")
    g.add_edge("environment", "execute")
    g.add_edge("execute", "evaluate")

    # Conditional edge after evaluate: the improvement loop. The router is
    # resolved lazily (task 14.3 owns its guard logic); the mapping routes a
    # refine-target back to its phase node or ends the run.
    g.add_conditional_edges(
        "evaluate",
        _route_after_eval,
        {
            "flow": "flow",
            "operationalize": "operationalize",
            "environment": "environment",
            "finish": END,
        },
    )

    return g.compile(checkpointer=checkpointer)
