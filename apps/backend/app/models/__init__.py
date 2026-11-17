"""Domain models (SQLAlchemy) for the Pipeline_UI backend.

Importing this package registers every ORM model on ``Base.metadata`` (both the
enums and the concrete tables live in submodules). ``import_models()`` in
:mod:`app.db.base` imports this package for exactly that side effect, so Alembic
autogenerate and the ``create_all`` bootstrap see the full schema.

_Requirements: 9.1, 9.7, 11.1, 11.2, 6.8_
"""

from __future__ import annotations

from app.models.domain import (
    Ability,
    AbilityResult,
    Adversary,
    AgentEvaluation,
    AgentIteration,
    AgentPhaseOutput,
    AgentRun,
    AttackFlowEdgeRow,
    AttackFlowObjectRow,
    AttackFlowRow,
    AuditLogEntry,
    Case,
    ContainerEmulability,
    EnvironmentParametersRow,
    ExecutionReport,
    ExperienceMemory,
    IngestSourceState,
    Operation,
    PhaseRun,
    SessionState,
    TechniqueEmulability,
    UserPreferences,
)
from app.models.enums import (
    PHASE_ORDER,
    AbilityResultStatus,
    AgentPhase,
    AgentRunStatus,
    EnvironmentPlatform,
    EvaluatorDecision,
    FlowOrigin,
    FlowProvenance,
    IngestSource,
    IngestStatus,
    Language,
    OperationState,
    PhaseState,
    TechniqueEmulabilityKind,
    Theme,
    TranslationSource,
)

__all__ = [
    "PHASE_ORDER",
    "Ability",
    "AbilityResult",
    "AbilityResultStatus",
    "Adversary",
    "AgentEvaluation",
    "AgentIteration",
    "AgentPhase",
    "AgentPhaseOutput",
    # AI curation agent bookkeeping (task 3.1)
    "AgentRun",
    "AgentRunStatus",
    "AttackFlowEdgeRow",
    # Attack flow corpus and ingestion state (agentic-only-flow-redesign 6.1)
    "AttackFlowObjectRow",
    "AttackFlowRow",
    "AuditLogEntry",
    # Models
    "Case",
    "ContainerEmulability",
    "EnvironmentParametersRow",
    "EnvironmentPlatform",
    "EvaluatorDecision",
    "ExecutionReport",
    "ExperienceMemory",
    "FlowOrigin",
    "FlowProvenance",
    "IngestSource",
    "IngestSourceState",
    "IngestStatus",
    "Language",
    "Operation",
    "OperationState",
    "PhaseRun",
    "PhaseState",
    "SessionState",
    # Container emulability (agentic-phase-model-refactor task 12.1)
    "TechniqueEmulability",
    "TechniqueEmulabilityKind",
    "Theme",
    # Enums
    "TranslationSource",
    "UserPreferences",
]
