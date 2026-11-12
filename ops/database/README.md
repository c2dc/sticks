# ops/database

Platform-agnostic PostgreSQL helpers, usable with Docker, Kubernetes or AWS. They connect through
the standard libpq variables (`PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD`); no
credentials are stored in this repository.

| Path | Purpose |
| --- | --- |
| `sql/bootstrap.sql` | Idempotent `CREATE EXTENSION IF NOT EXISTS vector;` |
| `scripts/bootstrap.sh`, `.ps1` | Apply `bootstrap.sql` with `psql` |
| `scripts/backup.sh`, `.ps1` | `pg_dump` in custom format to `BACKUP_DIR` (default `./backups`) |
| `scripts/restore.sh`, `.ps1` | `pg_restore --clean --if-exists` from a dump file |

Notes:

- The Docker stack uses the `pgvector/pgvector:pg16` image and the Alembic migrations already enable
  the extension. The bootstrap is only needed to prepare a database ahead of migrations (for
  example, on a managed service).
- Restore drops existing objects before recreating them. Check `PGHOST` and `PGDATABASE` first.

## Curated cases seed

A fresh installation starts with an empty database. Runtime data lives in the relational and vector
databases; the 8 curated campaigns come from versioned, read-only seed JSON files in the backend
(`apps/backend/app/seeds/data/<slug>.json`, one per campaign, no SQL dump), loaded through
`importlib.resources`. They are applied by the `curated_seed` ingestion source at the first start, or
manually from `apps/backend` with `python -m app.seeds.curated` (idempotent; it never prints the
database URL).

| Path | Purpose |
| --- | --- |
| `scripts/export_curated_seed.py` | Read-only exporter. Reads the curated cases from a database (`SELECT` in a `READ ONLY` transaction, legacy or current schema) and writes the 8 files `<slug>.json` into `--output-dir` (default `apps/backend/app/seeds/data`). Fails if the counts differ from 8 campaigns, 108 abilities and 8 adversaries. |

```bash
# from the repository root, with the backend virtualenv (Scripts/python on Windows)
apps/backend/.venv/bin/python ops/database/scripts/export_curated_seed.py \
    --output-dir apps/backend/app/seeds/data
```

The connection comes from the backend settings (`PIPELINE_UI_DATABASE_URL` or `.env`). Output is
deterministic (sorted keys, indent 2, LF), so re-runs are byte-identical.

Known corrections applied on export (`KNOWN_CORRECTIONS` in the script): the `outer_space` adversary
`atomic_ordering` orphan id `6afb1856-6e5a-5f9f-8f8c-rothos` is replaced by
`a8de43be-fcde-5302-9cfd-rothos` (ordering fixed), and every flow node is pruned to `node_id`, `node_type`, `technique_id`, `technique_name` and `tactic` (the flow content to `campaign_name`, `edges` and `structural_nodes`). The files have only the keys `campaign`, `abilities`, `adversary` and `attack_flow`; `campaign.curated` and `attack_flow.origin` are not exported (the applier sets `curated = true` and `origin = human_curation`). An
ability of `costaricto` that references `shadowray` is valid: abilities are resolved globally.

## Backup and restore with the seed

- A backup taken with `backup.*` already contains the curated rows, so a restore needs no seed.
- After restoring into an empty database (or a dump taken before the curated cases existed), run the
  backend or `python -m app.seeds.curated`: the upsert only adds what is missing and leaves AI-generated
  or edited rows untouched.
- Restore drops existing objects, so any edits made to curated rows since the dump are lost. Export the
  seed first if you want to keep a JSON snapshot of them.

Example:

```bash
export PGHOST=localhost PGPORT=5432 PGUSER=... PGDATABASE=... PGPASSWORD=...
./scripts/backup.sh
./scripts/restore.sh backups/<file>.dump
```