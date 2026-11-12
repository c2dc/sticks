"""Attack Flow corpus ingestion: fetch, parse, persist idempotently, embed.

Per file: ``parse_afb`` -> ``annotate_flow_cycles`` -> upsert by ``source_key``
(objects and edges are deleted and recreated in the same transaction) ->
best-effort embedding through ``ExperienceMemory.store_affinity`` (kind
``attack_flow``, ``dedup_key = sha256("attack_flow:" + source_key)``).

A file that cannot be parsed (or downloaded) is recorded in ``skipped`` as
``{item, reason}`` and the run continues. A failed embedding never rolls back
the relational rows. The service does not commit; the caller owns the
transaction, as in the sibling ingest services.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.services.ai.memory import ExperienceMemory
from app.services.attack_flow.afb_parser import AfbParseError, parse_afb
from app.services.attack_flow.corpus_fetcher import (
    AttackFlowCorpusFetcher,
    CorpusFetchError,
    CorpusFile,
)
from app.services.attack_flow.cycles import annotate_flow_cycles
from app.services.attack_flow.model import AttackFlow

logger = logging.getLogger(__name__)

_AFFINITY_KIND = "attack_flow"
_SOURCE = "attack_flow_corpus"


class CorpusFetcher(Protocol):
    def list_files(self) -> list[CorpusFile]: ...

    def fetch(self, corpus_file: CorpusFile) -> bytes: ...


@dataclass
class AttackFlowIngestSummary:
    """Outcome of one run; ``skipped`` entries are ``{item, reason}`` dicts."""

    files_seen: int = 0
    flows_inserted: int = 0
    flows_updated: int = 0
    flows_unchanged: int = 0
    embeddings_stored: int = 0
    vector_failures: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def attack_flow_dedup_key(source_key: str) -> str:
    return hashlib.sha256(f"attack_flow:{source_key}".encode("utf-8")).hexdigest()


def _embedding_content(flow: AttackFlow) -> str:
    techniques = sorted({o.technique_id for o in flow.objects if o.technique_id})
    return f"{flow.name}. {flow.description}. Techniques: {', '.join(techniques)}"


class AttackFlowIngestService:
    """Session-injected; fetcher and memory (embedder) are injectable."""

    def __init__(
        self,
        session: Session,
        *,
        memory: ExperienceMemory,
        fetcher: Optional[CorpusFetcher] = None,
    ) -> None:
        self._session = session
        self._memory = memory
        self._fetcher: CorpusFetcher = fetcher or AttackFlowCorpusFetcher()

    def vectorize_missing(
        self, progress: Optional[Callable[[int, int], None]] = None
    ) -> tuple[int, list[str]]:
        """Embed the stored corpus flows that have no embedding yet (no network).

        Rebuilds each flow from its persisted ``content``. Returns
        ``(embeddings_stored, errors)``; failures are isolated per flow.
        """
        row_cls = self._row_cls()
        rows = self._session.execute(
            select(row_cls).where(row_cls.source_key.is_not(None)).order_by(row_cls.id)
        ).scalars().all()
        stored_count = 0
        errors: list[str] = []
        for index, row in enumerate(rows, start=1):
            try:
                flow = AttackFlow.model_validate(row.content)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                errors.append(f"stored flow {row.source_key!r} is unreadable: {type(exc).__name__}")
                continue
            stored, error = self._vectorize(flow, row.source_key, replace=False)
            if stored:
                stored_count += 1
            elif error is not None:
                errors.append(error)
            if progress is not None and index % 10 == 0:
                progress(index, len(rows))
        return stored_count, errors

    def ingest(
        self, progress: Optional[Callable[[int, int], None]] = None
    ) -> AttackFlowIngestSummary:
        """Ingest every corpus file. A listing failure raises ``CorpusFetchError``
        before any write; per-file problems are isolated in ``skipped``.

        ``progress(done, total)`` is called after each file; ``total`` is the
        number of files in the listing (the source's expected count)."""
        summary = AttackFlowIngestSummary()
        files = self._fetcher.list_files()

        for corpus_file in files:
            if progress is not None:
                progress(summary.files_seen, len(files))
            summary.files_seen += 1
            source_key = corpus_file.name

            existing = self._session.execute(
                select(self._row_cls().id, self._row_cls().source_sha).where(
                    self._row_cls().source_key == source_key
                )
            ).first()
            if existing is not None and existing.source_sha == corpus_file.sha:
                summary.flows_unchanged += 1
                continue

            try:
                raw = self._fetcher.fetch(corpus_file)
                flow = annotate_flow_cycles(parse_afb(raw, source_key=source_key))
            except AfbParseError as exc:
                summary.skipped.append({"item": source_key, "reason": exc.reason})
                logger.warning("attack flow %s skipped: %s", source_key, exc.reason)
                continue
            except CorpusFetchError as exc:
                summary.skipped.append({"item": source_key, "reason": "download_failed"})
                logger.warning("attack flow %s download failed: %s", source_key, exc)
                continue

            # Relational half: one savepoint per flow so a DB error on one file
            # does not discard the flows already persisted.
            savepoint = self._session.begin_nested()
            try:
                inserted = self._upsert_flow(flow, corpus_file)
                savepoint.commit()
            except Exception as exc:  # noqa: BLE001 - isolate the file
                savepoint.rollback()
                summary.skipped.append({"item": source_key, "reason": "persist_failed"})
                logger.warning("attack flow %s persist failed: %s", source_key, exc)
                continue
            if inserted:
                summary.flows_inserted += 1
            else:
                summary.flows_updated += 1

            stored, error = self._vectorize(flow, source_key, replace=not inserted)
            if stored:
                summary.embeddings_stored += 1
            elif error is not None:
                summary.vector_failures += 1
                summary.errors.append(error)

        return summary

    # ---- relational ---------------------------------------------------------

    @staticmethod
    def _row_cls() -> Any:
        from app.models.domain import AttackFlowRow

        return AttackFlowRow

    def _upsert_flow(self, flow: AttackFlow, corpus_file: CorpusFile) -> bool:
        """Upsert by ``source_key``; returns True when a new row was created."""
        from app.models.domain import (
            AttackFlowEdgeRow,
            AttackFlowObjectRow,
            AttackFlowRow,
        )
        from app.models.enums import FlowOrigin

        row = self._session.execute(
            select(AttackFlowRow).where(AttackFlowRow.source_key == corpus_file.name)
        ).scalar_one_or_none()
        inserted = row is None
        if row is None:
            row = AttackFlowRow(
                campaign_id=None,
                origin=FlowOrigin.MITRE_CORPUS,
                source_key=corpus_file.name,
                identifier=corpus_file.name,
                created_at=dt.datetime.utcnow(),
            )
            self._session.add(row)

        row.source_sha = corpus_file.sha
        row.label = flow.name
        row.scope = flow.scope or None
        row.description = flow.description or None
        row.has_cycles = bool(flow.cycles)
        row.provenance = None
        row.content = flow.model_dump(mode="json")
        self._session.flush()

        if not inserted:
            self._session.execute(
                delete(AttackFlowEdgeRow).where(AttackFlowEdgeRow.flow_id == row.id)
            )
            self._session.execute(
                delete(AttackFlowObjectRow).where(AttackFlowObjectRow.flow_id == row.id)
            )
            self._session.flush()

        for obj in flow.objects:
            self._session.add(
                AttackFlowObjectRow(
                    flow_id=row.id,
                    stix_id=obj.id,
                    type=obj.type,
                    name=obj.name,
                    technique_id=obj.technique_id,
                    tactic_id=obj.tactic_id,
                    properties=self._object_properties(obj),
                )
            )
        back_edges = {(src, dst) for src, dst in flow.back_edges}
        for src, dst in flow.edges():
            self._session.add(
                AttackFlowEdgeRow(
                    flow_id=row.id,
                    source_stix_id=src,
                    target_stix_id=dst,
                    kind="effect",
                    is_back_edge=(src, dst) in back_edges,
                )
            )
        for obj in flow.objects:
            for asset_id in obj.asset_refs:
                self._session.add(
                    AttackFlowEdgeRow(
                        flow_id=row.id,
                        source_stix_id=obj.id,
                        target_stix_id=asset_id,
                        kind="asset",
                        is_back_edge=False,
                    )
                )
        self._session.flush()
        return inserted

    @staticmethod
    def _object_properties(obj: Any) -> dict[str, Any]:
        props: dict[str, Any] = dict(obj.properties)
        for key in ("description", "pattern", "operator"):
            value = getattr(obj, key)
            if value is not None:
                props[key] = value
        return props

    # ---- vector (best-effort) -----------------------------------------------

    def _vectorize(
        self, flow: AttackFlow, source_key: str, *, replace: bool
    ) -> tuple[bool, Optional[str]]:
        """One embedding per flow, keyed by ``source_key``.

        When the flow was re-ingested (``replace``), the previous embedding row
        is removed first so the stored content follows the new description.
        Any failure rolls back only the savepoint and is reported.
        """
        dedup_key = attack_flow_dedup_key(source_key)
        savepoint = self._session.begin_nested()
        try:
            if replace:
                from app.models.domain import ExperienceMemory as ExperienceMemoryRow

                self._session.execute(
                    delete(ExperienceMemoryRow).where(
                        ExperienceMemoryRow.meta["dedup_key"].as_string() == dedup_key
                    )
                )
            stored = self._memory.store_affinity(
                kind=_AFFINITY_KIND,
                content=_embedding_content(flow),
                metadata={
                    "source": _SOURCE,
                    "source_key": source_key,
                    "scope": flow.scope,
                },
                dedup_key=dedup_key,
            )
            savepoint.commit()
            return stored, None
        except Exception as exc:  # noqa: BLE001 - vectorization is best-effort
            try:
                savepoint.rollback()
            except Exception:  # noqa: BLE001
                pass
            message = (
                f"vectorization skipped for attack flow {source_key!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            logger.warning(message)
            return False, message


__all__ = [
    "AttackFlowIngestService",
    "AttackFlowIngestSummary",
    "attack_flow_dedup_key",
]
