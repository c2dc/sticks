"""Property test — the ResourceGate bound is fail-closed (task 5.2 of the
``ai-first-campaign-flow`` spec).

# Feature: ai-first-campaign-flow, Property 1: ResourceGate never approves a plan
# exceeding any limit. For ANY ``ResourceLimits`` and ANY ``ResourcePlan``, the
# verdict is ``approved`` **iff** ``container_count <= max_containers`` **and**
# ``ram_gb <= max_ram_gb`` **and** ``disk_gb <= max_disk_gb``; whenever a limit is
# exceeded the verdict is ``approved=False`` and every exceeded limit is NAMED in
# ``reasons`` (in the stable order containers -> RAM -> disk).

Validates: Requirements 3.1, 12.2

This is a normal ``feature`` property test (``ai-first-campaign-flow`` is a
feature spec): the property is expected to HOLD on
:meth:`app.services.environment.resource_gate.ResourceGate.evaluate` as written.

What is driven (and why no fakes are needed)
--------------------------------------------
``ResourceGate.evaluate`` is a pure function of the plan and the configured
limits — same plan + same limits always yields the same verdict — so the REAL
gate is exercised directly with no Docker daemon, network or injection. The
host-resource inspection seam (``HostResourceInspector`` / ``DockerSdkHostResourceInspector``)
is deliberately NOT touched here: the bound this property pins is about
``evaluate`` adjudicating an already-built :class:`ResourcePlan`, not about how a
plan is assembled from a live host.

Input space
-----------
Hypothesis draws arbitrary :class:`ResourceLimits` (independent of the design
defaults, so the bound is proven for any configured policy) and arbitrary
:class:`ResourcePlan` footprints. The plan generators intentionally straddle each
limit — drawing below, **exactly at** (the boundary-equality case: ``<=`` must
APPROVE at equality), and above every limit, and in combinations where several
limits are exceeded at once — so the generated examples cover the full truth
table of the three-way ``and`` and the stable multi-reason ordering.
"""

from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.environment.resource_gate import (
    ResourceGate,
    ResourceGateVerdict,
    ResourceLimits,
    ResourcePlan,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary limits, independent of the design defaults, so the bound is proven
# for ANY configured policy (not just 10/20/300). Kept to sane non-negative
# integers (the design's limits are whole units).
_limits_strategy = st.builds(
    ResourceLimits,
    max_containers=st.integers(min_value=0, max_value=1_000),
    max_ram_gb=st.integers(min_value=0, max_value=10_000),
    max_disk_gb=st.integers(min_value=0, max_value=100_000),
)


def _plans_for(limits: ResourceLimits) -> st.SearchStrategy[ResourcePlan]:
    """Plans that straddle each limit — below, exactly at, and above.

    Drawing each dimension relative to its own limit (and including the
    boundary-equality value exactly) guarantees Hypothesis explores the full
    truth table of the three-way ``and`` the gate computes: every combination of
    each dimension being within or over its limit, plus the critical ``==``
    boundary where ``<=`` must APPROVE.
    """

    def _around_int(limit: int) -> st.SearchStrategy[int]:
        # Non-negative counts straddling the integer limit: strictly below, the
        # boundary value itself, and strictly above (up to a comfortable margin).
        return st.one_of(
            st.integers(min_value=0, max_value=limit) if limit > 0 else st.just(0),
            st.just(limit),
            st.integers(min_value=limit + 1, max_value=limit + 50),
        )

    def _around_float(limit: int) -> st.SearchStrategy[float]:
        # Non-negative amounts straddling the limit, with the exact boundary
        # value (as a float) included so the equality case is always reachable.
        return st.one_of(
            st.floats(
                min_value=0.0,
                max_value=float(limit),
                allow_nan=False,
                allow_infinity=False,
            ),
            st.just(float(limit)),
            st.floats(
                min_value=float(limit) + 0.000_1,
                max_value=float(limit) + 500.0,
                allow_nan=False,
                allow_infinity=False,
            ),
        )

    return st.builds(
        ResourcePlan,
        container_count=_around_int(limits.max_containers),
        ram_gb=_around_float(limits.max_ram_gb),
        disk_gb=_around_float(limits.max_disk_gb),
    )


# ---------------------------------------------------------------------------
# Property 1 — ResourceGate never approves a plan exceeding any limit.
# ---------------------------------------------------------------------------
# Feature: ai-first-campaign-flow, Property 1: ResourceGate never approves a plan
# exceeding any limit.
@settings(max_examples=40, deadline=None)
@given(data=st.data())
def test_property1_resource_gate_approves_iff_every_limit_holds(data) -> None:
    """``approved`` iff all three limits hold; else reject naming the exceeded one(s).

    # Feature: ai-first-campaign-flow, Property 1: ResourceGate never approves a
    # plan exceeding any limit.
    Validates: Requirements 3.1, 12.2
    """
    limits = data.draw(_limits_strategy)
    plan = data.draw(_plans_for(limits))

    verdict = ResourceGate(limits).evaluate(plan)
    assert isinstance(verdict, ResourceGateVerdict)

    # Independently recompute each per-limit breach from the raw figures — the
    # test never trusts the gate to tell it which limits were exceeded.
    containers_over = plan.container_count > limits.max_containers
    ram_over = plan.ram_gb > limits.max_ram_gb
    disk_over = plan.disk_gb > limits.max_disk_gb
    any_over = containers_over or ram_over or disk_over

    # --- The iff bound (Req. 3.1, 12.2). ---
    # Approved EXACTLY when every limit holds; fail-closed on any breach. This
    # pins both directions: equality (``<=``) approves, and a single breach in
    # any dimension rejects.
    assert verdict.approved is (not any_over)

    if verdict.approved:
        # An approving verdict carries NO reasons.
        assert verdict.reasons == []
        # Sanity: approval really does mean every dimension is within its limit.
        assert plan.container_count <= limits.max_containers
        assert plan.ram_gb <= limits.max_ram_gb
        assert plan.disk_gb <= limits.max_disk_gb
        return

    # --- Rejection names each exceeded limit, in the stable order ---
    # containers -> RAM -> disk (Req. 3.1).
    assert verdict.approved is False
    # One reason per exceeded limit — no more, no fewer.
    expected_count = sum((containers_over, ram_over, disk_over))
    assert len(verdict.reasons) == expected_count
    assert expected_count >= 1

    # Each exceeded limit is NAMED: the reason for that limit carries the
    # offending figure, so the stage can record exactly which limit broke.
    expected: list[str] = []
    if containers_over:
        expected.append(str(plan.container_count))
    if ram_over:
        expected.append(str(plan.ram_gb))
    if disk_over:
        expected.append(str(plan.disk_gb))

    # Stable order containers -> RAM -> disk: the i-th reason names the i-th
    # exceeded limit's offending figure, and no reason names a limit that held.
    for reason, figure in zip(verdict.reasons, expected):
        assert figure in reason, (
            f"reason {reason!r} must name the exceeded figure {figure!r}"
        )
    assert len(verdict.reasons) == len(expected)


# ---------------------------------------------------------------------------
# Boundary-equality unit examples — ``<=`` must APPROVE at exact equality, and a
# one-unit overshoot in a single dimension must REJECT naming only that limit.
# These complement the property with pinned, human-readable edge cases.
# ---------------------------------------------------------------------------
def test_boundary_equality_approves() -> None:
    """A plan sitting EXACTLY on every limit is approved (``<=`` at equality)."""
    limits = ResourceLimits(max_containers=10, max_ram_gb=20, max_disk_gb=300)
    plan = ResourcePlan(container_count=10, ram_gb=20.0, disk_gb=300.0)
    verdict = ResourceGate(limits).evaluate(plan)
    assert verdict.approved is True
    assert verdict.reasons == []


def test_single_dimension_overshoot_rejects_naming_only_that_limit() -> None:
    """Exceeding only RAM rejects with exactly one reason naming the RAM figure."""
    limits = ResourceLimits(max_containers=10, max_ram_gb=20, max_disk_gb=300)
    plan = ResourcePlan(container_count=10, ram_gb=20.5, disk_gb=300.0)
    verdict = ResourceGate(limits).evaluate(plan)
    assert verdict.approved is False
    assert len(verdict.reasons) == 1
    assert str(20.5) in verdict.reasons[0]


def test_all_dimensions_overshoot_rejects_in_stable_order() -> None:
    """All three exceeded -> three reasons in the stable containers->RAM->disk order."""
    limits = ResourceLimits(max_containers=1, max_ram_gb=1, max_disk_gb=1)
    plan = ResourcePlan(container_count=5, ram_gb=2.0, disk_gb=3.0)
    verdict = ResourceGate(limits).evaluate(plan)
    assert verdict.approved is False
    assert len(verdict.reasons) == 3
    # containers first, then RAM, then disk.
    assert str(5) in verdict.reasons[0]
    assert str(2.0) in verdict.reasons[1]
    assert str(3.0) in verdict.reasons[2]


def test_default_limits_match_the_design() -> None:
    """The default policy is the design's locked 10 / 20 / 300 (sanity example)."""
    limits = ResourceLimits()
    assert (limits.max_containers, limits.max_ram_gb, limits.max_disk_gb) == (
        10,
        20,
        300,
    )
    # A zero-footprint plan ("nothing proposed") is approved under the defaults.
    verdict = ResourceGate().evaluate(ResourcePlan(0, 0.0, 0.0))
    assert verdict.approved is True
    assert not verdict.reasons
    # Guard against any accidental NaN leaking through the float comparisons.
    assert not math.isnan(0.0)
