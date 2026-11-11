"""Integration test: the runtime never reads ``packages/cli/data`` (task 5.3).

Sub-step 2.4 removed every request-time disk read from the runtime: once the
curated content is ingested into the database (sub-step 2.1), ``CaseService`` and
``DagService`` resolve campaigns from the ``campaigns``/``dags``/``abilities``/
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
* DAG endpoint (``GET /api/campaigns/{c}/dag``),
* emulation preview (``POST .../stage/5/emulation/preview``),
* topology (``GET /api/campaigns/{c}/topology``).

The whole campaign is seeded directly as ORM rows (a ``campaigns`` row, the
structural ``dags`` row, the ``abilities`` and the ``adversaries`` row) plus the
``stix_campaigns`` guard rows via :func:`tests._stix_seed.ensure_stix_campaigns`,
so the DB-backed guard (``_known_campaign_or_404``) accepts the slug. Nothing is
read from disk to set this up, and the probe stays armed for the duration of
every request.

Concrete rename-behaviour case
------------------------------
Task 5.2 renamed ``StageService.run_stage1``/``read_stage1`` to
``run_stage2_selection``/``read_stage2_selection`` (a semantic rename, no logic
change). As a concrete case in this same file,
:func:`test_run_and_read_stage2_selection_produce_expected_stage_result` drives
both renamed methods against the seeded DB and asserts they produce the expected
Stage result (the techniques of the seeded DAG), proving behaviour is unchanged
by the rename (Req. 4.3).

_Requirements: 1.1, 4.2, 5.2, 4.3_
"""

from __future__ import annotations

import builtins
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
from app.core.sticks_config import _repo_root
from app.db.base import Base, import_models
from app.db.session import create_db_engine, get_db
from app.main import create_app
from app.models.domain import Ability, Adversary, Case, Dag
from app.models.enums import DagOrigin, StageState, TranslationSource
from app.services.campaign_service import CURATED_CASES
from app.services.stage.stage_service import StageService

from tests._stix_seed import ensure_stix_campaigns


# ---------------------------------------------------------------------------
# The two curated directories that the runtime must NEVER open during a request.
# ---------------------------------------------------------------------------

_DATA_ROOT = _repo_root() / "packages" / "cli" / "data"
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
# A curated slug is used so the DB-first DAG resolution in ``DagService.get_dag``
# engages (it only does for a curated slug) and the ``/dag`` + topology endpoints
# resolve a non-empty graph. The content lives only in the seeded ORM rows, so
# the runtime has no reason to touch disk — and the probe proves it does not.
# ---------------------------------------------------------------------------

_SLUG = "shadowray"
assert _SLUG in CURATED_CASES, "the seeded slug must be curated for the DAG path"
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

# Expected techniques, in the DAG's first-seen order, as the pure Stage-1
# extractor (`_extract_techniques`) derives them from the seeded nodes.
_EXPECTED_TECHNIQUES: list[tuple[str, str, str]] = [
    ("T1190", "Exploit Public-Facing Application", "initial-access"),
    ("T1059", "Command and Scripting Interpreter", "execution"),
]

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

    Mirrors what ``CuratedIngestService`` persists for a campaign — a
    ``campaigns`` row, the structural ``dags`` row and the ``Ability``/
    ``Adversary`` rows keyed by ``campaign_id`` — so the runtime resolves
    everything from the database. The ``campaigns`` display name is distinct from
    the curated ``CaseService`` name to keep the DB-sourced resolution honest.
    """
    session.add(
        Case(
            id=_SLUG,
            name=_CAMPAIGN_NAME,
            translation_source=TranslationSource.HUMAN_CURATION,
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
            executors=[
                {"name": "sh", "platform": "linux", "command": _ABILITY_1_COMMAND}
            ],
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
            executors=[
                {"name": "sh", "platform": "linux", "command": _ABILITY_2_COMMAND}
            ],
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
        Dag(
            campaign_id=_SLUG,
            origin=DagOrigin.HUMAN_CURATION,
            identifier="human-curation-v1",
            label="DB-sourced DAG",
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

    # 3. Stage 2 selection — read view (never executes/persists).
    resp = client.get(f"/api/campaigns/{_SLUG}/stage/2")
    assert resp.status_code == 200, resp.text

    # 4. Stage 2 selection — select (extracts from the DB-sourced DAG).
    resp = client.post(
        f"/api/campaigns/{_SLUG}/stage/2/select", json={"slug": _SLUG}
    )
    assert resp.status_code == 200, resp.text

    # 5. DAG endpoint — structural graph resolved from the ``dags`` table.
    resp = client.get(f"/api/campaigns/{_SLUG}/dag")
    assert resp.status_code == 200, resp.text
    dag_body = resp.json()
    assert dag_body["curated"] is True
    node_techniques = {n["technique_id"] for n in dag_body["nodes"]}
    assert node_techniques == {"T1190", "T1059"}

    # 6. Emulation preview — Abilities + Adversary resolved from the DB.
    resp = client.post(f"/api/campaigns/{_SLUG}/stage/5/emulation/preview")
    assert resp.status_code == 200, resp.text
    preview_ids = {a["ability_id"] for a in resp.json()["abilities"]}
    assert preview_ids == {_ABILITY_1_ID, _ABILITY_2_ID}

    # 7. Topology — DAG + Abilities + name resolved from the DB.
    resp = client.get(f"/api/campaigns/{_SLUG}/topology")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == _CAMPAIGN_NAME

    # The whole point: not one request opened a curated data file.
    assert breaches_empty(filesystem_probe), (
        "the runtime opened a forbidden curated data file during a request: "
        f"{filesystem_probe}"
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
        builtins.open(target)

    assert any(str(target.resolve()) == b for b in filesystem_probe), (
        "the probe must record an open under packages/cli/data/api"
    )


# ---------------------------------------------------------------------------
# Concrete rename-behaviour case: run_stage2_selection / read_stage2_selection
# produce the expected Stage result (behaviour unchanged by the task 5.2 rename).
# ---------------------------------------------------------------------------


def test_run_and_read_stage2_selection_produce_expected_stage_result(
    seeded: sessionmaker[Session],
    filesystem_probe: list[str],
) -> None:
    """The renamed Stage-2 selection methods produce the expected result (Req. 4.3).

    Task 5.2 renamed ``run_stage1``/``read_stage1`` to
    ``run_stage2_selection``/``read_stage2_selection`` without changing the
    logic. ``run_stage2_selection`` runs the structural modeling off the
    DB-sourced DAG, persists it and completes the stage; ``read_stage2_selection``
    then reflects the persisted state read-only. Both must surface exactly the
    techniques of the seeded DAG, in the DAG's first-seen order — the behaviour
    the pre-rename methods had. The probe stays armed to confirm the rename also
    kept the no-disk contract (Req. 1.1, 4.2).
    """
    session = seeded()
    try:
        service = StageService(session=session)

        # run_stage2_selection: models + persists; stage completes (Req. 4.3).
        run_result = service.run_stage2_selection(_SLUG)
    finally:
        session.close()

    assert run_result.state is StageState.COMPLETED
    assert run_result.ok is True
    assert run_result.output is not None
    run_techniques = [
        (t.technique_id, t.technique_name, t.tactic)
        for t in run_result.output.techniques
    ]
    assert run_techniques == _EXPECTED_TECHNIQUES

    # read_stage2_selection: read-only view of the now-completed stage; it must
    # reflect the same techniques without re-running or persisting.
    session = seeded()
    try:
        read_result = StageService(session=session).read_stage2_selection(_SLUG)
    finally:
        session.close()

    assert read_result.state is StageState.COMPLETED
    assert read_result.output is not None
    read_techniques = [
        (t.technique_id, t.technique_name, t.tactic)
        for t in read_result.output.techniques
    ]
    assert read_techniques == _EXPECTED_TECHNIQUES

    # The rename kept the no-disk contract: neither method read a curated file.
    assert breaches_empty(filesystem_probe), (
        "a Stage-2 selection method opened a forbidden curated data file: "
        f"{filesystem_probe}"
    )
