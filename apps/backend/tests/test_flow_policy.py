"""Fixed-case tests for the three-path flow decision policy.

**Property 6: Flow decision policy is total and consistent**

``classify_flow_path`` is pure, so these deterministic examples cover every
branch: same name, same technique set, Jaccard above / at / below the
threshold, empty candidate list, and independence from input order.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**
"""

from __future__ import annotations

import itertools

import pytest

from app.models.enums import FlowProvenance
from app.services.attack_flow.policy import (
    DEFAULT_SIMILARITY_THRESHOLD,
    MAX_REFERENCES,
    SIMILARITY_ENV_VAR,
    FlowCandidate,
    classify_flow_path,
    jaccard,
    normalize_name,
    similarity_threshold,
)


def _cand(flow_id: int, name: str, *techniques: str) -> FlowCandidate:
    return FlowCandidate(flow_id=flow_id, name=name, technique_ids=frozenset(techniques))


def _classify(candidates, techniques=("T1", "T2", "T3", "T4"), name="Campaign X", aliases=()):
    return classify_flow_path(name, aliases, frozenset(techniques), candidates)


@pytest.fixture(autouse=True)
def _clear_threshold_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SIMILARITY_ENV_VAR, raising=False)


def test_same_normalized_name_is_exact() -> None:
    decision = _classify([_cand(5, "  campaign,  X!! ", "T9")])
    assert decision.kind is FlowProvenance.EXACT
    assert decision.references == (5,)


def test_alias_name_match_is_exact() -> None:
    decision = _classify([_cand(5, "Other Name", "T9")], aliases=("other name",))
    assert decision.kind is FlowProvenance.EXACT
    assert decision.references == (5,)


def test_same_technique_set_is_exact_and_tie_goes_to_lowest_id() -> None:
    decision = _classify(
        [_cand(9, "A", "T4", "T3", "T2", "T1"), _cand(3, "B", "T1", "T2", "T3", "T4")]
    )
    assert decision.kind is FlowProvenance.EXACT
    assert decision.references == (3,)
    assert decision.similarity == 1.0


def test_jaccard_above_threshold_is_adapted() -> None:
    decision = _classify([_cand(1, "Doc", "T1", "T2", "T3", "T9")])  # 3/5 = 0.6
    assert decision.kind is FlowProvenance.ADAPTED
    assert decision.references == (1,)
    assert decision.similarity == pytest.approx(0.6)


def test_jaccard_equal_to_threshold_is_adapted() -> None:
    decision = _classify([_cand(1, "Doc", "T1")])  # 1/4 == 0.25
    assert decision.kind is FlowProvenance.ADAPTED


def test_jaccard_below_threshold_is_created_without_references() -> None:
    decision = _classify(
        [_cand(1, "Doc", "T1")], techniques=("T1", "T2", "T3", "T4", "T5")
    )  # 1/5 = 0.2
    assert decision.kind is FlowProvenance.CREATED
    assert decision.references == ()
    assert decision.similarity == 0.0


def test_empty_candidates_is_created() -> None:
    decision = _classify([])
    assert decision.kind is FlowProvenance.CREATED
    assert decision.references == ()


def test_empty_campaign_techniques_never_match_by_technique_set() -> None:
    decision = _classify([_cand(1, "Doc")], techniques=())
    assert decision.kind is FlowProvenance.CREATED


def test_adapted_references_are_capped_ordered_and_subset_of_candidates() -> None:
    candidates = [
        _cand(4, "d", "T1", "T2", "T9"),  # 2/5
        _cand(2, "b", "T1", "T2", "T3", "T9"),  # 3/5
        _cand(3, "c", "T1", "T2", "T9"),  # 2/5 (ties with 4, lower id first)
        _cand(1, "a", "T1", "T2", "T3", "T9"),  # 3/5
    ]
    decision = _classify(candidates)
    assert decision.kind is FlowProvenance.ADAPTED
    assert len(decision.references) == MAX_REFERENCES
    assert decision.references == (1, 2, 3)
    assert set(decision.references) <= {c.flow_id for c in candidates}


def test_decision_is_independent_of_input_order() -> None:
    candidates = [
        _cand(7, "g", "T1", "T2", "T8"),
        _cand(2, "b", "T1", "T9"),
        _cand(5, "e", "T1", "T2", "T3", "T8"),
    ]
    aliases = ("Alias One", "Alias Two")
    baseline = classify_flow_path("N", aliases, frozenset({"T1", "T2", "T3"}), candidates)
    for perm in itertools.permutations(candidates):
        for alias_perm in itertools.permutations(aliases):
            got = classify_flow_path("N", alias_perm, frozenset({"T3", "T1", "T2"}), list(perm))
            assert got == baseline


def test_threshold_env_var_and_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    assert similarity_threshold() == DEFAULT_SIMILARITY_THRESHOLD
    monkeypatch.setenv(SIMILARITY_ENV_VAR, "0.9")
    assert similarity_threshold() == 0.9
    assert _classify([_cand(1, "Doc", "T1", "T2", "T3", "T9")]).kind is FlowProvenance.CREATED
    for invalid in ("abc", "1.5", "-0.1", ""):
        monkeypatch.setenv(SIMILARITY_ENV_VAR, invalid)
        assert similarity_threshold() == DEFAULT_SIMILARITY_THRESHOLD


def test_helpers_normalize_and_jaccard() -> None:
    assert normalize_name("  A-b,  C ") == "a b c"
    assert jaccard(frozenset(), frozenset()) == 0.0
    assert jaccard(frozenset({"a", "b"}), frozenset({"b", "c"})) == pytest.approx(1 / 3)
