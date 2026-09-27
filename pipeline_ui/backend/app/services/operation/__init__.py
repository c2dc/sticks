"""OperationRunner — conducts an emulation Operation (security core).

The ``operation`` package implements the ``OperationRunner`` component described
in the design (see "Components and Interfaces" -> ``OperationRunner`` and the
emulation sequence diagram). The runner is the enforcement point behind
Property 6 (explicit confirmation) and coordinates the containment pre-flight
(Properties 1 and 2), the Caldera availability check (Req. 4.6), the per-Ability
result persistence and aggregation (Req. 4.5), and the 1:1 audit trail (Req. 6.8).

- ``runner`` (task 7.3): gates on explicit confirmation, runs the destinations /
  isolation / availability pre-flight in order, then creates and polls the
  Caldera Operation, persists ``AbilityResult`` rows and ``Operation`` state
  transitions, aggregates ``total_sucesso`` / ``total_falha`` and records one
  audit row per executed command. The per-command loop is hook-friendly so the
  audit-failure abort (task 7.5, Req. 6.9) can be inserted cleanly.

_Requisitos: 6.6, 6.7, 4.5, 6.1, 6.2, 6.3, 6.4, 4.6_
"""

from __future__ import annotations

from app.services.operation.runner import (
    AbilityRunResult,
    ContainmentValidator,
    IsolationVerifier,
    OperationRunner,
    OperationRunResult,
    RunOutcome,
)

__all__ = [
    "AbilityRunResult",
    "ContainmentValidator",
    "IsolationVerifier",
    "OperationRunner",
    "OperationRunResult",
    "RunOutcome",
]
