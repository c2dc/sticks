"""Deterministic tests for the ingestion orchestrator and its REST routes.

Covers Properties 8-12 of the ``agentic-only-flow-redesign`` spec with fake
fetchers and a fake embedder (no network, no Hypothesis, temp-file SQLite):

* Property 8: re-running the whole ingestion does not duplicate any row.
* Property 9: an invalid ``.afb`` never blocks the valid ones and is reported
  in ``skipped`` with a non-empty reason.
* Property 10: a re-run completes only what is missing (rows and embeddings).
* Property 11: source failures are isolated and resumable.
* Property 12: a ``running`` source rejects a concurrent start (``409``).

**Validates: Requirements 4.3, 4.4, 7.3, 7.5, 7.6, 7.7, 7.10**
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from _atomic_docs import documents_from_dir
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.main import create_app
from app.models.domain import (
    AttackFlowEdgeRow,
    AttackFlowObjectRow,
    AttackFlowRow,
    ImageCatalog,
    IngestSourceState,
    TechniqueCommand,
)
from app.models.domain import ExperienceMemory as ExperienceMemoryRow
from app.models.enums import IngestSource, IngestStatus
from app.services.atomic_red_team_ingest_service import AtomicFetchError
from app.services.attack_flow.corpus_fetcher import CorpusFetchError, CorpusFile
from app.services.docker_catalog_ingest_service import (
    DockerCatalogEntry,
    DockerCatalogFetchError,
)
from app.services.ingestion.orchestrator import (
    IngestionOrchestrator,
    StartResult,
)
from app.services.ingestion.runners import (
    AtomicSourceRunner,
    AttackFlowSourceRunner,
    DockerCatalogSourceRunner,
)

ALL_SOURCES = (
    IngestSource.ATOMIC,
    IngestSource.DOCKER_CATALOG,
    IngestSource.ATTACK_FLOW,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Deterministic embedder, never touches the network."""

    def embed(self, text: str) -> list[float]:
        value = float(len(text) % 7)
        return [value, value, value]


class FakeAtomicFetcher:
    """Returns a pre-written ``atomics/`` tree; optionally fails."""

    def __init__(self, atomics_dir: Path) -> None:
        self._dir = atomics_dir
        self.calls = 0
        self.fail = False

    def fetch_documents(self, *, ref: str) -> list:
        self.calls += 1
        if self.fail:
            raise AtomicFetchError("simulated network failure", ref=ref)
        return documents_from_dir(self._dir)


class FakeDockerFetcher:
    """Yields in-memory entries; can fail after ``fail_after`` entries."""

    def __init__(self, entries: list[DockerCatalogEntry]) -> None:
        self._entries = entries
        self.calls = 0
        self.fail_after: int | None = None

    def fetch_entries(self, *, max_images: int) -> Iterable[DockerCatalogEntry]:
        self.calls += 1
        for index, entry in enumerate(self._entries):
            if self.fail_after is not None and index >= self.fail_after:
                raise DockerCatalogFetchError("simulated network failure")
            yield entry


class FakeCorpusFetcher:
    """Serves ``name -> bytes``; the sha is derived from the content."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.list_calls = 0
        self.fail = False

    def list_files(self) -> list[CorpusFile]:
        self.list_calls += 1
        if self.fail:
            raise CorpusFetchError("simulated network failure")
        return [
            CorpusFile(name=name, sha=f"sha-{abs(hash(raw)) % 10**8}-{len(raw)}")
            for name, raw in sorted(self.files.items())
        ]

    def fetch(self, corpus_file: CorpusFile) -> bytes:
        return self.files[corpus_file.name]


# ---------------------------------------------------------------------------
# Fixtures data
# ---------------------------------------------------------------------------


def _afb(name: str, technique: str) -> bytes:
    document = {
        "schema": "attack_flow_v2",
        "objects": [
            {
                "id": "flow",
                "instance": f"flow-{name}",
                "properties": [["name", f"Flow {name}"], ["description", "demo"]],
            },
            {
                "id": "action",
                "instance": f"action-{name}",
                "properties": [["name", f"Action {name}"], ["technique_id", technique]],
            },
        ],
    }
    return json.dumps(document).encode("utf-8")


def _corpus() -> dict[str, bytes]:
    return {
        "a.afb": _afb("a", "T1059"),
        "b.afb": _afb("b", "T1003"),
        "bad.afb": json.dumps({"schema": "other"}).encode("utf-8"),
        "c.afb": _afb("c", "T1082"),
    }


VALID_FLOWS = {"a.afb", "b.afb", "c.afb"}


def _entries() -> list[DockerCatalogEntry]:
    return [
        DockerCatalogEntry(
            image=f"library/img{i}",
            target_os="linux",
            description=f"Image number {i}.",
            services=("svc",),
        )
        for i in range(3)
    ]


def _write_atomics(root: Path) -> Path:
    atomics = root / "atomics"
    for technique, command in (("T1033", "whoami"), ("T1082", "uname -a")):
        folder = atomics / technique
        folder.mkdir(parents=True, exist_ok=True)
        document = {
            "attack_technique": technique,
            "atomic_tests": [
                {
                    "name": f"{technique} test",
                    "supported_platforms": ["linux"],
                    "executor": {"name": "sh", "command": command},
                }
            ],
        }
        (folder / f"{technique}.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return atomics


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        db_path = tmp_dir / "ingestion.db"
        self.engine = create_db_engine(Settings(database_url=f"sqlite:///{db_path.as_posix()}"))
        import_models()
        Base.metadata.create_all(bind=self.engine)
        self.factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )
        self.atomic = FakeAtomicFetcher(_write_atomics(tmp_dir))
        self.docker = FakeDockerFetcher(_entries())
        self.corpus = FakeCorpusFetcher(_corpus())
        embedder = FakeEmbedder()
        self.orchestrator = IngestionOrchestrator(
            self.factory,
            {
                IngestSource.ATOMIC: AtomicSourceRunner(embedder=embedder, fetcher=self.atomic),
                IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(
                    embedder=embedder, fetcher=self.docker
                ),
                IngestSource.ATTACK_FLOW: AttackFlowSourceRunner(
                    embedder=embedder, fetcher=self.corpus
                ),
            },
        )

    def count(self, model: Any) -> int:
        with self.factory() as session:
            return int(session.execute(select(func.count()).select_from(model)).scalar_one())

    def snapshot(self) -> dict[str, int]:
        return {
            "attack_flows": self.count(AttackFlowRow),
            "objects": self.count(AttackFlowObjectRow),
            "edges": self.count(AttackFlowEdgeRow),
            "images": self.count(ImageCatalog),
            "commands": self.count(TechniqueCommand),
            "memory": self.count(ExperienceMemoryRow),
        }

    def states(self) -> dict[IngestSource, Any]:
        return {s.source: s for s in self.orchestrator.status()}

    def set_state(self, source: IngestSource, **values: Any) -> None:
        self.orchestrator.status()  # makes sure the rows exist
        with self.factory() as session:
            session.execute(
                update(IngestSourceState).where(IngestSourceState.source == source).values(**values)
            )
            session.commit()

    def dedup_keys(self) -> list[str]:
        with self.factory() as session:
            rows = session.execute(select(ExperienceMemoryRow.meta)).scalars().all()
        return [m.get("dedup_key") for m in rows if m.get("dedup_key")]


@pytest.fixture()
def env() -> Iterator[Env]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="ingestion_orchestrator_"))
    environment = Env(tmp_dir)
    try:
        yield environment
    finally:
        environment.engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _client(env: Env) -> TestClient:
    app = create_app()
    app.state.ingestion = env.orchestrator
    return TestClient(app)


# ---------------------------------------------------------------------------
# Property 8: idempotent corpus ingestion
# ---------------------------------------------------------------------------


def test_rerun_does_not_duplicate_and_skips_the_network(env: Env) -> None:
    env.orchestrator.run_all()
    first = env.snapshot()
    assert first["attack_flows"] == len(VALID_FLOWS)
    assert first["images"] == 3
    assert first["commands"] == 2
    assert all(s.status == IngestStatus.READY for s in env.states().values())
    calls = (env.atomic.calls, env.docker.calls, env.corpus.list_calls)

    for _ in range(2):
        env.orchestrator.run_all()

    assert env.snapshot() == first
    keys = env.dedup_keys()
    assert len(keys) == len(set(keys))
    # Empty plan means ready without any network access (Req. 7.3).
    assert (env.atomic.calls, env.docker.calls, env.corpus.list_calls) == calls
    assert all(s.status == IngestStatus.READY for s in env.states().values())


# ---------------------------------------------------------------------------
# Property 9: an invalid flow never blocks the valid ones
# ---------------------------------------------------------------------------


def test_invalid_flow_is_skipped_and_valid_ones_are_persisted(env: Env) -> None:
    env.orchestrator.run_source(IngestSource.ATTACK_FLOW)

    with env.factory() as session:
        keys = set(session.execute(select(AttackFlowRow.source_key)).scalars().all())
        state = session.get(IngestSourceState, IngestSource.ATTACK_FLOW)
        skipped = list(state.skipped)
    assert keys == VALID_FLOWS
    assert [item["item"] for item in skipped] == ["bad.afb"]
    assert all(item["reason"] for item in skipped)
    assert state.status == IngestStatus.READY

    # The skipped file does not trigger endless refetches.
    before = env.corpus.list_calls
    env.orchestrator.run_all()
    assert env.corpus.list_calls == before


# ---------------------------------------------------------------------------
# Property 10: reconciliation completes only what is missing
# ---------------------------------------------------------------------------


def test_missing_embeddings_are_completed_without_network(env: Env) -> None:
    env.orchestrator.run_all()
    before = env.snapshot()
    calls = (env.atomic.calls, env.docker.calls, env.corpus.list_calls)
    with env.factory() as session:
        memory_ids = sorted(session.execute(select(ExperienceMemoryRow.id)).scalars().all())
        flow_ids = sorted(session.execute(select(AttackFlowRow.id)).scalars().all())
        dropped = memory_ids[::2]
        session.execute(delete(ExperienceMemoryRow).where(ExperienceMemoryRow.id.in_(dropped)))
        session.commit()
    assert env.count(ExperienceMemoryRow) < before["memory"]

    env.orchestrator.verify_all()
    for source in ALL_SOURCES:
        env.orchestrator.run_source(source)

    after = env.snapshot()
    assert after == before
    assert (env.atomic.calls, env.docker.calls, env.corpus.list_calls) == calls
    keys = env.dedup_keys()
    assert len(keys) == len(set(keys))
    with env.factory() as session:
        kept = set(memory_ids) - set(dropped)
        now_ids = set(session.execute(select(ExperienceMemoryRow.id)).scalars().all())
        assert kept <= now_ids  # previously present rows are untouched
        assert sorted(session.execute(select(AttackFlowRow.id)).scalars().all()) == flow_ids


def test_missing_flow_row_is_refetched_and_the_rest_is_unchanged(env: Env) -> None:
    env.orchestrator.run_all()
    before = env.snapshot()
    with env.factory() as session:
        victim = session.execute(
            select(AttackFlowRow).where(AttackFlowRow.source_key == "b.afb")
        ).scalar_one()
        others = {
            r.source_key: r.id
            for r in session.execute(
                select(AttackFlowRow).where(AttackFlowRow.source_key != "b.afb")
            ).scalars()
        }
        session.execute(delete(AttackFlowEdgeRow).where(AttackFlowEdgeRow.flow_id == victim.id))
        session.execute(delete(AttackFlowObjectRow).where(AttackFlowObjectRow.flow_id == victim.id))
        session.execute(delete(AttackFlowRow).where(AttackFlowRow.id == victim.id))
        session.commit()
    assert env.count(AttackFlowRow) == len(VALID_FLOWS) - 1

    env.orchestrator.verify_all()
    env.orchestrator.run_source(IngestSource.ATTACK_FLOW)

    assert env.snapshot() == before
    with env.factory() as session:
        for key, flow_id in others.items():
            stored = session.execute(
                select(AttackFlowRow.id).where(AttackFlowRow.source_key == key)
            ).scalar_one()
            assert stored == flow_id
    keys = env.dedup_keys()
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Property 11: failures are isolated and resumable
# ---------------------------------------------------------------------------


def _break(env: Env, source: IngestSource, broken: bool) -> None:
    if source is IngestSource.ATOMIC:
        env.atomic.fail = broken
    elif source is IngestSource.DOCKER_CATALOG:
        env.docker.fail_after = 1 if broken else None
    else:
        env.corpus.fail = broken


@pytest.mark.parametrize(
    "failing",
    [
        (IngestSource.ATOMIC,),
        (IngestSource.DOCKER_CATALOG,),
        (IngestSource.ATTACK_FLOW,),
        (IngestSource.ATOMIC, IngestSource.ATTACK_FLOW),
        ALL_SOURCES,
    ],
)
def test_network_failure_is_isolated_and_resumable(
    env: Env, failing: tuple[IngestSource, ...]
) -> None:
    for source in failing:
        _break(env, source, True)

    env.orchestrator.run_all()

    for source, state in env.states().items():
        if source in failing:
            assert state.status == IngestStatus.FAILED
            assert state.error and state.error.startswith("network:")
        else:
            assert state.status == IngestStatus.READY
            assert state.error is None

    for source in failing:
        _break(env, source, False)
    env.orchestrator.run_all()

    assert all(s.status == IngestStatus.READY for s in env.states().values())
    assert all(s.error is None for s in env.states().values())
    snapshot = env.snapshot()
    assert snapshot["attack_flows"] == len(VALID_FLOWS)
    assert snapshot["images"] == 3
    assert snapshot["commands"] == 2
    keys = env.dedup_keys()
    assert len(keys) == len(set(keys))

    env.orchestrator.run_all()
    assert env.snapshot() == snapshot


def test_partial_docker_download_keeps_progress_and_resumes(env: Env) -> None:
    env.docker.fail_after = 1
    env.orchestrator.run_source(IngestSource.DOCKER_CATALOG)
    state = env.states()[IngestSource.DOCKER_CATALOG]
    assert state.status == IngestStatus.FAILED
    assert state.loaded_count == 1

    env.docker.fail_after = None
    env.orchestrator.run_source(IngestSource.DOCKER_CATALOG)
    state = env.states()[IngestSource.DOCKER_CATALOG]
    assert state.status == IngestStatus.READY
    assert env.count(ImageCatalog) == 3


# ---------------------------------------------------------------------------
# Property 12: a running source rejects a concurrent start
# ---------------------------------------------------------------------------


def _running(env: Env, source: IngestSource, *, age: dt.timedelta) -> None:
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    env.set_state(
        source,
        status=IngestStatus.RUNNING,
        started_at=now - age,
        heartbeat_at=now - age,
        finished_at=None,
        error=None,
    )


def test_running_source_rejects_start_and_keeps_state(env: Env) -> None:
    _running(env, IngestSource.ATTACK_FLOW, age=dt.timedelta(seconds=5))
    before = env.states()[IngestSource.ATTACK_FLOW]

    assert env.orchestrator.start(IngestSource.ATTACK_FLOW) is StartResult.ALREADY_RUNNING
    assert env.orchestrator.run_source(IngestSource.ATTACK_FLOW) is StartResult.ALREADY_RUNNING

    assert env.states()[IngestSource.ATTACK_FLOW] == before
    assert env.corpus.list_calls == 0
    assert env.count(AttackFlowRow) == 0

    # Another source is still accepted.
    assert env.orchestrator.start(IngestSource.DOCKER_CATALOG) is StartResult.STARTED
    env.orchestrator.join(30)
    assert env.states()[IngestSource.DOCKER_CATALOG].status == IngestStatus.READY


def test_stale_running_source_can_be_claimed_again(env: Env) -> None:
    _running(env, IngestSource.DOCKER_CATALOG, age=dt.timedelta(hours=2))
    assert env.orchestrator.run_source(IngestSource.DOCKER_CATALOG) is StartResult.STARTED
    assert env.states()[IngestSource.DOCKER_CATALOG].status == IngestStatus.READY


def test_start_route_returns_409_for_running_source(env: Env) -> None:
    _running(env, IngestSource.ATOMIC, age=dt.timedelta(seconds=5))
    client = _client(env)

    conflict = client.post("/api/ingestion/atomic/start")
    assert conflict.status_code == 409
    assert env.states()[IngestSource.ATOMIC].status == IngestStatus.RUNNING
    assert env.atomic.calls == 0

    accepted = client.post("/api/ingestion/docker_catalog/start")
    assert accepted.status_code == 202
    assert accepted.json()["source"] == "docker_catalog"
    env.orchestrator.join(30)

    assert client.post("/api/ingestion/unknown/start").status_code == 404


# ---------------------------------------------------------------------------
# Status and verify routes (Req. 7.8)
# ---------------------------------------------------------------------------


def test_status_route_reports_persisted_state(env: Env) -> None:
    client = _client(env)

    pending = client.get("/api/ingestion/status")
    assert pending.status_code == 200
    assert [item["source"] for item in pending.json()] == [s.value for s in ALL_SOURCES]
    assert {item["status"] for item in pending.json()} == {"pending"}

    env.orchestrator.run_all()
    body = {item["source"]: item for item in client.get("/api/ingestion/status").json()}
    assert {item["status"] for item in body.values()} == {"ready"}
    assert body["docker_catalog"]["loaded_count"] == 3
    assert body["docker_catalog"]["embedded_count"] == 3
    assert body["attack_flow"]["loaded_count"] == len(VALID_FLOWS)
    assert body["attack_flow"]["skipped_count"] == 1
    assert body["attack_flow"]["error"] is None


def test_status_route_reports_failure_cause(env: Env) -> None:
    env.docker.fail_after = 0
    env.orchestrator.run_all()
    body = {item["source"]: item for item in _client(env).get("/api/ingestion/status").json()}
    assert body["docker_catalog"]["status"] == "failed"
    assert body["docker_catalog"]["error"].startswith("network:")
    assert body["atomic"]["status"] == "ready"
    assert body["attack_flow"]["status"] == "ready"


def test_verify_route_does_not_start_when_nothing_is_missing(env: Env) -> None:
    env.orchestrator.run_all()
    calls = (env.atomic.calls, env.docker.calls, env.corpus.list_calls)

    response = _client(env).post("/api/ingestion/verify")

    assert response.status_code == 200
    assert {item["status"] for item in response.json()} == {"ready"}
    env.orchestrator.join(30)
    assert (env.atomic.calls, env.docker.calls, env.corpus.list_calls) == calls


# ---------------------------------------------------------------------------
# Atomic: two-phase persistence through the orchestrator (task 40.3)
# ---------------------------------------------------------------------------


class SwitchableEmbedder(FakeEmbedder):
    """``FakeEmbedder`` that can be switched off like a stopped Ollama."""

    def __init__(self) -> None:
        self.down = False

    def _check(self) -> None:
        if self.down:
            from app.services.ai.embedder import EMBEDDER_UNAVAILABLE, EmbedderError

            raise EmbedderError(EMBEDDER_UNAVAILABLE, "embedder is down.")

    def embed(self, text: str) -> list[float]:
        self._check()
        return super().embed(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self._check()
        return [super(SwitchableEmbedder, self).embed(t) for t in texts]


def test_atomic_embedder_failure_keeps_rows_and_resumes_without_network(env: Env) -> None:
    embedder = SwitchableEmbedder()
    embedder.down = True
    orchestrator = IngestionOrchestrator(
        env.factory,
        {IngestSource.ATOMIC: AtomicSourceRunner(embedder=embedder, fetcher=env.atomic)},
    )

    orchestrator.run_source(IngestSource.ATOMIC)
    failed = next(s for s in orchestrator.status() if s.source == IngestSource.ATOMIC)
    assert failed.status == IngestStatus.FAILED
    assert failed.error == "embedder_unavailable"
    assert (failed.loaded_count, failed.embedded_count, failed.expected_count) == (2, 0, 2)
    assert env.count(TechniqueCommand) == 2

    embedder.down = False
    orchestrator.run_source(IngestSource.ATOMIC)
    ready = next(s for s in orchestrator.status() if s.source == IngestSource.ATOMIC)
    assert ready.status == IngestStatus.READY
    assert ready.loaded_count == ready.embedded_count == 2
    assert env.atomic.calls == 1  # the resume vectorized without downloading again
    assert env.count(TechniqueCommand) == 2
