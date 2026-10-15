"""STIX intelligence services.

``StixService`` (task 1.1 skeleton) locates, lists, downloads and analyses the
MITRE ATT&CK ``enterprise-attack`` STIX 2.x bundles that live under
``sticks/data/stix``. It powers the new flow's inspection endpoints
(``/api/stix/*``): the bases listing (Req. 2), the intelligence dashboard
(Req. 3) and the campaign/actor selection (Req. 4).

This package exposes the typed pt-BR response models (``BaseStixInfo``,
``ContagemTipo``, ``DashboardStix``, ``CampanhaStix``, ``AtorStix``), the
backend-maintained constants (``VERSOES_CONHECIDAS``, ``DESCRICOES_TIPO``,
``SLUG_PARA_STIX``) and the service itself. Behaviour is implemented across
tasks 1.2 (listar_bases), 2.x (ler_bundle/dashboard), 3.x (campanhas/atores) and
4.x (baixar_base).

The Estágio 2 thin orchestrator (``Estagio2Service``, task 3.1) is **not**
re-exported here on purpose: it imports ``StageService`` (which in turn imports
from :mod:`app.services.stix.stix_service`), so eagerly importing it in this
package ``__init__`` would create an import cycle. Import it from its submodule
instead (``from app.services.stix.estagio2_service import Estagio2Service``).
"""

from app.services.stix.stix_service import (
    DESCRICOES_TIPO,
    ROTULO_TIPO,
    SLUG_PARA_STIX,
    VERSOES_CONHECIDAS,
    AtorStix,
    BaseStixInfo,
    CampanhaStix,
    ContagemTipo,
    DashboardStix,
    StixService,
)

__all__ = [
    "StixService",
    # Response models (pt-BR contracts)
    "BaseStixInfo",
    "ContagemTipo",
    "DashboardStix",
    "CampanhaStix",
    "AtorStix",
    # Backend-maintained constants
    "VERSOES_CONHECIDAS",
    "DESCRICOES_TIPO",
    "ROTULO_TIPO",
    "SLUG_PARA_STIX",
]
