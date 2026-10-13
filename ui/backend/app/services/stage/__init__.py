"""Stage orchestration services.

``StageService`` (task 8.1) implements Stage 1 — *Modelagem Estrutural
Automatizada* — for the curated cases: it derives the structural elements from
the DAG the curated pipeline produced (techniques, relationships, indicators,
infrastructure, malware and campaign metadata), persists them and marks the
stage ``concluido`` (Req. 2.2–2.4), treating an empty extraction as a success
(Req. 2.5) and a read/parse/persist failure as an error with ``etapa_falha`` set
and no partial persistence (Req. 2.6).

Task 8.2 adds the Entrada/Saída assembly for Stages 2 and 3:

* Stage 2 assembles the Entrada (abstract techniques from Stage 1) and the Saída
  (curated Abilities with the concrete command per executor, ordering /
  dependencies, the Adversary grouping and the explicit ``origem_traducao``),
  handling the absence states cleanly (Req. 3.1–3.8, 11.1).
* Stage 3 assembles the Entrada (Adversary + Abilities) and delegates the
  emulation to an injected ``OperationRunner`` (task 7.3), mapping its result
  into the per-Ability Saída + aggregate (Req. 4.1, 4.4, 4.5).

Task 8.3 adds the sequential blocking + aggregate progress + state/percentage
reporting surface (pure reads over the persisted ``StageRun`` rows):

* :meth:`StageService.is_stage_startable` — a stage is startable iff it is
  Stage 1 or its preceding stage is ``concluido``; otherwise it is BLOCKED
  (Req. 5.4).
* :meth:`StageService.get_stage_states` / :meth:`StageService.get_stage_state`
  — per-stage state (``nao_iniciado`` / ``em_andamento`` / ``concluido`` /
  ``erro``) plus the ``bloqueado`` flag (Req. 5.3).
* :meth:`StageService.aggregate_progress` — the number of cases whose all three
  stages are ``concluido``, out of the 8 curated cases (Req. 5.5).
* :meth:`StageService.stage_progress_event` — a stage's current state and
  percentage (and the transition it made) for the WebSocket layer (task 11.3)
  to push (Req. 1.4, 1.5, 1.6).
"""

from app.services.stage.stage_service import (
    AggregateProgress,
    CaseStageStates,
    FIRST_STAGE,
    IndicatorElement,
    InfrastructureElement,
    LAST_STAGE,
    MalwareElement,
    OperationRunResult,
    OperationRunnerProtocol,
    RelationshipElement,
    StageProgressEvent,
    StageStateView,
    VALID_STAGES,
    Stage1Input,
    Stage1Output,
    Stage1Result,
    Stage1Step,
    Stage2AbilityView,
    Stage2AdversaryView,
    Stage2ExecutorView,
    Stage2Input,
    Stage2InputTechnique,
    Stage2Output,
    Stage2Result,
    Stage3AbilityInput,
    Stage3AbilityResult,
    Stage3AdversaryInput,
    Stage3Aggregate,
    Stage3Input,
    Stage3Output,
    Stage3Result,
    StageService,
    TechniqueElement,
)

__all__ = [
    "StageService",
    # Stage 1
    "Stage1Result",
    "Stage1Input",
    "Stage1Output",
    "Stage1Step",
    "TechniqueElement",
    "RelationshipElement",
    "IndicatorElement",
    "InfrastructureElement",
    "MalwareElement",
    # Stage 2
    "Stage2Result",
    "Stage2Input",
    "Stage2InputTechnique",
    "Stage2Output",
    "Stage2AbilityView",
    "Stage2AdversaryView",
    "Stage2ExecutorView",
    # Stage 3
    "Stage3Result",
    "Stage3Input",
    "Stage3AbilityInput",
    "Stage3AdversaryInput",
    "Stage3Output",
    "Stage3AbilityResult",
    "Stage3Aggregate",
    # OperationRunner seam (task 7.3)
    "OperationRunnerProtocol",
    "OperationRunResult",
    # Sequential blocking + aggregate progress + reporting (task 8.3)
    "StageStateView",
    "CaseStageStates",
    "AggregateProgress",
    "StageProgressEvent",
    "VALID_STAGES",
    "FIRST_STAGE",
    "LAST_STAGE",
]
