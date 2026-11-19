"""Three-path flow decision policy (``exact`` / ``adapted`` / ``created``).

:func:`classify_flow_path` is a pure function: no LLM, no I/O. The LLM is only
called afterwards, by the flow agent, on the ``adapted`` and ``created`` paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass

from app.models.enums import FlowProvenance

DEFAULT_SIMILARITY_THRESHOLD = 0.25
SIMILARITY_ENV_VAR = "ATTACK_FLOW_SIMILARITY_MIN"
DEFAULT_COVERAGE_THRESHOLD = 0.5
COVERAGE_ENV_VAR = "ATTACK_FLOW_COVERAGE_MIN"
MAX_REFERENCES = 3

REASON_EXACT_MATCH = "exact_match"
REASON_ADAPTED = "adapted_above_threshold"
REASON_NO_CANDIDATE = "no_candidate_above_threshold"
REASON_ALL_EXCLUDED = "all_candidates_excluded"
REASON_LOW_COVERAGE = "low_coverage_override"

_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def _read_unit_interval(env_var: str, default: float) -> float:
    """Read a float in ``[0, 1]`` from the environment, else the default."""
    raw = os.environ.get(env_var)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not 0.0 <= value <= 1.0:
        return default
    return value


def similarity_threshold() -> float:
    """Return the Jaccard threshold, honouring ``ATTACK_FLOW_SIMILARITY_MIN``.

    Falls back to the default when the variable is absent, not a number or
    outside ``[0, 1]``.
    """
    return _read_unit_interval(SIMILARITY_ENV_VAR, DEFAULT_SIMILARITY_THRESHOLD)


def coverage_threshold() -> float:
    """Return the minimum technique coverage, honouring ``ATTACK_FLOW_COVERAGE_MIN``.

    Same rule as :func:`similarity_threshold`.
    """
    return _read_unit_interval(COVERAGE_ENV_VAR, DEFAULT_COVERAGE_THRESHOLD)


def flow_hash(
    technique_ids: Iterable[str],
    edges: Iterable[tuple[str, str]] = (),
) -> str:
    """Canonical SHA-256 of a flow: its sorted techniques and sorted edges."""
    payload = {
        "techniques": sorted(set(technique_ids)),
        "edges": sorted({(source, target) for source, target in edges}),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FlowCandidate:
    flow_id: int
    name: str
    technique_ids: frozenset[str]
    vector_score: float | None = None  # cosine similarity, when found by vector
    flow_hash: str | None = None  # canonical hash, used only for exclusion


@dataclass(frozen=True)
class FlowDecision:
    kind: FlowProvenance
    references: tuple[int, ...]  # flow ids, always a subset of the candidates
    similarity: float  # best Jaccard score; 0.0 for ``created``
    reason_code: str = REASON_NO_CANDIDATE
    coverage: float = 0.0  # |campaign & best reference| / |campaign|


def normalize_name(value: str) -> str:
    """Lowercase, drop punctuation and collapse whitespace."""
    cleaned = _NON_ALNUM_RE.sub(" ", value.lower())
    return _SPACES_RE.sub(" ", cleaned).strip()


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Jaccard similarity of two sets; 0.0 when both are empty."""
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def coverage_of(campaign_techniques: frozenset[str], reference: frozenset[str]) -> float:
    """Share of the campaign techniques present in the reference; 0.0 if empty."""
    if not campaign_techniques:
        return 0.0
    return len(campaign_techniques & reference) / len(campaign_techniques)


def classify_flow_path(
    campaign_name: str,
    aliases: Sequence[str],
    campaign_techniques: frozenset[str],
    candidates: Sequence[FlowCandidate],
    *,
    threshold: float | None = None,
    excluded_flow_ids: Collection[int] = (),
    excluded_hashes: Collection[str] = (),
    coverage_min: float | None = None,
) -> FlowDecision:
    """Decide how the campaign flow is obtained, from the candidates alone.

    0. Exclusion: candidates whose ``flow_id`` or ``flow_hash`` was already
       presented are dropped. If there were candidates and all were dropped,
       the result is ``created`` with ``all_candidates_excluded``.
    1. ``exact``: a candidate has the same (non-empty) technique set as the
       campaign, or the same normalized name as the campaign or an alias.
       Ties go to the lowest ``flow_id``.
    2. ``adapted``: otherwise, the best Jaccard is ``>=`` the threshold. The
       references are up to ``MAX_REFERENCES`` candidates at or above the
       threshold, ordered by Jaccard descending then ``flow_id``.
    3. ``created``: otherwise (including no candidates); no references.
    4. Downgrade: an ``exact`` or ``adapted`` result whose best candidate
       covers less than ``coverage_min`` of the campaign techniques becomes
       ``created`` with ``low_coverage_override``.
    """
    limit = similarity_threshold() if threshold is None else threshold
    min_coverage = coverage_threshold() if coverage_min is None else coverage_min
    techniques = frozenset(campaign_techniques)
    wanted_names = {
        normalized
        for normalized in (normalize_name(n) for n in (campaign_name, *aliases))
        if normalized
    }

    blocked_ids = set(excluded_flow_ids)
    blocked_hashes = set(excluded_hashes)
    remaining = [
        c
        for c in candidates
        if c.flow_id not in blocked_ids
        and (c.flow_hash is None or c.flow_hash not in blocked_hashes)
    ]
    if candidates and not remaining:
        return FlowDecision(FlowProvenance.CREATED, (), 0.0, REASON_ALL_EXCLUDED, 0.0)

    scored = [(jaccard(c.technique_ids, techniques), c) for c in remaining]

    exact = [
        (score, c)
        for score, c in scored
        if (techniques and c.technique_ids == techniques)
        or (normalize_name(c.name) in wanted_names and normalize_name(c.name))
    ]
    if exact:
        score, chosen = min(exact, key=lambda item: item[1].flow_id)
        decision = FlowDecision(
            FlowProvenance.EXACT,
            (chosen.flow_id,),
            score,
            REASON_EXACT_MATCH,
            coverage_of(techniques, chosen.technique_ids),
        )
        return _apply_coverage_floor(decision, techniques, min_coverage)

    above = sorted(
        ((score, c) for score, c in scored if score >= limit),
        key=lambda item: (-item[0], item[1].flow_id),
    )
    if above:
        refs = tuple(c.flow_id for _, c in above[:MAX_REFERENCES])
        decision = FlowDecision(
            FlowProvenance.ADAPTED,
            refs,
            above[0][0],
            REASON_ADAPTED,
            coverage_of(techniques, above[0][1].technique_ids),
        )
        return _apply_coverage_floor(decision, techniques, min_coverage)

    return FlowDecision(FlowProvenance.CREATED, (), 0.0, REASON_NO_CANDIDATE, 0.0)


def _apply_coverage_floor(
    decision: FlowDecision, techniques: frozenset[str], min_coverage: float
) -> FlowDecision:
    """Downgrade to ``created`` when the best reference covers too little."""
    if techniques and decision.coverage < min_coverage:
        return FlowDecision(FlowProvenance.CREATED, (), 0.0, REASON_LOW_COVERAGE, decision.coverage)
    return decision
