"""Integration tests for the real-time WebSocket channels (task 11.3).

These exercise the channel wired in ``app.ws.router`` end to end through
FastAPI's ``TestClient.websocket_connect`` against an **ephemeral SQLite DB**:

* ``WS /ws/stages/{campaign}`` — connecting yields at least one stage-state frame
  reflecting the persisted ``StageRun`` (state + progress percentage), and while
  the stage is ``em_andamento`` more than one frame is produced (Req. 1.4, 1.5).
* ``WS /ws/operation/{operation}`` — connecting yields per-Ability status frames
  (``pendente`` / ``em_execucao`` / ``success`` / ``failure``) with command output
  and a final aggregate for a seeded Operation (Req. 4.3, 4.4, 4.5). A missing
  Operation yields a ``not_found`` frame.

A **short/zero push interval** is passed via the ``intervalo`` query param so the
loops emit one or two frames without waiting the 5s default, and ``max_frames``
bounds the otherwise-open ``em_andamento`` loop deterministically.

The channels open a fresh session from ``app.db.session.SessionLocal`` per poll;
each test monkeypatches that module attribute to a factory bound to the
ephemeral engine, mirroring the DB-override pattern used elsewhere in the suite.

_Requirements: 1.3, 4.3, 4.4, 4.5_
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.db.session as db_session_module
from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.main import create_app
from app.models.domain import (
    AbilityResult,
    Case,
    Operation,
)
from app.models.enums import (
    AbilityResultStatus,
    OperationState,
)


# ---------------------------------------------------------------------------
# Ephemeral SQLite engine / session factory (temp file, cleaned up)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_engine() -> Iterator[Engine]:
    """Yield an ephemeral temp-file SQLite engine with the full schema built."""
    tmp_dir = tempfile.mkdtemp(prefix="pipeline_ui_ws_")
    db_path = Path(tmp_dir) / "ws.db"
    settings = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings)
    try:
        import_models()
        Base.metadata.create_all(bind=engine)
        yield engine
    finally:
        engine.dispose()
        if db_path.exists():
            os.remove(db_path)
        os.rmdir(tmp_dir)

    assert not db_path.exists()


@pytest.fixture()
def session_factory(sqlite_engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to the ephemeral engine."""
    return sessionmaker(
        bind=sqlite_engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )


@pytest.fixture()
def client(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """A TestClient for the app, with SessionLocal pointed at the ephemeral DB.

    The WS routes resolve ``app.db.session.SessionLocal`` lazily at connect time,
    so patching the module attribute makes every per-poll session use the
    ephemeral engine.
    """
    monkeypatch.setattr(db_session_module, "SessionLocal", session_factory)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_case(session: Session, slug: str = "shadowray") -> None:
    session.add(Case(id=slug, name="ShadowRay"))
    session.commit()


# ---------------------------------------------------------------------------
# WS /ws/stages/{campaign} was removed with the curated pipeline
# ---------------------------------------------------------------------------


def test_removed_stages_channel_is_not_served(client: TestClient) -> None:
    """The legacy ``/ws/stages/{campaign}`` channel no longer exists (Req. 1.3)."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/stages/shadowray"):
            pass

# ---------------------------------------------------------------------------
# WS /ws/operation/{operation}
# ---------------------------------------------------------------------------


def test_operation_channel_emits_per_ability_and_final_aggregate(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A finished Operation yields per-Ability frames + a final aggregate.

    Seeds a ``finalizada`` Operation with two AbilityResults (one ``success``,
    one ``failure``) and aggregate totals. The (terminal) frame must carry both
    per-Ability results with their status + command output and the aggregate.
    _Requirements: 4.3, 4.4, 4.5_
    """
    session = session_factory()
    try:
        _seed_case(session)
        op = Operation(
            campaign_id="shadowray",
            state=OperationState.FINISHED,
            total_success=1,
            total_failure=1,
            started_at=dt.datetime(2024, 1, 1, 0, 0, 0),
            finished_at=dt.datetime(2024, 1, 1, 0, 5, 0),
        )
        session.add(op)
        session.commit()
        session.refresh(op)

        session.add_all(
            [
                AbilityResult(
                    operation_id=op.id,
                    ability_id="ability-ok",
                    status=AbilityResultStatus.SUCCESS,
                    command_output="ok output",
                ),
                AbilityResult(
                    operation_id=op.id,
                    ability_id="ability-bad",
                    status=AbilityResultStatus.FAILURE,
                    command_output="err output",
                ),
            ]
        )
        session.commit()
        operation_id = op.id
    finally:
        session.close()

    with client.websocket_connect(
        f"/ws/operation/{operation_id}?intervalo=0"
    ) as ws:
        frame = ws.receive_json()

    assert frame["found"] is True
    assert frame["final"] is True
    assert frame["state"] == OperationState.FINISHED.value  # "finished"
    assert frame["aggregate"] == {"total_success": 1, "total_failure": 1}

    results = {r["ability_id"]: r for r in frame["results"]}
    assert results["ability-ok"]["status"] == AbilityResultStatus.SUCCESS.value
    assert results["ability-ok"]["command_output"] == "ok output"
    assert results["ability-bad"]["status"] == AbilityResultStatus.FAILURE.value
    assert results["ability-bad"]["command_output"] == "err output"


def test_operation_channel_pushes_while_running_then_stops_on_max_frames(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """A running Operation keeps pushing per-Ability status frames (Req. 4.3).

    A ``em_execucao`` Operation with a ``pendente``/``em_execucao`` ability is
    not terminal, so ``max_frames`` bounds the loop. The first frame carries the
    live per-Ability status.
    """
    session = session_factory()
    try:
        _seed_case(session)
        op = Operation(
            campaign_id="shadowray",
            state=OperationState.RUNNING,
            total_success=0,
            total_failure=0,
        )
        session.add(op)
        session.commit()
        session.refresh(op)
        session.add(
            AbilityResult(
                operation_id=op.id,
                ability_id="ability-run",
                status=AbilityResultStatus.RUNNING,
                command_output=None,
            )
        )
        session.commit()
        operation_id = op.id
    finally:
        session.close()

    with client.websocket_connect(
        f"/ws/operation/{operation_id}?intervalo=0&max_frames=1"
    ) as ws:
        frame = ws.receive_json()

    assert frame["found"] is True
    assert frame["final"] is False
    assert frame["state"] == OperationState.RUNNING.value  # "running"
    assert frame["results"][0]["ability_id"] == "ability-run"
    assert (
        frame["results"][0]["status"] == AbilityResultStatus.RUNNING.value
    )


def test_operation_channel_reports_not_found_for_missing_operation(
    client: TestClient,
) -> None:
    """A missing Operation id yields a single ``not_found`` frame and closes."""
    with client.websocket_connect(
        "/ws/operation/999999?intervalo=0"
    ) as ws:
        frame = ws.receive_json()

    assert frame["operation_id"] == 999999
    assert frame["found"] is False
    assert "message" in frame
