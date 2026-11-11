"""Example tests — selectable-everywhere + absent-input Run failure (task 6.6).

Two contracts of the AI-first reorientation (Axis 4), proven by example:

* **Selectable everywhere (Req. 4.1).** The curated-campaign gate is removed:
  :meth:`app.services.stix.stage2_service.Stage2Service.list_campaigns` marks
  EVERY listed campaign ``selectable=True`` — both the curated campaign (present
  in ``SLUG_TO_STIX`` / resolvable to curated artifacts) and a non-curated one —
  so a non-curated campaign's STIX/ATT&CK context can flow through the AI stages
  as the agent ``agent_input`` with no human-curated artifacts required to
  select.

* **Absent-input failure, no human-curation fallback (Req. 4.4).** When a Stage
  cannot be filled for the Selected_Campaign because required Agent_Input is
  absent, the Run fails AT THAT STAGE and records the FAILING STAGE and the
  ABSENT INPUT into ``agent_runs.error`` — and it does NOT fall back to human
  curation (no ``agent_stage_outputs`` / ``agent_iterations`` /
  ``agent_evaluations`` rows are written and ``final_verdict_success`` stays
  null).

Harness: the Stage 2 half reuses the real curated artifacts (read-only) +
a temporary STIX bundle in an isolated ``data_dir`` and an ephemeral per-test
SQLite, exactly like ``test_stage2_service.py``. The Run half drives
:class:`app.services.ai.run_service.AgentRunService` with a FAKE compiled graph
(``_build_graph`` monkeypatched) whose ``invoke`` raises an absent-input node
error, over a fresh temp-file SQLite built straight from the ORM, exactly like
``test_run_service_lifecycle.py`` — no SEPINF endpoint, no PostgreSQL, no real
LangGraph.

Note on concurrency: the ``agent_runs.error``-writing behaviour is owned by
task 3.7's extension of ``_fail_run``. These tests assert that contract directly
(the failing stage + absent-input cause must land in ``agent_runs.error``); if a
test fails solely because 3.7's ``_fail_run`` change is not yet merged, that is
the expected signal, not a defect in the test.

_Requirements: 4.1, 4.4_
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base, import_models
from app.db.session import create_db_engine
from app.models.domain import (
    AgentEvaluation,
    AgentIteration,
    AgentRun,
    AgentStageOutput,
)
from app.models.enums import AgentRunStatus
from app.services.ai.run_service import AgentRunService, RunFailedError
from app.services.campaign_service import CaseService
from app.services.stage.stage_service import StageService
from app.services.stix.stage2_service import ListedCampaign, Stage2Service
from app.services.stix.stix_service import StixService

VERSION = "18.0"
_CAMPAIGN_ID = "campaign-under-test"


# ===========================================================================
# (a) Selectable everywhere — Req. 4.1
#
# Stage 2 harness: real curated data dir (read-only) + temp STIX bundle in an
# isolated data_dir, ephemeral SQLite — mirrors test_stage2_service.py.
# ===========================================================================


def _real_data_dir() -> Path:
    """tests/ -> backend/ -> apps/ -> repo root -> packages/cli/data (read-only)."""
    return Path(__file__).resolve().parents[3] / "packages" / "cli" / "data"


def _bundle_with_curated_and_non_curated() -> dict:
    """A STIX bundle with the curated ShadowRay campaign (C0045) + a non-curated one."""
    return {
        "type": "bundle",
        "objects": [
            {
                "type": "campaign",
                "id": "campaign--shadow",
                "name": "ShadowRay",
                "external_references": [{"external_id": "C0045"}],
                "aliases": ["Shadow Ray"],
            },
            {
                "type": "campaign",
                "id": "campaign--other",
                "name": "Non-Curated Campaign",
                "external_references": [{"external_id": "C9999"}],
            },
        ],
    }


@pytest.fixture()
def stage2_session() -> Iterator[Session]:
    tmp_dir = Path(tempfile.mkdtemp(prefix="selectable_stage2_"))
    db_path = tmp_dir / "stage2.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    db_session = session_factory()
    try:
        yield db_session
    finally:
        db_session.close()
        engine.dispose()
        shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture()
def stix_tmp(tmp_path: Path) -> StixService:
    """A ``StixService`` over an isolated ``data_dir`` seeded with the v18 bundle."""
    stix = StixService(data_dir=tmp_path)
    path_ = stix.base_path(VERSION)
    path_.parent.mkdir(parents=True, exist_ok=True)
    path_.write_text(
        json.dumps(_bundle_with_curated_and_non_curated()), encoding="utf-8"
    )
    return stix


def _stage2_service(session: Session, stix_tmp: StixService) -> Stage2Service:
    stage = StageService(
        session,
        case_service=CaseService(data_dir=_real_data_dir()),
        stix_service=stix_tmp,
    )
    return Stage2Service(stage)


def test_list_campaigns_marks_every_campaign_selectable_curated_and_non_curated(
    stage2_session: Session, stix_tmp: StixService
) -> None:
    """EVERY listed campaign is selectable — curated AND non-curated (Req. 4.1).

    The curated-campaign gate is removed: both the curated ShadowRay campaign
    and the non-curated campaign come back ``selectable=True``, so a non-curated
    campaign's STIX/ATT&CK context can flow through the AI stages.
    """
    service = _stage2_service(stage2_session, stix_tmp)

    campaigns = service.list_campaigns(VERSION)

    assert campaigns, "expected the bundle's campaigns to be listed"
    assert all(isinstance(c, ListedCampaign) for c in campaigns)

    curated = [c for c in campaigns if c.curated]
    non_curated = [c for c in campaigns if not c.curated]
    assert curated, "expected at least one curated campaign (ShadowRay/C0045)"
    assert non_curated, "expected at least one non-curated campaign (C9999)"

    # The gate is gone: curated and non-curated alike are selectable, and no
    # campaign in the single consistent list is left unselectable (Req. 4.1).
    assert all(c.selectable is True for c in curated)
    assert all(c.selectable is True for c in non_curated)
    assert all(c.selectable is True for c in campaigns)


# ===========================================================================
# (b) Absent-input Run failure, no human-curation fallback — Req. 4.4
#
# Run harness: fresh temp-file SQLite + ORM schema and a FAKE compiled graph
# whose invoke raises an absent-input node error — mirrors
# test_run_service_lifecycle.py.
# ===========================================================================


def _build_ephemeral_session() -> tuple[Session, Path, Any]:
    tmp_dir = tempfile.mkdtemp(prefix="absent_input_run_")
    db_path = Path(tmp_dir) / "run_service.db"
    settings_obj = Settings(database_url=f"sqlite:///{db_path.as_posix()}")

    engine = create_db_engine(settings_obj)
    import_models()
    Base.metadata.create_all(bind=engine)

    session_factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        class_=Session,
    )
    return session_factory(), db_path, engine


@pytest.fixture()
def run_session() -> Iterator[Session]:
    sess, db_path, engine = _build_ephemeral_session()
    try:
        yield sess
    finally:
        sess.close()
        engine.dispose()
        try:
            db_path.unlink()
        except OSError:
            pass


class _FakeGraph:
    """Stand-in compiled graph exposing only ``invoke(state, config=...)``.

    Raises a controlled absent-input node error so the Run_Service's failure
    path (``_fail_run``) is exercised end to end against the ephemeral SQLite.
    """

    def __init__(self, *, raises: BaseException) -> None:
        self._raises = raises
        self.invocations: list[dict] = []

    def invoke(self, state: Any, config: dict | None = None) -> dict:
        self.invocations.append(config or {})
        raise self._raises


def _install_fake_graph(monkeypatch: pytest.MonkeyPatch, graph: _FakeGraph) -> None:
    """Patch ``_build_graph`` to return ``graph`` and no-op the dependency probe."""
    monkeypatch.setattr(
        AgentRunService, "_build_graph", lambda self: graph, raising=True
    )
    monkeypatch.setattr(
        AgentRunService, "_require_dependencies", lambda self: None, raising=True
    )


def _count_ai_rows(session: Session) -> tuple[int, int, int]:
    """Return (agent_iterations, agent_stage_outputs, agent_evaluations) counts."""
    iterations = session.execute(
        select(func.count()).select_from(AgentIteration)
    ).scalar_one()
    outputs = session.execute(
        select(func.count()).select_from(AgentStageOutput)
    ).scalar_one()
    evaluations = session.execute(
        select(func.count()).select_from(AgentEvaluation)
    ).scalar_one()
    return iterations, outputs, evaluations


def test_absent_agent_input_fails_at_stage_recording_error_without_curation_fallback(
    run_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent required Agent_Input fails the Run at that Stage into ``agent_runs.error``.

    When the DAG stage cannot be filled because its required Agent_Input is
    absent (modelled by the DAG node raising a ``DagGenerationError`` whose
    ``cause`` names the absent input), the Run:

    * is flipped to ``FAILED`` (the failing stage halted the Run);
    * records the FAILING STAGE (``"dag"``) and the ABSENT-INPUT cause into
      ``agent_runs.error`` (Req. 4.4, the contract task 3.7 implements);
    * does NOT fall back to human curation — no AI artifact / iteration /
      evaluation rows are written and ``final_verdict_success`` stays null.
    """
    from app.services.ai.agents.dag_agent import DagGenerationError

    # The DAG node signals an absent required Agent_Input (no modeling/chat model
    # available): node identifies the failing stage, cause names the absent input.
    absent_input_error = DagGenerationError(
        "AiDagGenerator requires a chat model to generate a DAG; none was injected.",
        node="dag",
        cause="missing_chat_model",
    )
    _install_fake_graph(monkeypatch, _FakeGraph(raises=absent_input_error))

    svc = AgentRunService(run_session)
    with pytest.raises(RunFailedError) as excinfo:
        svc.start(_CAMPAIGN_ID)

    # The Run failed AT the stage whose input was absent (Req. 4.4).
    assert excinfo.value.node == "dag"

    run = run_session.execute(select(AgentRun)).scalar_one()
    assert run.status is AgentRunStatus.FAILED

    # agent_runs.error records the failing stage + the absent-input cause so the
    # failure is auditable with no human-curation fallback (Req. 4.4 / task 3.7).
    assert run.error is not None, (
        "expected agent_runs.error to record the failing stage + absent input; "
        "if this is None, task 3.7's _fail_run extension is not yet merged — the "
        "contract under test is that agent_runs.error holds the failing stage + "
        "absent-input cause."
    )
    assert "dag" in run.error.lower(), "error must name the failing stage"
    assert "missing_chat_model" in run.error, "error must name the absent input"

    # No fall back to human curation: no AI artifacts/iterations/evaluations were
    # persisted and the run carries no passing verdict (Req. 4.4).
    assert run.final_verdict_success is None
    assert _count_ai_rows(run_session) == (0, 0, 0)
