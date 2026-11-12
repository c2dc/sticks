"""Ground-truth comparison harness against the 8 human-curated campaigns (task 19.1).

⚠️  LAB-ONLY — **A executar exclusivamente na máquina de laboratório do
usuário. NÃO rodar no ambiente de desenvolvimento nem em CI.**

This is the final evaluation deliverable of the AI-curation spec: it measures
AI-generated curation with the **existing Stage 6 procedural-sufficiency metric**
and compares it against the **human-curated baseline** — the
``Ground_Truth_Benchmark`` of **exactly 8** human-curated campaigns
(the ``campaigns.curated`` rows). For each campaign it:

* runs AI-generated curation through the pipeline via
  :meth:`app.services.ai.run_service.AgentRunService.start` (the compiled
  LangGraph graph over the PostgreSQL checkpointer, closing the
  plan→execute→measure→improve loop over the unchanged Stage 6 runner);
* reads the AI run's Procedural_Sufficiency_Metric **unchanged** from the
  persisted :class:`~app.models.domain.AgentEvaluation` of the run's final
  iteration (``procedural_coverage`` / ``success_rate``), exactly the Stage 6
  numbers the evaluator recorded (Req. 10.1 — the metric is *read*, never
  recomputed here);
* computes the **human-curated baseline** for the same campaign with the same
  Stage 6 measurement (:class:`~app.services.environment.measurement.MeasurementService`
  over the curated :class:`~app.services.curated_case_models.CaseData`); and
* **persists, for each completed run, a comparison result** of the AI run's
  score against that baseline (Req. 10.7) as a per-run JSON artifact under the
  lab output directory.

Why it is skipped by default
----------------------------
A real AI run needs the **SEPINF LLM endpoint** reachable over the network AND
the **real lab Caldera + hardened Docker environment** so the Stage 6 execution
inside the loop satisfies containment/isolation. The dev/CI environment has
neither, so — exactly like the other ``*_real_lab.py`` suites — this harness is
guarded **two ways** and never runs off the lab machine:

1. The ``@pytest.mark.lab`` marker — ``addopts = -m "not lab"`` in
   ``pyproject.toml`` deselects it by default.
2. ``@pytest.mark.skipif(os.getenv("PIPELINE_UI_LAB") != "1", ...)`` — even if
   the marker filter is overridden, it stays **skipped** unless the researcher
   explicitly opts in on the lab machine.

All heavy collaborators (the AI package / LangGraph / the LLM client, the real
``CalderaClient``) are imported **lazily inside the test body / fixtures**, so
``pytest --collect-only`` resolves this module cleanly under the backend
``.venv`` WITHOUT a live lab — the module-level guards keep the body inert.

How to run it on the lab machine
--------------------------------
1. Apply the hardened compose (lab-only — see the design decision):
   ``docker compose -f docker/docker-compose.yml -f docker/docker-compose.hardened.yml up -d``
2. Confirm Caldera answers at ``http://localhost:8888`` (header ``KEY``) and the
   SEPINF endpoint is reachable (``SEPINF_BASE_URL`` / ``SEPINF_API_KEY`` /
   ``SEPINF_MODEL_ID`` set in the root ``.env``).
3. Point ``PIPELINE_UI_DATABASE_URL`` at the lab's real **PostgreSQL + pgvector**
   (the AI run needs the LangGraph checkpointer and the pgvector memory — a
   SQLite fallback cannot host them).
4. From ``apps/backend`` with the venv active:
   ``$env:PIPELINE_UI_LAB = "1"; pytest -m lab tests/test_ground_truth_comparison_real_lab.py``
   (POSIX alternative: ``PIPELINE_UI_LAB=1 pytest -m lab``).

   By default the harness runs all 8 curated campaigns; override the selection
   with ``PIPELINE_UI_LAB_CASES`` (comma-separated slugs) — the same knob the
   other lab suites use. The per-run comparison JSON is written under
   ``PIPELINE_UI_LAB_OUTPUT_DIR`` (default: a temp dir printed to the log).

_Requirements: 10.1, 10.7_
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

import pytest

# Only lightweight, side-effect-free imports at module scope so collection is
# clean off the lab machine. The curated slug registry is a plain constant (no network /
# DB at import) and gives us the Ground_Truth_Benchmark registry. The heavy AI
# package, LangGraph, the LLM client and the real Caldera client are imported
# lazily inside the fixtures / test body.
from tests._curated_db import CURATED_NAMES as CURATED_CASES


# ---------------------------------------------------------------------------
# The Ground_Truth_Benchmark: EXACTLY the 8 human-curated campaigns (Req. 10.7).
# A researcher may narrow the selection on the lab machine via
# PIPELINE_UI_LAB_CASES (comma-separated slugs) — the same override knob the
# other *_real_lab.py suites expose — without editing this file.
# ---------------------------------------------------------------------------

_BENCHMARK_SIZE = 8


def _benchmark_cases() -> list[str]:
    """The curated case slugs to evaluate against the AI run (default: all 8)."""
    override = os.getenv("PIPELINE_UI_LAB_CASES", "").strip()
    if override:
        return [slug.strip() for slug in override.split(",") if slug.strip()]
    return list(CURATED_CASES.keys())


# Both guards: the ``lab`` marker (deselected by default via pyproject addopts)
# and a skipif on the opt-in env var, so the harness is inert in dev/CI
# regardless of how pytest is invoked.
pytestmark = [
    pytest.mark.lab,
    pytest.mark.skipif(
        os.getenv("PIPELINE_UI_LAB") != "1",
        reason=(
            "requires the real lab (SEPINF LLM endpoint + Caldera + hardened "
            "Docker + PostgreSQL/pgvector) to run AI curation end to end "
            "(task 19.1); set PIPELINE_UI_LAB=1 on the lab machine to run"
        ),
    ),
]


@pytest.fixture(scope="module")
def session_factory() -> Iterator[Any]:
    """A session factory over the lab's real PostgreSQL + pgvector.

    The AI run needs the LangGraph **PostgreSQL checkpointer** and the
    **pgvector** experience memory, so — unlike the SQLite-backed Stage 5/6 lab
    suites — this harness requires ``PIPELINE_UI_DATABASE_URL`` to point at the
    lab's real PostgreSQL (with the ``vector`` extension enabled by the AI
    migration). If it is not set, the whole harness is skipped with a clear
    message rather than silently falling back to a store that cannot host the
    checkpointer/pgvector.
    """
    from sqlalchemy.orm import Session, sessionmaker  # lazy: no ORM at import

    from app.core.config import Settings
    from app.db.base import Base, import_models
    from app.db.session import create_db_engine

    db_url = os.getenv("PIPELINE_UI_DATABASE_URL", "").strip()
    if not db_url:
        pytest.skip(
            "defina PIPELINE_UI_DATABASE_URL apontando para o PostgreSQL real do "
            "laboratório (com a extensão pgvector habilitada) — a corrida de IA "
            "precisa do checkpointer LangGraph e da memória pgvector, que o "
            "SQLite não hospeda"
        )

    settings_obj = Settings(database_url=db_url)
    engine = create_db_engine(settings_obj)
    import_models()
    # The AI bookkeeping + pgvector tables are created by the Alembic migration
    # on the lab database; create_all is idempotent and only fills any gaps for
    # the ORM-mapped tables, never touching the vector extension/index.
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


@pytest.fixture(scope="module")
def output_dir() -> Iterator[Path]:
    """Directory where the per-run comparison JSON artifacts are persisted.

    Defaults to a temp directory (kept after the run so the researcher can
    inspect the results); override with ``PIPELINE_UI_LAB_OUTPUT_DIR``.
    """
    configured = os.getenv("PIPELINE_UI_LAB_OUTPUT_DIR", "").strip()
    if configured:
        out = Path(configured)
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(tempfile.mkdtemp(prefix="pipeline_ui_ground_truth_"))
    yield out


def _utcnow() -> dt.datetime:
    """Naive-UTC timestamp, matching the models' ``_utcnow`` convention."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _human_baseline_coverage(session: Any, campaign_slug: str) -> dict[str, Any]:
    """Compute the human-curated baseline metric for a campaign (lazy imports).

    Measures the curated :class:`CaseData` with the SAME Stage 6 measurement the
    AI run is scored by (:class:`MeasurementService`), over the curated attack flow and
    the ability→technique map. The baseline reflects the curated universe of
    techniques (the denominator) so the AI run's procedural-sufficiency score is
    compared on the same footing (Req. 10.7). Returns the key baseline numbers.
    """
    from app.services.curated_case_reader import build_case_from_db
    from app.services.environment.measurement import MeasurementService

    case = build_case_from_db(session, campaign_slug)
    metrics = MeasurementService().measure_from_campaign(campaign=case, results=[])
    return {
        "procedural_coverage": metrics.procedural_coverage,
        "success_rate": metrics.success_rate,
        "total_flow_techniques": metrics.total_flow_techniques,
    }


def _ai_run_final_metric(session: Any, run: Any) -> Optional[dict[str, Any]]:
    """Read the AI run's Stage 6 procedural-sufficiency metric UNCHANGED (Req. 10.1).

    The evaluator records, per iteration, the Stage 6 numbers it keyed on into an
    :class:`~app.models.domain.AgentEvaluation` row (``procedural_coverage`` /
    ``success_rate`` / ``verdict_success``) — read, never recomputed. This reads
    the evaluation of the run's FINAL iteration (highest ``index``) and returns
    those numbers as the AI run's score. Returns ``None`` when the run completed
    no scored iteration (e.g. every iteration's Stage 6 report was unreadable —
    Req. 10.6), in which case there is no Procedural_Sufficiency_Metric to compare.
    """
    from sqlalchemy import select

    from app.models.domain import AgentEvaluation, AgentIteration

    stmt = (
        select(AgentEvaluation)
        .join(AgentIteration, AgentEvaluation.iteration_id == AgentIteration.id)
        .where(AgentIteration.run_id == run.id)
        .order_by(AgentIteration.index.desc())
    )
    evaluation = session.execute(stmt).scalars().first()
    if evaluation is None:
        return None
    return {
        "procedural_coverage": float(evaluation.procedural_coverage),
        "success_rate": float(evaluation.success_rate),
        "verdict_success": bool(evaluation.verdict_success),
    }


def _persist_comparison(
    output_dir: Path, comparison: dict[str, Any]
) -> Path:
    """Persist one per-run comparison result as a JSON artifact (Req. 10.7).

    There is no dedicated comparison table in the data model (the AI bookkeeping
    tables stop at ``agent_evaluations``), so — per the design's "existing
    procedural-sufficiency metric + 8-campaign benchmark" — the per-run
    comparison of the AI score against the human baseline is persisted as a JSON
    artifact keyed on the run id. The artifact links the ``agent_runs`` row
    (``run_id``) and the campaign so it is traceable back to the persisted run.
    """
    path = output_dir / f"comparison_run_{comparison['run_id']}_{comparison['campaign_id']}.json"
    path.write_text(json.dumps(comparison, indent=2, sort_keys=True), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def real_caldera() -> Iterator[Any]:
    """The REAL CalderaClient pointed at localhost:8888 (no mock transport).

    The AI run's Stage 6 ``execute`` node drives the real lab through the
    unchanged ``OperationRunner``; it reads the Caldera settings from the
    environment on its own, but we verify availability up front here so the whole
    harness is skipped with a clear message when the hardened stack is not up,
    rather than failing deep inside a run.
    """
    from app.services.caldera import CalderaClient  # lazy: no socket at import

    client = CalderaClient()  # real settings -> http://localhost:8888, header KEY
    availability = client.check_availability()
    if not availability.available:
        client.close()
        pytest.skip(
            "lab Caldera não respondeu em http://localhost:8888 — suba a stack "
            "endurecida (docker-compose.hardened.yml) antes de rodar o harness de "
            "comparação com a verdade-base"
        )
    try:
        yield client
    finally:
        client.close()


def test_ground_truth_benchmark_is_exactly_eight() -> None:
    """The Ground_Truth_Benchmark is EXACTLY the 8 human-curated campaigns (Req. 10.7).

    A cheap, lab-independent guard on the comparison baseline: the benchmark the
    harness iterates must be the 8 curated campaigns (unless the researcher
    deliberately narrowed it via PIPELINE_UI_LAB_CASES). This body does not touch
    the network/DB, but the module guards still keep it skipped off the lab
    machine so it collects cleanly and runs only when opted in.
    """
    assert len(CURATED_CASES) == _BENCHMARK_SIZE, (
        "o Ground_Truth_Benchmark deve conter exatamente 8 campanhas curadas "
        f"(encontrado: {len(CURATED_CASES)})"
    )
    # When not overridden, the harness evaluates all 8 campaigns.
    if not os.getenv("PIPELINE_UI_LAB_CASES", "").strip():
        assert len(_benchmark_cases()) == _BENCHMARK_SIZE


@pytest.mark.parametrize("campaign_slug", _benchmark_cases())
def test_ai_curation_vs_ground_truth_and_persist_comparison(
    campaign_slug: str,
    session_factory: Any,
    output_dir: Path,
    real_caldera: Any,  # noqa: ARG001 - availability gate (fixture side effect)
) -> None:
    """Run AI curation for a curated campaign, score it vs ground truth, persist.

    On the lab machine (SEPINF reachable, hardened Caldera/Docker up, real
    PostgreSQL+pgvector) this:

    1. runs AI-generated curation through the pipeline via
       :meth:`AgentRunService.start` — the compiled graph drives the three AI
       stage agents and the unchanged Stage 6 ``OperationRunner`` (containment →
       isolation → preview/confirm → execution → 1:1 audit → report) inside the
       bounded improvement loop, persisting ``agent_runs`` / ``agent_iterations``
       / ``agent_stage_outputs`` / ``agent_evaluations`` (Req. 10.1 flows through
       the evaluator, which records the Stage 6 metric unchanged);
    2. reads the AI run's Procedural_Sufficiency_Metric **unchanged** from the
       final iteration's persisted ``AgentEvaluation`` (Req. 10.1);
    3. computes the **human-curated baseline** for the same campaign with the
       same Stage 6 measurement; and
    4. **persists a per-run comparison result** of the AI score against that
       baseline as a JSON artifact linking the ``agent_runs`` row (Req. 10.7).

    Everything heavy is imported lazily inside the body / fixtures, so the module
    collects cleanly off the lab machine where the guards keep it skipped.
    """
    from app.models.enums import AgentRunStatus
    from app.services.ai.run_service import AgentRunService

    session = session_factory()
    try:
        service = AgentRunService(session)

        # (1) Run AI curation end to end through the pipeline. This opens the real
        # SEPINF connection (planning) and drives the unchanged Stage 6 runner on
        # the hardened lab (contained execution). The run row and its iterations/
        # evaluations are persisted by the service.
        run = service.start(campaign_slug)
        session.commit()

        # A run reaches a terminal state: COMPLETED (passing verdict) or ABORTED
        # (guard stopped the loop without one). Both are "completed runs" for the
        # purpose of the comparison (Req. 10.7); a FAILED run produced no scored
        # iteration and is reported as such below.
        assert run.id is not None
        assert run.status in (
            AgentRunStatus.COMPLETED,
            AgentRunStatus.ABORTED,
            AgentRunStatus.FAILED,
        )

        # (2) Read the AI run's Stage 6 procedural-sufficiency metric UNCHANGED
        # from the persisted final evaluation (Req. 10.1).
        ai_metric = _ai_run_final_metric(session, run)

        # (3) Human-curated baseline for the same campaign, same Stage 6 metric.
        baseline = _human_baseline_coverage(session, campaign_slug)

        # (4) Build and PERSIST the per-run comparison result (Req. 10.7). When
        # the run produced a scored iteration, record the signed delta of the AI
        # procedural coverage against the baseline; otherwise record that no
        # metric was available (every iteration's report was unreadable — Req.
        # 10.6) so the completed run still has a traceable comparison artifact.
        if ai_metric is not None:
            delta = ai_metric["procedural_coverage"] - baseline["procedural_coverage"]
            meets_or_exceeds = (
                ai_metric["procedural_coverage"] >= baseline["procedural_coverage"]
            )
        else:
            delta = None
            meets_or_exceeds = None

        comparison: dict[str, Any] = {
            "run_id": run.id,
            "campaign_id": campaign_slug,
            "campaign_name": CURATED_CASES.get(campaign_slug, campaign_slug),
            "run_status": run.status.value,
            "iterations_used": run.iterations_used,
            "final_verdict_success": run.final_verdict_success,
            "ai_metric": ai_metric,
            "human_baseline": baseline,
            "procedural_coverage_delta": delta,
            "ai_meets_or_exceeds_baseline": meets_or_exceeds,
            "compared_at": _utcnow().isoformat(),
        }

        artifact_path = _persist_comparison(output_dir, comparison)

        # The comparison artifact was persisted and round-trips (Req. 10.7): it
        # exists, links the persisted run, and names both scores under comparison.
        assert artifact_path.exists(), (
            "o resultado de comparação por corrida deve ser persistido"
        )
        persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
        assert persisted["run_id"] == run.id
        assert persisted["campaign_id"] == campaign_slug
        assert "human_baseline" in persisted
        assert "ai_metric" in persisted

        # The AI metric, when present, is the Stage 6 number read unchanged — a
        # procedural coverage in [0, 1], never recomputed by this harness
        # (Req. 10.1).
        if ai_metric is not None:
            assert 0.0 <= ai_metric["procedural_coverage"] <= 1.0
            assert 0.0 <= ai_metric["success_rate"] <= 1.0
        # The human baseline shares the same metric footing.
        assert 0.0 <= baseline["procedural_coverage"] <= 1.0
    finally:
        session.close()
