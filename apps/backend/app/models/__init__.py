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
    AuditLogEntry,
    Case,
    Dag,
    EnvironmentParametersRow,
    ExecutionReport,
    Operation,
    ProvisionedEnvironmentRow,
    SessionState,
    StageRun,
    StixVersion,
    UserPreferences,
)
from app.models.enums import (
    AbilityResultStatus,
    DagOrigin,
    EnvironmentPlatform,
    Language,
    OperationState,
    StageState,
    Theme,
    TranslationSource,
)

__all__ = [
    # Enums
    "TranslationSource",
    "DagOrigin",
    "EnvironmentPlatform",
    "StageState",
    "OperationState",
    "AbilityResultStatus",
    "Theme",
    "Language",
    # Models
    "Case",
    "Dag",
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
    "ProvisionedEnvironmentRow",
    "ExecutionReport",
]
