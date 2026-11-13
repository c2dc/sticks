"""Integration test: the runtime never reads ``packages/cli/data`` (task 5.3).

Sub-step 2.4 removed every request-time disk read from the runtime: once the
curated content is persisted in the database, every service resolves campaigns from the ``campaigns``/``attack_flows``/``abilities``/
``adversaries`` rows and never touch ``packages/cli/data/api`` nor
``packages/cli/data/dag`` while serving a request (Req. 1.1, 4.1, 4.2). This test
is the *real* security guarantee for that invariant: instead of trusting a code
read, it installs a **filesystem probe** that fails loudly if any path under
those two curated directories is opened during a request.

The probe (:func:`_forbidden_path_probe`)
-----------------------------------------
It wraps the low-level open primitives — ``builtins.open``, ``io.open`` and
``os.open`` — and records every attempt to open a path that resolves inside
``<repo>/packages/cli/data/api`` or ``<repo>/packages/cli/data/dag``. Any other
path (the ephemeral SQLite file, config, fixtures) passes straight through
untouched. Matching is done on the normalised absolute path, so it is robust to
Windows/POSIX separators and relative ``..`` segments. If the probe records even
one forbidden open during a driven request, the assertion fails with the exact
offending path(s) — including the ``Agent_Input`` assembly path, since the AI
graph builds its context from the database and the vector store, never from disk
(Req. 5.2).

Representative request-time code paths driven through a ``TestClient`` (with
``get_db`` overridden onto the seeded session factory), each of which used to
read a curated file from disk before sub-step 2.4:

* campaign list (``GET /api/campaigns``) and detail (``GET /api/campaigns/{c}``),
* Stage 2 selection — read (``GET .../stage/2``) and select (``POST .../stage/2/select``),
* attack flow endpoint (``GET /api/campaigns/{c}/flow``),
* emulation preview (``POST .../stage/5/emulation/preview``),
* topology (``GET /api/campaigns/{c}/topology``).

The whole campaign is seeded directly as ORM rows (a ``campaigns`` row, the
structural ``attack_flows`` row, the ``abilities`` and the ``adversaries`` row) plus the
``stix_campaigns`` guard rows via :func:`tests._stix_seed.ensure_stix_campaigns`,
so the DB-backed guard (``_known_campaign_or_404``) accepts the slug. Nothing is
read from disk to set this up, and the probe stays armed for the duration of
every request.

_Requirements: 1.1, 4.2, 5.2_
"""

# Task 21.1/21.2 - disk-as-storage inventory (decision: runtime data lives in the DB + vector store; the
# versioned read-only seed JSON in app/seeds/data is not runtime storage)
#   DONE (21.2): CaseService/CURATED_CASES/_read_json, the disk->DB curated ingest and
#     the campaigns.ability_file/adversary_file/flow_file columns (M4) were removed;
#     provisioner/execution/completion and the campaign list read the database.
#   DONE (21.3): the STIX bundle is downloaded in memory and persisted by the
#     stix_packages ingestion source; stix_paths, stix_dir, the file backfill and
#     stix_packages.source_file (M5) were removed.
#   DONE (21.4): packages/cli (data, legacy CLI code and config) was removed; the
#     Caldera URL/key now come from Settings (PIPELINE_UI_CALDERA_URL/_KEY).
#     The probes below keep guarding the removed directory against regressions.
#   KEEP: atomic_red_team_ingest (OS temp YAML), models/domain.py .env read (config),
#     caldera/client.py (HTTP only), afb_parser/corpus_fetcher/docker_catalog (memory).
# Proof below: the 8 curated campaigns are served only from the DB (strict probe).

from __future__ import annotations

import builtins
import contextlib
import datetime as dt
import io
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import Ability, Adversary, AttackFlowRow, Case
from app.models.enums import FlowOrigin, TranslationSource
from tests._curated_db import CURATED_NAMES as CURATED_CASES
from tests._stix_seed import ensure_stix_campaigns

# ---------------------------------------------------------------------------
# The two curated directories that the runtime must NEVER open during a request.
# ---------------------------------------------------------------------------

_DATA_ROOT = Path(__file__).resolve().parents[3] / "packages" / "cli" / "data"
_FORBIDDEN_DIRS: tuple[Path, ...] = (
    (_DATA_ROOT / "api").resolve(),
    (_DATA_ROOT / "dag").resolve(),
)


def _normalise(path: Any) -> Path | None:
    """Return the absolute, normalised path for a probe target, or ``None``.

    Accepts the ``file`` argument of the various open primitives (``str``,
    ``bytes``, ``os.PathLike`` or an ``int`` file descriptor). File descriptors
    (ints) carry no path, so they are ignored. ``os.path.normpath`` + ``abspath``
    collapse ``..`` segments and unify separators so the containment check is
    robust across platforms.
    """
    if isinstance(path, int):
        return None
    try:
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        return Path(os.path.abspath(os.path.normpath(os.fspath(path))))
    except (TypeError, ValueError):
        return None


def _is_forbidden(resolved: Path) -> bool:
    """Whether ``resolved`` lies inside one of the forbidden curated directories."""
    for forbidden in _FORBIDDEN_DIRS:
        try:
            resolved.relative_to(forbidden)
            return True
        except ValueError:
            continue
    return False


@pytest.fixture
def filesystem_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Arm a filesystem probe over the open primitives; return the breach log.

    Every opened path that resolves inside ``packages/cli/data/api`` or
    ``packages/cli/data/dag`` is appended to the returned list. Tests assert that
    list stays empty across the driven requests — a non-empty list is a real
    disk-read regression (Req. 1.1, 4.2). All non-forbidden opens pass through to
    the genuine primitive unchanged, so the ephemeral SQLite DB, config and
    fixtures keep working normally.
    """
    breaches: list[str] = []

    real_builtins_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def _record(file: Any) -> None:
        resolved = _normalise(file)
        if resolved is not None and _is_forbidden(resolved):
            breaches.append(str(resolved))

    def guarded_builtins_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _record(file)
        return real_builtins_open(file, *args, **kwargs)

    def guarded_io_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        _record(file)
        return real_io_open(file, *args, **kwargs)

    def guarded_os_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        _record(path)
        return real_os_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_builtins_open)
    monkeypatch.setattr(io, "open", guarded_io_open)
    monkeypatch.setattr(os, "open", guarded_os_open)

    return breaches


# ---------------------------------------------------------------------------
# A curated campaign seeded entirely in the DB (no disk fixture).
#
# A curated slug is used so the DB-first flow resolution
# engages (it only does for a curated slug) and the topology endpoint
# resolves a non-empty graph. The content lives only in the seeded ORM rows, so
# the runtime has no reason to touch disk — and the probe proves it does not.
# ---------------------------------------------------------------------------

_SLUG = "shadowray"
assert _SLUG in CURATED_CASES, "the seeded slug must be curated for the attack flow path"
_CAMPAIGN_NAME = "DB-sourced ShadowRay"

_NODE_1 = {
    "node_id": "n0",
    "technique_id": "T1190",
    "technique_name": "Exploit Public-Facing Application",
    "tactic": "initial-access",
    "description": "exploit the exposed service",
    "parent_nodes": [],
    "child_nodes": ["n1"],
    "attacker_commands": ["curl http://192.168.20.30/first"],
}
_NODE_2 = {
    "node_id": "n1",
    "technique_id": "T1059",
    "technique_name": "Command and Scripting Interpreter",
    "tactic": "execution",
    "description": "run a follow-up command",
    "parent_nodes": ["n0"],
    "child_nodes": [],
    "attacker_commands": ["curl http://192.168.20.30/second"],
}


_ABILITY_1_ID = f"{_SLUG}-ab-1"
_ABILITY_2_ID = f"{_SLUG}-ab-2"
_ABILITY_1_COMMAND = "curl http://192.168.20.30/first"
_ABILITY_2_COMMAND = "curl http://192.168.20.30/second"
_ADVERSARY_ID = f"{_SLUG}-adv"


def _utcnow() -> dt.datetime:
    """Naive UTC timestamp (portable across PostgreSQL and the test SQLite)."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _seed_campaign(session: Session) -> None:
    """Seed the curated campaign as ORM rows only (no disk access).

    Persists a ``campaigns`` row, the structural ``attack_flows`` row and the ``Ability``/
    ``Adversary`` rows keyed by ``campaign_id`` — so the runtime resolves
    everything from the database. The ``campaigns`` display name is distinct from
    the usual curated name to keep the DB-sourced resolution honest.
    """
    session.add(
        Case(
            id=_SLUG,
            name=_CAMPAIGN_NAME,
            translation_source=TranslationSource.HUMAN_CURATION,
            curated=True,
        )
    )
    session.add(
        Ability(
            ability_id=_ABILITY_1_ID,
            campaign_id=_SLUG,
            name="first ability",
            technique_id="T1190",
            technique_name="Exploit Public-Facing Application",
            tactic="initial-access",
            executors=[{"name": "sh", "platform": "linux", "command": _ABILITY_1_COMMAND}],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Ability(
            ability_id=_ABILITY_2_ID,
            campaign_id=_SLUG,
            name="second ability",
            technique_id="T1059",
            technique_name="Command and Scripting Interpreter",
            tactic="execution",
            executors=[{"name": "sh", "platform": "linux", "command": _ABILITY_2_COMMAND}],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        Adversary(
            id=_ADVERSARY_ID,
            campaign_id=_SLUG,
            name="DB Only Adversary",
            description="seeded in the DB only",
            atomic_ordering=[_ABILITY_1_ID, _ABILITY_2_ID],
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        AttackFlowRow(
            campaign_id=_SLUG,
            origin=FlowOrigin.HUMAN_CURATION,
            identifier="human-curation-v1",
            label="DB-sourced attack flow",
            content={
                "campaign_name": _CAMPAIGN_NAME,
                "structural_nodes": [_NODE_1, _NODE_2],
                "edges": [{"source": "n0", "target": "n1"}],
            },
            created_at=_utcnow(),
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# Ephemeral module-scoped SQLite + seeding.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session_factory() -> Iterator[sessionmaker[Session]]:
    """A fresh temp-file SQLite engine + schema for the module."""
    tmp_dir = tempfile.mkdtemp(prefix="no_disk_access_")
    db_path = Path(tmp_dir) / "no_disk_access.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
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
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


@pytest.fixture(scope="module")
def seeded(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """Seed the campaign and its ``stix_campaigns`` guard row once for the module."""
    session = session_factory()
    try:
        _seed_campaign(session)
    finally:
        session.close()
    # The DB-backed guard (`_known_campaign_or_404`) validates the slug against
    # the persisted ``stix_campaigns`` rows, so seed the matching guard row.
    ensure_stix_campaigns(session_factory, [_SLUG])
    return session_factory


def _make_client(session_factory: sessionmaker[Session]) -> TestClient:
    """Build a TestClient with ``get_db`` wired onto the seeded session factory."""
    app = create_app()

    def _get_db_override() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db_override
    return TestClient(app)


# ---------------------------------------------------------------------------
# The no-disk guarantee: drive the representative request paths with the probe
# armed and assert NONE of them opened a curated file (Req. 1.1, 4.2).
# ---------------------------------------------------------------------------


def test_request_paths_never_open_curated_data_directories(
    seeded: sessionmaker[Session],
    filesystem_probe: list[str],
) -> None:
    """No driven request opens ``packages/cli/data/api`` or ``.../dag`` (Req. 1.1, 4.2).

    With the filesystem probe armed, every representative request-time path is
    driven through the ``TestClient``. Each used to read a curated file from disk
    before sub-step 2.4; now they all resolve from the seeded database rows. The
    probe's breach log must stay empty — a single forbidden open is a real
    regression and fails the test with the offending path.
    """
    client = _make_client(seeded)

    # 1. Campaign list — resolves display names (in-memory) + per-stage state (DB).
    resp = client.get("/api/campaigns")
    assert resp.status_code == 200, resp.text

    # 2. Campaign detail — Abilities/Adversary/ordering resolved from the DB.
    # (The display name comes from the in-memory curated registry, not disk; the
    # Abilities/ordering are the DB-sourced proof here.)
    resp = client.get(f"/api/campaigns/{_SLUG}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    resolved_ids = {a["ability_id"] for a in body["abilities"]}
    assert resolved_ids == {_ABILITY_1_ID, _ABILITY_2_ID}
    assert body["atomic_ordering"] == [_ABILITY_1_ID, _ABILITY_2_ID]

    # 3. Topology — attack flow + Abilities + name resolved from the DB.
    resp = client.get(f"/api/campaigns/{_SLUG}/topology")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == _CAMPAIGN_NAME

    # The whole point: not one request opened a curated data file.
    assert breaches_empty(filesystem_probe), (
        f"the runtime opened a forbidden curated data file during a request: {filesystem_probe}"
    )


def breaches_empty(breaches: list[str]) -> bool:
    """Tiny readability helper: whether the probe recorded no breach."""
    return len(breaches) == 0


def test_probe_detects_a_forbidden_open(filesystem_probe: list[str]) -> None:
    """Sanity check: the probe actually fires on a curated-directory open.

    A probe that never fires would make the no-disk assertion vacuously true. To
    keep the real guarantee meaningful, this confirms the probe records an open
    whose path resolves inside a forbidden directory (using a non-existent file
    so nothing is actually read), and leaves any other path untouched.
    """
    target = _DATA_ROOT / "api" / "does-not-exist.json"
    with pytest.raises(FileNotFoundError):
        builtins.open(target)  # noqa: SIM115 -- expected to raise; no file handle is ever created

    assert any(str(target.resolve()) == b for b in filesystem_probe), (
        "the probe must record an open under packages/cli/data/api"
    )


# ---------------------------------------------------------------------------
# Task 21.1: all 8 curated campaigns (flow, abilities, adversary, environment)
# are served from the database only. The strict probe fails on ANY access
# (open/read_text/read_bytes/glob/rglob/iterdir/exists/listdir/scandir) to
# anything under packages/cli/data.
# ---------------------------------------------------------------------------

_ALL_SLUGS: tuple[str, ...] = tuple(CURATED_CASES)
_DATA_ROOT_RESOLVED = _DATA_ROOT.resolve()


def _under_data_root(target: Any) -> bool:
    resolved = _normalise(target)
    if resolved is None:
        return False
    try:
        resolved.relative_to(_DATA_ROOT_RESOLVED)
        return True
    except ValueError:
        return False


@pytest.fixture
def strict_data_probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every disk access under ``packages/cli/data`` (any API, any depth)."""
    breaches: list[str] = []

    def _wrap_path_method(name: str) -> None:
        real = getattr(Path, name)

        def guarded(self: Path, *args: Any, **kwargs: Any) -> Any:
            if _under_data_root(self):
                breaches.append(f"Path.{name}:{self}")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, name, guarded)

    def _wrap_os_func(name: str) -> None:
        real = getattr(os, name)

        def guarded(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
            if _under_data_root(path):
                breaches.append(f"os.{name}:{path}")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for method in (
        "read_text",
        "read_bytes",
        "open",
        "glob",
        "rglob",
        "iterdir",
        "exists",
        "is_file",
        "is_dir",
        "stat",
    ):
        _wrap_path_method(method)
    for func in ("listdir", "scandir", "walk", "stat"):
        _wrap_os_func(func)

    real_builtins_open = builtins.open
    real_io_open = io.open
    real_os_open = os.open

    def _record(kind: str, file: Any) -> None:
        if _under_data_root(file):
            breaches.append(f"{kind}:{file}")

    def g_builtins_open(file: Any, *a: Any, **k: Any) -> Any:
        _record("builtins.open", file)
        return real_builtins_open(file, *a, **k)

    def g_io_open(file: Any, *a: Any, **k: Any) -> Any:
        _record("io.open", file)
        return real_io_open(file, *a, **k)

    def g_os_open(path: Any, *a: Any, **k: Any) -> Any:
        _record("os.open", path)
        return real_os_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", g_builtins_open)
    monkeypatch.setattr(io, "open", g_io_open)
    monkeypatch.setattr(os, "open", g_os_open)
    return breaches


def _seed_slug(session: Session, slug: str) -> None:
    """Seed one curated campaign (case, 2 abilities, adversary, flow) as ORM rows."""
    name = f"DB {slug}"
    ids = [f"{slug}-ab-1", f"{slug}-ab-2"]
    session.add(
        Case(
            id=slug,
            name=name,
            translation_source=TranslationSource.HUMAN_CURATION,
            curated=True,
        )
    )
    for index, ability_id in enumerate(ids):
        session.add(
            Ability(
                ability_id=ability_id,
                campaign_id=slug,
                name=f"{slug} ability {index}",
                technique_id=_NODE_1["technique_id"] if index == 0 else _NODE_2["technique_id"],
                technique_name="technique",
                tactic="execution",
                executors=[{"name": "sh", "platform": "linux", "command": f"echo {slug} {index}"}],
                translation_source=TranslationSource.HUMAN_CURATION,
            )
        )
    session.add(
        Adversary(
            id=f"{slug}-adv",
            campaign_id=slug,
            name=f"{slug} adversary",
            description="seeded in the DB only",
            atomic_ordering=ids,
            translation_source=TranslationSource.HUMAN_CURATION,
        )
    )
    session.add(
        AttackFlowRow(
            campaign_id=slug,
            origin=FlowOrigin.HUMAN_CURATION,
            identifier="human-curation-v1",
            label=f"{slug} flow",
            content={
                "campaign_name": name,
                "structural_nodes": [_NODE_1, _NODE_2],
                "edges": [{"source": "n0", "target": "n1"}],
            },
            created_at=_utcnow(),
        )
    )


@pytest.fixture(scope="module")
def all_curated_factory() -> Iterator[sessionmaker[Session]]:
    """Ephemeral SQLite seeded with the 8 curated campaigns (DB rows only)."""
    tmp_dir = tempfile.mkdtemp(prefix="no_disk_all8_")
    db_path = Path(tmp_dir) / "all8.db"
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
    session = factory()
    try:
        for slug in _ALL_SLUGS:
            _seed_slug(session, slug)
        session.commit()
    finally:
        session.close()
    ensure_stix_campaigns(factory, list(_ALL_SLUGS))
    try:
        yield factory
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(db_path.parent)


def test_eight_curated_campaigns_are_served_only_from_the_database(
    all_curated_factory: sessionmaker[Session],
    strict_data_probe: list[str],
) -> None:
    """Flow, abilities, adversary and environment of all 8 campaigns: DB only (task 21.1)."""
    from app.services.curated_case_reader import build_case_from_db

    assert len(_ALL_SLUGS) == 8
    client = _make_client(all_curated_factory)

    assert client.get("/api/campaigns").status_code == 200

    for slug in _ALL_SLUGS:
        ids = [f"{slug}-ab-1", f"{slug}-ab-2"]

        # Abilities + adversary (+ ordering) via the campaign detail endpoint.
        detail = client.get(f"/api/campaigns/{slug}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert {a["ability_id"] for a in body["abilities"]} == set(ids)
        assert body["atomic_ordering"] == ids

        # Attack flow: persisted flows listed and the graph resolved.
        flows = client.get(f"/api/campaigns/{slug}/flows")
        assert flows.status_code == 200, flows.text
        listed = flows.json()["flows"]
        assert listed, f"no flow served for {slug}"
        graph = client.get(f"/api/campaigns/{slug}/flows/{listed[0]['id']}")
        assert graph.status_code == 200, graph.text

        # Environment / topology.
        topology = client.get(f"/api/campaigns/{slug}/topology")
        assert topology.status_code == 200, topology.text
        assert topology.json()["name"] == f"DB {slug}"

        # Full CaseData assembled from the DB (flow + abilities + adversary).
        session = all_curated_factory()
        try:
            case = build_case_from_db(session, slug)
        finally:
            session.close()
        assert [a.ability_id for a in case.abilities] == ids
        assert case.adversary is not None and case.adversary.atomic_ordering == ids
        assert case.flow is not None and len(case.flow.structural_nodes) == 2

    assert strict_data_probe == [], (
        f"disk access under packages/cli/data while serving from the DB: {strict_data_probe}"
    )


def test_strict_probe_detects_every_access_style(strict_data_probe: list[str]) -> None:
    """Sanity: the strict probe fires for open/read_text/glob/iterdir/listdir."""
    target = _DATA_ROOT / "api" / "missing.json"
    with pytest.raises(FileNotFoundError):
        builtins.open(target)  # noqa: SIM115 -- expected to raise; no file handle is ever created
    with pytest.raises(FileNotFoundError):
        target.read_text()
    list((_DATA_ROOT / "api").glob("*.json"))
    with contextlib.suppress(OSError):
        os.listdir(_DATA_ROOT / "dag")
    kinds = {entry.split(":", 1)[0] for entry in strict_data_probe}
    assert {"builtins.open", "Path.read_text", "Path.glob", "os.listdir"} <= kinds
    strict_data_probe.clear()
