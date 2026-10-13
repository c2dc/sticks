"""REST endpoints for the STIX intelligence flow (task 5.1).

Wires the ``/api/stix/*`` contracts from the design's "Endpoints REST
(contratos em pt-BR)" section to :class:`~app.services.stix.StixService`.
Handlers are kept **thin**: each one constructs a ``StixService()`` and
delegates, returning the service's already-typed Pydantic results (wrapped in a
small response model where the design envelopes the list under a pt-BR key).

Endpoints implemented here (Req. 2.1, 2.5, 2.7, 3.1, 3.2, 3.5, 4.2, 4.3, 4.5):

* ``GET  /api/stix/bases`` — the known ATT&CK Enterprise bases, newest→oldest,
  each flagged ``baixada``/``selecionavel`` by live disk presence. Side-effect
  free: no network egress (Req. 2.1).
* ``GET  /api/stix/bases/{versao}/dashboard`` — per-type intelligence counts for
  a selected base. A bundle read/parse failure maps to the
  ``{"detail": {"motivo": "bundle_invalido", ...}}`` 422 contract (Req. 3.1,
  3.2, 3.5). Side-effect free.
* ``GET  /api/stix/bases/{versao}/campanhas`` — the ``campaign`` objects with the
  curated-case flag, enveloped as ``{"versao", "campanhas"}`` (Req. 4.2, 4.5).
  Side-effect free; same 422 contract on an invalid bundle.
* ``GET  /api/stix/bases/{versao}/atores`` — the ``intrusion-set`` actors with
  their attributed campaigns, enveloped as ``{"versao", "atores"}`` (Req. 4.3).
  Side-effect free; same 422 contract on an invalid bundle.
* ``POST /api/stix/bases/{versao}/download`` — the ONLY route with a side effect:
  the single network egress, triggered only by this explicit POST. A download
  failure maps to ``{"detail": {"motivo": "falha_download", ...}}`` — 422 when
  the version is unknown (rejected before touching disk/network), 502 for a
  network/content/write failure (Req. 2.5, 2.7).

This module does NOT register itself with the root router — that is task 5.2.

_Requisitos: 2.1, 2.5, 2.7, 3.1, 3.2, 3.5, 4.2, 4.3, 4.5_
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, status
from pydantic import BaseModel, Field

from app.services.stix.stix_service import (
    AtorStix,
    BaseStixInfo,
    CampanhaStix,
    DashboardStix,
    StixDownloadError,
    StixFileError,
    StixService,
)

router = APIRouter(prefix="/api", tags=["stix"])


# ---------------------------------------------------------------------------
# Response envelopes (small wrappers around the service list results so the
# JSON matches the design's pt-BR contracts: a named key per collection).
# ---------------------------------------------------------------------------


class BasesResponse(BaseModel):
    """``GET /api/stix/bases`` payload — the known bases listing (Req. 2.1)."""

    bases: list[BaseStixInfo] = Field(default_factory=list)


class CampanhasResponse(BaseModel):
    """``GET /api/stix/bases/{versao}/campanhas`` payload (Req. 4.2, 4.5)."""

    versao: str
    campanhas: list[CampanhaStix] = Field(default_factory=list)


class AtoresResponse(BaseModel):
    """``GET /api/stix/bases/{versao}/atores`` payload (Req. 4.3)."""

    versao: str
    atores: list[AtorStix] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/stix/bases",
    response_model=BasesResponse,
    summary="Lista as bases MITRE ATT&CK conhecidas (mais nova → mais antiga)",
)
def listar_bases() -> BasesResponse:
    """List the known ATT&CK Enterprise bases, newest→oldest (Req. 2.1).

    Delegates to :meth:`StixService.listar_bases`, which enumerates the
    backend-maintained version index and flags each base ``baixada`` /
    ``selecionavel`` from live disk presence. READ-ONLY and side-effect free:
    no network egress (the only egress is ``POST .../download``).
    """
    return BasesResponse(bases=StixService().listar_bases())


@router.get(
    "/stix/bases/{versao}/dashboard",
    response_model=DashboardStix,
    summary="Dashboard de inteligência (contagens por tipo) da base selecionada",
)
def obter_dashboard(
    versao: str = Path(..., description="Versão da base ATT&CK Enterprise (ex.: 19.1)"),
) -> DashboardStix:
    """Return the per-type intelligence counts for the selected base (Req. 3.1, 3.2).

    Delegates to :meth:`StixService.dashboard`. READ-ONLY and side-effect free.
    A bundle read/parse failure (missing / malformed / invalid structure) is a
    typed :class:`StixFileError`, mapped to the ``bundle_invalido`` 422 contract
    so no partial set of counts is ever returned as if valid (Req. 3.5).
    """
    try:
        return StixService().dashboard(versao)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc


@router.get(
    "/stix/bases/{versao}/campanhas",
    response_model=CampanhasResponse,
    summary="Lista as campanhas da base, com a marcação de caso curado",
)
def listar_campanhas(
    versao: str = Path(..., description="Versão da base ATT&CK Enterprise (ex.: 19.1)"),
) -> CampanhasResponse:
    """Return the ``campaign`` objects of the base with the curated flag (Req. 4.2, 4.5).

    Delegates to :meth:`StixService.listar_campanhas`. READ-ONLY and side-effect
    free. An invalid bundle surfaces the ``bundle_invalido`` 422 contract.
    """
    try:
        campanhas = StixService().listar_campanhas(versao)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc
    return CampanhasResponse(versao=versao, campanhas=campanhas)


@router.get(
    "/stix/bases/{versao}/atores",
    response_model=AtoresResponse,
    summary="Lista os atores (intrusion-sets) com suas campanhas atribuídas",
)
def listar_atores(
    versao: str = Path(..., description="Versão da base ATT&CK Enterprise (ex.: 19.1)"),
) -> AtoresResponse:
    """Return the ``intrusion-set`` actors with attributed campaigns (Req. 4.3).

    Delegates to :meth:`StixService.listar_atores`. READ-ONLY and side-effect
    free. An invalid bundle surfaces the ``bundle_invalido`` 422 contract.
    """
    try:
        atores = StixService().listar_atores(versao)
    except StixFileError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"motivo": "bundle_invalido", "mensagem": exc.detail},
        ) from exc
    return AtoresResponse(versao=versao, atores=atores)


@router.post(
    "/stix/bases/{versao}/download",
    response_model=BaseStixInfo,
    summary="Baixa a base ATT&CK Enterprise (única saída de rede)",
)
def baixar_base(
    versao: str = Path(..., description="Versão da base ATT&CK Enterprise (ex.: 19.1)"),
) -> BaseStixInfo:
    """Download and persist the ``enterprise-attack-{versao}`` bundle (Req. 2.5, 2.7).

    This is the ONLY route with a side effect: the single network egress,
    triggered exclusively by this explicit POST. Delegates to
    :meth:`StixService.baixar_base`, which validates ``versao`` against the known
    index *before* touching disk/network and writes atomically. On failure it
    raises a typed :class:`StixDownloadError`, mapped to the ``falha_download``
    contract: HTTP 422 when the version is unknown (rejected pre-flight), HTTP
    502 for a network/content/write failure.
    """
    try:
        return StixService().baixar_base(versao)
    except StixDownloadError as exc:
        # Unknown version → 422 (rejected before any disk/network access);
        # any network/content/write failure → 502 (upstream/egress failure).
        from app.services.stix.stix_service import VERSOES_CONHECIDAS

        desconhecida = exc.versao is not None and exc.versao not in VERSOES_CONHECIDAS
        codigo = (
            status.HTTP_422_UNPROCESSABLE_ENTITY
            if desconhecida
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(
            status_code=codigo,
            detail={"motivo": "falha_download", "mensagem": exc.detail},
        ) from exc
