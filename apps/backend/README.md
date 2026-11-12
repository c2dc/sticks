# Pipeline UI — Backend (FastAPI)

Backend for the project's **Pipeline UI** (`sticks`). It orchestrates the
existing three-stage pipeline, exposes the REST/WebSocket API to the frontend,
enforces the containment checks, and persists state.

This project is **additive**: it reuses (without duplicating) the code and
configuration of the existing `sticks/` package at the repository root.

## Structure

```
apps/backend/
  app/
    core/       # configuration + integration with the existing sticks pipeline
    models/     # SQLAlchemy domain models
    services/   # application services (CampaignService, StageService, ...)
    api/        # REST routes
    ws/         # WebSocket/SSE channels
    main.py     # app factory + root router
  tests/
  pyproject.toml
```

## Development (native hot-reload)

The project uses **PostgreSQL only** — there is no SQLite anywhere. The
infrastructure (including Postgres) runs via Docker; the backend and frontend
run natively on the host with hot-reload, pointing at the containerized Postgres.

```bash
# 1. start the infrastructure (includes Postgres published on 127.0.0.1:5432)
cd ops/docker && docker compose up -d

# 2. setup (installation): prepare the native environment once (backend venv + node_modules)
#    from the repository root
npm run setup

# 3. run frontend + backend natively with hot-reload, against the containerized
#    Postgres (applies the Alembic migrations before serving)
npm run dev
```

- UI: http://localhost:8080
- API: http://localhost:9080 — health check: `GET /health` → `{"status": "ok"}`

The default database (`PIPELINE_UI_DATABASE_URL`) already points at the
containerized Postgres on `localhost:5432`. The compose `backend` service
(`containers` profile, opt-in) overrides the host to `postgres:5432` and the
Caldera URL to `http://caldera-c2:8888` (it joins `caldera-mgmt-network`).
Caldera is reached through `PIPELINE_UI_CALDERA_URL` (default `http://localhost:8888`)
and `PIPELINE_UI_CALDERA_KEY` (default `ADMIN123`, sent as the `KEY` header).

### STIX packages (database only)

No STIX bundle is stored on disk. The `stix_packages` ingestion source downloads
`enterprise-attack-{version}.json` from MITRE into memory and persists it in the
database (`stix_packages` and related tables), idempotently per version. It runs
in the background on startup (disable with `STICKS_INGEST_ON_START=0`) and when a
version that is not ingested yet is chosen in the UI
(`POST /api/stix/bases/{version}/download`, `202`).

Versions kept ingested (a union):

- the newest ATT&CK Enterprise version, the default. It is resolved by asking the
  upstream index (`index.json` of `mitre-attack/attack-stix-data`); offline or on
  error it falls back to `max(KNOWN_VERSIONS)`. `STIX_VERSIONS` (comma or
  whitespace separated, `latest` allowed, e.g. `STIX_VERSIONS=19.1,latest`)
  replaces only this item;
- the version(s) required by the curated cases, declared in the seed
  (`campaign.stix_version`, today `18.0`; `app.seeds.manifest.REQUIRED_STIX_VERSIONS`).
  `STIX_VERSIONS` never removes them;
- every version already in the database.

Each version is about 50 MB and the first start of a fresh installation downloads
two (the newest and 18.0). A failing version leaves the source `failed` (see
`GET /api/ingestion/status`) and the next run fetches only what is missing.
`plan` never uses the network; only `run` queries the upstream index.

### Curated cases seed (fresh installation)

Principle: runtime data lives in the relational and vector databases. The seed is
versioned, read-only JSON, one file per campaign, in `app/seeds/data/<slug>.json`
(8 files, about 124 KB in total). Each file has exactly four top-level keys: `campaign`, `abilities`, `adversary` and `attack_flow`. The `campaign` object declares the STIX binding of the case: `stix_version` (the ATT&CK version it was curated against, `18.0`) and `stix_campaign_key` (the STIX campaign external id, e.g. `C0045`); both are validated by the loader and not stored in the database. The files carry no `curated` flag and no flow `origin`: the applier (`app/seeds/apply.py`) always stores `curated = true` and `origin = human_curation`. The flow `content` has only `campaign_name`, `edges` ({from, to}) and `structural_nodes`; each node has only `node_id`, `node_type`, `technique_id`, `technique_name` and `tactic`. Hosts and vulnerabilities are produced by the agents, not stored in the seed.

A new database has no curated cases. The 8 curated campaigns (108 abilities, 8
adversaries, 8 attack flows) are read lazily by `app/seeds/loader.py` through
`importlib.resources` (the files ship as package data, so this works in Docker,
Kubernetes and AWS) and applied through the ORM by an idempotent upsert
(`app/seeds/apply.py`). `app/seeds/manifest.py` is a hard-coded constant with the
slugs and expected counts (there is no `index.json`), so the ingestion plan can
check the database without opening any file; `tests/test_curated_seed_data.py`
keeps it in sync with the files.

Cross-campaign references are valid: an ability of `costaricto` may point to
`shadowray`, and abilities are resolved globally, not per campaign.

- Automatic: the `curated_seed` ingestion source applies the seed at the first
  start (and completes it if rows are missing). The source order is
  `stix_packages → curated_seed → atomic → docker_catalog → attack_flow`.
- Manual: `python -m app.seeds.curated` (from `apps/backend`, venv active; uses
  `PIPELINE_UI_DATABASE_URL`). It prints a JSON summary and never the database URL.
- Rows whose `translation_source` is not `human_curation` (edited or AI-generated)
  and flows of other origins are never overwritten.
- Regenerate the seed (read-only on the database) from a database that holds the
  curated cases, from the repository root:
  `python ops/database/scripts/export_curated_seed.py --output-dir apps/backend/app/seeds/data`.
  The exporter applies the documented `KNOWN_CORRECTIONS`: the `outer_space`
  adversary `atomic_ordering` orphan id `6afb1856-6e5a-5f9f-8f8c-rothos` is replaced
  by `a8de43be-fcde-5302-9cfd-rothos` (corrected ordering), and every flow node and the flow content are pruned to the lean format described above. See `ops/database/README.md`.
- Tests: `pytest tests/test_curated_seed.py tests/test_curated_seed_data.py`.

No real Caldera or Docker attack network is used in development; everything
uses mocks/fixtures.

## Tests

```powershell
# from apps/backend, with the venv active
pytest
```

The suite uses only mocks/fixtures and does not touch Caldera or the Docker
attack network. The end-to-end integration test against the **REAL**
environment (`tests/test_e2e_real_lab.py`, Task 15.3) is **skipped by default** —
it sits behind the `lab` marker (deselected via `-m "not lab"` in
`pyproject.toml`) and behind `skipif(PIPELINE_UI_LAB != "1")`, so it never runs
in dev/CI. To run it on the **lab machine** (real Caldera + hardened compose,
containment satisfied), see [`LAB.md`](./LAB.md).
