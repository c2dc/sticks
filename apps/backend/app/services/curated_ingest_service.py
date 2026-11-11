"""CuratedIngestService - ingest the 8 curated cases from disk into the DB.

Phase 2, sub-step 2.1 of the disk->DB migration. This service is the single
point that reads the curated cases from disk (via the existing CaseService) and
PERSISTS their structured content into the database so the rest of the pipeline
can, in later sub-steps, stop reading disk at request time.

It only ADDS/updates data; it never deletes nor rewrites the on-disk files. The
write is idempotent per campaign: re-running upserts by natural key (campaign
slug, ability id, adversary id, and a single curated DAG per campaign), so no
duplicate rows accumulate.

Mapping disk -> DB:
* CaseData            -> campaigns row (slug, name, description, *_file paths)
* DagData             -> dags row (origin=human_curation, content JSON with
                         structural_nodes + derived edges)
* list[AbilityData]   -> abilities rows (executors JSON)
* AdversaryData       -> adversaries row (atomic_ordering JSON)

This module performs no network access and is Session-injected like the other
services.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.domain import (
    Ability,
    Adversary,
    Case,
    Dag,
)
from app.models.enums import DagOrigin, TranslationSource
from app.services.campaign_service import CaseData, CaseService


def _utcnow() -> dt.datetime:
    """Naive-UTC timestamp (project convention; portable across PG/SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


# Identifier of the curated DAG row (one human-curated DAG per campaign).
_CURATED_DAG_IDENTIFIER = "human-curation-v1"


class CuratedIngestService:
    """Persist the curated cases read from disk into the database (idempotent)."""

    def __init__(self, session: Session, case_service: Optional[CaseService] = None) -> None:
        self._session = session
        self._case_service = case_service or CaseService()

    # ---- public API --------------------------------------------------------

    def ingest_all(self) -> dict[str, Any]:
        """Ingest every curated case that loads cleanly; return a summary.

        Loads all curated cases via ``CaseService.load_all_cases_safe`` (per-case
        error isolation), then upserts each case's campaign/dag/abilities/
        adversary rows. A per-case load failure is recorded in the summary's
        ``failed`` list and never aborts the others. Idempotent: upserts by
        natural key so a second run does not duplicate rows.
        """
        catalog = self._case_service.load_all_cases_safe()

        summary: dict[str, Any] = {
            "campaigns": 0,
            "dags": 0,
            "abilities": 0,
            "adversaries": 0,
            "failed": sorted(catalog.errors.keys()),
        }

        for slug, case in catalog.cases.items():
            self._ingest_one(case)
            summary["campaigns"] += 1
            if case.dag is not None:
                summary["dags"] += 1
            summary["abilities"] += len(case.abilities)
            if case.adversary is not None:
                summary["adversaries"] += 1

        self._session.commit()
        return summary

    # ---- per-case upsert ---------------------------------------------------

    def _ingest_one(self, case: CaseData) -> None:
        """Upsert one case's campaign + dag + abilities + adversary (no commit)."""
        self._upsert_campaign(case)
        self._session.flush()  # ensure the campaigns row exists for the FKs
        self._upsert_dag(case)
        self._upsert_abilities(case)
        self._upsert_adversary(case)

    def _upsert_campaign(self, case: CaseData) -> None:
        row = self._session.get(Case, case.id)
        description = case.dag.campaign_name if case.dag else None
        if row is None:
            self._session.add(
                Case(
                    id=case.id,
                    name=case.name,
                    description=description,
                    ability_file=case.ability_file,
                    adversary_file=case.adversary_file,
                    dag_file=case.dag_file,
                    translation_source=TranslationSource.HUMAN_CURATION,
                )
            )
        else:
            row.name = case.name
            row.ability_file = case.ability_file
            row.adversary_file = case.adversary_file
            row.dag_file = case.dag_file
            row.translation_source = TranslationSource.HUMAN_CURATION

    def _upsert_dag(self, case: CaseData) -> None:
        if case.dag is None:
            return
        content = self._dag_content(case)
        # One curated DAG per campaign: find an existing human-curation row.
        from sqlalchemy import select

        existing = (
            self._session.execute(
                select(Dag).where(
                    Dag.campaign_id == case.id,
                    Dag.identifier == _CURATED_DAG_IDENTIFIER,
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            self._session.add(
                Dag(
                    campaign_id=case.id,
                    origin=DagOrigin.HUMAN_CURATION,
                    identifier=_CURATED_DAG_IDENTIFIER,
                    label=case.name,
                    content=content,
                    created_at=_utcnow(),
                )
            )
        else:
            existing.label = case.name
            existing.content = content

    @staticmethod
    def _dag_content(case: CaseData) -> dict[str, Any]:
        """Build the DAG content JSON: structural_nodes + derived edges."""
        dag = case.dag
        assert dag is not None
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, str]] = []
        for node in dag.structural_nodes:
            nodes.append(node.model_dump())
            if node.node_id:
                for child in node.child_nodes:
                    edges.append({"from": node.node_id, "to": child})
        return {
            "campaign_name": dag.campaign_name,
            "campaign_file": dag.campaign_file,
            "generated_at": dag.generated_at,
            "structural_nodes": nodes,
            "edges": edges,
            "metadata": dag.metadata,
            "capability_flow": dag.capability_flow,
        }

    def _upsert_abilities(self, case: CaseData) -> None:
        for ability in case.abilities:
            if not ability.ability_id:
                continue
            row = self._session.get(Ability, ability.ability_id)
            executors = [e.model_dump() for e in ability.executors]
            if row is None:
                self._session.add(
                    Ability(
                        ability_id=ability.ability_id,
                        campaign_id=case.id,
                        name=ability.name,
                        tactic=ability.tactic,
                        technique_name=ability.technique_name,
                        technique_id=ability.technique_id,
                        description=ability.description,
                        executors=executors,
                        translation_source=TranslationSource.HUMAN_CURATION,
                    )
                )
            else:
                row.campaign_id = case.id
                row.name = ability.name
                row.tactic = ability.tactic
                row.technique_name = ability.technique_name
                row.technique_id = ability.technique_id
                row.description = ability.description
                row.executors = executors
                row.translation_source = TranslationSource.HUMAN_CURATION

    def _upsert_adversary(self, case: CaseData) -> None:
        adversary = case.adversary
        if adversary is None:
            return
        row = self._session.get(Adversary, adversary.id)
        ordering = list(adversary.atomic_ordering)
        if row is None:
            self._session.add(
                Adversary(
                    id=adversary.id,
                    campaign_id=case.id,
                    name=adversary.name,
                    description=adversary.description,
                    atomic_ordering=ordering,
                    translation_source=TranslationSource.HUMAN_CURATION,
                )
            )
        else:
            row.campaign_id = case.id
            row.name = adversary.name
            row.description = adversary.description
            row.atomic_ordering = ordering
            row.translation_source = TranslationSource.HUMAN_CURATION
