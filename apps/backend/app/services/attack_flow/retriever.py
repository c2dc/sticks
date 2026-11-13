"""Candidate retrieval for the flow decision policy.

Candidates come from two sources, merged and deduplicated by ``flow_id``:

* relational: flows having an object whose ``technique_id`` is one of the
  campaign techniques (``attack_flow_objects``);
* vector: ``ExperienceMemory.retrieve`` hits with ``metadata.kind ==
  "attack_flow"``. When the vector side is unavailable it degrades silently to
  the relational candidates only.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import AttackFlowObjectRow, AttackFlowRow
from app.models.enums import FlowOrigin
from app.services.ai.memory import ExperienceMemory
from app.services.attack_flow.policy import FlowCandidate

logger = logging.getLogger(__name__)

DEFAULT_VECTOR_K = 5
# Campaign id passed to ``retrieve`` so it excludes nothing real: flow
# embeddings are anchored to the affinity sentinel campaign.
_NO_CAMPAIGN = "__flow_retriever_no_campaign__"


class FlowRetriever:
    """Finds corpus flows that may match a campaign."""

    def __init__(self, session: Session, *, memory: ExperienceMemory | None = None) -> None:
        self._session = session
        self._memory = memory

    def retrieve(
        self,
        *,
        campaign_techniques: Iterable[str],
        campaign_description: str = "",
        k: int = DEFAULT_VECTOR_K,
    ) -> list[FlowCandidate]:
        """Return deduplicated candidates ordered by ``flow_id``."""
        techniques = sorted({t for t in campaign_techniques if t})

        relational_ids = self._relational_flow_ids(techniques)
        vector_scores = self._vector_hits(campaign_description, k)

        flow_ids = sorted(set(relational_ids) | set(vector_scores))
        if not flow_ids:
            return []

        names = self._flow_names(flow_ids)
        technique_sets = self._flow_techniques(flow_ids)
        return [
            FlowCandidate(
                flow_id=flow_id,
                name=names.get(flow_id, ""),
                technique_ids=frozenset(technique_sets.get(flow_id, ())),
                vector_score=vector_scores.get(flow_id),
            )
            for flow_id in flow_ids
            if flow_id in names
        ]

    # ----- relational ------------------------------------------------------

    def _relational_flow_ids(self, techniques: Sequence[str]) -> list[int]:
        if not techniques:
            return []
        rows = self._session.execute(
            select(AttackFlowObjectRow.flow_id)
            .join(AttackFlowRow, AttackFlowRow.id == AttackFlowObjectRow.flow_id)
            .where(
                AttackFlowObjectRow.technique_id.in_(techniques),
                AttackFlowRow.origin == FlowOrigin.MITRE_CORPUS,
            )
            .distinct()
        ).all()
        return [int(row[0]) for row in rows]

    def corpus_available(self) -> bool:
        """True when at least one documented (corpus) flow has been ingested."""
        row = self._session.execute(
            select(AttackFlowRow.id).where(AttackFlowRow.origin == FlowOrigin.MITRE_CORPUS).limit(1)
        ).first()
        return row is not None

    def _flow_techniques(self, flow_ids: Sequence[int]) -> dict[int, set[str]]:
        """Complete technique sets of the given flows (second read)."""
        result: dict[int, set[str]] = defaultdict(set)
        rows = self._session.execute(
            select(AttackFlowObjectRow.flow_id, AttackFlowObjectRow.technique_id)
            .where(
                AttackFlowObjectRow.flow_id.in_(flow_ids),
                AttackFlowObjectRow.technique_id.is_not(None),
            )
            .distinct()
        ).all()
        for flow_id, technique_id in rows:
            result[int(flow_id)].add(technique_id)
        return result

    def _flow_names(self, flow_ids: Sequence[int]) -> dict[int, str]:
        rows = self._session.execute(
            select(AttackFlowRow.id, AttackFlowRow.label, AttackFlowRow.identifier).where(
                AttackFlowRow.id.in_(flow_ids),
                AttackFlowRow.origin == FlowOrigin.MITRE_CORPUS,
            )
        ).all()
        return {int(r[0]): (r[1] or r[2] or "") for r in rows}

    # ----- vector ----------------------------------------------------------

    def _vector_hits(self, description: str, k: int) -> dict[int, float | None]:
        """Map ``flow_id`` to a score for ``attack_flow`` hits; ``{}`` if unavailable."""
        if self._memory is None or not description.strip():
            return {}
        try:
            hits = self._memory.retrieve(campaign_id=_NO_CAMPAIGN, query=description, k=k)
        except Exception as exc:  # noqa: BLE001 -- degrade to relational only
            logger.warning(
                "Flow vector retrieval skipped (%s: %s); using relational candidates only.",
                type(exc).__name__,
                exc,
            )
            return {}

        flow_hits = [
            h
            for h in hits
            if isinstance(h.get("metadata"), dict) and h["metadata"].get("kind") == "attack_flow"
        ]
        ids = self._resolve_hit_flow_ids(flow_hits)
        return dict(ids)

    def _resolve_hit_flow_ids(
        self, hits: Sequence[dict[str, Any]]
    ) -> list[tuple[int, float | None]]:
        """Resolve hits to flow ids via ``metadata.flow_id`` or ``source_key``."""
        resolved: list[tuple[int, float | None]] = []
        pending_keys: dict[str, float | None] = {}
        for hit in hits:
            meta = hit["metadata"]
            score = meta.get("score") if isinstance(meta.get("score"), float) else None
            flow_id = meta.get("flow_id")
            if isinstance(flow_id, int) and not isinstance(flow_id, bool):
                resolved.append((flow_id, score))
            elif isinstance(meta.get("source_key"), str):
                pending_keys[meta["source_key"]] = score
        if pending_keys:
            rows = self._session.execute(
                select(AttackFlowRow.id, AttackFlowRow.source_key).where(
                    AttackFlowRow.source_key.in_(list(pending_keys))
                )
            ).all()
            for flow_id, source_key in rows:
                resolved.append((int(flow_id), pending_keys[source_key]))
        return resolved
