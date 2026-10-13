"""Session-state services.

``SessionStateService`` (tasks 9.1, 9.2) reads and writes the Estado_de_Sessão —
the current case plus the completed-stages-per-case and per-Ability operation
results derived from ``StageRun``/``Operation``/``AbilityResult`` — giving the
Pesquisador continuity across machines (Req. 9.1, 9.2, 9.4, 9.5, 9.6, 9.7). It
also persists Stage completions while preserving the previously persisted state
unchanged on a persist failure (``persist_stage_completion`` — Req. 9.3).
"""

from app.services.session.session_state_service import (
    AbilityResultView,
    OperationResultView,
    RESTORE_FAILED_MESSAGE,
    SAVE_FAILED_MESSAGE,
    SessionStateService,
    SessionStateView,
    StageCompletionResult,
)

__all__ = [
    "SessionStateService",
    "SessionStateView",
    "OperationResultView",
    "AbilityResultView",
    "StageCompletionResult",
    "RESTORE_FAILED_MESSAGE",
    "SAVE_FAILED_MESSAGE",
]
