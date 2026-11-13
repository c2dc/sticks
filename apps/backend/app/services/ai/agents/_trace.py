"""Shared Audit_Trace persistence helper for the stage persist hooks (Req. 2.1, 2.5).

Each stage agent's ``persist_*`` hook (``flow_agent.persist_flow_stage_output``,
``op_agent.persist_stage_output``, ``env_agent.persist_stage_output``) persists
its ``agent_stage_outputs`` row and then calls :func:`persist_stage_trace` with
the matching :class:`~app.services.ai.state.AgentTrace` dict — looked up by the
Run_Service from ``final_state["traces"]`` by ``(stage, iteration)`` — to write
the single, 1:1 ``agent_stage_traces`` row for the just-persisted output.

The write lands on the SAME injected ``Session`` and is only ``add``/``flush``ed
(never committed), so it stays inside the Run_Service's existing
``_persist_iterations`` ``begin_nested()`` SAVEPOINT: a trace-persistence failure
rolls the whole iteration back (design §B).

Importing this module performs no I/O; the ORM import is lazy (Requirement 1.7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from sqlalchemy.orm import Session

    from app.models.domain import AgentStageOutput, AgentStageTrace

# The processing elements an ``AgentTrace`` carries. When the trace omits one (a
# stage that ran without an LLM call, a curation fallback that captured nothing),
# the key is recorded with an explicit ``None`` sentinel rather than dropped, so
# the serialization/read layer maps it to the "not available" marker instead of
# silently omitting the stage (design §A "Not available" indicator).
_PROCESSING_KEYS = (
    "llm_prompt",
    "llm_response",
    "reasoning",
    "tools_invoked",
    "repair_attempts",
    "repair_hints",
)


def _normalize_processing(processing: dict | None) -> dict:
    """Return a processing dict with every known element present.

    Genuinely-absent elements are mapped to the explicit ``None`` sentinel (not
    omitted) so the read layer renders "not available" for them (design §A). Any
    extra keys the agent captured (e.g. the Environment agent's
    ``fixed_infrastructure`` / ``agent_decided`` / ``gates`` / ``substages``) are
    preserved as-is.
    """
    source = dict(processing) if isinstance(processing, dict) else {}
    normalized: dict[str, Any] = {key: source.get(key) for key in _PROCESSING_KEYS}
    for key, value in source.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def persist_stage_trace(
    session: Session,
    stage_output: AgentStageOutput,
    trace: dict | None,
) -> AgentStageTrace | None:
    """Write the single 1:1 ``agent_stage_traces`` row for ``stage_output``.

    Builds one :class:`~app.models.domain.AgentStageTrace` linked to the
    just-persisted ``agent_stage_outputs`` row via ``stage_output_id`` (the 1:1
    FK), storing the trace's ``agent_input`` and the normalized ``processing``
    (absent processing elements carried as the explicit ``None`` sentinel). The
    row is ``add``/``flush``ed onto the caller's ``Session`` but never committed,
    so it lands inside the Run_Service's per-iteration SAVEPOINT (design §B).

    When ``trace`` is ``None`` (no matching trace was captured for this stage
    output) nothing is written and ``None`` is returned — the audit trail simply
    carries no trace for that output.

    Args:
        session: The injected ``Session`` owning the per-iteration transaction.
        stage_output: The just-persisted, flushed ``agent_stage_outputs`` row
            (so ``stage_output.id`` is populated for the FK).
        trace: The matching ``AgentTrace`` dict, or ``None`` when absent.

    Returns:
        The persisted :class:`~app.models.domain.AgentStageTrace`, or ``None``
        when no trace was supplied.
    """
    if trace is None:
        return None

    from app.models.domain import AgentStageTrace

    agent_input = trace.get("agent_input")
    if not isinstance(agent_input, dict):
        agent_input = {}

    row = AgentStageTrace(
        stage_output_id=stage_output.id,
        agent_input=agent_input,
        processing=_normalize_processing(trace.get("processing")),
    )
    session.add(row)
    session.flush()
    return row
