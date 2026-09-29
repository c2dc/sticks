"""REST endpoints for emulation preview + execution with the containment
pre-flight (task 11.2) — SECURITY CORE (the HTTP surface of containment).

This module exposes the two emulation endpoints of the design's "Endpoints REST"
section and the emulation sequence diagram (the ``.../emulacao/preview`` +
``.../emulacao`` pair), wiring them to the already-built security core:

* ``POST /api/casos/{caso}/emulacao/preview`` — returns the **complete list of
  concrete commands with the target container of each, without executing**
  anything. It is a pure presentation of :func:`preview_abilities` over the
  case's Abilities (Req. 6.5). The confirmation modal (``EmulationConfirmModal``)
  renders this before any confirmation.
* ``POST /api/casos/{caso}/emulacao`` — accepts a body with ``confirmado`` and
  runs the four-gate pre-flight through :meth:`OperationRunner.run`
  (confirmation -> destinations -> isolation -> availability). It maps the
  runner's typed :class:`RunOutcome` and the propagated
  :class:`CalderaUnavailable` to the HTTP contract the design mandates:

    - ``RunOutcome.CONTAINMENT_REFUSED`` (external destination, Req. 6.2) and
      ``RunOutcome.ISOLATION_FAILED`` (container not isolated, Req. 6.4) ->
      **HTTP 409** with the offending Ability/command or container in the body.
    - ``CalderaUnavailable`` (Caldera silent within 10s, Req. 4.6) -> **HTTP 503**.
    - ``RunOutcome.NOT_CONFIRMED`` (``confirmado`` not true / cancelled,
      Req. 6.6, 6.7) -> **HTTP 200** with a body indicating no operation started
      (the key requirement being that *nothing ran*).
    - success (``COMPLETED`` / ``ABORTED``) -> the operation result (state +
      aggregate + per-Ability results). ``ABORTED`` (audit-trail failure,
      Req. 6.9) surfaces its message so the UI can flag the interrupted trail.

Handlers are kept thin: they resolve the case (its Abilities + Adversary) via
:class:`CaseService`, then delegate to the preview builder or the
:class:`OperationRunner`. All the enforcement lives in the runner and the
``containment`` package — this layer only translates outcomes into HTTP.

Dependency injection (Windows dev; NO real Caldera/Docker)
----------------------------------------------------------
The :class:`OperationRunner` needs a :class:`CalderaClient` + :class:`AuditLogger`
+ session. Those are provided through FastAPI dependencies so task 11.4 (and the
lightweight verification here) can override them with a ``httpx.MockTransport``
backed client + a fake isolation verifier, without a real Caldera or Docker
daemon:

* :func:`get_caldera_client` — yields a :class:`CalderaClient` (default settings
  in production; overridden with a MockTransport-backed client in tests).
* :func:`get_runner_factory` — yields a small factory
  ``(session) -> OperationRunner`` so the handler builds the runner bound to the
  request-scoped session while keeping the Caldera client (and any test-injected
  isolation verifier) overridable via a single dependency.

Overriding either dependency via ``app.dependency_overrides`` is enough to drive
the whole endpoint fully mocked.

_Requisitos: 6.5, 6.6, 6.7, 6.2, 6.4, 4.6_
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.enums import AbilityResultStatus, OperationState
from app.services.audit import AuditLogger
from app.services.caldera import CalderaApiError, CalderaClient, CalderaUnavailable
from app.services.case_service import AbilityData, CaseData, CaseService
from app.services.containment import EmulationPreview, preview_abilities
from app.services.operation.runner import (
    OperationRunResult,
    OperationRunner,
    RunOutcome,
)

router = APIRouter(prefix="/api", tags=["emulacao"])


@dataclass(frozen=True)
class _RunnerAbility:
    """Runner-facing view of a curated Ability with plain-dict executors.

    The :class:`OperationRunner` builds the Caldera ``POST /api/v2/abilities``
    payload straight from ``ability.executors``, which must be JSON-serialisable.
    The curated :class:`AbilityData` carries Pydantic :class:`Executor` models, so
    this adapter exposes the same duck-typed surface the runner / containment /
    preview consume (``ability_id``/``name``/``executors``) with ``executors`` as
    plain ``{name, platform, command}`` dicts.
    """

    ability_id: str
    name: str | None
    tactic: str | None
    technique_name: str | None
    technique_id: str | None
    description: str | None
    executors: tuple[dict[str, object], ...]


def _runner_abilities(abilities: list[AbilityData]) -> list[_RunnerAbility]:
    """Adapt curated :class:`AbilityData` to runner-facing abilities.

    Converts each Pydantic :class:`Executor` to a plain dict so the runner's
    Caldera payload builder can JSON-serialise it, preserving order and identity.
    """
    return [
        _RunnerAbility(
            ability_id=a.ability_id,
            name=a.name,
            tactic=a.tactic,
            technique_name=a.technique_name,
            technique_id=a.technique_id,
            description=a.description,
            executors=tuple(e.model_dump() for e in a.executors),
        )
        for a in abilities
    ]


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class EmulationRequest(BaseModel):
    """Body of ``POST /api/casos/{caso}/emulacao`` (Req. 6.6, 6.7).

    ``confirmado`` is the **explicit** confirmation flag. The Operation starts
    iff this is exactly ``True``; anything else (absent/false/cancelled) means
    "not confirmed" and nothing runs.
    """

    confirmado: bool = Field(
        default=False,
        description=(
            "Confirmação explícita do Pesquisador. A emulação só inicia com "
            "confirmado=true; caso contrário nenhum comando é executado."
        ),
    )


class PreviewDestinationView(BaseModel):
    """One destination of a command, resolved to its target container (Req. 6.5)."""

    valor: str
    origem: str
    externo: bool
    container: Optional[str] = None
    alvo: str


class CommandPreviewView(BaseModel):
    """A single concrete command and its resolved target(s) (Req. 6.5)."""

    comando: str
    local: bool
    tem_externo: bool
    destinos: list[PreviewDestinationView] = Field(default_factory=list)
    alvos: list[str] = Field(default_factory=list)


class AbilityPreviewView(BaseModel):
    """The preview of every command of a single Ability (Req. 6.5)."""

    ability_id: Optional[str] = None
    ability_name: Optional[str] = None
    tem_externo: bool
    comandos: list[CommandPreviewView] = Field(default_factory=list)


class EmulationPreviewResponse(BaseModel):
    """``POST /api/casos/{caso}/emulacao/preview`` payload (Req. 6.5).

    The complete, concrete list of commands + target container of each, plus a
    presentation-only ``tem_externo`` flag so the modal can highlight any
    external destination. Nothing is executed to build this.
    """

    caso_id: str
    tem_externo: bool
    abilities: list[AbilityPreviewView] = Field(default_factory=list)


class AbilityResultView(BaseModel):
    """One per-Ability result of a run (part of the emulation response)."""

    ability_id: Optional[str] = None
    status: AbilityResultStatus
    comando: Optional[str] = None
    container_destino: str
    saida: Optional[str] = None


class EmulationResponse(BaseModel):
    """``POST /api/casos/{caso}/emulacao`` success payload (COMPLETED/ABORTED).

    Carries the runner's typed outcome, the final Operation state, the aggregate
    (``total_sucesso`` / ``total_falha``, Req. 4.5), the per-Ability results and
    the human-readable message (which, for ABORTED, explains the audit-trail
    failure — Req. 6.9).
    """

    caso_id: str
    iniciada: bool
    resultado: RunOutcome
    estado: OperationState
    operacao_id: Optional[int] = None
    caldera_operacao_id: Optional[str] = None
    total_sucesso: int = 0
    total_falha: int = 0
    resultados: list[AbilityResultView] = Field(default_factory=list)
    mensagem: str = ""


# ---------------------------------------------------------------------------
# Dependencies (overridable for tests — no real Caldera/Docker in dev)
# ---------------------------------------------------------------------------

#: A factory building an :class:`OperationRunner` bound to a request-scoped
#: session. Injected so the handler stays thin and tests can override the whole
#: construction (Caldera client + isolation verifier) via a single dependency.
RunnerFactory = Callable[[Session], OperationRunner]


def get_caldera_client() -> Iterator[CalderaClient]:
    """FastAPI dependency yielding a :class:`CalderaClient`.

    In production this builds a client from default settings (which reads the
    Caldera URL + ``KEY`` header from config). In tests, override this dependency
    with one yielding a :class:`CalderaClient` backed by
    :class:`httpx.MockTransport` so no real Caldera is contacted. The client is
    always closed after the request.
    """
    client = CalderaClient()
    try:
        yield client
    finally:
        client.close()


def get_runner_factory(
    caldera: CalderaClient = Depends(get_caldera_client),
) -> RunnerFactory:
    """FastAPI dependency yielding a runner factory bound to the given Caldera.

    Returns a ``(session) -> OperationRunner`` so the handler builds the runner
    with the **request-scoped** SQLAlchemy session (and its :class:`AuditLogger`)
    while the Caldera client comes from the overridable
    :func:`get_caldera_client`. Tests that need a fake isolation verifier can
    override *this* dependency instead and inject
    ``isolation_verifier=`` into the built runner.
    """

    def _factory(session: Session) -> OperationRunner:
        return OperationRunner(
            session=session,
            caldera=caldera,
            audit_logger=AuditLogger(session),
        )

    return _factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_case_or_error(caso: str, case_service: CaseService) -> CaseData:
    """Resolve a curated case, mapping unknown/failed reads to HTTP errors.

    An unknown slug is a 404; a missing/malformed case file is a descriptive 422
    (mirroring the casos router's per-case handling). The returned
    :class:`CaseData` exposes the Abilities and Adversary the emulation needs.
    """
    if caso not in case_service.list_cases():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Caso desconhecido: {caso!r}.",
        )
    result = case_service.try_load_case(caso)
    if not result.ok or result.case is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                result.error.detail if result.error else f"Falha ao ler {caso!r}."
            ),
        )
    return result.case


def _preview_view(caso: str, preview: EmulationPreview) -> EmulationPreviewResponse:
    """Map the domain :class:`EmulationPreview` to the response model (Req. 6.5)."""
    abilities: list[AbilityPreviewView] = []
    for ability in preview.abilities:
        comandos: list[CommandPreviewView] = []
        for command in ability.commands:
            destinos = [
                PreviewDestinationView(
                    valor=dest.value,
                    origem=dest.raw,
                    externo=dest.is_external,
                    container=dest.container,
                    alvo=dest.target_label,
                )
                for dest in command.destinations
            ]
            comandos.append(
                CommandPreviewView(
                    comando=command.command,
                    local=command.is_local,
                    tem_externo=command.has_external,
                    destinos=destinos,
                    alvos=command.target_labels,
                )
            )
        abilities.append(
            AbilityPreviewView(
                ability_id=ability.ability_id,
                ability_name=ability.ability_name,
                tem_externo=ability.has_external,
                comandos=comandos,
            )
        )
    return EmulationPreviewResponse(
        caso_id=caso,
        tem_externo=preview.has_external,
        abilities=abilities,
    )


def _success_view(caso: str, result: OperationRunResult) -> EmulationResponse:
    """Map a COMPLETED/ABORTED :class:`OperationRunResult` to the response model."""
    return EmulationResponse(
        caso_id=caso,
        iniciada=result.started,
        resultado=result.outcome,
        estado=result.state,
        operacao_id=result.operation_id,
        caldera_operacao_id=result.caldera_operation_id,
        total_sucesso=result.total_sucesso,
        total_falha=result.total_falha,
        resultados=[
            AbilityResultView(
                ability_id=ar.ability_id,
                status=ar.status,
                comando=ar.command,
                container_destino=ar.container_destino,
                saida=ar.output,
            )
            for ar in result.ability_results
        ],
        mensagem=result.message,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/casos/{caso}/emulacao/preview",
    response_model=EmulationPreviewResponse,
    summary="Lista completa de comandos concretos + container de destino (sem executar)",
)
def preview_emulacao(
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
) -> EmulationPreviewResponse:
    """Return the complete command/destination preview for a case (Req. 6.5).

    Builds :func:`preview_abilities` over the case's curated Abilities and maps
    it to the response model. Nothing is executed and no Operation is created —
    this is the presentation the confirmation modal shows *before* any
    confirmation. External destinations are flagged (``tem_externo``) but not
    refused here; refusal is the emulation endpoint's job.
    """
    case_service = CaseService()
    case = _load_case_or_error(caso, case_service)
    preview = preview_abilities(case.abilities)
    return _preview_view(caso, preview)


@router.post(
    "/casos/{caso}/emulacao",
    response_model=EmulationResponse,
    summary="Executa a emulação com pre-flight de contenção (confirmado=true)",
    responses={
        status.HTTP_409_CONFLICT: {
            "description": (
                "Contenção violada — destino externo (Req. 6.2) ou container-alvo "
                "não isolado (Req. 6.4). Nada foi executado."
            )
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Caldera não respondeu em 10s (Req. 4.6). Nada foi executado."
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Caldera respondeu com erro ao receber a operação."
        },
    },
)
def executar_emulacao(
    body: EmulationRequest,
    caso: str = Path(..., description="Slug do caso curado"),
    db: Session = Depends(get_db),
    runner_factory: RunnerFactory = Depends(get_runner_factory),
) -> EmulationResponse:
    """Run the emulation pre-flight + execution for a case (Req. 6.6/6.7/6.2/6.4/4.6).

    Resolves the case's Abilities + Adversary, builds an
    :class:`OperationRunner` bound to the request-scoped session (via the
    overridable :func:`get_runner_factory`), and calls :meth:`OperationRunner.run`
    with the body's ``confirmado`` flag. The runner enforces the four gates in
    order (confirmation -> destinations -> isolation -> availability); this
    handler only maps the typed outcome to HTTP:

    * ``CONTAINMENT_REFUSED`` (external destination) / ``ISOLATION_FAILED``
      (container not isolated) -> **409** with the offending detail.
    * ``CalderaUnavailable`` (propagated from the availability gate) -> **503**.
    * ``NOT_CONFIRMED`` -> **200** with a body indicating nothing started.
    * ``COMPLETED`` / ``ABORTED`` -> **200** with the operation result (ABORTED
      surfaces the audit-failure message).
    """
    case_service = CaseService()
    case = _load_case_or_error(caso, case_service)

    adversary_id = case.adversary.id if case.adversary else ""

    runner = runner_factory(db)
    try:
        result = runner.run(
            caso_id=caso,
            abilities=_runner_abilities(case.abilities),
            adversary_id=adversary_id,
            confirmado=body.confirmado,
        )
    except CalderaUnavailable as exc:
        # Gate 4 (Req. 4.6): Caldera silent within 10s -> 503. Nothing started.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except CalderaApiError as exc:
        # Caldera respondeu, mas recusou/fracassou ao processar o payload. Isso
        # é uma falha do upstream (502), não um erro interno genérico da API.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "motivo": "caldera_api_error",
                "mensagem": str(exc),
                "endpoint": exc.path,
                "status_caldera": exc.status_code,
            },
        ) from exc

    if result.outcome is RunOutcome.CONTAINMENT_REFUSED:
        # Gate 2 (Req. 6.2): external destination -> 409, identifying the
        # offending Ability/command/destination.
        violations = (
            result.containment.describe_violations() if result.containment else []
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "contencao_recusada",
                "mensagem": result.message,
                "violacoes": violations,
            },
        )

    if result.outcome is RunOutcome.ISOLATION_FAILED:
        # Gate 3 (Req. 6.4): a target container is not isolated -> 409,
        # identifying the offending container(s).
        containers = (
            [v.container for v in result.isolation.offending]
            if result.isolation
            else []
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "motivo": "isolamento_falhou",
                "mensagem": result.message,
                "containers": containers,
            },
        )

    # NOT_CONFIRMED (Req. 6.6/6.7): nothing ran — 200 with a "not started" body.
    # COMPLETED / ABORTED: the operation result (ABORTED surfaces its message).
    return _success_view(caso, result)
