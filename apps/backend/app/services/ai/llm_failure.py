"""Classify a model failure into a stable machine code.

The backend returns only codes; the frontend translates them.
"""

from __future__ import annotations

import httpx
import openai

from app.services.ai.llm import SepinfConfigError

__all__ = ["classify_llm_failure"]

_MAX_CHAIN_DEPTH = 10


def _chain(exc: BaseException) -> list[BaseException]:
    """Return ``exc`` and its ``__cause__``/``__context__`` ancestors, without cycles."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [exc]
    while pending and len(seen) < _MAX_CHAIN_DEPTH:
        current = pending.pop(0)
        if current is None or any(current is item for item in seen):
            continue
        seen.append(current)
        pending.extend([current.__cause__, current.__context__])
    return seen


def classify_llm_failure(exc: BaseException) -> str:
    """Return the cause code for a failed model call.

    Codes: ``llm_timeout``, ``llm_unreachable``, ``llm_not_configured``,
    ``llm_error`` and ``run_error``. Timeouts are checked before connection
    errors because ``openai.APITimeoutError`` subclasses
    ``openai.APIConnectionError``.
    """
    chain = _chain(exc)
    if any(isinstance(e, openai.APITimeoutError | httpx.TimeoutException) for e in chain):
        return "llm_timeout"
    if any(isinstance(e, openai.APIConnectionError | httpx.ConnectError) for e in chain):
        return "llm_unreachable"
    if any(isinstance(e, SepinfConfigError) for e in chain):
        return "llm_not_configured"
    if any(isinstance(e, openai.APIStatusError) for e in chain):
        return "llm_error"
    return "run_error"
