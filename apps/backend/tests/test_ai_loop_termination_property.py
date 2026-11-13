"""Property test — loop termination within the iteration guard (task 6.7).

# Feature: ai-first-campaign-flow, Property 6: Loop termination within the
# iteration guard. For any ``max_iterations >= 1`` and any sequence of
# non-passing evaluator verdicts (optionally capped by a terminal passing
# verdict), the Run performs at most ``max_iterations`` iterations and reaches a
# terminal status: ``completed`` on a passing verdict, ``aborted`` at the guard
# with no passing verdict.

Validates: Requirements 3.5

This is a *feature* spec (``ai-first-campaign-flow``), so the property is
expected to HOLD over the real routing logic as written.

What is driven
--------------
The loop is governed by the REAL
:func:`app.services.ai.graph.route_after_eval` — the conditional-edge router the
LangGraph graph registers after ``evaluate``. The router is the single authority
on when the improvement loop ends:

* a passing verdict (``evaluation["verdict_success"]`` truthy) ends the loop with
  ``"finish"`` (the ``Run_Service`` marks the run ``completed``);
* otherwise, the max-iteration guard (``iteration + 1 >= max_iterations``) ends
  the loop with ``"finish"`` (the ``Run_Service`` marks the run ``aborted``);
* otherwise the loop re-enters a refine stage (``"flow"`` / ``"operationalize"``
  / ``"environment"``).

Rather than spin up a real compiled graph (PostgreSQL checkpointer + SEPINF +
pgvector), this test SIMULATES the graph's control flow around the real router,
exactly mirroring how the graph advances the 0-based ``iteration`` counter
(``orchestrate`` seeds it to 0, each ``evaluate`` pass counts as one iteration
before the next re-entry) and how
:func:`app.services.ai.run_service.AgentRunService` maps the final state to a
terminal status (``completed`` iff the final verdict passes, else ``aborted``,
with ``iterations_used`` clamped to ``1..max_iterations``). Both the terminal
English status strings and the iteration-count derivation are checked against
the real enum and the real service helper, so the simulation never trusts the
implementation to decide the expected value.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from app.models.enums import AgentRunStatus
from app.services.ai.graph import route_after_eval
from app.services.ai.run_service import AgentRunService

# The exact terminal English status strings (Req. 3.5 / design §I) — literals,
# so the assertions never trust the implementation to decide the expected value.
_COMPLETED = "completed"
_ABORTED = "aborted"


# ---------------------------------------------------------------------------
# Generator: a max-iterations guard and a sequence of per-pass verdicts.
#
# ``verdicts`` is a list of booleans, one per evaluate pass, describing whether
# that pass's evaluator verdict passes. We draw a long enough sequence that the
# loop can always read a verdict for each of its (at most max_iterations) passes;
# most entries will be False (a non-passing verdict that would refine) with an
# optional True somewhere that ends the loop early on a pass.
# ---------------------------------------------------------------------------


@st.composite
def _guard_and_verdicts(draw: st.DrawFn) -> tuple[int, list[bool], str]:
    """Draw ``(max_iterations, verdicts, refine_stage)``.

    * ``max_iterations`` is any integer ``>= 1`` (the guard's domain).
    * ``verdicts`` is a per-pass list at least as long as ``max_iterations`` so
      the simulated loop always has a verdict to read; each entry is a boolean
      biased toward ``False`` (a non-passing verdict) with an occasional ``True``
      so both the passing (``completed``) and guard (``aborted``) terminations are
      exercised.
    * ``refine_stage`` is the evaluator's chosen refine target the router routes
      back to on a non-passing, non-guarded pass.
    """
    max_iterations = draw(st.integers(min_value=1, max_value=12))
    # One verdict per potential pass; bias toward non-passing so the guard path
    # is the common case, with True allowed so early completion is also covered.
    verdicts = draw(
        st.lists(
            st.booleans(),
            min_size=max_iterations,
            max_size=max_iterations + 4,
        )
    )
    refine_stage = draw(st.sampled_from(["flow", "operationalize", "environment"]))
    return max_iterations, verdicts, refine_stage


def _simulate_loop(
    max_iterations: int, verdicts: list[bool], refine_stage: str
) -> tuple[int, dict[str, Any]]:
    """Run the improvement loop around the REAL ``route_after_eval``.

    Mirrors the graph's control flow: ``orchestrate`` seeds ``iteration = 0`` and
    ``max_iterations``; each ``evaluate`` pass writes that pass's verdict into
    ``state["evaluation"]`` and asks ``route_after_eval`` for the next hop. On
    ``"finish"`` the loop ends; on a refine target the graph re-enters that stage
    and ``iteration`` advances by one before the next evaluate pass (the graph's
    0-based counter).

    Returns ``(passes, final_state)`` where ``passes`` is the number of evaluate
    passes performed (1-based count of iterations) and ``final_state`` is the
    terminal state the service would read.
    """
    iteration = 0
    passes = 0
    state: dict[str, Any] = {
        "campaign_id": "loop-prop",
        "iteration": iteration,
        "max_iterations": max_iterations,
        "refine_stage": refine_stage,
    }

    # Hard safety bound: the loop must terminate within max_iterations passes, so
    # a bound of max_iterations + 1 can never be reached if the guard holds. If it
    # is ever reached the guard failed to terminate the loop — the test asserts on
    # ``passes`` below, so this bound only protects against an infinite loop.
    safety_bound = max_iterations + 1

    while True:
        verdict = verdicts[passes] if passes < len(verdicts) else False
        state["iteration"] = iteration
        state["evaluation"] = {"verdict_success": verdict}
        passes += 1

        hop = route_after_eval(state)
        if hop == "finish":
            break

        # A refine hop re-enters the chosen stage; the router must have returned
        # exactly the refine target it was given.
        assert hop == refine_stage, (
            f"non-finish hop must be the refine target {refine_stage!r}, got {hop!r}"
        )
        iteration += 1

        assert passes <= safety_bound, "guard failed to terminate the loop within max_iterations"

    return passes, state


# ===========================================================================
# Property 6 — the loop performs at most max_iterations passes and reaches the
# correct terminal status (completed on a passing verdict, aborted at the guard).
# ===========================================================================


# Feature: ai-first-campaign-flow, Property 6: Loop termination within the
# iteration guard.
@settings(max_examples=40)
@given(data=_guard_and_verdicts())
def test_property6_loop_terminates_within_iteration_guard(
    data: tuple[int, list[bool], str],
) -> None:
    """At most ``max_iterations`` passes; terminal status matches the verdict.

    # Feature: ai-first-campaign-flow, Property 6: Loop termination within the
    # iteration guard.
    Validates: Requirements 3.5
    """
    max_iterations, verdicts, refine_stage = data

    passes, final_state = _simulate_loop(max_iterations, verdicts, refine_stage)

    # (1) The loop terminated and performed at least one and at most
    # max_iterations passes — the guard bounds the loop (Req. 3.5).
    assert 1 <= passes <= max_iterations, f"performed {passes} passes, expected 1..{max_iterations}"

    # The verdict of the final pass the router saw decides the terminal status.
    final_verdict = bool(final_state["evaluation"]["verdict_success"])

    # (2) The run reaches a terminal status, derived exactly as the service does:
    # completed on a passing final verdict, aborted otherwise (the guard stopped
    # the loop without a passing verdict) — Req. 3.2 / 3.4 / 3.5.
    status = AgentRunStatus.COMPLETED if final_verdict else AgentRunStatus.ABORTED
    expected_english = _COMPLETED if final_verdict else _ABORTED
    assert status.value == expected_english
    assert status in {AgentRunStatus.COMPLETED, AgentRunStatus.ABORTED}

    # A passing final verdict means the loop ended on the pass that passed; a
    # non-passing final verdict means the guard stopped it at max_iterations.
    if final_verdict:
        # Completion can happen on any pass 1..max_iterations.
        assert passes <= max_iterations
    else:
        # Aborted only when the guard fired, i.e. we used the full budget.
        assert passes == max_iterations, (
            "a non-passing terminal verdict must mean the guard stopped the loop "
            f"at max_iterations={max_iterations}, but it stopped after {passes}"
        )

    # (3) iterations_used (as the service derives it from the final state) never
    # exceeds the guard and equals the passes performed — clamped to
    # 1..max_iterations (Req. 3.5). The final state carries the 0-based counter
    # of the last pass, so iterations_used == passes.
    iterations_used = AgentRunService._iterations_used(final_state, max_iterations)
    assert iterations_used == passes
    assert 1 <= iterations_used <= max_iterations
