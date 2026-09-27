"""CalderaClient — wrapper around the MITRE Caldera v2 API.

The ``caldera`` package implements the ``CalderaClient`` component described in
the design (see "Components and Interfaces" -> ``CalderaClient``). It reuses the
existing sticks configuration (``CALDERA_URL`` and the ``KEY`` header) via
:mod:`app.core.config` — nothing under ``sticks/`` is modified.

- ``client`` (task 6.1): availability check with an explicit **10s** timeout and
  an **injectable** ``httpx`` client / transport so tests can simulate a healthy
  response and a timeout without a real Caldera. Exposes the typed
  :class:`AvailabilityResult` and the typed :class:`CalderaUnavailable` error the
  API layer maps to HTTP 503 (Req. 4.6).

  Ability / adversary loading and operation create/poll are **task 6.2**, which
  extends :class:`CalderaClient` on top of the same shared ``httpx`` client.

_Requisitos: 4.6_
"""

from __future__ import annotations

from app.services.caldera.client import (
    ABILITIES_PATH,
    ADVERSARIES_PATH,
    DEFAULT_GROUP,
    DEFAULT_JITTER,
    DEFAULT_PLANNER,
    DEFAULT_TIMEOUT_SECONDS,
    HEALTH_PATH,
    OPERATIONS_PATH,
    AbilityRef,
    AdversaryRef,
    AvailabilityResult,
    CalderaApiError,
    CalderaClient,
    CalderaUnavailable,
    CreatedOperation,
    OperationLink,
    OperationStateResult,
)

__all__ = [
    "ABILITIES_PATH",
    "ADVERSARIES_PATH",
    "DEFAULT_GROUP",
    "DEFAULT_JITTER",
    "DEFAULT_PLANNER",
    "DEFAULT_TIMEOUT_SECONDS",
    "HEALTH_PATH",
    "OPERATIONS_PATH",
    "AbilityRef",
    "AdversaryRef",
    "AvailabilityResult",
    "CalderaApiError",
    "CalderaClient",
    "CalderaUnavailable",
    "CreatedOperation",
    "OperationLink",
    "OperationStateResult",
]
