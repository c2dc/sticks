"""Lightweight manifest of the curated seed (``app/seeds/data/*.json``).

Design choice: the manifest is a hard-coded constant (slugs, expected counts and
the STIX binding of each case), not derived from the files at runtime. The
ingestion ``plan()`` runs on every start and only needs to compare the database
with these numbers, so it must not open the 8 JSON files.
``tests/test_curated_seed_data.py`` validates this constant against the real
files, so the two cannot drift silently.

STIX binding: every seed file declares, in its ``campaign`` object, the ATT&CK
version the case was curated against (``stix_version``) and the STIX campaign
external id (``stix_campaign_key``, e.g. ``C0045``). They are mirrored here as
:data:`STIX_VERSION_BY_SLUG` and :data:`STIX_CAMPAIGN_KEYS`; this module is the
single source of truth in code for the curated STIX version
(:data:`CURATED_STIX_VERSION`) and for the versions the ``stix_packages``
ingestion must always keep installed (:data:`REQUIRED_STIX_VERSIONS`).
"""

from __future__ import annotations

CAMPAIGN_SLUGS: tuple[str, ...] = (
    "apt41_dust",
    "c0010",
    "c0026",
    "costaricto",
    "operation_midnighteclipse",
    "outer_space",
    "salesforce_data_exfiltration",
    "shadowray",
)

EXPECTED_COUNTS: dict[str, int] = {
    "campaigns": 8,
    "abilities": 108,
    "adversaries": 8,
    "attack_flows": 8,
}

# slug -> ATT&CK Enterprise version the case was curated against.
STIX_VERSION_BY_SLUG: dict[str, str] = {
    "apt41_dust": "18.0",
    "c0010": "18.0",
    "c0026": "18.0",
    "costaricto": "18.0",
    "operation_midnighteclipse": "18.0",
    "outer_space": "18.0",
    "salesforce_data_exfiltration": "18.0",
    "shadowray": "18.0",
}

# slug -> STIX campaign external id (``stix_campaigns.external_id``).
STIX_CAMPAIGN_KEYS: dict[str, str] = {
    "apt41_dust": "C0040",
    "c0010": "C0010",
    "c0026": "C0026",
    "costaricto": "C0004",
    "operation_midnighteclipse": "C0048",
    "outer_space": "C0042",
    "salesforce_data_exfiltration": "C0059",
    "shadowray": "C0045",
}


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


# Versions the seed needs installed, oldest first (today only ``18.0``).
REQUIRED_STIX_VERSIONS: tuple[str, ...] = tuple(
    sorted(set(STIX_VERSION_BY_SLUG.values()), key=_version_key)
)

# The version the curated cases are bound to. All 8 cases share one version
# (pinned by a test); when they ever differ this is the oldest required one.
CURATED_STIX_VERSION: str = REQUIRED_STIX_VERSIONS[0]
