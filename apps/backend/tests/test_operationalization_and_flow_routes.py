"""Deterministic route tests: operationalization listing and attack-flow routes.

Covers task 8.3 of the ``agentic-only-flow-redesign`` spec:

* ``GET /api/stix/bases/{version}/campaigns/{campaign_key}/operationalization``
  (Property 13: the listing is an exact relational lookup; Req. 6.2, 6.3, 6.4, 6.6).
* ``GET /api/campaigns/{campaign}/flows``, ``.../flows/{flow_id}`` and
  ``GET /api/attack-flows/{flow_id}`` (Req. 2.1, 2.6, 3.6).

Everything runs on a temporary SQLite database through ``TestClient``: no
network, no embedder, no Hypothesis.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import (
    AttackFlowRow,
    Case,
    IngestSourceState,
    StixCampaign,
    StixCampaignTechnique,
    StixPackage,
    TechniqueCommand,
)
from app.models.enums import FlowOrigin, IngestSource, IngestStatus

_VERSION = "18.0"
_CAMPAIGN_KEY = "ops-campaign"


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    tmp_dir = tempfile.mkdtemp(prefix="ops_flow_routes_")
    db_path = Path(tmp_dir) / "routes.db"
    engine = create_db_engine(Settings(database_url=f"sqlite:///{db_path.as_posix()}"))
    import_models()
    Base.metadata.create_all(bind=engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Operationalization listing
# ---------------------------------------------------------------------------


def _seed_campaign(factory: sessionmaker[Session]) -> None:
    """Persisted package with one campaign using T1059 (commands) and T1105 (none)."""
    with factory() as session:
        package = StixPackage(version=_VERSION, name="Enterprise ATT&CK v18.0")
        session.add(package)
        session.flush()
        campaign = StixCampaign(
            package_id=package.id,
            stix_id="campaign--ops",
            name="Ops Campaign",
            external_id="C9000",
            description="Operationalization fixture.",
            aliases=[],
            curated=False,
            curated_slug=_CAMPAIGN_KEY,
        )
        session.add(campaign)
        session.flush()
        session.add_all(
            [
                StixCampaignTechnique(
                    campaign_id=campaign.id,
                    technique_id="T1105",
                    technique_name="Ingress Tool Transfer",
                    tactic="command-and-control",
                    description="d",
                ),
                StixCampaignTechnique(
                    campaign_id=campaign.id,
                    technique_id="T1059",
                    technique_name="Command and Scripting Interpreter",
                    tactic="execution",
                    description="d",
                ),
            ]
        )
        # Commands for T1059 (two) and for a technique outside the campaign.
        session.add_all(
            [
                TechniqueCommand(
                    technique_id="T1059",
                    command="whoami",
                    executor="sh",
                    platform="linux",
                    source="atomic_red_team",
                    command_hash="h-1",
                ),
                TechniqueCommand(
                    technique_id="T1059",
                    command="id",
                    executor="sh",
                    platform="linux",
                    source="atomic_red_team",
                    command_hash="h-2",
                ),
                TechniqueCommand(
                    technique_id="T9999",
                    command="outside",
                    executor="sh",
                    platform="linux",
                    source="atomic_red_team",
                    command_hash="h-3",
                ),
            ]
        )
        session.commit()


def _url() -> str:
    return f"/api/stix/bases/{_VERSION}/campaigns/{_CAMPAIGN_KEY}/operationalization"


def test_operationalization_lists_exact_commands_and_empty_technique(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _seed_campaign(session_factory)
    with session_factory() as session:
        session.add(
            IngestSourceState(
                source=IngestSource.ATOMIC,
                status=IngestStatus.READY,
                loaded_count=3,
                expected_count=3,
            )
        )
        session.commit()

    resp = client.get(_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_key"] == _CAMPAIGN_KEY
    assert body["atomic_status"] == {
        "status": "ready",
        "loaded_count": 3,
        "expected_count": 3,
    }

    by_id = {t["technique_id"]: t for t in body["techniques"]}
    # Exactly the campaign techniques, sorted by id (Req. 6.2).
    assert [t["technique_id"] for t in body["techniques"]] == ["T1059", "T1105"]
    # Technique with commands: only its own rows, ordered by id.
    assert [c["command"] for c in by_id["T1059"]["commands"]] == ["whoami", "id"]
    assert {c["source"] for c in by_id["T1059"]["commands"]} == {"atomic_red_team"}
    assert all(isinstance(c["id"], int) for c in by_id["T1059"]["commands"])
    # Technique without commands: explicit empty list, no similarity fallback (6.3, 6.6).
    assert by_id["T1105"]["commands"] == []
    assert by_id["T1105"]["technique_name"] == "Ingress Tool Transfer"
    # Commands of techniques outside the campaign never leak in.
    assert "outside" not in resp.text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, "pending"),
        (IngestStatus.RUNNING, "running"),
        (IngestStatus.FAILED, "failed"),
    ],
)
def test_operationalization_atomic_status_not_ready(
    client: TestClient,
    session_factory: sessionmaker[Session],
    state: IngestStatus | None,
    expected: str,
) -> None:
    _seed_campaign(session_factory)
    if state is not None:
        with session_factory() as session:
            session.add(
                IngestSourceState(
                    source=IngestSource.ATOMIC,
                    status=state,
                    loaded_count=1,
                    expected_count=10,
                )
            )
            session.commit()

    resp = client.get(_url())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["atomic_status"]["status"] == expected
    if state is not None:
        assert body["atomic_status"]["loaded_count"] == 1
        assert body["atomic_status"]["expected_count"] == 10
    # The technique list is still served while ingestion is not ready (Req. 6.4).
    assert [t["technique_id"] for t in body["techniques"]] == ["T1059", "T1105"]


def test_operationalization_unknown_campaign_404(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    _seed_campaign(session_factory)
    resp = client.get(
        f"/api/stix/bases/{_VERSION}/campaigns/does-not-exist/operationalization"
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["motivo"] == "campanha_nao_encontrada"


# ---------------------------------------------------------------------------
# Attack-flow routes
# ---------------------------------------------------------------------------

_LEGACY_CONTENT = {
    "structural_nodes": [
        {"node_id": "n1", "technique_id": "T1059", "technique_name": "Scripting", "tactic": "execution"},
        {"node_id": "n2", "technique_id": "T1105", "technique_name": "Transfer", "tactic": "command-and-control"},
        {"node_id": "n3", "technique_id": "T1021", "technique_name": "Remote", "tactic": "lateral-movement"},
    ],
    "edges": [
        {"from": "n1", "to": "n2"},
        {"from": "n2", "to": "n3"},
        {"from": "n3", "to": "n1"},  # return edge -> cycle
    ],
}


def _seed_flows(factory: sessionmaker[Session]) -> dict[str, int]:
    now = dt.datetime(2025, 1, 1)
    with factory() as session:
        session.add_all(
            [
                Case(id="camp-a", name="Camp A"),
                Case(id="camp-b", name="Camp B"),
            ]
        )
        session.flush()
        corpus = AttackFlowRow(
            campaign_id=None,
            origin=FlowOrigin.MITRE_CORPUS,
            identifier="corpus-1",
            label="Corpus Flow",
            source_key="corpus-1.afb",
            scope="incident",
            content=_LEGACY_CONTENT,
            created_at=now,
        )
        session.add(corpus)
        session.flush()
        own = AttackFlowRow(
            campaign_id="camp-a",
            origin=FlowOrigin.AI_GENERATED,
            identifier="ai-1",
            label="AI Flow",
            content=_LEGACY_CONTENT,
            provenance={"kind": "adapted", "references": [corpus.id], "similarity": 0.5},
            created_at=now,
        )
        other = AttackFlowRow(
            campaign_id="camp-b",
            origin=FlowOrigin.HUMAN_CURATION,
            identifier="cur-1",
            label="Other Flow",
            content={"structural_nodes": [], "edges": []},
            created_at=now,
        )
        session.add_all([own, other])
        session.commit()
        return {"corpus": corpus.id, "own": own.id, "other": other.id}


def test_list_flows_returns_only_campaign_flows_with_provenance(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    ids = _seed_flows(session_factory)
    resp = client.get("/api/campaigns/camp-a/flows")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["campaign_id"] == "camp-a"
    assert [f["id"] for f in body["flows"]] == [ids["own"]]
    prov = body["flows"][0]["provenance"]
    assert prov["kind"] == "adapted"
    assert prov["references"] == [ids["corpus"]]
    assert prov["reference_flows"][0]["source_key"] == "corpus-1.afb"

    empty = client.get("/api/campaigns/unknown/flows")
    assert empty.status_code == 200
    assert empty.json()["flows"] == []


def test_get_flow_detail_has_graph_mermaid_cycles_and_back_edges(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    ids = _seed_flows(session_factory)
    resp = client.get(f"/api/campaigns/camp-a/flows/{ids['own']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [n["technique_id"] for n in body["nodes"]] == ["T1059", "T1105", "T1021"]
    assert len(body["edges"]) == 3
    assert body["has_cycles"] is True
    assert len(body["cycles"]) == 1 and len(body["cycles"][0]) == 3
    assert len(body["back_edges"]) == 1
    assert sum(1 for e in body["edges"] if e["is_back_edge"]) == 1
    # Back edges are drawn dashed in the Mermaid diagram.
    assert body["mermaid"].startswith("flowchart TD")
    assert body["mermaid"].count("-.->") == 1
    assert body["provenance"]["kind"] == "adapted"


def test_get_flow_of_another_campaign_or_missing_is_404(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    ids = _seed_flows(session_factory)
    # Flow exists but belongs to camp-b.
    wrong = client.get(f"/api/campaigns/camp-a/flows/{ids['other']}")
    assert wrong.status_code == 404
    assert wrong.json()["detail"]["reason"] == "flow_not_found"
    assert client.get("/api/campaigns/camp-a/flows/99999").status_code == 404


def test_get_attack_flow_by_id_serves_corpus_flow_and_404(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    ids = _seed_flows(session_factory)
    resp = client.get(f"/api/attack-flows/{ids['corpus']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == ids["corpus"]
    assert body["campaign_id"] is None
    assert body["origin"] == "mitre_corpus"
    assert body["scope"] == "incident"
    assert body["has_cycles"] is True

    assert client.get("/api/attack-flows/99999").status_code == 404
