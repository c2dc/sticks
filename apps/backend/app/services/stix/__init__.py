"""STIX intelligence services.

``StixService`` locates, lists, downloads and analyses the MITRE ATT&CK
``enterprise-attack`` STIX 2.x bundles that live under ``sticks/data/stix``. It
powers the flow's inspection endpoints (``/api/stix/*``): the bases listing
(Req. 2), the intelligence dashboard (Req. 3) and the campaign/adversary
selection (Req. 4).

This package exposes the typed response models (``BaseStixInfo``, ``TypeCount``,
``DashboardStix``, ``CampaignStix``, ``AdversaryStix``), the backend-maintained
constants (``VERSOES_CONHECIDAS``, ``DESCRICOES_TIPO``, ``SLUG_PARA_STIX``) and
the service itself.

The Stage 2 thin orchestrator (``Stage2Service``) is **not** re-exported here on
purpose: it imports ``StageService`` (which in turn imports from
:mod:`app.services.stix.stix_service`), so eagerly importing it in this package
``__init__`` would create an import cycle. Import it from its submodule instead
(``from app.services.stix.estagio2_service import Stage2Service``).
"""

from app.services.stix.stix_service import (
    DESCRICOES_TIPO,
    ROTULO_TIPO,
    SLUG_PARA_STIX,
    VERSOES_CONHECIDAS,
    AdversaryStix,
    BaseStixInfo,
    CampaignStix,
    DashboardStix,
    StixService,
    TypeCount,
)

__all__ = [
    "StixService",
    # Response models
    "BaseStixInfo",
    "TypeCount",
    "DashboardStix",
    "CampaignStix",
    "AdversaryStix",
    # Backend-maintained constants
    "VERSOES_CONHECIDAS",
    "DESCRICOES_TIPO",
    "ROTULO_TIPO",
    "SLUG_PARA_STIX",
]
