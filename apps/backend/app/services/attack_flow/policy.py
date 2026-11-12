"""Three-path flow decision policy (``exact`` / ``adapted`` / ``created``).

:func:`classify_flow_path` is a pure function: no LLM, no I/O. The LLM is only
called afterwards, by the flow agent, on the ``adapted`` and ``created`` paths.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.models.enums import FlowProvenance

DEFAULT_SIMILARITY_THRESHOLD = 0.25
SIMILARITY_ENV_VAR = "ATTACK_FLOW_SIMILARITY_MIN"
MAX_REFERENCES = 3

_NON_ALNUM_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES_RE = re.compile(r"\s+")


def similarity_threshold() -> float:
    """Return the Jaccard threshold, honouring ``ATTACK_FLOW_SIMILARITY_MIN``.

    Falls back to the default when the variable is absent, not a number or
    outside ``[0, 1]``.
    """
    raw = os.environ.get(SIMILARITY_ENV_VAR)
    if raw is None or not raw.strip():
        return DEFAULT_SIMILARITY_THRESHOLD
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SIMILARITY_THRESHOLD
    if not 0.0 <= value <= 1.0:
        return DEFAULT_SIMILARITY_THRESHOLD
    return value


@dataclass(frozen=True)
class FlowCandidate:
    flow_id: int
    name: str
    technique_ids: frozenset[str]
    vector_score: float | None = None  # cosine similarity, when found by vector


@dataclass(frozen=True)
class FlowDecision:
    kind: FlowProvenance
    references: tuple[int, ...]  # flow ids, always a subset of the candidates
    similarity: float  # best Jaccard score; 0.0 for ``created``


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


def classify_flow_path(
    campaign_name: str,
    aliases: Sequence[str],
    campaign_techniques: frozenset[str],
    candidates: Sequence[FlowCandidate],
    *,
    threshold: float | None = None,
) -> FlowDecision:
    """Decide how the campaign flow is obtained, from the candidates alone.

    1. ``exact``: a candidate has the same (non-empty) technique set as the
       campaign, or the same normalized name as the campaign or an alias.
       Ties go to the lowest ``flow_id``.
    2. ``adapted``: otherwise, the best Jaccard is ``>=`` the threshold. The
       references are up to ``MAX_REFERENCES`` candidates at or above the
       threshold, ordered by Jaccard descending then ``flow_id``.
    3. ``created``: otherwise (including no candidates); no references.
    """
    limit = similarity_threshold() if threshold is None else threshold
    techniques = frozenset(campaign_techniques)
    wanted_names = {
        normalized
        for normalized in (normalize_name(n) for n in (campaign_name, *aliases))
        if normalized
    }

    scored = [(jaccard(c.technique_ids, techniques), c) for c in candidates]

    exact = [
        (score, c)
        for score, c in scored
        if (techniques and c.technique_ids == techniques)
        or (normalize_name(c.name) in wanted_names and normalize_name(c.name))
    ]
    if exact:
        score, chosen = min(exact, key=lambda item: item[1].flow_id)
        return FlowDecision(FlowProvenance.EXACT, (chosen.flow_id,), score)

    above = sorted(
        ((score, c) for score, c in scored if score >= limit),
        key=lambda item: (-item[0], item[1].flow_id),
    )
    if above:
        refs = tuple(c.flow_id for _, c in above[:MAX_REFERENCES])
        return FlowDecision(FlowProvenance.ADAPTED, refs, above[0][0])

    return FlowDecision(FlowProvenance.CREATED, (), 0.0)
