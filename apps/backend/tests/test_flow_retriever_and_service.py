"""Retriever and FlowService tests on a temporary SQLite database (no network).

**Validates: Requirements 3.1, 3.5, 2.6, 3.6**
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import AttackFlowObjectRow, AttackFlowRow
from app.models.enums import FlowOrigin, FlowProvenance
from app.services.attack_flow.retriever import FlowRetriever
from app.services.flow.flow_service import FlowService


@pytest.fixture()
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_db_engine(
        Settings(database_url=f"sqlite:///{(tmp_path / 'flows.db').as_posix()}")
    )
    import_models()
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as db:
        yield db
    engine.dispose()


def _add_flow(
    db: Session,
    *,
    label: str,
    techniques: list[str],
    origin: FlowOrigin = FlowOrigin.MITRE_CORPUS,
    campaign_id: str | None = None,
    source_key: str | None = None,
    content: dict | None = None,
    provenance: dict | None = None,
) -> int:
    row = AttackFlowRow(
        campaign_id=campaign_id,
        origin=origin,
        identifier=label.lower(),
        label=label,
        source_key=source_key,
        content=content or {},
        provenance=provenance,
        created_at=dt.datetime(2024, 1, 1),
    )
    db.add(row)
    db.flush()
    for i, technique in enumerate(techniques):
        db.add(
            AttackFlowObjectRow(
                flow_id=row.id,
                stix_id=f"attack-action--{row.id}-{i}",
                type="attack-action",
                technique_id=technique,
            )
        )
    db.commit()
    return row.id


class _Memory:
    def __init__(self, hits=None, error: Exception | None = None) -> None:
        self._hits = hits or []
        self._error = error

    def retrieve(self, *, campaign_id, query, k):
        if self._error:
            raise self._error
        return self._hits


def test_relational_candidates_only_corpus_flows_with_full_technique_sets(
    session: Session,
) -> None:
    corpus = _add_flow(session, label="Doc", techniques=["T1", "T2", "T3"])
    _add_flow(
        session, label="Mine", techniques=["T1"], origin=FlowOrigin.AI_GENERATED,
        campaign_id="c1",
    )
    _add_flow(session, label="Unrelated", techniques=["T9"])

    found = FlowRetriever(session).retrieve(campaign_techniques=["T1"])

    assert [c.flow_id for c in found] == [corpus]
    assert found[0].technique_ids == frozenset({"T1", "T2", "T3"})
    assert found[0].name == "Doc"


def test_vector_hits_are_merged_deduplicated_and_non_flow_hits_ignored(
    session: Session,
) -> None:
    by_tech = _add_flow(session, label="A", techniques=["T1"])
    by_vector = _add_flow(session, label="B", techniques=["T7"], source_key="b.json")
    memory = _Memory(
        [
            {"metadata": {"kind": "attack_flow", "flow_id": by_tech, "score": 0.9}},
            {"metadata": {"kind": "attack_flow", "source_key": "b.json", "score": 0.5}},
            {"metadata": {"kind": "campaign_note", "flow_id": 999}},
        ]
    )

    found = FlowRetriever(session, memory=memory).retrieve(
        campaign_techniques=["T1"], campaign_description="lateral movement"
    )

    assert [c.flow_id for c in found] == sorted([by_tech, by_vector])
    assert {c.flow_id: c.vector_score for c in found} == {by_tech: 0.9, by_vector: 0.5}


def test_vector_failure_degrades_to_relational_only(session: Session) -> None:
    flow_id = _add_flow(session, label="A", techniques=["T1"])
    retriever = FlowRetriever(session, memory=_Memory(error=RuntimeError("down")))

    found = retriever.retrieve(campaign_techniques=["T1"], campaign_description="x")

    assert [c.flow_id for c in found] == [flow_id]


def test_corpus_available_reflects_ingested_corpus(session: Session) -> None:
    retriever = FlowRetriever(session)
    assert retriever.corpus_available() is False
    _add_flow(session, label="Mine", techniques=[], origin=FlowOrigin.AI_GENERATED,
              campaign_id="c1")
    assert retriever.corpus_available() is False
    _add_flow(session, label="Doc", techniques=["T1"])
    assert retriever.corpus_available() is True


# ---------------------------------------------------------------------------
# FlowService
# ---------------------------------------------------------------------------


def _legacy_content(edges: list[tuple[str, str]]) -> dict:
    return {
        "structural_nodes": [
            {"node_id": n, "technique_id": f"T{n}", "technique_name": f"Name {n}"}
            for n in ("1", "2", "3")
        ],
        "arestas": [{"de": a, "para": b} for a, b in edges],
    }


def test_flow_service_converts_legacy_content_and_dashes_back_edges(
    session: Session,
) -> None:
    corpus = _add_flow(session, label="Doc", techniques=["T1"], source_key="doc.json")
    flow_id = _add_flow(
        session,
        label="Mine",
        techniques=[],
        origin=FlowOrigin.AI_GENERATED,
        campaign_id="c1",
        content=_legacy_content([("1", "2"), ("2", "3"), ("3", "2")]),
        provenance={"kind": "adapted", "references": [corpus, 12345], "similarity": 0.5},
    )

    service = FlowService(session)
    detail = service.get_flow("c1", flow_id)

    assert detail is not None
    assert len(detail.nodes) == 3
    assert detail.has_cycles is True
    assert len(detail.cycles) == 1
    assert len(detail.back_edges) == 1
    assert sum(e.is_back_edge for e in detail.edges) == 1
    assert detail.mermaid.startswith("flowchart TD")
    assert detail.mermaid.count("-.->") == 1
    assert detail.provenance is not None
    assert detail.provenance.kind is FlowProvenance.ADAPTED
    # Unknown reference ids are dropped from ``reference_flows`` only.
    assert [r.id for r in detail.provenance.reference_flows] == [corpus]
    assert detail.provenance.reference_flows[0].source_key == "doc.json"


def test_flow_service_acyclic_flow_has_no_back_edges(session: Session) -> None:
    flow_id = _add_flow(
        session, label="Line", techniques=[], origin=FlowOrigin.AI_GENERATED,
        campaign_id="c1", content=_legacy_content([("1", "2"), ("2", "3")]),
    )
    detail = FlowService(session).get_flow("c1", flow_id)

    assert detail is not None
    assert detail.cycles == [] and detail.back_edges == []
    assert "-.->" not in detail.mermaid


def test_flow_service_list_and_campaign_scoping(session: Session) -> None:
    mine = _add_flow(
        session, label="Mine", techniques=[], origin=FlowOrigin.AI_GENERATED,
        campaign_id="c1", content=_legacy_content([]),
    )
    _add_flow(
        session, label="Other", techniques=[], origin=FlowOrigin.AI_GENERATED,
        campaign_id="c2", content=_legacy_content([]),
    )
    service = FlowService(session)

    assert [f.id for f in service.list_flows("c1")] == [mine]
    assert service.get_flow("c2", mine) is None
    assert service.get_flow("c1", 99999) is None
    assert service.get_corpus_flow(mine) is not None
