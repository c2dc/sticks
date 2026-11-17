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

Embedding requests to Ollama time out after 120 s by default, which leaves room for
a cold model to load on the first call. Set `OLLAMA_EMBED_TIMEOUT` (seconds, env or root
`.env`) to change it; a missing, non-numeric, zero or negative value falls back to 120.

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

The newest version is detected from the upstream index (cached for 1 h, 5 s
timeout, `max(KNOWN_VERSIONS)` when offline). `plan` only reads that cache: on the
`POST /api/ingestion/verify` request it never waits for the network (a cold or
expired cache is refreshed in a background thread), while the background ingestion
cycle may fill it inline. When a version newer than the newest installed one is
published, the next start or verify downloads it (about 50 MB each); when the
newest is already installed the plan is empty. To opt out of following new
releases, pin versions with `STIX_VERSIONS` (e.g. `STIX_VERSIONS=19.1`).

`GET /api/stix/bases` serves the same cached index (newest first) united with the
versions persisted in the database; if the index fails (offline, empty, invalid
JSON) it serves `KNOWN_VERSIONS` and caches that failure for 60 s. The lifespan
warms the cache in a daemon thread (`stix-version-warmup`), so startup does not
wait for the network. `POST /api/stix/bases/{version}/download` returns `422`
(`falha_download`) for a version outside that list. No test uses the network.

### Atomic Red Team ingestion

The `atomic` source streams `redcanaryco/atomic-red-team` from `codeload.github.com`
and parses only `atomics/T####/T####.yaml` in memory (no repository extraction, no
`git clone`). Relational rows are saved first and vectors afterwards, in batches; an
embedder failure ends the source `failed` and the next run only vectorizes what is
missing. A fetch with zero techniques or fewer than `ATOMIC_MIN_TECHNIQUES` (default
`100`) ends `failed` (`implausible_atomic_count`). Malformed YAML files are skipped
and listed in the source's `skipped` list (`{item, reason}`). `#{argument}`
placeholders stay literal, `manual` executors without `command` are discarded, and a
command listed for several platforms is stored once per technique (first
platform/executor wins), so `expected_count` is the number of unique
(technique, command) pairs. A truncated download is never returned as a partial result: the gzip stream is drained to its trailer (CRC/size) and the bytes read are checked against `Content-Length`, so a truncation is retried with the same backoff and ends in `AtomicFetchError` once retries run out.

Vectors are created in batches of 32 (one `/api/embed` call); a failing batch is
redone entry by entry. `progress(done, total)` and the heartbeat run throughout.
The next run downloads again when nothing is stored, when the previous failure was
not an embedder one, when fewer than `ATOMIC_MIN_TECHNIQUES` techniques are stored,
or when `expected_count` is unknown or larger than the stored rows (for example a
run interrupted after a few rows); otherwise it only vectorizes what is missing, and
a complete source has an empty plan and uses no network.


Environment variables (process environment or root `.env`):

- `INGEST_WORK_DIR`: absolute directory used for the ingestion's temporary files.
  Unset or empty (default): memory only. When valid, the YAML files are staged in a
  unique `atomic_red_team_<uuid>` subfolder, removed at the end. Invalid values log a
  warning and are ignored.
- `ATOMIC_MIN_TECHNIQUES`: plausibility floor (default `100`).

Tip for Windows: antivirus software may block some Atomic YAML files (credential
dumping techniques such as T1003.002 or T1555.004) when they are written to disk.
The default in-memory parsing avoids this; if you set `INGEST_WORK_DIR`, point it
to a folder with an antivirus exception, for example `C:\sticks-ingest`.

### Docker image catalog ingestion

Docker image catalog ingestion: only text is read from the public Docker Hub API (no image layers). The anonymous API serves at most 200 results per query (a deeper page answers `403 pagination ... anonymous`, which ends that query normally; `next` is never followed), so the catalog is built from several short queries: the **official** images (`/v2/repositories/library/?page_size=100`, queried with `ordering=name` and `ordering=-name`; the union by name covers the whole namespace, about 180 images) and **popular** images found through a curated list of lab-relevant search terms (pages 1-2, `page_size=100`), keeping only results with a description and at least `DOCKER_CATALOG_MIN_PULLS` pulls. 429/5xx/timeouts are retried up to 4 attempts with exponential backoff plus jitter (honoring `Retry-After` / `x-ratelimit-reset`) and an identifiable `User-Agent` (`sticks-ingest/1.0`). If a query fails for good but others delivered images, the ingestion ends `ready` with the failure listed as a warning (skipped item); it only fails when no image was obtained, and the recorded `error` keeps the original cause (HTTP status, URL path and API message). A fresh catalog has about 1600 images. Every image is cataloged as `linux`: Docker Hub has **no official Windows images** (Microsoft publishes them on `mcr.microsoft.com`). The source is fetched again when the catalog is empty, below `DOCKER_CATALOG_MIN_IMAGES` or the last run failed; upserts only add, so loaded images are never lost.

Environment variables (process environment or root `.env`):

- `DOCKER_CATALOG_MIN_IMAGES` (default `150`): completeness floor; below it the catalog is fetched again.
- `DOCKER_CATALOG_MIN_PULLS` (default `100000`): minimum pulls for non-official images.


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
check the database without opening any file.

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

No real Caldera or Docker attack network is used in development; everything
uses mocks/fixtures.

## Tests

The repository keeps a deliberately small suite (10 tests in total, 8 here and
2 in the frontend), described in `.kiro/steering/test-policy.md`. Property tests
run with `max_examples=10`. New tests are only added with explicit owner approval.

```powershell
# from apps/backend, with the venv active
pytest
```

The suite uses only fakes and ephemeral SQLite databases; it never touches
Caldera, the Docker attack network, the LLM or the real database. `tests/conftest.py`
hides inherited `PIPELINE_UI_*` / `OLLAMA_*` variables and disables ingestion on start.

| Test | Guards |
|---|---|
| `test_ai_isolation_gate_failclosed_property` | an environment proposed by the agent is only ready if the isolation gate approves it |
| `test_isolation_gate_network_boundary` | destinations outside the internal subnets are rejected |
| `test_operation_runner_failclosed_firing_property` | a command fires only if confirmed and isolated |
| `test_provision_failclosed_property` | a provisioning failure never leaves a target with egress |
| `test_migration_roundtrip_property` | migration up then down keeps curated data |
| `test_atomic_reingest_property` | Atomic re-ingestion is idempotent |
| `test_atomic_arguments_substitution_property` | argument substitution is a single total pass and keeps `PathToAtomicsFolder` |
| `test_agent_context_property` | the agent context carries the original command template |
