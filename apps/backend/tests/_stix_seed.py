"""Shared test helper: seed persisted STIX campaigns for the DB-backed guard.

The campaigns / stages API guard (``_known_campaign_or_404`` in
``app/api/campaigns.py``, and the same guard applied in ``app/api/ai.py``)
validates a campaign against the persisted ``stix_campaigns`` rows instead of
the 8 curated slugs (Req. 2.2, 2.3, 4.4). The HTTP-surface tests exercise real
campaign keys through that guard, so their ephemeral DBs must carry the matching
``stix_campaigns`` rows for the guard to accept them — otherwise every stage
request would 404.

This helper seeds, idempotently, one ``stix_packages`` row for the curated
version plus one ``stix_campaigns`` row per requested campaign key. A key is
stored as the ``curated_slug`` when it is one of the 8 curated slugs (so the
Campaign_Key resolves to the slug), otherwise as a plain ``stix_id`` (a
non-curated persisted campaign). Both cases pass the guard, matching the
production invariant that all 52 campaigns are persisted.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.domain import StixCampaign, StixPackage
from app.services.campaign_service import CURATED_CASES
from app.services.stix.stix_service import CURATED_VERSION


def ensure_stix_campaigns(
    session_factory: sessionmaker[Session],
    campaign_keys: Iterable[str],
    *,
    version: str = CURATED_VERSION,
) -> None:
    """Ensure each campaign key is persisted as a ``stix_campaigns`` row.

    Idempotent and self-healing: it creates the ``stix_packages`` row for
    ``version`` when missing and adds only the ``stix_campaigns`` rows that are
    not yet present. A key that is a curated slug is stored with
    ``curated=True`` + ``curated_slug=key``; any other key is stored as a plain
    ``stix_id`` (non-curated). In both cases the resolved Campaign_Key equals the
    key, so the DB-backed guard accepts it.
    """
    session = session_factory()
    try:
        package_id = session.execute(
            select(StixPackage.id).where(StixPackage.version == version)
        ).scalar_one_or_none()
        if package_id is None:
            package = StixPackage(
                version=version,
                name=f"Enterprise ATT&CK v{version}",
            )
            session.add(package)
            session.flush()
            package_id = package.id

        # Existing rows of the package, so the Campaign_Key dedup matches the
        # same (curated_slug or stix_id) rule the read service exposes. A test
        # that already persisted a richer campaign for a key (with techniques)
        # must not be shadowed by a bare seeded row for the same key.
        existing = (
            session.execute(
                select(StixCampaign.stix_id, StixCampaign.curated_slug).where(
                    StixCampaign.package_id == package_id
                )
            )
            .all()
        )
        existing_keys = {
            curated_slug if curated_slug else stix_id
            for stix_id, curated_slug in existing
        }

        seen_in_batch: set[str] = set()
        for key in campaign_keys:
            if key in seen_in_batch or key in existing_keys:
                continue
            seen_in_batch.add(key)
            is_curated = key in CURATED_CASES
            session.add(
                StixCampaign(
                    package_id=package_id,
                    stix_id=f"campaign--seed-{key}" if is_curated else key,
                    name=CURATED_CASES.get(key, key),
                    curated=is_curated,
                    curated_slug=key if is_curated else None,
                )
            )
        session.commit()
    finally:
        session.close()
