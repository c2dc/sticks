"""Shared Caldera v2 ability-payload projection.

# Feature: ai-run-caldera-ability-technique-name
# Task 3.1: Unify the two ``_ability_payloads`` projections and enforce the
# ``technique_name`` invariant (Decision A — authoritative).

Historically the logic that projects an ability object into a
``POST /api/v2/abilities`` payload dict lived in two places with identical field
loops:

* ``OperationRunner._ability_payloads``
  (``app/services/operation/runner.py``)
* ``CurationEnvironmentProvisioner._ability_payloads``
  (``app/services/environment/provisioner.py``)

Both copied ``technique_name`` only when ``getattr(ability, "technique_name",
None)`` was not ``None``. Ability sources that do not expose the attribute
(notably ``Stage2AbilityView`` on the AI path) therefore produced a payload with
the key omitted, and this Caldera version raises ``KeyError: 'technique_name'``
when loading such an ability (``data_svc.py:189``) → HTTP 500 → the run is marked
FAILED.

This module provides a single ``build_caldera_ability_payloads`` helper that both
call sites delegate to. It copies the same fields as before and then enforces the
invariant from the design (Property 1): **every** payload carries a non-empty
``technique_name``. When the source lacks a usable value (``None`` or
empty/whitespace-only), a deterministic non-empty fallback is derived in the
documented order ``name`` → ``technique_id`` → ``"unknown-technique"`` sentinel.

For any source that already carries a non-empty ``technique_name`` the fallback
branch is never taken and every field is projected exactly as before, so the
payload is byte-for-byte identical (design Property 2 — Preservation).

_Requirements: 2.1, 2.2_
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

# The fields copied verbatim from the ability source into the payload (same order
# and set as the two original projections used).
_PROJECTED_FIELDS: tuple[str, ...] = (
    "name",
    "tactic",
    "technique_name",
    "technique_id",
    "description",
)

# Final deterministic sentinel used only when neither ``name`` nor ``technique_id``
# can supply a non-empty value. Keeps the key present and non-empty for every
# source so Caldera never raises ``KeyError: 'technique_name'``.
UNKNOWN_TECHNIQUE_SENTINEL = "unknown-technique"


def _is_missing(value: Any) -> bool:
    """Return ``True`` when ``value`` cannot serve as a usable ``technique_name``.

    Treats ``None`` and empty/whitespace-only strings as missing, so an empty
    string never reaches Caldera as an unusable value (design Bug Condition).
    Non-string truthy values are considered present.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    return False


def _derive_technique_name(ability: Any) -> str:
    """Derive a non-empty ``technique_name`` for an ability lacking a usable one.

    Deterministic fallback order (design Fix Implementation > Decision A):
    ``name`` → ``technique_id`` → ``UNKNOWN_TECHNIQUE_SENTINEL``.
    """
    for attr in ("name", "technique_id"):
        candidate = getattr(ability, attr, None)
        if isinstance(candidate, str) and candidate.strip() != "":
            return candidate
        if candidate is not None and not isinstance(candidate, str):
            # A non-string, non-None candidate (defensive): stringify it so the
            # key is still populated with something meaningful.
            text = str(candidate)
            if text.strip() != "":
                return text
    return UNKNOWN_TECHNIQUE_SENTINEL


def build_caldera_ability_payloads(
    abilities: Sequence[Any],
    *,
    executor_normalizer: Callable[[Any], Any] | None = None,
) -> list[dict[str, Any]]:
    """Project ability objects into ``POST /api/v2/abilities`` payload dicts.

    Accepts SQLAlchemy ``Ability`` rows, ``Stage2AbilityView`` instances or any
    duck-typed equivalent; copies the fields Caldera's v2 ability schema uses and
    guarantees a non-empty ``technique_name`` on every payload.

    Args:
        abilities: the ability sources to project.
        executor_normalizer: optional callable applied to the raw ``executors``
            attribute to shape it for the payload. When omitted the executors are
            passed through unchanged (the ``OperationRunner`` behavior). The
            provisioner passes a normalizer that calls ``model_dump()`` on Pydantic
            executors. This keeps each call site's executor handling exactly as
            before while sharing the ``technique_name`` invariant.

    Returns:
        A list of payload dicts, one per ability, each with a present, non-empty
        ``technique_name``.
    """
    payloads: list[dict[str, Any]] = []
    for ability in abilities:
        payload: dict[str, Any] = {}

        ability_id = getattr(ability, "ability_id", None)
        if ability_id:
            payload["ability_id"] = ability_id

        for attr in _PROJECTED_FIELDS:
            value = getattr(ability, attr, None)
            if value is not None:
                payload[attr] = value

        # Enforce the invariant: technique_name must be present and non-empty.
        # For an isBugCondition-false source the key is already set to a usable
        # value above, so this branch is a no-op and the payload is unchanged.
        if _is_missing(payload.get("technique_name")):
            payload["technique_name"] = _derive_technique_name(ability)

        executors = getattr(ability, "executors", None)
        if executors:
            payload["executors"] = (
                executor_normalizer(executors) if executor_normalizer is not None else executors
            )

        payloads.append(payload)
    return payloads
