"""Agent state schema and reducers.

Defines the ``PhaseArtifacts``, ``EvaluationResult`` and ``AgentState``
TypedDicts along with the append-only ``history`` reducer
(``Annotated[list[dict], add]``) used by the LangGraph ``StateGraph``.

``iteration`` and the running ``history`` form the short-term memory; the
PostgreSQL checkpointer snapshots this ``AgentState`` after every node so a run
can resume (Requirements 1.2, 4.1).

Importing this module performs no I/O (Requirement 1.7).
"""

from __future__ import annotations

from operator import add
from typing import Annotated, TypedDict


class PhaseArtifacts(TypedDict, total=False):
    """Serialized per-phase outputs produced in the current iteration."""

    flow: dict  # ProducedFlow.model_dump()
    operationalization: dict  # ProducedOperationalization.model_dump()
    environment: dict  # ProvisionedEnvironment.model_dump()


class ExecutionResult(TypedDict, total=False):
    """The real execution outcome the ``execution`` phase hands to ``evaluation``.

    This is the explicit contract that decouples the verdict from the execution
    step (Req. 2.1, 2.2): the ``execution`` node (:func:`app.services.ai.graph.
    execute_node`) computes this structured view from the live Execution phase runner
    result — the runner ``verdict`` (its :class:`~app.services.operation.runner.
    RunOutcome` value), the containment/isolation booleans and whether the
    Operation executed to completion — and the ``evaluation`` node reads it as its
    input rather than re-deriving the verdict from the raw runner internals.

    The derived ``containment_passed`` / ``isolation_passed`` booleans are carried
    here explicitly because the serialized ``report`` only preserves the runner's
    dataclass FIELDS (``abilities`` / ``verdicts``), not the ``@property``
    aggregates (``allowed`` / ``passed``): the execution phase, which still holds
    the live result object, is the one place those verdicts are available, so it
    records them for the evaluator (Req. 2.2 — real containment/isolation state).
    """

    verdict: str  # the runner RunOutcome value, e.g. "completed" / "isolation_failed"
    verdict_success: bool  # True iff the Operation ran to completion and passed
    executed: bool  # True iff the Operation actually started executing on Caldera
    containment_passed: bool | None  # containment pre-flight verdict (None if absent)
    isolation_passed: bool | None  # isolation pre-flight verdict (None if absent)
    total_success: int
    total_failure: int
    message: str


class EvaluationResult(TypedDict, total=False):
    """Evaluator output for the last pass over the Execution phase report."""

    success_rate: float
    procedural_coverage: float
    verdict_success: bool
    achieved_coverage: float  # percentage 0-100, absent when no coverage could be derived
    planned_coverage: float  # percentage 0-100, absent when not derived
    failed_nodes: list[str]
    decision: str  # EvaluatorDecision value
    rationale: str


class AgentTrace(TypedDict, total=False):
    """Per-node input + processing trace captured for the Audit_Trace.

    Appended to the append-only ``traces`` channel by each AI agent's node.
    The produced output is already carried in ``artifacts[...]`` and linked
    back to this trace by the ``(phase, iteration)`` pair.
    """

    phase: str  # English AgentPhase value: flow | operationalization | environment | execution | evaluation
    iteration: int
    agent_input: (
        dict  # prior-phase artifacts, selected-campaign context, retrieved experience, attack/stix
    )
    processing: dict  # llm_prompt, llm_response, reasoning, tools_invoked, repair_attempts, retrieved_experience, prior_artifacts


class AgentState(TypedDict, total=False):
    """Shared LangGraph state. Persisted by the PostgreSQL checkpointer.

    ``iteration`` and the running ``history`` are the short-term memory; the
    checkpointer snapshots this TypedDict after every node so a run can resume.
    """

    campaign_id: str
    iteration: int  # 0-based, incremented on each loop
    max_iterations: int  # guard
    retrieved_experience: list[dict]  # RAG hits injected into prompts
    artifacts: PhaseArtifacts  # outputs of the current iteration
    report: dict | None  # Execution phase report of the last execute
    execution_result: ExecutionResult | None  # structured result execution -> evaluation
    evaluation: EvaluationResult | None  # evaluator output of the last pass
    refine_phase: str | None  # AgentPhase to re-enter, or None
    history: Annotated[list[dict], add]  # append-only per-node trace (reducer)
    traces: Annotated[list[dict], add]  # NEW: per-node input+processing trace
    error: str | None
    # Attack flows already presented in this run (from ``agent_runs.flow_proposals``);
    # the flow phase avoids them when asked to generate another one.
    excluded_flow_ids: list[int]
    excluded_hashes: list[str]
