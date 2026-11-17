"""Session-state services.

``SessionStateService`` (tasks 9.1, 9.2) reads and writes the Session State —
the current case plus the completed-phases-per-case and per-Ability operation
results derived from ``PhaseRun``/``Operation``/``AbilityResult`` — giving the
Researcher continuity across machines (Req. 9.1, 9.2, 9.4, 9.5, 9.6, 9.7). It
also persists phase completions while preserving the previously persisted state
unchanged on a persist failure (``persist_phase_completion`` — Req. 9.3).
"""

from app.services.session.session_state_service import (
    RESTORE_FAILED_MESSAGE,
    SAVE_FAILED_MESSAGE,
    AbilityResultView,
    OperationResultView,
    PhaseCompletionResult,
    SessionStateService,
    SessionStateView,
)

__all__ = [
    "RESTORE_FAILED_MESSAGE",
    "SAVE_FAILED_MESSAGE",
    "AbilityResultView",
    "OperationResultView",
    "PhaseCompletionResult",
    "SessionStateService",
    "SessionStateView",
]
