"""REST endpoints for casos and estágios (task 11.1).

Wires the curated-case / stage endpoints from the design's "Endpoints REST
(contratos em pt-BR)" section to the existing services. Handlers are kept thin:
each one constructs the relevant service with the *request-scoped* SQLAlchemy
session (via the ``get_db`` dependency) and delegates, returning the services'
already-typed Pydantic results as the response models.

Endpoints implemented here (Req. 5.1, 5.6, 2.1, 2.2, 3.1, 4.1):

* ``GET  /api/casos`` — the 8 curated cases with per-stage state and aggregate
  progress. Per-case read errors are reported per case without dropping the
  rest (Req. 5.1, 5.6).
* ``GET  /api/casos/{caso}`` — case detail (Abilities, Adversary, ordering,
  translation source).
* ``GET  /api/casos/{caso}/estagio/1`` — Stage 1 Entrada/Processamento/Saída
  (Req. 2.1, 2.2).
* ``GET  /api/casos/{caso}/estagio/2`` — Stage 2 (abstract techniques in;
  curated abilities/ordering/adversary/origem_traducao out) (Req. 3.1).
* ``GET  /api/casos/{caso}/estagio/3`` — Stage 3 Entrada (adversary + abilities)
  + accumulated Saída (Req. 4.1).
* ``POST /api/casos/{caso}/estagio/1/executar`` — trigger structural modeling
  (Req. 2.2).
* ``GET  /api/casos/{caso}/operacao`` — per-Ability results + aggregate
  (Req. 4.1, 4.5).

The emulation preview/execute endpoints (``POST .../emulacao/preview`` and
``POST .../emulacao``) are intentionally NOT implemented here — they belong to
task 11.2.

_Requisitos: 5.1, 5.6, 2.1, 2.2, 3.1, 4.1_
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.services.case_service import (
    AbilityData,
    AdversaryData,
    CaseLoadError,
    CaseService,
)
from app.models.enums import TranslationSource
from app.services.session.session_state_service import (
    OperationResultView,
    SessionStateService,
)
from app.services.stage.stage_service import (
    AggregateProgress,
    CaseStageStates,
    Stage1Result,
    Stage2Result,
    Stage3Input,
    StageService,
)

router = APIRouter(prefix="/api", tags=["casos"])


# ---------------------------------------------------------------------------
# Response models specific to the casos listing / detail endpoints
# ---------------------------------------------------------------------------


class CaseListItem(BaseModel):
    """One curated case in ``GET /api/casos`` (Req. 5.1, 5.6).

    Carries the case identity plus its per-stage state (``estagios`` from
    :class:`CaseStageStates`). When the case failed to load (missing/malformed
    files, Req. 5.6) ``erro`` is populated with the descriptive per-case error
    and ``estados`` is ``None`` — the case is still listed so one bad case never
    drops the rest.
    """

    id: str
    nome: str
    origem_traducao: Optional[TranslationSource] = None
    estados: Optional[CaseStageStates] = None
    erro: Optional[CaseLoadError] = None


class CasesResponse(BaseModel):
    """``GET /api/casos`` payload — the 8 cases + aggregate progress (Req. 5.1)."""

    casos: list[CaseListItem] = Field(default_factory=list)
    progresso: AggregateProgress


class CaseExecutorView(BaseModel):
    """A concrete executor (command) of a curated Ability (case detail)."""

    name: Optional[str] = None
    platform: Optional[str] = None
    command: Optional[str] = None


class CaseAbilityView(BaseModel):
    """One curated Ability in the case-detail response (Req. 3.2)."""

    ability_id: str
    name: Optional[str] = None
    tactic: Optional[str] = None
    technique_name: Optional[str] = None
    technique_id: Optional[str] = None
    description: Optional[str] = None
    executors: list[CaseExecutorView] = Field(default_factory=list)


class CaseAdversaryView(BaseModel):
    """The curated Adversary in the case-detail response (Req. 3.5)."""

    id: str
    name: Optional[str] = None
    description: Optional[str] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class CaseDetailResponse(BaseModel):
    """``GET /api/casos/{caso}`` payload — case detail.

    Abilities, Adversary, ordering and translation source (Req. 3.2, 3.3, 3.5,
    11.1). ``atomic_ordering`` mirrors the adversary's ordering (empty when there
    is no Adversary).
    """

    id: str
    nome: str
    origem_traducao: TranslationSource
    abilities: list[CaseAbilityView] = Field(default_factory=list)
    adversary: Optional[CaseAdversaryView] = None
    atomic_ordering: list[str] = Field(default_factory=list)


class Stage3Response(BaseModel):
    """``GET /api/casos/{caso}/estagio/3`` payload (Req. 4.1).

    Entrada = Adversary + curated Abilities (:class:`Stage3Input`); Saída
    acumulada = the per-Ability operation results + aggregate persisted so far
    for the case (from :class:`SessionStateService.get_operation_results`).
    """

    entrada: Stage3Input
    saida: list[OperationResultView] = Field(default_factory=list)


class OperationResponse(BaseModel):
    """``GET /api/casos/{caso}/operacao`` payload (Req. 4.1, 4.5).

    Per-Ability result + aggregate for every Operation of the case.
    """

    caso_id: str
    operacoes: list[OperationResultView] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _known_slug_or_404(slug: str, case_service: CaseService) -> None:
    """Raise 404 when ``slug`` is not one of the 8 curated cases."""
    if slug not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {slug!r}.",
        )


def _ability_view(a: AbilityData) -> CaseAbilityView:
    return CaseAbilityView(
        ability_id=a.ability_id,
        name=a.name,
        tactic=a.tactic,
        technique_name=a.technique_name,
        technique_id=a.technique_id,
        description=a.description,
        executors=[
            CaseExecutorView(name=e.name, platform=e.platform, command=e.command)
            for e in a.executors
        ],
    )


def _adversary_view(adv: AdversaryData) -> CaseAdversaryView:
    return CaseAdversaryView(
        id=adv.id,
        name=adv.name,
        description=adv.description,
        atomic_ordering=list(adv.atomic_ordering),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/casos",
    response_model=CasesResponse,
    summary="Lista os 8 casos curados com estado por estágio e progresso agregado",
)
def listar_casos(db: Session = Depends(get_db)) -> CasesResponse:
    """List the 8 curated cases with per-stage state and aggregate progress.

    Uses :class:`CaseService` (per-case error isolation, Req. 5.6),
    :meth:`StageService.get_stage_states` (per-stage state, Req. 5.3) and
    :meth:`StageService.aggregate_progress` (Req. 5.5). A case that fails to load
    is reported with its descriptive error and still listed — one bad case never
    drops the rest (Req. 5.1, 5.6).
    """
    case_service = CaseService()
    stage_service = StageService(db, case_service=case_service)

    catalog = case_service.load_all_cases_safe()
    display_names = case_service.list_cases()

    casos: list[CaseListItem] = []
    for slug, nome in display_names.items():
        if slug in catalog.errors:
            casos.append(
                CaseListItem(id=slug, nome=nome, erro=catalog.errors[slug])
            )
            continue
        case = catalog.cases.get(slug)
        casos.append(
            CaseListItem(
                id=slug,
                nome=nome,
                origem_traducao=(
                    case.origem_traducao if case else TranslationSource.HUMAN_CURATION
                ),
                estados=stage_service.get_stage_states(slug),
            )
        )

    progresso = stage_service.aggregate_progress()
    return CasesResponse(casos=casos, progresso=progresso)


@router.get(
    "/casos/{caso}",
    response_model=CaseDetailResponse,
    summary="Detalha um caso (Abilities, Adversary, ordenação, origem de tradução)",
)
def detalhar_caso(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> CaseDetailResponse:
    """Return a curated case's Abilities, Adversary, ordering and origin.

    A missing/malformed case surfaces a descriptive 422 error via the per-case
    load result (Req. 5.6). An unknown slug is a 404.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    result = case_service.try_load_case(caso)
    if not result.ok or result.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(result.error.detail if result.error else f"Falha ao ler {caso!r}."),
        )

    case = result.case
    return CaseDetailResponse(
        id=case.id,
        nome=case.nome,
        origem_traducao=case.origem_traducao,
        abilities=[_ability_view(a) for a in case.abilities],
        adversary=_adversary_view(case.adversary) if case.adversary else None,
        atomic_ordering=list(case.atomic_ordering),
    )


@router.get(
    "/casos/{caso}/estagio/1",
    response_model=Stage1Result,
    summary="Entrada/Processamento/Saída do Estágio 1",
)
def obter_estagio1(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage1Result:
    """Return the Stage-1 result (structural modeling) for the case (Req. 2.1, 2.2).

    Delegates to :meth:`StageService.run_stage1`, which reads the persisted /
    derived structural elements. A read/parse/modeling failure is reported in
    the typed result (``estado == erro``), not as an HTTP error.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).run_stage1(caso)


@router.get(
    "/casos/{caso}/estagio/2",
    response_model=Stage2Result,
    summary="Entrada (técnicas abstratas) e Saída (Abilities curadas) do Estágio 2",
)
def obter_estagio2(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage2Result:
    """Return the Stage-2 assembly (Req. 3.1).

    Entrada = abstract techniques from Stage 1; Saída = curated
    Abilities/ordering/Adversary with the explicit ``origem_traducao``.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).assemble_stage2(caso)


@router.get(
    "/casos/{caso}/estagio/3",
    response_model=Stage3Response,
    summary="Entrada (Adversary + Abilities) e Saída acumulada do Estágio 3",
)
def obter_estagio3(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage3Response:
    """Return the Stage-3 Entrada + accumulated Saída (Req. 4.1).

    Entrada = Adversary + curated Abilities (assembled, no side effects). Saída
    acumulada = the per-Ability operation results persisted so far for the case.
    Execution itself is not triggered here (that is task 11.2).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)

    stage_service = StageService(db, case_service=case_service)
    entrada = stage_service.assemble_stage3(caso)
    saida = SessionStateService(db).get_operation_results(caso)
    return Stage3Response(entrada=entrada, saida=saida)


@router.post(
    "/casos/{caso}/estagio/1/executar",
    response_model=Stage1Result,
    summary="Dispara a modelagem estrutural (Estágio 1)",
)
def executar_estagio1(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> Stage1Result:
    """Trigger Stage-1 structural modeling for the case (Req. 2.2).

    Delegates to :meth:`StageService.run_stage1`, which persists the extracted
    elements and marks the stage ``concluido`` on success (or ``erro`` with the
    failing step on failure — reported in the typed result, not as HTTP error).
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    return StageService(db, case_service=case_service).run_stage1(caso)


@router.get(
    "/casos/{caso}/operacao",
    response_model=OperationResponse,
    summary="Resultado por Ability e agregado da Operação",
)
def obter_operacao(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> OperationResponse:
    """Return the per-Ability results + aggregate for the case (Req. 4.1, 4.5).

    Delegates to :meth:`SessionStateService.get_operation_results`.
    """
    case_service = CaseService()
    _known_slug_or_404(caso, case_service)
    operacoes = SessionStateService(db).get_operation_results(caso)
    return OperationResponse(caso_id=caso, operacoes=operacoes)
