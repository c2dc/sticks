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
    AgentRun,
    AgentStageOutput,
    AuditLogEntry,
    Case,
    ContainerEmulability,
    AttackFlowEdgeRow,
    AttackFlowObjectRow,
    AttackFlowRow,
    EnvironmentParametersRow,
    ExecutionReport,
    ExperienceMemory,
    IngestSourceState,
    Operation,
    SessionState,
    StageRun,
    StixVersion,
    TechniqueEmulability,
    UserPreferences,
)
from app.models.enums import (
    PHASE_ORDER,
    AbilityResultStatus,
    AgentRunStatus,
    AgentStage,
    EnvironmentPlatform,
    EvaluatorDecision,
    FlowOrigin,
    FlowProvenance,
    IngestSource,
    IngestStatus,
    Language,
    OperationState,
    StageState,
    TechniqueEmulabilityKind,
    Theme,
    TranslationSource,
)

__all__ = [
    # Enums
    "TranslationSource",
    "FlowOrigin",
    "FlowProvenance",
    "IngestSource",
    "IngestStatus",
    "EnvironmentPlatform",
    "StageState",
    "OperationState",
    "AbilityResultStatus",
    "Theme",
    "Language",
    "AgentRunStatus",
    "AgentStage",
    "PHASE_ORDER",
    "EvaluatorDecision",
    "TechniqueEmulabilityKind",
    # Models
    "Case",
    "AttackFlowRow",
    "StageRun",
    "Ability",
    "Adversary",
    "Operation",
    "AbilityResult",
    "AuditLogEntry",
    "SessionState",
    "UserPreferences",
    "StixVersion",
    "EnvironmentParametersRow",
    "ExecutionReport",
    # AI curation agent bookkeeping (task 3.1)
    "AgentRun",
    "AgentIteration",
    "AgentStageOutput",
    "AgentEvaluation",
    "ExperienceMemory",
    # Container emulability (agentic-phase-model-refactor task 12.1)
    "TechniqueEmulability",
    "ContainerEmulability",
    # Attack flow corpus and ingestion state (agentic-only-flow-redesign 6.1)
    "AttackFlowObjectRow",
    "AttackFlowEdgeRow",
    "IngestSourceState",
]
