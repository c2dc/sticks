"""REST endpoints for AI curation runs (task 16.1).

Two thin endpoints over :class:`~app.services.ai.run_service.AgentRunService`:

* ``POST /api/campaigns/{campaign}/ai/run`` — start an AI curation run for a
  campaign. The handler delegates synchronously to
  :meth:`AgentRunService.start`, which creates the ``agent_runs`` row and invokes
  the compiled StateGraph; on success the row is committed and returned. The
  AI_Agent_System must create the row and invoke the graph within 2 seconds of
  the start request (Req. 13.1) — this handler just delegates to the service
  synchronously, so the 2s SLA is met by the service's own invocation path, not
  by any extra work here.

  On ANY failure — a missing dependency
  (:class:`~app.services.ai.run_service.DependencyUnavailableError`), the graph
  invocation failing (:class:`~app.services.ai.run_service.RunFailedError`),
  per-iteration persistence failing
  (:class:`~app.services.ai.run_service.PersistenceError`), or any other error —
  the handler rolls the session back so NO partial ``agent_runs`` row (nor any
  flushed-but-uncommitted bookkeeping row) is persisted, and returns an error
  response indicating the run could not be started (Req. 13.2). The service only
  ``flush``es, never ``commit``s, so a rollback here discards everything the
  failed attempt wrote.

* ``GET /api/campaigns/{campaign}/ai/runs/{id}`` — read a run with its iterations
  (the ``agent_iterations`` rows) and the evaluator verdict (from the
  ``agent_evaluations`` rows), scoped to the campaign (Req. 13.2).

Handlers stay thin and follow the existing router conventions (prefix ``/api``,
pydantic response models with ``Field`` descriptions, injected ``Session`` via
``Depends(get_db)``).

_Requirements: 13.1, 13.2_
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.ai.run_service import (
    AgentRunService,
    DependencyUnavailableError,
    PersistenceError,
    RunFailedError,
)

router = APIRouter(prefix="/api", tags=["ai"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AiRunStartRequest(BaseModel):
    """Body of ``POST /api/campaigns/{campaign}/ai/run`` (Req. 13.1).

    ``max_iterations`` is the optional loop guard. When omitted the service
    resolves the configured default (``SepinfSettings.max_iterations``, default
    3); when provided it is clamped by the service to the inclusive ``1..100``
    range.
    """

    max_iterations: Optional[int] = Field(
        default=None,
        description=(
            "Optional loop guard for the run. Omitted → the configured default "
            "(default 3); provided → clamped by the service to 1..100."
        ),
    )


class AiRunResponse(BaseModel):
    """A single ``agent_runs`` row returned by the start/read endpoints.

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_runs row.")
    campaign_id: str = Field(
        ..., description="Campaign this run curates (FK into campaigns)."
    )
    status: str = Field(
        ...,
        description=(
            "Run status: pendente / em_execucao / concluido / falhou / abortado."
        ),
    )
    thread_id: str = Field(
        ..., description="LangGraph checkpoint thread key making the run resumable."
    )
    max_iterations: int = Field(..., description="The run's loop guard.")
    iterations_used: int = Field(
        ..., description="Number of plan→execute→measure→improve passes performed."
    )
    final_verdict_success: Optional[bool] = Field(
        default=None,
        description=(
            "Overall verdict summary; null until the run reaches a terminal state."
        ),
    )


class AiEvaluationView(BaseModel):
    """The evaluator's verdict for an iteration (from ``agent_evaluations``).

    ``from_attributes`` lets FastAPI build this directly from the ORM row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_evaluations row.")
    report_id: Optional[int] = Field(
        default=None, description="Stage 6 execution report the evaluator read."
    )
    success_rate: float = Field(..., description="Execution success rate read.")
    procedural_coverage: float = Field(
        ..., description="Procedural-sufficiency metric read from the report."
    )
    verdict_success: bool = Field(
        ..., description="Whether the evaluator's verdict passed."
    )
    decision: str = Field(
        ..., description="Evaluator decision driving the loop: refinar / finalizar."
    )
    rationale: str = Field(
        ..., description="LLM reasoning for the decision (≤10,000 chars)."
    )


class AiIterationView(BaseModel):
    """One iteration of a run with its evaluator verdict (Req. 13.2).

    Carries the ``agent_iterations`` row plus the optional ``agent_evaluations``
    verdict recorded for that iteration (0..1 per iteration).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(..., description="Primary key of the agent_iterations row.")
    index: int = Field(..., description="0-based iteration number within the run.")
    refined_stage: Optional[str] = Field(
        default=None,
        description=(
            "Stage the evaluator refined into this iteration; null on the first "
            "pass."
        ),
    )
    notes: Optional[str] = Field(default=None, description="Free-form iteration notes.")
    created_at: dt.datetime = Field(
        ..., description="Naive-UTC creation timestamp of the iteration."
    )
    evaluation: Optional[AiEvaluationView] = Field(
        default=None,
        description="The evaluator verdict recorded for this iteration, if any.",
    )


class AiRunDetailResponse(BaseModel):
    """``GET /api/campaigns/{campaign}/ai/runs/{id}`` payload (Req. 13.2).

    The run summary plus its iterations (oldest first) each carrying its
    evaluator verdict.
    """

    run: AiRunResponse = Field(..., description="The agent_runs summary row.")
    iterations: list[AiIterationView] = Field(
        default_factory=list,
        description="The run's iterations with their evaluator verdicts, oldest first.",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/campaigns/{campaign}/ai/run",
    response_model=AiRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start an AI curation run (create row + invoke graph within 2s)",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": (
                "The run could not be started: a required dependency "
                "(checkpointer / pgvector) was unavailable, the graph invocation "
                "failed, or persistence failed. No partial agent_runs row was "
                "persisted (Req. 13.2)."
            )
        },
    },
)
def start_ai_run(
    body: AiRunStartRequest,
    campaign: str = Path(..., description="Campaign to curate with the AI agents"),
    db: Session = Depends(get_db),
) -> AiRunResponse:
    """Start an AI curation run for a campaign (Req. 13.1, 13.2).

    Delegates synchronously to :meth:`AgentRunService.start`, which creates the
    ``agent_runs`` row and invokes the compiled StateGraph. Per Req. 13.1 the row
    must be created and the graph invoked within 2 seconds of the start request;
    the handler adds no extra work on the hot path — it just hands off to the
    service, which owns the invocation — so the 2s SLA is bounded by the
    service's own synchronous invocation.

    On success the created row is committed and returned. On ANY failure
    (``DependencyUnavailableError`` / ``RunFailedError`` / ``PersistenceError`` or
    any other error) the session is rolled back so NO partial ``agent_runs`` row
    is persisted, and an error response indicating the run could not be started is
    returned (Req. 13.2). Because the service only ``flush``es (never ``commit``s),
    the rollback discards every row the failed attempt wrote.
    """
    try:
        run = AgentRunService(db).start(
            campaign, max_iterations=body.max_iterations
        )
    except DependencyUnavailableError as exc:
        # A required dependency (checkpointer / pgvector) was unavailable: the
        # service created no row, but roll back defensively so nothing partial
        # survives, then report the run could not be started (Req. 13.2).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "motivo": "dependencia_indisponivel",
                "mensagem": (
                    "A execução de IA não pôde ser iniciada: a dependência "
                    f"{exc.dependency!r} está indisponível."
                ),
                "dependencia": exc.dependency,
            },
        ) from exc
    except RunFailedError as exc:
        # The graph invocation failed. The service flushed a FAILED row; roll it
        # back so NO partial agent_runs row is persisted (Req. 13.2).
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "motivo": "execucao_falhou",
                "mensagem": (
                    "A execução de IA não pôde ser iniciada: a invocação do grafo "
                    f"falhou no nó {exc.node!r}."
                ),
                "no": exc.node,
            },
        ) from exc
    except PersistenceError as exc:
        # Per-iteration persistence failed; roll back so nothing partial survives.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "motivo": "persistencia_falhou",
                "mensagem": (
                    "A execução de IA não pôde ser iniciada: a persistência da "
                    "execução falhou."
                ),
            },
        ) from exc
    except Exception as exc:  # noqa: BLE001 - leave no partial row on any failure
        # Any other error: roll back so no partial agent_runs row is persisted
        # (Req. 13.2), then surface a generic "could not start" error response.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "motivo": "execucao_nao_iniciada",
                "mensagem": (
                    "A execução de IA não pôde ser iniciada devido a um erro "
                    "inesperado."
                ),
            },
        ) from exc

    # Commit the created row (and any bookkeeping) so the started run is durable;
    # the service only flushed it within the request-scoped session.
    db.commit()
    return AiRunResponse.model_validate(run)


@router.get(
    "/campaigns/{campaign}/ai/runs/{id}",
    response_model=AiRunDetailResponse,
    summary="Read an AI run with its iterations and the evaluator verdict",
    responses={
        status.HTTP_404_NOT_FOUND: {
            "description": "No AI run with that id exists for the campaign."
        },
    },
)
def get_ai_run(
    campaign: str = Path(..., description="Campaign the run belongs to"),
    id: int = Path(..., description="Id of the agent_runs row to read"),
    db: Session = Depends(get_db),
) -> AiRunDetailResponse:
    """Read a run with its iterations and the evaluator verdict (Req. 13.2).

    Loads the ``agent_runs`` row by ``id`` (scoped to ``campaign``), its
    ``agent_iterations`` rows (oldest first) and the ``agent_evaluations`` verdict
    recorded for each iteration. An unknown id — or an id that belongs to a
    different campaign — is a 404.
    """
    from app.models.domain import AgentEvaluation, AgentIteration, AgentRun

    run = db.get(AgentRun, id)
    if run is None or run.campaign_id != campaign:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Execução de IA desconhecida: id={id!r} para a campanha "
                f"{campaign!r}."
            ),
        )

    iteration_rows = (
        db.execute(
            select(AgentIteration)
            .where(AgentIteration.run_id == run.id)
            .order_by(AgentIteration.index.asc(), AgentIteration.id.asc())
        )
        .scalars()
        .all()
    )

    iterations: list[AiIterationView] = []
    for iteration in iteration_rows:
        # The evaluator records 0..1 verdict per iteration; take the latest row
        # for the iteration when present.
        evaluation_row = (
            db.execute(
                select(AgentEvaluation)
                .where(AgentEvaluation.iteration_id == iteration.id)
                .order_by(AgentEvaluation.id.desc())
            )
            .scalars()
            .first()
        )
        view = AiIterationView.model_validate(iteration)
        if evaluation_row is not None:
            view.evaluation = AiEvaluationView.model_validate(evaluation_row)
        iterations.append(view)

    return AiRunDetailResponse(
        run=AiRunResponse.model_validate(run),
        iterations=iterations,
    )
