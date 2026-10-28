"""Agent state schema and reducers.

Defines the ``StageArtifacts``, ``EvaluationResult`` and ``AgentState``
TypedDicts along with the append-only ``history`` reducer
(``Annotated[list[dict], add]``) used by the LangGraph ``StateGraph``.

``iteration`` and the running ``history`` form the short-term memory; the
PostgreSQL checkpointer snapshots this ``AgentState`` after every node so a run
can resume (Requirements 1.2, 4.1).

Importing this module performs no I/O (Requirement 1.7).
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Optional, TypedDict


class StageArtifacts(TypedDict, total=False):
    """Serialized per-stage outputs produced in the current iteration."""

    dag: dict  # ProducedDag.model_dump()
    operationalization: dict  # ProducedOperationalization.model_dump()
    environment: dict  # ProvisionedEnvironment.model_dump()


class EvaluationResult(TypedDict, total=False):
    """Evaluator output for the last pass over the Stage 6 report."""

    success_rate: float
    procedural_coverage: float
    verdict_success: bool
    failed_nodes: list[str]
    decision: str  # EvaluatorDecision value
    rationale: str


class AgentState(TypedDict, total=False):
    """Shared LangGraph state. Persisted by the PostgreSQL checkpointer.

    ``iteration`` and the running ``history`` are the short-term memory; the
    checkpointer snapshots this TypedDict after every node so a run can resume.
    """

    campaign_id: str
    iteration: int  # 0-based, incremented on each loop
    max_iterations: int  # guard
    retrieved_experience: list[dict]  # RAG hits injected into prompts
    artifacts: StageArtifacts  # outputs of the current iteration
    report: Optional[dict]  # Stage 6 report of the last execute
    evaluation: Optional[EvaluationResult]  # evaluator output of the last pass
    refine_stage: Optional[str]  # AgentStage to re-enter, or None
    history: Annotated[list[dict], add]  # append-only per-node trace (reducer)
    error: Optional[str]
