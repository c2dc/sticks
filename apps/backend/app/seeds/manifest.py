"""Lightweight manifest of the curated seed (``app/seeds/data/*.json``).

Design choice: the manifest is a hard-coded constant (slugs + expected counts),
not derived from the files at runtime. The ingestion ``plan()`` runs on every
start and only needs to compare the database with these numbers, so it must not
open the 8 JSON files. ``tests/test_curated_seed_data.py`` validates this
constant against the real files, so the two cannot drift silently.
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
