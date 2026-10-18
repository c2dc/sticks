"""Example tests for ``_derive_link_status`` sentinels (task 3.4).

These are **example** (not property) tests that pin the four sentinel mappings of
:func:`app.services.caldera.client._derive_link_status`, the helper that maps a raw
Caldera link ``status`` code to a per-Ability :class:`AbilityResultStatus`.

Caldera encodes a link's collection/result status as an integer sentinel:

* ``None`` — not yet reported ⇒ ``PENDING`` ("pendente")
* ``-3``  — running/uncollected ⇒ ``RUNNING`` ("em_execucao")
* ``0``   — command finished successfully ⇒ ``SUCCESS`` ("sucesso")
* any other finished status ⇒ ``FAILURE`` ("falha")

They pin the exact four-way mapping by example (edge cases + a few "other"
values), complementing the total/disjoint property test in task 3.3
(``test_operation_runner_property.py`` / Property 8).

_Requirements: 2.1_
"""

from __future__ import annotations

import pytest

from app.models.enums import AbilityResultStatus
from app.services.caldera.client import _LINK_STATUS_RUNNING, _derive_link_status


def test_none_maps_to_pending() -> None:
    """A link with no reported status (``None``) is ``pendente``."""
    assert _derive_link_status(None) is AbilityResultStatus.PENDING


def test_running_sentinel_maps_to_running() -> None:
    """The ``-3`` running sentinel is ``em_execucao``."""
    assert _derive_link_status(-3) is AbilityResultStatus.RUNNING
    # And the sentinel constant itself resolves the same way.
    assert _derive_link_status(_LINK_STATUS_RUNNING) is AbilityResultStatus.RUNNING


def test_zero_maps_to_success() -> None:
    """A ``0`` finished status is ``sucesso``."""
    assert _derive_link_status(0) is AbilityResultStatus.SUCCESS


@pytest.mark.parametrize("status_code", [1, -1, -2, 2, 42, 255, -100])
def test_other_codes_map_to_failure(status_code: int) -> None:
    """Any other finished status (not ``None``/``-3``/``0``) is ``falha``."""
    assert _derive_link_status(status_code) is AbilityResultStatus.FAILURE
