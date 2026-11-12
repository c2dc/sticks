"""Contract test for the semantic, number-free AgentStage enum (task 1).

Guards the central rename of the agentic-phase-model-refactor: AgentStage must
expose the semantic phase values (``operationalization``, ``flow``,
``environment``, ``execution``, ``evaluation``) and must NOT expose any legacy
``stage_N_*`` value. A single deterministic test, per the spec test policy
(few high-value deterministic tests, no property tests).

_Requirements: 1.1, 1.2, 1.6, 2.1_
"""

from __future__ import annotations

from app.models.enums import PHASE_ORDER, AgentStage


def test_agent_stage_exposes_semantic_values_without_legacy_numbering() -> None:
    """AgentStage serializes to semantic names and drops the legacy numbering."""
    # Exact semantic value set, in the agentic phase order (Req. 1.1, 1.2, 2.1).
    assert [member.value for member in AgentStage] == [
        "operationalization",
        "flow",
        "environment",
        "execution",
        "evaluation",
    ]

    # Explicit members for the new, own phases (Req. 2.1).
    assert AgentStage.EXECUTION.value == "execution"
    assert AgentStage.EVALUATION.value == "evaluation"

    # No legacy stage_N_* value survives anywhere in the enum (Req. 1.2).
    assert not any(member.value.startswith("stage_") for member in AgentStage)

    # Members are ``str`` so they serialize to their value over the API/DB.
    assert AgentStage.FLOW == "flow"

    # The legacy ``dag`` phase is gone (renamed to ``flow``).
    assert not hasattr(AgentStage, "DAG")


def test_phase_order_puts_operationalization_before_flow() -> None:
    """PHASE_ORDER is the redesigned order and omits the user-only ``input``."""
    assert PHASE_ORDER == (
        "operationalization",
        "flow",
        "environment",
        "execution",
        "evaluation",
    )
    assert "input" not in PHASE_ORDER
