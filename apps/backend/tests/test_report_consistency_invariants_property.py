"""Property-based test for Property 4 of the stage6-execution-redesign design
(task 6.2) — consistency invariants of the final report.

# Feature: stage6-execution-redesign, Property 4: Invariantes de consistência do relatório final

**Validates: Requirements 3.4**

The bug this guards against (observed on the real ShadowRay run): the final
report was presented *inconsistent* — verdict "Falha", "0 nós falharam", yet
techniques stuck "em execução"/"pendente". :class:`ReportBuilder` fixes the
report side by deriving the view **deterministically** from the per-Ability
results and guaranteeing, for a report presented as final (Req. 3.4):

* **(a)** the verdict is a deterministic function of the computed success rate;
* **(b)** ``total_success + total_failure`` equals the campaign's total
  techniques;
* **(c)** the failed-nodes count equals the number of techniques in ``falha``;
* **(d)** no technique remains in a non-terminal state (``pendente`` /
  ``em_execucao``).

The derivation under test — :meth:`ReportBuilder._derive` — is a **pure**,
DB-free function (per the design note): it reads only ``.status`` and
``.ability_id`` of each per-Ability result. So this property exercises it
directly with lightweight ``AbilityResult``-like inputs, with **no** database,
no Caldera and no clock involved.

The generator deliberately includes the **non-terminal** statuses
(``pendente`` / ``em_execucao``) alongside the terminal ones (``sucesso`` /
``falha``), so each example confirms the normalization of invariant (d): a
final report built from a mix that still carries pending/running techniques must
not leave any of them non-terminal.

_Requirements: 3.4_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import AbilityResultStatus
from app.services.operation.report import ReportBuilder

# ---------------------------------------------------------------------------
# Lightweight AbilityResult-like input.
#
# ``ReportBuilder._derive`` consumes only ``.status`` and ``.ability_id`` of
# each result (it is pure and DB-free — the design note), so a frozen dataclass
# is a faithful, dependency-free stand-in for a persisted ``AbilityResult`` row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeAbilityResult:
    """A per-Ability result exposing exactly what ``_derive`` reads."""

    status: AbilityResultStatus
    ability_id: Optional[str] = None


# The full status space, including the two NON-terminal states that must never
# survive into a final report (invariant d). Mixing these in is what makes the
# property meaningful: the derivation has to normalize them to ``falha``.
_ALL_STATUSES: tuple[AbilityResultStatus, ...] = (
    AbilityResultStatus.PENDING,
    AbilityResultStatus.RUNNING,
    AbilityResultStatus.SUCCESS,
    AbilityResultStatus.FAILURE,
)

_NON_TERMINAL_STATUSES = frozenset(
    {AbilityResultStatus.PENDING, AbilityResultStatus.RUNNING}
)
_TERMINAL_STATUSES = frozenset(
    {AbilityResultStatus.SUCCESS, AbilityResultStatus.FAILURE}
)


def _ability_result_strategy() -> st.SearchStrategy[FakeAbilityResult]:
    """A per-Ability result with an arbitrary status and ability id.

    ``ability_id`` is drawn from non-empty strings and ``None`` (Caldera may not
    associate a link to an ability — ``_derive`` coerces that to ``""`` but it is
    still a terminal entry), so the generator spans the real input space.
    """
    return st.builds(
        FakeAbilityResult,
        status=st.sampled_from(_ALL_STATUSES),
        ability_id=st.one_of(
            st.none(),
            st.text(alphabet="abcdef0123456789-", min_size=1, max_size=12),
        ),
    )


@settings(max_examples=200, deadline=None)
@given(
    results=st.lists(_ability_result_strategy(), min_size=0, max_size=20),
    operation_id=st.one_of(st.none(), st.integers(min_value=1, max_value=10_000)),
)
def test_property4_final_report_consistency_invariants(
    results: list[FakeAbilityResult],
    operation_id: Optional[int],
) -> None:
    """Property 4: the derived final report satisfies the four invariants.

    # Feature: stage6-execution-redesign, Property 4: Invariantes de consistência do relatório final
    **Validates: Requirements 3.4**

    For any set of per-Ability results — including non-terminal ones
    (``pendente`` / ``em_execucao``) — the view produced by
    :meth:`ReportBuilder._derive` satisfies simultaneously:

    (a) the verdict is a deterministic function of the computed success rate;
    (b) ``total_success + total_failure == total_techniques``;
    (c) ``len(failed_nodes) == total_failure``;
    (d) every ``per_ability`` entry is in a terminal state (no technique left
        ``pendente`` / ``em_execucao``).
    """
    # The derivation is pure/DB-free, so a builder without a real session can
    # invoke ``_derive`` directly (it never touches ``self._session``).
    builder = ReportBuilder.__new__(ReportBuilder)
    view = builder._derive(operation_id=operation_id, results=results)

    total = len(results)
    expected_success = sum(
        1 for r in results if r.status is AbilityResultStatus.SUCCESS
    )
    expected_failure = total - expected_success

    # (b) sum of terminal buckets equals the campaign's total techniques.
    assert view.total_techniques == total
    assert view.total_success + view.total_failure == view.total_techniques
    assert view.total_success == expected_success
    assert view.total_failure == expected_failure

    # (c) failed-nodes count equals the number of techniques in ``falha``.
    assert len(view.failed_nodes) == view.total_failure

    # (d) no technique remains non-terminal in the final report: every entry is
    # terminal, and the per-Ability list accounts for every input result.
    assert len(view.per_ability) == total
    for entry in view.per_ability:
        assert entry.status in _TERMINAL_STATUSES
        assert entry.status not in _NON_TERMINAL_STATUSES

    # (a) the verdict is a deterministic function of the computed success rate:
    # success_rate = total_success / total (0.0 when empty), and the verdict is
    # True iff there is at least one technique and the rate is exactly 1.0.
    expected_rate = (expected_success / total) if total else 0.0
    assert view.success_rate == expected_rate
    expected_verdict = total > 0 and expected_rate >= 1.0
    assert view.success_verdict is expected_verdict

    # Determinism: re-deriving from the same inputs yields the same verdict and
    # the same counts (a pure function of the results).
    view_again = builder._derive(operation_id=operation_id, results=results)
    assert view_again.success_verdict is view.success_verdict
    assert view_again.success_rate == view.success_rate
    assert view_again.total_success == view.total_success
    assert view_again.total_failure == view.total_failure

    # The verdict is a function of the success rate alone (determinism of (a)):
    # a 100%-success run (and only such a run with ≥1 technique) is a pass.
    if total > 0 and expected_success == total:
        assert view.success_verdict is True
    else:
        assert view.success_verdict is False

    # The operation_id is carried through unchanged (single-execution report).
    assert view.operation_id == operation_id
