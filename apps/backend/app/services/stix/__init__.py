"""STIX intelligence services.

``StixService`` downloads (in memory, never to disk) and analyses the MITRE
ATT&CK ``enterprise-attack`` STIX 2.x bundles; the derived content is persisted
in the database by ``StixPersistenceService`` through the ``stix_packages``
ingestion source and served by ``StixPackageReadService`` on the ``/api/stix/*``
endpoints.

This package exposes the typed response models (``BaseStixInfo``, ``TypeCount``,
``DashboardStix``, ``CampaignStix``, ``AdversaryStix``), the backend-maintained
constants (``KNOWN_VERSIONS``, ``TYPE_DESCRIPTIONS``, ``SLUG_TO_STIX``) and
the service itself.

"""

from app.services.stix.stix_service import (
    TYPE_DESCRIPTIONS,
    ROTULO_TIPO,
    SLUG_TO_STIX,
    KNOWN_VERSIONS,
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
    "KNOWN_VERSIONS",
    "TYPE_DESCRIPTIONS",
    "ROTULO_TIPO",
    "SLUG_TO_STIX",
]
