"""Deterministic tests of the curated-cases seed (task 24.3).

Temporary SQLite database, no network, no Hypothesis. Covers:

* seed counts after applying it to an empty database (8/108/8/8);
* idempotency (two applications never duplicate; the second inserts nothing);
* the light manifest agrees with the JSON files;
* equivalence with ``build_case_from_db`` (``shadowray`` and ``outer_space``);
* AI-generated / edited data is never overwritten;
* the orchestrator runs ``curated_seed`` before ``atomic`` and a second
  ``run_all`` does not read the JSON files again;
* the ``python -m app.seeds.curated`` CLI (and it never prints the database URL);
* importing ``app.main`` does not read the seed JSON files (loader not imported).

**Validates: Requirements 7.1, 7.3, 7.5**
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import (
    Ability,
    Adversary,
    AttackFlowRow,
    Case,
    StixCampaign,
    StixPackage,
)
from app.models.enums import FlowOrigin, IngestSource, IngestStatus, TranslationSource
from app.seeds import loader, manifest
from app.seeds.apply import apply_curated_seed
from app.services.curated_case_reader import build_case_from_db
from app.services.ingestion.orchestrator import IngestionOrchestrator
from app.services.ingestion.runners import (
    AtomicSourceRunner,
    AttackFlowSourceRunner,
    CuratedSeedSourceRunner,
    DockerCatalogSourceRunner,
    STIX_VERSIONS_ENV,
    StixPackagesSourceRunner,
)
from app.services.stix.stix_service import KNOWN_VERSIONS, StixService
from _stix_fakes import FakeStixHttp, small_bundle
from tests.test_ingestion_orchestrator import (
    FakeAtomicFetcher,
    FakeCorpusFetcher,
    FakeDockerFetcher,
    FakeEmbedder,
    _corpus,
    _entries,
    _write_atomics,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
LOADER_MODULE = "app.seeds.loader"


class SeedDb:
    """Temporary SQLite database with the full schema."""

    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        self.url = f"sqlite:///{(tmp_dir / 'seed.db').as_posix()}"
        self.engine = create_db_engine(Settings(database_url=self.url))
        import_models()
        Base.metadata.create_all(bind=self.engine)
        self.factory: sessionmaker[Session] = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, future=True
        )

    def counts(self) -> dict[str, int]:
        with self.factory() as session:

            def _count(model: Any) -> int:
                return int(session.execute(select(func.count()).select_from(model)).scalar_one())

            return {
                "campaigns": _count(Case),
                "abilities": _count(Ability),
                "adversaries": _count(Adversary),
                "attack_flows": _count(AttackFlowRow),
            }


@pytest.fixture()
def db() -> Iterator[SeedDb]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="curated_seed_"))
    database = SeedDb(tmp_dir)
    try:
        yield database
    finally:
        database.engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _cases() -> list[dict[str, Any]]:
    return loader.load_all()


def _apply(db: SeedDb) -> Any:
    with db.factory() as session:
        summary = apply_curated_seed(session)
        session.commit()
    return summary


EXPECTED = {"campaigns": 8, "abilities": 108, "adversaries": 8, "attack_flows": 8}


# (a) counts ----------------------------------------------------------------


def test_apply_on_empty_database_yields_expected_counts(db: SeedDb) -> None:
    assert db.counts() == {"campaigns": 0, "abilities": 0, "adversaries": 0, "attack_flows": 0}
    summary = _apply(db)
    assert db.counts() == EXPECTED
    assert summary.inserted == sum(EXPECTED.values())
    with db.factory() as session:
        assert session.execute(
            select(func.count()).select_from(Case).where(Case.curated.is_(True))
        ).scalar_one() == 8
        origins = set(session.execute(select(AttackFlowRow.origin)).scalars().all())
    assert origins == {FlowOrigin.HUMAN_CURATION}


# (b) idempotency -----------------------------------------------------------


def test_second_application_inserts_nothing_and_never_duplicates(db: SeedDb) -> None:
    _apply(db)
    first = db.counts()
    second = _apply(db)
    assert db.counts() == first == EXPECTED
    assert second.inserted == 0
    assert second.existing == sum(EXPECTED.values())
    assert all(kind["inserted"] == 0 and kind["updated"] == 0 for kind in second.as_dict().values())


# (c) manifest consistency --------------------------------------------------


def test_manifest_matches_the_seed_files() -> None:
    cases = _cases()
    assert tuple(sorted(c["campaign"]["id"] for c in cases)) == tuple(sorted(manifest.CAMPAIGN_SLUGS))
    assert manifest.EXPECTED_COUNTS == {
        "campaigns": len(cases),
        "abilities": sum(len(c["abilities"]) for c in cases),
        "adversaries": len(cases),
        "attack_flows": len(cases),
    }
    assert manifest.EXPECTED_COUNTS == EXPECTED


# (d) equivalence with build_case_from_db -----------------------------------


def _seed_rows(slug: str) -> tuple[list[dict], dict, dict]:
    case = loader.load_case(slug)
    return case["abilities"], case["adversary"], case["attack_flow"]


@pytest.mark.parametrize("slug", ["shadowray", "outer_space"])
def test_build_case_from_db_matches_the_seed_literals(db: SeedDb, slug: str) -> None:
    _apply(db)
    seed_abilities, seed_adversary, seed_flow = _seed_rows(slug)
    with db.factory() as session:
        case = build_case_from_db(session, slug)

    assert case.id == slug
    assert case.name == loader.load_case(slug)["campaign"]["name"]

    by_id = {a.ability_id: a for a in case.abilities}
    assert set(by_id) == {a["ability_id"] for a in seed_abilities}
    assert len(case.abilities) == len(seed_abilities)
    for literal in seed_abilities:
        ability = by_id[literal["ability_id"]]
        for field in ("name", "tactic", "technique_name", "technique_id", "description"):
            assert getattr(ability, field) == literal.get(field)
        assert [e.model_dump() for e in ability.executors] == [
            {"name": e.get("name"), "platform": e.get("platform"), "command": e.get("command")}
            for e in (literal.get("executors") or [])
        ]

    assert case.adversary is not None
    assert case.adversary.id == seed_adversary["id"]
    assert case.adversary.name == seed_adversary["name"]
    assert case.adversary.description == seed_adversary["description"]
    assert case.adversary.atomic_ordering == list(seed_adversary["atomic_ordering"])

    assert case.flow is not None
    content = seed_flow["content"]
    assert case.flow.campaign_name == content["campaign_name"]
    assert [n.node_id for n in case.flow.structural_nodes] == [
        n["node_id"] for n in content["structural_nodes"]
    ]


@pytest.mark.parametrize("slug", ["shadowray", "outer_space"])
def test_flow_service_reads_the_lean_seed_flow(db: SeedDb, slug: str) -> None:
    from app.services.flow.flow_service import FlowService

    _apply(db)
    content = loader.load_case(slug)["attack_flow"]["content"]
    with db.factory() as session:
        service = FlowService(session)
        summaries = service.list_flows(slug)
        assert len(summaries) == 1
        detail = service.get_flow(slug, summaries[0].id)
    assert detail is not None
    assert len(detail.nodes) == len(content["structural_nodes"])
    assert {n.technique_id for n in detail.nodes} == {
        n["technique_id"] for n in content["structural_nodes"]
    }
    # The edges of the lean format ({from, to}) drive the graph.
    assert content["edges"]
    assert len(detail.edges) == len(content["edges"])
    assert detail.mermaid.startswith("flowchart TD")


def test_outer_space_ordering_runs_end_of_outer_space_with_no_orphans(db: SeedDb) -> None:
    _apply(db)
    _, adversary, _ = _seed_rows("outer_space")
    with db.factory() as session:
        case = build_case_from_db(session, "outer_space")
    known = {a.ability_id for a in case.abilities}
    assert "a8de43be-fcde-5302-9cfd-rothos" in case.atomic_ordering
    assert "a8de43be-fcde-5302-9cfd-rothos" in known
    assert [ref for ref in case.atomic_ordering if ref not in known] == []
    assert case.atomic_ordering == list(adversary["atomic_ordering"])


def test_cross_campaign_ability_reference_is_resolved_globally(db: SeedDb) -> None:
    _apply(db)
    shadow_id = "5c0eccee-eee4-5d88-b6e5-c83b25d44eaa"
    ordering = loader.load_case("costaricto")["adversary"]["atomic_ordering"]
    assert shadow_id in ordering
    with db.factory() as session:
        row = session.execute(select(Ability).where(Ability.ability_id == shadow_id)).scalar_one()
        assert row.campaign_id == "shadowray"
        adversary = session.execute(
            select(Adversary).where(Adversary.campaign_id == "costaricto")
        ).scalar_one()
        assert shadow_id in adversary.atomic_ordering

# (e) never overwrite AI / edited data --------------------------------------


def test_ai_and_edited_rows_are_left_intact(db: SeedDb) -> None:
    ability_literal = _cases()[0]["abilities"][0]
    slug = ability_literal["campaign_id"]
    edited_name = "Edited by the AI"
    with db.factory() as session:
        session.add(Case(id=slug, name="Existing", curated=True))
        session.flush()
        session.add(
            Ability(
                ability_id=ability_literal["ability_id"],
                campaign_id=slug,
                name=edited_name,
                translation_source=TranslationSource.AI_GENERATED,
            )
        )
        session.add(
            AttackFlowRow(
                campaign_id=slug,
                origin=FlowOrigin.AI_GENERATED,
                identifier="ai-run-1",
                label="AI flow",
                content={"campaign_name": "ai"},
                created_at=dt.datetime(2026, 1, 1),
            )
        )
        session.commit()

    _apply(db)

    with db.factory() as session:
        ability = session.execute(
            select(Ability).where(Ability.ability_id == ability_literal["ability_id"])
        ).scalar_one()
        assert ability.name == edited_name
        assert ability.translation_source == TranslationSource.AI_GENERATED
        ai_flow = session.execute(
            select(AttackFlowRow).where(AttackFlowRow.origin == FlowOrigin.AI_GENERATED)
        ).scalar_one()
        assert ai_flow.identifier == "ai-run-1"
        assert ai_flow.content == {"campaign_name": "ai"}
        # The seed's own flow for that campaign is added next to the AI one.
        curated_flows = session.execute(
            select(AttackFlowRow).where(
                AttackFlowRow.campaign_id == slug,
                AttackFlowRow.origin == FlowOrigin.HUMAN_CURATION,
            )
        ).scalars().all()
        assert len(curated_flows) == 1
    counts = db.counts()
    assert counts["abilities"] == 108
    assert counts["attack_flows"] == 9


# (f) orchestrator -----------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []


class _RecordingCuratedRunner(CuratedSeedSourceRunner):
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder

    def run(self, session: Session, plan: Any, progress: Any) -> Optional[Any]:
        self._recorder.events.append("curated_seed")
        return super().run(session, plan, progress)


class _RecordingAtomicFetcher(FakeAtomicFetcher):
    def __init__(self, atomics_dir: Path, recorder: _Recorder) -> None:
        super().__init__(atomics_dir)
        self._recorder = recorder

    def fetch_atomics_dir(self, *, ref: str, dest: Path) -> Path:
        self._recorder.events.append("atomic")
        return super().fetch_atomics_dir(ref=ref, dest=dest)


def _orchestrator(db: SeedDb, recorder: _Recorder) -> IngestionOrchestrator:
    embedder = FakeEmbedder()
    return IngestionOrchestrator(
        db.factory,
        {
            IngestSource.CURATED_SEED: _RecordingCuratedRunner(recorder),
            IngestSource.ATOMIC: AtomicSourceRunner(
                embedder=embedder, fetcher=_RecordingAtomicFetcher(_write_atomics(db.tmp_dir), recorder)
            ),
            IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(
                embedder=embedder, fetcher=FakeDockerFetcher(_entries())
            ),
            IngestSource.ATTACK_FLOW: AttackFlowSourceRunner(
                embedder=embedder, fetcher=FakeCorpusFetcher(_corpus())
            ),
        },
    )


def test_fresh_install_runs_curated_seed_before_atomic_and_ends_ready(db: SeedDb) -> None:
    recorder = _Recorder()
    orchestrator = _orchestrator(db, recorder)

    orchestrator.run_all()

    assert recorder.events[:2] == ["curated_seed", "atomic"]
    states = {s.source: s for s in orchestrator.status()}
    assert states[IngestSource.CURATED_SEED].status == IngestStatus.READY
    assert all(s.status == IngestStatus.READY for s in states.values())
    with db.factory() as session:
        slugs = set(session.execute(select(Case.id).where(Case.curated.is_(True))).scalars().all())
        curated_flows = session.execute(
            select(func.count())
            .select_from(AttackFlowRow)
            .where(AttackFlowRow.origin == FlowOrigin.HUMAN_CURATION)
        ).scalar_one()
    assert slugs == set(manifest.CAMPAIGN_SLUGS)
    assert curated_flows == 8
    # Other sources may add their own rows (e.g. the MITRE corpus pseudo-campaign),
    # so only the curated campaigns are counted here.
    counts = db.counts()
    assert (counts["abilities"], counts["adversaries"]) == (108, 8)


def test_second_run_all_does_not_read_the_seed_files_again(
    db: SeedDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder()
    orchestrator = _orchestrator(db, recorder)
    orchestrator.run_all()
    assert recorder.events.count("curated_seed") == 1
    before = db.counts()

    # Count any further application or file read.
    import app.seeds.apply as apply_module

    applications: list[int] = []
    original = apply_module.apply_curated_seed

    def counting(*args: Any, **kwargs: Any) -> Any:
        applications.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(apply_module, "apply_curated_seed", counting)
    reads: list[str] = []
    original_load_case = loader.load_case

    def counting_load_case(slug: str) -> Any:
        reads.append(slug)
        return original_load_case(slug)

    monkeypatch.setattr(loader, "load_case", counting_load_case)

    orchestrator.run_all()
    orchestrator.verify_all()

    assert applications == []
    assert recorder.events.count("curated_seed") == 1  # empty plan: run() not invoked
    assert reads == []
    assert db.counts() == before


# (g) CLI -------------------------------------------------------------------


def test_cli_applies_the_seed_without_printing_the_database_url(db: SeedDb) -> None:
    env = {**os.environ, "PIPELINE_UI_DATABASE_URL": db.url}

    def _run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "app.seeds.curated"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    first = _run()
    assert first.returncode == 0, first.stderr
    summary = json.loads(first.stdout)
    assert summary["campaigns"]["inserted"] == 8
    assert summary["abilities"]["inserted"] == 108
    assert summary["adversaries"]["inserted"] == 8
    assert summary["attack_flows"]["inserted"] == 8
    assert db.counts() == EXPECTED

    second = _run()
    assert second.returncode == 0, second.stderr
    assert all(kind["inserted"] == 0 for kind in json.loads(second.stdout).values())
    assert db.counts() == EXPECTED

    for output in (first.stdout, first.stderr, second.stdout, second.stderr):
        assert db.url not in output
        assert db.tmp_dir.as_posix() not in output.replace("\\", "/")


# Weight and guards ------------------------------------------------------------


def test_importing_app_main_does_not_read_the_seed_files() -> None:
    env = {**os.environ, "PIPELINE_UI_DATABASE_URL": "sqlite://"}
    code = (
        "import sys, app.main; "
        f"print({LOADER_MODULE!r} in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "False"


# (h) simulated fresh install, end to end (task 33.3) ---------------------------

NEWEST = "99.0"  # fake "newest upstream" version


def _curated_stix_bundle() -> dict[str, Any]:
    """A small bundle holding the 8 curated STIX campaigns with their real external ids."""
    bundle = small_bundle()
    for index, (slug, key) in enumerate(sorted(manifest.STIX_CAMPAIGN_KEYS.items()), start=100):
        bundle["objects"].append(
            {
                "type": "campaign",
                "id": f"campaign--00000000-0000-0000-0000-{index:012d}",
                "name": f"Campaign for {slug}",
                "external_references": [{"source_name": "mitre-attack", "external_id": key}],
            }
        )
    return bundle


class _FreshInstall:
    """Orchestrator with every source on fakes, over the temporary SQLite."""

    def __init__(self, db: SeedDb) -> None:
        self.db = db
        self.http = FakeStixHttp(
            {manifest.CURATED_STIX_VERSION: _curated_stix_bundle(), NEWEST: _curated_stix_bundle()}
        )
        self.latest_calls = 0
        embedder = FakeEmbedder()
        self.atomic = FakeAtomicFetcher(_write_atomics(db.tmp_dir))
        self.docker = FakeDockerFetcher(_entries())
        self.corpus = FakeCorpusFetcher(_corpus())
        self.orchestrator = IngestionOrchestrator(
            db.factory,
            {
                IngestSource.STIX_PACKAGES: StixPackagesSourceRunner(
                    stix_service=StixService(http_client=self.http),
                    latest_fetcher=self._latest,
                    index_vectors=False,
                ),
                IngestSource.CURATED_SEED: CuratedSeedSourceRunner(),
                IngestSource.ATOMIC: AtomicSourceRunner(embedder=embedder, fetcher=self.atomic),
                IngestSource.DOCKER_CATALOG: DockerCatalogSourceRunner(
                    embedder=embedder, fetcher=self.docker
                ),
                IngestSource.ATTACK_FLOW: AttackFlowSourceRunner(
                    embedder=embedder, fetcher=self.corpus
                ),
            },
        )

    def _latest(self) -> str:
        self.latest_calls += 1
        return NEWEST

    def states(self) -> dict[IngestSource, Any]:
        return {s.source: s for s in self.orchestrator.status()}

    def network_calls(self) -> tuple[int, ...]:
        return (
            len(self.http.calls),
            self.latest_calls,
            self.atomic.calls,
            self.docker.calls,
            self.corpus.list_calls,
        )

    def curated_stix_rows(self, version: str) -> dict[str, str]:
        """``external_id -> curated_slug`` of the curated ``stix_campaigns`` of ``version``."""
        with self.db.factory() as session:
            rows = session.execute(
                select(StixCampaign.external_id, StixCampaign.curated_slug)
                .join(StixPackage, StixCampaign.package_id == StixPackage.id)
                .where(StixPackage.version == version, StixCampaign.curated.is_(True))
            ).all()
        return {external_id: slug for external_id, slug in rows}

    def persisted_versions(self) -> set[str]:
        with self.db.factory() as session:
            return set(session.execute(select(StixPackage.version)).scalars())


@pytest.fixture()
def fresh(db: SeedDb, monkeypatch: pytest.MonkeyPatch) -> _FreshInstall:
    monkeypatch.delenv(STIX_VERSIONS_ENV, raising=False)
    return _FreshInstall(db)


def _assert_curated_stix_marking(fresh: _FreshInstall, version: str) -> None:
    by_key = fresh.curated_stix_rows(version)
    assert by_key == {key: slug for slug, key in manifest.STIX_CAMPAIGN_KEYS.items()}


def test_fresh_install_ends_ready_with_curated_stix_campaigns_in_both_versions(
    fresh: _FreshInstall, db: SeedDb
) -> None:
    fresh.orchestrator.run_all()

    assert all(s.status == IngestStatus.READY for s in fresh.states().values())
    assert fresh.persisted_versions() == {manifest.CURATED_STIX_VERSION, NEWEST}
    _assert_curated_stix_marking(fresh, manifest.CURATED_STIX_VERSION)
    _assert_curated_stix_marking(fresh, NEWEST)
    with db.factory() as session:
        curated_cases = session.execute(
            select(func.count()).select_from(Case).where(Case.curated.is_(True))
        ).scalar_one()
        curated_flows = session.execute(
            select(func.count())
            .select_from(AttackFlowRow)
            .where(AttackFlowRow.origin == FlowOrigin.HUMAN_CURATION)
        ).scalar_one()
    counts = db.counts()
    assert (curated_cases, counts["abilities"], counts["adversaries"], curated_flows) == (8, 108, 8, 8)


def test_fresh_install_routes_are_coherent_and_second_run_all_is_silent(
    fresh: _FreshInstall, db: SeedDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh.orchestrator.run_all()
    calls = fresh.network_calls()
    counts = db.counts()

    app = create_app()
    app.state.ingestion = fresh.orchestrator

    def _override() -> Iterator[Session]:
        session = db.factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override
    client = TestClient(app)

    bases = {b["version"]: b for b in client.get("/api/stix/bases").json()["bases"]}
    assert bases[manifest.CURATED_STIX_VERSION]["downloaded"] is True
    assert bases[NEWEST]["downloaded"] is True and bases[NEWEST]["selectable"] is True
    assert all("curated" not in base for base in bases.values())
    assert bases[KNOWN_VERSIONS[0]]["downloaded"] is (KNOWN_VERSIONS[0] in {manifest.CURATED_STIX_VERSION, NEWEST})

    for version in (manifest.CURATED_STIX_VERSION, NEWEST):
        response = client.get(f"/api/stix/bases/{version}/campaigns")
        assert response.status_code == 200, response.text
        listed = {c["external_id"]: c for c in response.json()}
        assert all("curated" in c for c in listed.values())
        for slug, key in manifest.STIX_CAMPAIGN_KEYS.items():
            assert listed[key]["curated"] is True
            assert listed[key]["curated_slug"] == slug
            assert listed[key]["campaign_key"] == slug
        assert sum(1 for c in listed.values() if c["curated"]) == 8
        assert listed["C9001"]["curated"] is False

    # Second run_all: no download, no lookup, and the seed JSON files are not reloaded.
    import app.seeds.apply as apply_module

    reads: list[str] = []
    original = loader.load_case
    monkeypatch.setattr(loader, "load_case", lambda slug: reads.append(slug) or original(slug))
    applications: list[int] = []
    original_apply = apply_module.apply_curated_seed
    monkeypatch.setattr(
        apply_module, "apply_curated_seed", lambda *a, **k: applications.append(1) or original_apply(*a, **k)
    )

    fresh.orchestrator.run_all()

    assert fresh.network_calls() == calls
    assert (reads, applications) == ([], [])
    assert db.counts() == counts
    assert all(s.status == IngestStatus.READY for s in fresh.states().values())


def test_newest_version_network_failure_does_not_block_curated_version_or_seed(
    fresh: _FreshInstall, db: SeedDb
) -> None:
    fresh.http.fail_versions.add(NEWEST)

    fresh.orchestrator.run_all()

    states = fresh.states()
    assert states[IngestSource.STIX_PACKAGES].status == IngestStatus.FAILED
    assert states[IngestSource.STIX_PACKAGES].error == "network: StixDownloadError"
    for source in (
        IngestSource.CURATED_SEED,
        IngestSource.ATOMIC,
        IngestSource.DOCKER_CATALOG,
        IngestSource.ATTACK_FLOW,
    ):
        assert states[source].status == IngestStatus.READY
    assert fresh.persisted_versions() == {manifest.CURATED_STIX_VERSION}
    _assert_curated_stix_marking(fresh, manifest.CURATED_STIX_VERSION)
    assert db.counts()["abilities"] == 108

    fresh.http.fail_versions.clear()
    fresh.http.calls.clear()
    fresh.orchestrator.run_all()

    assert all(s.status == IngestStatus.READY for s in fresh.states().values())
    assert fresh.persisted_versions() == {manifest.CURATED_STIX_VERSION, NEWEST}
    assert [c.rsplit("-", 1)[-1] for c in fresh.http.calls] == [f"{NEWEST}.json"]  # only what was missing
    _assert_curated_stix_marking(fresh, NEWEST)


def test_offline_latest_lookup_still_installs_the_seed_required_version(
    db: SeedDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(STIX_VERSIONS_ENV, raising=False)

    def offline() -> str:
        raise ConnectionError("offline")

    http = FakeStixHttp(
        {manifest.CURATED_STIX_VERSION: _curated_stix_bundle(), KNOWN_VERSIONS[0]: small_bundle()}
    )
    orchestrator = IngestionOrchestrator(
        db.factory,
        {
            IngestSource.STIX_PACKAGES: StixPackagesSourceRunner(
                stix_service=StixService(http_client=http), latest_fetcher=offline, index_vectors=False
            ),
            IngestSource.CURATED_SEED: CuratedSeedSourceRunner(),
        },
    )

    orchestrator.run_all()

    assert all(s.status == IngestStatus.READY for s in orchestrator.status())
    with db.factory() as session:
        versions = set(session.execute(select(StixPackage.version)).scalars())
    assert versions == {manifest.CURATED_STIX_VERSION, KNOWN_VERSIONS[0]}


# (i) API guards use the version derived from the seed ---------------------------


def test_campaign_and_ai_guards_use_the_seed_derived_version_and_accept_curated_cases(
    fresh: _FreshInstall, db: SeedDb
) -> None:
    from app.api import ai as ai_api
    from app.api import campaigns as campaigns_api
    from app.services.campaign_materializer import CampaignMaterializer
    from app.services.stix.stix_service import CURATED_VERSION

    assert CURATED_VERSION == manifest.CURATED_STIX_VERSION == "18.0"
    assert campaigns_api.CURATED_VERSION == CURATED_VERSION
    assert "18.0" not in Path(campaigns_api.__file__).read_text(encoding="utf-8")
    assert "18.0" not in Path(ai_api.__file__).read_text(encoding="utf-8")

    fresh.orchestrator.run_all()

    with db.factory() as session:
        for slug in manifest.CAMPAIGN_SLUGS:
            campaigns_api._known_campaign_or_404(slug, session, campaigns_api.CURATED_VERSION)
            # The AI run start performs the same guard, then an idempotent materialization.
            assert CampaignMaterializer(session).materialize(slug, campaigns_api.CURATED_VERSION) == slug
        session.commit()
        with pytest.raises(HTTPException) as unknown:
            campaigns_api._known_campaign_or_404("not_a_campaign", session, campaigns_api.CURATED_VERSION)
        assert unknown.value.status_code == 404
    assert db.counts()["abilities"] == 108
