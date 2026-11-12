#!/usr/bin/env python
"""Read-only exporter: generate the curated seed JSON files from the database.

Writes one file per curated campaign, ``<output-dir>/<slug>.json`` (default
``apps/backend/app/seeds/data``), where the slug is ``campaigns.id``. Each file::

    {
      "campaign": {...},
      "abilities": [...],
      "adversary": {...},
      "attack_flow": {...}
    }

``campaign`` has no ``curated`` flag and ``attack_flow`` has no ``origin``: the
seed applier always stores ``curated = true`` and ``origin = human_curation``.
The flow ``content`` keeps only ``campaign_name``, ``edges`` and
``structural_nodes``; each node keeps only ``node_id``, ``node_type``,
``technique_id``, ``technique_name`` and ``tactic``.

Seed JSON is read-only, versioned initial data; runtime storage is the database.

Reads with plain ``SELECT`` statements inside a ``READ ONLY`` transaction. The
database is never modified. The connection comes from the backend settings
(``PIPELINE_UI_DATABASE_URL`` / ``.env``); the URL is never printed.

Supported source schemas, detected automatically:

* legacy (alembic ``b9c0d1e2f3a4`` and older): table ``dags``; ``campaigns``
  carries removed file-path columns, which are ignored. Each ``dags`` row
  becomes an attack flow with ``origin=human_curation``; ``curated`` is true.
* current: table ``attack_flows``; campaigns with ``curated`` true and flows
  with ``origin=human_curation``.

Only the most recent human-curation flow of each campaign is kept. The flow
``content`` loses the dead keys listed in ``KNOWN_CORRECTIONS``; legacy
``arestas`` {de,para} become ``edges`` {from,to}.

Output stability (small git diffs, byte-identical re-runs): ``indent=2``,
``sort_keys=True``, ``ensure_ascii=False``, UTF-8, LF, trailing newline, no
timestamps. Ordering: ``abilities`` are sorted by ``ability_id`` (the adversary
keeps its own order in ``atomic_ordering``).

KNOWN CORRECTIONS (explicit list ``KNOWN_CORRECTIONS`` below, applied while
exporting, each only when its precondition holds):

(a) ``outer_space`` adversary: ``atomic_ordering`` references the nonexistent
    id ``6afb1856-6e5a-5f9f-8f8c-rothos``; it is replaced by
    ``a8de43be-fcde-5302-9cfd-rothos`` ("END OF OUTER SPACE", T1529), the only
    outer_space ability missing from the ordering. Applied only if the orphan
    id is present and the target ability exists.
(b) Every attack flow node is reduced to ``node_id``, ``node_type``,
    ``technique_id``, ``technique_name`` and ``tactic``; all other node keys
    (including the obsolete ``ai_prompt_template``) are dead and removed.
(c) The flow ``content`` is reduced to ``campaign_name``, ``edges`` and
    ``structural_nodes``; ``capability_flow``, ``metadata``, ``campaign_file``
    and ``generated_at`` are removed.
(d) The top-level ``environment_parameters``/``provisioning_map`` keys,
    ``campaign.curated`` and ``attack_flow.origin`` are not exported.

Usage (from the repository root, with the backend virtualenv)::

    apps/backend/.venv/Scripts/python ops/database/scripts/export_curated_seed.py \
        [--output-dir apps/backend/app/seeds/data]

Exit codes: 0 ok, 1 count mismatch or unsupported schema.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "apps" / "backend"
DEFAULT_OUTPUT_DIR = BACKEND_DIR / "app" / "seeds" / "data"

# Expected counts of the curated corpus (task 24).
EXPECTED_COUNTS = {"campaigns": 8, "abilities": 108, "adversaries": 8, "attack_flows": 8}

# Known corrections (see module docstring). Explicit and documented.
KNOWN_CORRECTIONS: dict[str, Any] = {
    # (a) campaign slug -> {orphan id: replacement ability id} in atomic_ordering
    "atomic_ordering_replacements": {
        "outer_space": {
            "6afb1856-6e5a-5f9f-8f8c-rothos": "a8de43be-fcde-5302-9cfd-rothos",
        },
    },
    # (b) the only node keys kept (everything else has no live reader)
    "node_fields_kept": (
        "node_id",
        "node_type",
        "technique_id",
        "technique_name",
        "tactic",
    ),
    # (c) the only flow content keys kept
    "flow_content_keys_kept": ("campaign_name", "edges", "structural_nodes"),
}


def _rows(conn: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    from sqlalchemy import text

    return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _normalize_flow_content(content: dict[str, Any]) -> dict[str, Any]:
    """Map legacy ``arestas`` {de,para} to ``edges``; key pruning is a correction."""
    out = dict(content)
    if "arestas" in out:
        legacy = out.pop("arestas") or []
        if not out.get("edges"):
            out["edges"] = [
                {"from": e["de"], "to": e["para"]}
                for e in legacy
                if isinstance(e, dict) and e.get("de") and e.get("para")
            ]
    return out


def apply_known_corrections(case: dict[str, Any]) -> list[str]:
    """Apply KNOWN_CORRECTIONS to one case in place; return what was applied."""
    applied: list[str] = []
    slug = case["campaign"]["id"]

    # (a) orphan ids in atomic_ordering
    replacements = KNOWN_CORRECTIONS["atomic_ordering_replacements"].get(slug, {})
    ability_ids = {a["ability_id"] for a in case["abilities"]}
    ordering = case["adversary"].get("atomic_ordering")
    if isinstance(ordering, list):
        for orphan, target in replacements.items():
            if orphan in ordering and orphan not in ability_ids and target in ability_ids:
                case["adversary"]["atomic_ordering"] = [
                    target if x == orphan else x for x in ordering
                ]
                ordering = case["adversary"]["atomic_ordering"]
                applied.append(f"{slug}: atomic_ordering {orphan} -> {target}")

    # (b) keep only the live node keys; (c) keep only the live flow content keys
    node_keep = KNOWN_CORRECTIONS["node_fields_kept"]
    content_keep = KNOWN_CORRECTIONS["flow_content_keys_kept"]
    content = case["attack_flow"]["content"]
    removed_node_fields = 0
    for node in content.get("structural_nodes") or []:
        for key in [k for k in node if k not in node_keep]:
            del node[key]
            removed_node_fields += 1
    removed_content_keys = [k for k in content if k not in content_keep]
    for key in removed_content_keys:
        del content[key]
    if removed_node_fields:
        applied.append(f"{slug}: removed {removed_node_fields} dead node field(s)")
    if removed_content_keys:
        applied.append(f"{slug}: removed flow content key(s) {sorted(removed_content_keys)}")
    return applied


def read_database() -> dict[str, Any]:
    """Collect the seed data with SELECT statements only, in a READ ONLY transaction."""
    sys.path.insert(0, str(BACKEND_DIR))
    # The backend settings read ``.env`` relative to the working directory.
    os.chdir(BACKEND_DIR)
    import sqlalchemy as sa
    from app.core.config import get_settings

    engine = sa.create_engine(
        get_settings().database_url, connect_args={"connect_timeout": 10}
    )
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                # Must be the first statement of the transaction.
                conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            try:
                return _collect(conn)
            finally:
                conn.rollback()
    finally:
        engine.dispose()


def _collect(conn: Any) -> dict[str, Any]:
    tables = {
        r["table_name"]
        for r in _rows(
            conn,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()",
        )
    }
    legacy = "dags" in tables
    if not legacy and "attack_flows" not in tables:
        raise SystemExit("unsupported schema: neither 'dags' nor 'attack_flows' found")

    revision = None
    if "alembic_version" in tables:
        rows = _rows(conn, "SELECT version_num FROM alembic_version")
        revision = rows[0]["version_num"] if rows else None

    if legacy:
        campaigns = _rows(
            conn,
            "SELECT id, name, description, translation_source FROM campaigns "
            "WHERE translation_source = 'human_curation' ORDER BY id",
        )
        flow_table = "dags"
    else:
        campaigns = _rows(
            conn,
            "SELECT id, name, description, translation_source FROM campaigns "
            "WHERE curated IS TRUE ORDER BY id",
        )
        flow_table = "attack_flows"
    ids = [c["id"] for c in campaigns]

    def per_campaign(sql: str) -> list[dict[str, Any]]:
        return [r for r in _rows(conn, sql) if r["campaign_id"] in ids]

    abilities = per_campaign(
        "SELECT ability_id, campaign_id, name, tactic, technique_name, technique_id, "
        "description, executors, translation_source FROM abilities "
        "ORDER BY campaign_id, ability_id"
    )
    adversaries = per_campaign(
        "SELECT id, campaign_id, name, description, atomic_ordering, translation_source "
        "FROM adversaries ORDER BY campaign_id, id"
    )
    raw_flows = per_campaign(
        f"SELECT id, campaign_id, identifier, label, content, created_at "
        f"FROM {flow_table} WHERE origin = 'human_curation' ORDER BY campaign_id, id"
    )
    # Superseded flows (older rows of the same campaign) are not exported:
    # keep the most recent human-curation flow of each campaign.
    latest: dict[str, dict[str, Any]] = {}
    for row in raw_flows:
        best = latest.get(row["campaign_id"])
        if best is None or (row["created_at"], row["id"]) > (best["created_at"], best["id"]):
            latest[row["campaign_id"]] = row

    cases: list[dict[str, Any]] = []
    for c in campaigns:
        slug = c["id"]
        c_abilities = sorted(
            (
                {k: v for k, v in a.items()}
                for a in abilities
                if a["campaign_id"] == slug
            ),
            key=lambda a: a["ability_id"],
        )
        c_adversaries = [a for a in adversaries if a["campaign_id"] == slug]
        flow = latest.get(slug)
        if len(c_adversaries) != 1 or flow is None:
            raise SystemExit(
                f"campaign {slug}: expected 1 adversary and 1 flow, "
                f"got {len(c_adversaries)} adversaries, flow={'yes' if flow else 'no'}"
            )
        cases.append(
            {
                "campaign": {
                    "id": slug,
                    "name": c["name"],
                    "description": c["description"],
                    "translation_source": c["translation_source"],
                },
                "abilities": c_abilities,
                "adversary": c_adversaries[0],
                "attack_flow": {
                    "campaign_id": slug,
                    "identifier": flow["identifier"],
                    "label": flow["label"],
                    "content": _normalize_flow_content(flow["content"]),
                },
            }
        )

    return {
        "schema": "legacy" if legacy else "current",
        "revision": revision,
        "dropped_flows": len(raw_flows) - len(latest),
        "cases": cases,
    }


def counts_of(data: dict[str, Any]) -> dict[str, int]:
    cases = data["cases"]
    return {
        "campaigns": len(cases),
        "abilities": sum(len(c["abilities"]) for c in cases),
        "adversaries": len(cases),
        "attack_flows": len(cases),
    }


def render_case(case: dict[str, Any]) -> str:
    """Render one seed file. Deterministic: same data, same bytes."""
    return json.dumps(case, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    args.output_dir = args.output_dir.resolve()  # before read_database() changes the cwd

    data = read_database()
    for case in data["cases"]:
        for line in apply_known_corrections(case):
            print(f"correction: {line}")
    counts = counts_of(data)
    print(f"source schema: {data['schema']} (revision {data['revision']})")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    print(f"  superseded flows skipped: {data['dropped_flows']}")

    bad = {k: (counts[k], v) for k, v in EXPECTED_COUNTS.items() if counts[k] != v}
    if bad:
        for k, (got, want) in bad.items():
            print(f"count mismatch for {k}: got {got}, expected {want}", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for case in data["cases"]:
        path = args.output_dir / f"{case['campaign']['id']}.json"
        path.write_text(render_case(case), encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
