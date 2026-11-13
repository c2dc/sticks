# STICKS

### The Procedural Semantics Gap in ATT&CK-in-STIX — Measuring Procedural Sufficiency for APT Emulation

STICKS is a research platform that evaluates the **procedural completeness** of
STIX-based cyber threat intelligence (CTI) and its ability to **reproduce APT
campaign behavior** inside a fully contained laboratory. It pairs a modern,
guided **web application (Pipeline_UI)** with a MITRE Caldera adversary-emulation
lab, so a researcher can carry a campaign end to end — from raw STIX to a
measured execution report — one explicit agentic phase at a time.

> This repository accompanies the paper
> *"The Procedural Semantics Gap in ATT&CK-in-STIX: Measuring Procedural
> Sufficiency for APT Emulation."*

---

## What it does

Structured CTI tells you **which** ATT&CK techniques a campaign used, but not the
**procedure** — the concrete commands, their order, and their dependencies. STICKS
makes that gap measurable:

- It is **agentic-centric**: once the researcher picks a STIX base and campaign
  (the **Input**), a multi-agent AI loop drives the emulation through semantic,
  number-free **agentic phases**.
- It executes the campaign against an **isolated Docker lab** through
  MITRE Caldera, tracking each technique live.
- It measures **procedural sufficiency** and produces a consistent execution
  report (success rate, per-technique telemetry, failed nodes, verdict).

Security containment is **non-negotiable**: adversary commands never run on the
host or reach external targets — they execute only inside internal-only Docker
networks, verified before and during every run.

---

## Glossary: setup, ingestion, input, and agentic phases

- **Setup** — installation and first-run ingestion of the knowledge bases
  (curated cases, Atomic Red Team, Docker image catalog, Attack Flow corpus, STIX package),
  plus the verification that runs at every start. It is **not** a screen and
  **not** a run phase.
- **Ingestion** — the idempotent background process that performs the data part
  of setup (and the verification at each start).
- **Input** (`input`, `Input_Phase`) — the per-run phase where the researcher
  picks the STIX version (the package) and the campaign. Never called "setup".
- **Agentic phases** — `operationalization → flow → environment → execution →
  evaluation`; `input` precedes them and is not one of them.

## Setup, Input, and the Agentic phases

STICKS has **three** separate concepts. They run at different times, for
different consumers — do not treat them as synonyms. The phase names are
**semantic and number-free**; the legacy `stage_N` numbering has been removed.
Every phase decision (how many phases, their names, splitting execution from
evaluation, and the network boundary) is traceable to the academic sources in
[`docs/agentic-phases-literature.md`](docs/agentic-phases-literature.md).

| Concept | When | Nature |
|---------|------|--------|
| **Setup (installation + ingestion)** | Automatic at the first start; verified at every start | Background, **idempotent** ingestion that populates the databases from the knowledge sources (curated cases seed, Atomic Red Team, Docker catalog, Attack Flow corpus, STIX package). |
| **Input** (`input`) | Per run | The researcher selects the STIX version (the package) + campaign. **Not an agent phase** — it precedes the agentic loop. Never called "setup". |
| **Agentic phases** | Per run | The agent loop: `operationalization → flow → environment → execution → evaluation`. |

Relationship: **Setup (ingestion) fills the databases → Input reads from them** to build
the initial context → the agentic phases run. Setup is a prerequisite for
everything else.

### First run (fresh installation)

A new installation starts with an empty database. Principle: **runtime data lives
in the databases** (relational + vector). The only JSON files in the repository
that feed the system are the **seeds**: versioned, read-only files in
`apps/backend/app/seeds/data/<slug>.json` (one per campaign, 8 files, about 124 KB). The backend loads them through `importlib.resources` and the setup
itself writes them into the database; nothing at runtime reads or writes them
as storage.

1. Start the infrastructure (PostgreSQL with pgvector is published on
   `127.0.0.1:5432`): `cd ops/docker && docker compose up -d`.
2. Prepare the native environment once (backend venv + frontend dependencies):
   `npm run setup`.
3. Apply the Alembic migrations: `npm run db:migrate` (`npm run dev` also applies
   them before serving).
4. Start the backend and frontend: `npm run dev`. On this first start the
   ingestion runs in the background, without blocking the UI, and loads every
   source below, including the curated cases (source `curated_seed`). Progress is
   shown in the UI and at `GET /api/ingestion/status`; when all sources are
   `ready` the setup is complete.

Manual alternatives and maintenance:

- Apply only the curated seed, from `apps/backend` with the backend venv active:
  `python -m app.seeds.curated`. It is idempotent, prints a JSON summary of
  inserted/updated/existing rows per kind, and never prints the database URL.
- Regenerate the seed from a database that already holds the curated cases (read
  only, it never writes to that database):
  `python ops/database/scripts/export_curated_seed.py --output-dir apps/backend/app/seeds/data`
  (use the backend venv: `apps/backend/.venv/Scripts/python` on Windows,
  `apps/backend/.venv/bin/python` on Linux/macOS). It rewrites the 8 JSON files
  and fails if the counts differ from 8 campaigns, 108 abilities and 8
  adversaries. The documented `KNOWN_CORRECTIONS` are applied: the `outer_space` ordering fix and the pruning of every flow node to `node_id`, `node_type`, `technique_id`, `technique_name` and `tactic` (and of the flow content to `campaign_name`, `edges` and `structural_nodes`). The seed has no `curated` flag or flow `origin`: the applier always stores `curated = true` and `origin = human_curation`. A `costaricto` ability that points to `shadowray` is valid,
  since abilities are resolved globally across campaigns. See
  [`ops/database/README.md`](ops/database/README.md).

### Ingestion (automatic) — operations, inputs, persistence

Ingestion runs **automatically in the background on the first start** and is
**verified on every start**: the Ingestion Orchestrator checks that each source
is loaded and vectorized and completes only what is missing, without blocking the
UI. It is idempotent (re-running the same sources does not duplicate rows or
embeddings). It populates both stores:

| Source | Operation | Persistence |
|--------|-----------|-------------|
| **STIX bundles** (ATT&CK-in-STIX, e.g. v18.0 / v19.0 / v19.1) | Parse and persist campaigns, techniques, relationships. | Relational `stix_*` tables + STIX-context embeddings. |
| **Curated cases** (`curated_seed`; versioned read-only JSON seeds in `app/seeds/data`, no network) | Idempotent upsert of the 8 curated campaigns, 108 abilities, 8 adversaries and 8 attack flows (`human_curation`). Rows edited or generated by the AI are never overwritten. | Relational `campaigns` (`curated = true`), `abilities`, `adversaries`, `attack_flows`. |
| **Atomic Red Team** (`redcanaryco/atomic-red-team`) | Index per-technique commands. | Relational `technique_commands` + command affinities in the Vector store (`experience_memory`). |
| **Attack Flow corpus** ([Attack Flow](https://center-for-threat-informed-defense.github.io/attack-flow/), MITRE Center for Threat-Informed Defense; [repository](https://github.com/center-for-threat-informed-defense/attack-flow)) | Download the published `.afb` flows from a pinned tag, parse them, and record cycles when present. | Relational `attack_flows` (+ objects and edges) + one flow-description embedding per flow in `experience_memory`. |
| **Docker image catalog** | Fetch **text-only** image descriptions via the public Docker Hub API (official images plus popular ones found by curated search terms; no image layers; caps ~10k images / ~10 MB; see the strategy below). | Relational `image_catalog` keyed by `(image, target_os)` + best-effort description/services embeddings (a Vector-store failure never aborts the relational write). |

Sources run sequentially in this order: `stix_packages` → `curated_seed` →
`atomic` → `docker_catalog` → `attack_flow`. `curated_seed` runs before `atomic`
because the Atomic affinity bootstrap derives from the curated cases. The seed
module is large, so it is imported only when something is missing; verifying a
complete database does not load it.

Ingestion requires **no campaign run** and isolates per-source failures: a
failure in one source preserves what the others already persisted, without
corrupting the databases.

Each source (`stix_packages`, `curated_seed`, `atomic`, `docker_catalog`, `attack_flow`) reports a status:

| Status | Meaning |
|--------|---------|
| `pending` | Not started yet. |
| `running` | Ingestion in progress. A second concurrent start of the same source is rejected. |
| `ready` | Loaded and vectorized; the next start only verifies. |
| `failed` | Stopped with a recorded cause (for example, no network). The next verification resumes only the pending work, and the other sources are unaffected. |

Status is exposed at `GET /api/ingestion/status` (counts of loaded and vectorized
items) and shown in the UI without blocking it. Configuration (environment
variables, also readable from `.env`):

| Variable | Purpose |
|----------|---------|
| `STICKS_INGEST_ON_START` | Enables the automatic ingestion at start (default `true`). Tests turn it off so nothing touches the network. |
| `ATTACK_FLOW_REF` | Git ref (tag) of the Attack Flow repository that the corpus is read from. |
| `GITHUB_TOKEN` | Optional token for the GitHub listing call (raises the anonymous rate limit). |
| `OLLAMA_BASE_URL` | Base URL of the Ollama service that produces embeddings. |
| `OLLAMA_EMBED_MODEL` | Embedding model name used for every vectorized source. |
| `OLLAMA_EMBED_TIMEOUT` | Timeout, in seconds, of each embedding request to Ollama (default `120`, so a cold model has time to load). Missing, non-numeric, zero or negative values fall back to the default. |
| `INGEST_WORK_DIR` | Absolute directory for the ingestion's temporary files (for example `C:\sticks-ingest`, a folder with an **antivirus exception**). Unset or empty (default): nothing is written to disk, the Atomic Red Team YAML files are parsed in memory. When valid, the selected YAML files are staged in a unique `atomic_red_team_<uuid>` subfolder, removed at the end (success or failure). A relative or unusable path logs a warning and behaves as unset. |
| `ATOMIC_MIN_TECHNIQUES` | Plausibility floor for the Atomic Red Team source (default `100`; the full repository has about 350 techniques). A download with zero techniques or fewer than this ends `failed` with `implausible_atomic_count`, never `ready`. |
| `DOCKER_CATALOG_MIN_IMAGES` | Completeness floor of the Docker image catalog (default `150`). A catalog with fewer images (for example the 200 of the old search) is fetched again; upserts only add. |
| `DOCKER_CATALOG_MIN_PULLS` | Minimum pull count for a **non-official** image to enter the catalog (default `100000`). Official images are not filtered by pulls. |

Atomic Red Team ingestion: the repository tarball is downloaded as a stream from
`codeload.github.com` (identifiable `User-Agent`, retries with backoff for 5xx/429/timeouts)
and only `atomics/T####/T####.yaml` is read; the repository is never extracted. All
commands (`technique_commands`, unique per technique and command hash) are saved
and committed first, then vectorized in batches. If Ollama fails, the source ends
`failed` with the embedder cause, the relational part stays complete and the next
verification only vectorizes what is missing. A truncated download is never returned as a partial result: the gzip stream is drained to its trailer (CRC/size) and the bytes read are checked against `Content-Length`, so a truncation is retried with the same backoff and ends in `AtomicFetchError` once retries run out. `#{argument}` placeholders are kept
literally; `manual` tests (no command) are discarded; every platform listed by
the test, including `containers` and `iaas:*`, is kept. A command listed for
several platforms is stored once per technique (the first platform/executor seen
is the one kept), so `expected_count` is the number of unique
(technique, command) pairs. A malformed YAML file is isolated and reported in the
source's `skipped` list (`{item, reason}`) without stopping the others.

Vectorization runs in batches of 32 (one `/api/embed` call each); a batch that
fails is redone entry by entry so one bad entry never blocks the rest.
`progress(done, total)` and the heartbeat keep the status counts and the lock
alive during the download, the relational phase and the embedding.

When the next run downloads again: nothing is stored; the previous run failed for
a reason other than the embedder; fewer techniques than `ATOMIC_MIN_TECHNIQUES`
are stored; or the relational part was never recorded as complete
(`expected_count` unknown or larger than the stored rows, for example a run
interrupted after a few rows); or any stored command still has a NULL
`input_arguments` (rows ingested before that column existed). Otherwise it only
vectorizes what is missing, and with nothing missing the plan is empty and no
network is used.

Re-ingest to backfill `input_arguments`: nothing manual is needed. The next start
(or `POST /api/ingestion/atomic/start`) sees the NULL column, downloads the
Atomic repository again and updates the existing rows in place (same
`(technique, command_hash, source)` key), so no row and no embedding is
duplicated. A test without arguments is stored as `{}` (processed, nothing to
substitute) and a row that no longer exists upstream is also set to `{}`, so after
one successful run the plan is empty again and the network is not used.

#### Command template, Atomic defaults and agent adaptation

`technique_commands` stores the **canonical command template**, not a ready
command: `command` keeps the Atomic text byte for byte, with its `#{name}`
placeholders, and `input_arguments` keeps each argument's name, description,
type and default. No substituted command is ever written to the database.

- **Defaults are a suggestion.** The Atomic defaults (Windows paths, example IPs)
  describe the Atomic Red Team's own test bed and may not exist in the emulated
  environment, which changes with every campaign.
- **Example on the Operationalization screen.** When the screen shows a command
  filled with the Atomic defaults, it is labeled as an example with the Atomic
  default values (not as the command to run), the copy button says so, and a
  collapsible "template" section shows the original `command`. The API keeps
  returning `command` (template), `resolved_command` (example), `arguments_used`
  and `unresolved_arguments`. Without a difference between the two, the screen
  shows a single plain block. *Implemented.*
- **`PathToAtomicsFolder` is an agent argument.** The token
  `PathToAtomicsFolder` / `$PathToAtomicsFolder` (the folder where the payload
  lives in the campaign container) is never substituted: it stays intact in the
  template and in the example, and is reported in `unresolved_arguments`. No
  environment variable configures it; the agent decides the folder per
  campaign. *Implemented.*
- **The agent receives the template and the arguments.** The pool the
  operationalization agent queries (`atomic_lookup`) returns, for each
  candidate, the unsubstituted template, its `command_ref` (the
  `technique_commands` row id) and its `input_arguments` plus
  `PathToAtomicsFolder`. A candidate found by similarity, without stored
  arguments, carries only the template and the detected placeholder names.
  *Implemented.*
- **The agent adapts the command to the environment.** The design is for the
  agent prompt to say that defaults are suggestions, that the environment is
  emulated and changes per campaign, and that the agent chooses IPs, hosts,
  containers and paths (including the payload folder) and answers with
  `{"template", "command"}` items without remaining placeholders. *Pending
  (tasks 45.3 and 45.4); the current prompt does not do this yet.*
- **Validation and fallback.** The design accepts a final command only if its
  `template` is in the technique's pool and it has no `#{...}` and no
  `PathToAtomicsFolder`; otherwise the reason goes to the trace and the
  deterministic fallback runs. Without a chat model, the fallback fills the
  placeholders with the Atomic defaults, records `atomic_defaults` as the source
  in the trace and keeps `command_ref`. *Pending (task 45.4).*

Docker image catalog ingestion: only text is read from the public Docker Hub API (no image layers). The anonymous API serves at most 200 results per query (a deeper page answers `403 pagination ... anonymous`, which ends that query normally; `next` is never followed), so the catalog is built from several short queries: the **official** images (`/v2/repositories/library/?page_size=100`, queried with `ordering=name` and `ordering=-name`; the union by name covers the whole namespace, about 180 images) and **popular** images found through a curated list of lab-relevant search terms (pages 1-2, `page_size=100`), keeping only results with a description and at least `DOCKER_CATALOG_MIN_PULLS` pulls. 429/5xx/timeouts are retried up to 4 attempts with exponential backoff plus jitter (honoring `Retry-After` / `x-ratelimit-reset`) and an identifiable `User-Agent` (`sticks-ingest/1.0`). If a query fails for good but others delivered images, the ingestion ends `ready` with the failure listed as a warning (skipped item); it only fails when no image was obtained, and the recorded `error` keeps the original cause (HTTP status, URL path and API message). A fresh catalog has about 1600 images. Every image is cataloged as `linux`: Docker Hub has **no official Windows images** (Microsoft publishes them on `mcr.microsoft.com`). The source is fetched again when the catalog is empty, below `DOCKER_CATALOG_MIN_IMAGES` or the last run failed; upserts only add, so loaded images are never lost.

### Input (`input`, per run) — operations, inputs, persistence

The researcher picks a STIX base version and a campaign from it. The system
extracts the campaign's attributes (techniques, relationships, indicators,
infrastructure, malware) by **reading from the ingested databases** — no disk,
no network at request time. `input` is the human choosing the target; it is
**not counted or numbered as an agent phase**. Its output (campaign identity +
techniques) is the initial context the agentic loop consumes.

### Agentic phases — operations, inputs, persistence

The agent loop runs five semantic phases, in this order. The Atomic commands come
**before** the flow, so the researcher validates the operationalization first.
`evaluation` observes the **real** execution result and either finishes (success)
or routes a refinement back to `operationalization`, `flow`, or `environment`,
bounded by a max-iterations guard.

| Phase | Operation | Input | Persistence |
|-------|-----------|-------|-------------|
| **`operationalization`** | For each technique of the campaign, list and choose concrete commands from the Atomic Red Team base (exact relational lookup by technique ID; the vector query is only a secondary aid — commands are never invented). | Campaign identity + techniques (from `input`). | Chosen commands per technique; audit trace. |
| **`flow`** | Build the **attack flow** of the campaign with the three-path policy below, and record its provenance. | The techniques and the chosen commands. | Attack flow objects/edges + provenance; per-phase trace + experience in the Vector store. |
| **`environment`** | Provision the contained lab. Resolve images **catalog-first** (Docker image catalog, then Docker Hub online only as fallback). **External egress is allowed here, automatically**, to pull images. | The attack flow and commands, with the environment parameters they require. | Provisioning map; environment state + audit trace. |
| **`execution`** | Run the offensive commands through Caldera against the targets over the **internal** lab network. Starts only after the **isolation gate** confirms external egress is cut; every command destination is validated by `is_internal_ip`. | The provisioned, **isolated** lab. | Run results, 1:1 audit trail, isolation proof/events. |
| **`evaluation`** | Judge the real execution result (runner verdict, containment/isolation state); decide finish vs. refine; write the experience. | The execution result. | Verdict + experience (success/failure) in the Vector store. |

**Network boundary (`environment` → `execution`).** Isolation is **cutting
external egress, not cutting the network**. After provisioning, an auditable
isolation gate severs external egress while **keeping the internal lab network
alive** — Caldera (the C2) needs it to reach the targets during `execution`.
`execution` cannot start without a valid isolation proof, and every command
destination must be internal/contained (`is_internal_ip`). See the network
boundary rationale and its sources in
[`docs/agentic-phases-literature.md`](docs/agentic-phases-literature.md).

#### Attack flow and the three-path policy

The **attack flow** replaces the former plain-graph concept. The format is
[Attack Flow](https://center-for-threat-informed-defense.github.io/attack-flow/),
from the MITRE Center for Threat-Informed Defense (source:
[center-for-threat-informed-defense/attack-flow](https://github.com/center-for-threat-informed-defense/attack-flow)).
It models adversary behavior as actions, conditions, operators, and assets, with
the edges given by each object's effect references. The published corpus is not
strictly acyclic (a few flows contain cycles), so STICKS keeps the original graph,
logs the cycles with the objects involved, and derives an acyclic execution view;
a cycle never stops the application.
*Description paraphrased from the project documentation for compliance with
licensing restrictions.*

The `flow` phase chooses one of three paths and records it as the flow's
**provenance** (path plus the IDs of the reference flows), shown in the UI:

| Path | When | Result |
|------|------|--------|
| `exact` | A documented flow of the corpus matches the campaign exactly. | The documented flow is used as is. |
| `adapted` | There is no exact match, but similar flows exist. | The closest flows are adapted into the nearest possible flow. |
| `created` | No reference flow exists. | The agent creates an original flow. |

Similar flows are found by vector similarity over the flow description and by a
relational query over the structured flows.

#### Curated campaigns are only a flag

The old curated pipeline (`StageService`, the `/stage/N` routes, and their screens)
was removed. "Curated" is now only the `curated` flag in the database: the flow,
commands, and environment of the 8 human-curated campaigns stay as **data**, and
serve as ground truth to compare against the AI-generated result. On a fresh
installation they are loaded by the `curated_seed` ingestion source (see
[First run](#first-run-fresh-installation)).

### STIX base selection - where the packages live

STIX packages live **only in the database**; no bundle file is ever written to
disk. The `stix_packages` knowledge source of the ingestion orchestrator
downloads each `enterprise-attack-{version}.json` (~50 MB) from the official MITRE
repository **into memory** and persists its derived content (type counts,
campaigns, techniques, actors) straight into the relational tables, then indexes
the campaigns in the vector store. The ingestion is idempotent per version,
resumable and isolates failures: one failing version never blocks the others.

It runs on the first start (and on every start, doing nothing when nothing is
missing). The versions kept ingested are the union of:

- **the newest ATT&CK version (the default).** A fresh installation downloads the
  most recent Enterprise version. The backend asks the upstream index
  (`index.json` of `mitre-attack/attack-stix-data`) which one that is; offline
  or on any error it falls back to the newest version built into the backend
  (`KNOWN_VERSIONS`), without failing. Setting `STIX_VERSIONS` (comma or
  whitespace separated, e.g. `STIX_VERSIONS=19.1,latest`; `latest` is the newest
  version) replaces **only this item**;
- **the version required by the curated cases (today `18.0`).** The 8 curated
  campaigns were curated against ATT&CK 18.0; every seed file declares it
  (`campaign.stix_version`, with `campaign.stix_campaign_key`, e.g. `C0045`).
  `STIX_VERSIONS` can never remove it;
- every version already in the database (never dropped);
- any version picked in the UI that is not ingested yet: choosing it calls
  `POST /api/stix/bases/{version}/download`, which starts the ingestion in the
  background (`202`) while `GET /api/stix/bases` (`downloaded`) and
  `GET /api/ingestion/status` report the progress.


Cost of the first start: each version is about 50 MB to download and persist, and
a fresh installation downloads **two** versions (the newest and 18.0), so expect
roughly 100 MB of traffic and a few minutes on a typical connection (progress is
in `GET /api/ingestion/status`). Later
starts download nothing when nothing is missing. If a version fails (for example
offline), only that version is left missing, the source is reported `failed`, and
the next start resumes just the missing ones.

Following new releases: when ATT&CK publishes a version newer than the newest one
installed (detected through the cached upstream index: 1 h cache, 5 s timeout,
offline falls back to `KNOWN_VERSIONS`), the next start or `POST /api/ingestion/verify`
downloads it (about 50 MB per version, in addition to the versions you already
have). The verify request never waits for the network; the index is refreshed in
the background. When the newest version is already installed the plan is empty.
To turn this off, pin the versions with `STIX_VERSIONS` (e.g. `STIX_VERSIONS=19.1`).

Version list in the UI: `GET /api/stix/bases` lists the Enterprise versions
published in the upstream index, newest first, **plus** every version already in
the database (a persisted version is never hidden). The index is cached for 1 h
(5 s timeout, warmed in the background at startup, so the API never waits for it
to come up). When it is unreachable, empty or invalid, the list falls back to the
built-in `KNOWN_VERSIONS` and that failure is cached for 60 s, so an offline
machine is not queried on every request. `POST /api/stix/bases/{version}/download`
accepts only versions of that list (`422` `falha_download` otherwise). Each
version costs about 50 MB of download.

**Curated campaigns** (flagged `curated`): APT41 - DUST, ATT&CK Campaign C0010, ATT&CK Campaign
C0026, CostaRicto, Operation MidnightEclipse, Operation Outer Space,
Salesforce Data Exfiltration, ShadowRay.

---

## Architecture

- **Backend** — FastAPI + SQLAlchemy + Alembic, **PostgreSQL only**. Orchestrates
  the agentic phases, exposes the REST/WebSocket API, enforces containment, and persists
  state. Live technique-by-technique progress streams over WebSocket.
- **Frontend** — React 19 + TypeScript + Vite + Tailwind + shadcn/ui + i18next
  (pt-BR / en). A guided wizard renders every phase with Input/Processing/Output,
  live execution tracking, and the consolidated report.
- **Ops** — the contained Docker lab (Caldera, Kali attacker, nginx target, DB)
  on static internal-only networks.

### Isolated lab network

| Network | Subnet | Host | IP |
|---------|--------|------|----|
| caldera-kali-network | 192.168.10.0/24 | caldera | 192.168.10.10 |
| caldera-kali-network | 192.168.10.0/24 | kali | 192.168.10.20 |
| kali-nginx-network | 192.168.20.0/24 | kali | 192.168.20.20 |
| kali-nginx-network | 192.168.20.0/24 | nginx | 192.168.20.30 |
| nginx-db-network | 192.168.30.0/24 | nginx | 192.168.30.30 |
| nginx-db-network | 192.168.30.0/24 | db | 192.168.30.40 |

---

## Repository layout

```
.
|-- apps/
|   |-- backend/            # FastAPI backend (API, services, Alembic migrations, tests)
|   `-- frontend/           # React + TypeScript (Vite) frontend
|-- ops/
|   |-- docker/             # Contained lab: compose + Caldera / Kali / nginx / db
|   |-- kubernetes/         # Planned (not implemented yet)
|   |-- aws/                # Planned (not implemented yet)
|   `-- database/           # Platform-agnostic database assets
|-- scripts/                # Cross-platform dev / setup / migrate runners (Node)
|-- docs/                   # Project documentation & journal
|-- .kiro/specs/            # Spec-driven plans (requirements -> design -> tasks)
|-- presentation/           # Slidev slides (separate project, outside the workspaces)
`-- package.json            # npm workspaces (apps/frontend only) + single dev commands
```

### `node_modules` folders

There are three `node_modules` directories, all ignored by git:

- **Root `node_modules/`**: the hoisted dependencies of the `apps/frontend`
  workspace. The root `package.json` declares `workspaces: ["apps/frontend"]`;
  `packages/*` was removed because that folder no longer exists.
- **`apps/frontend/node_modules/`**: only Vite caches (the dependencies themselves
  are hoisted to the root).
- **`presentation/node_modules/`**: the Slidev project, which is separate and not
  part of the workspaces. It has its own `package-lock.json`; install it with
  `npm install` inside `presentation/`.

---

## Quick start

**Prerequisites:** Docker (+ Compose), Node.js 18+, Python 3.11+.

The lab infrastructure (including PostgreSQL) runs in Docker; the backend and
frontend run natively on the host with hot-reload against the containerized
Postgres.

```bash
# 1. Start the contained infrastructure
#    (Caldera, lab targets, PostgreSQL on 127.0.0.1:5432)
cd ops/docker && docker compose up -d

# 2. Setup (installation): prepare the native environment once (backend venv + frontend deps)
#    from the repository root
npm run setup

# 3. Run backend + frontend with hot-reload (applies Alembic migrations first)
npm run dev
```

- **UI:** http://localhost:8000
- **API:** http://localhost:8010 - health check: `GET /health` -> `{"status": "ok"}`
- **Caldera:** http://localhost:8888 (login `red` / `admin`)

Database migrations can be applied on their own with `npm run db:migrate`.

---

## Development

```bash
# Backend tests (from apps/backend, using the project venv)
.venv/Scripts/python -m pytest          # Windows
.venv/bin/python -m pytest              # Linux / macOS

# Frontend type-check + tests (from apps/frontend)
npm run lint         # tsc --noEmit
npm run test         # vitest
```

### Linting and formatting

Linters are configured per app and must be run from that app's directory. Both
should finish with no findings.

```bash
# Frontend (from apps/frontend)
npm run lint:eslint   # ESLint 9 (flat config): typescript-eslint, react-hooks, jsx-a11y
npm run lint          # tsc --noEmit (type-check only)

# Backend (from apps/backend, using the project venv)
.venv/Scripts/python -m ruff check .            # Windows: lint
.venv/Scripts/python -m ruff format --check .   # Windows: formatting check
.venv/bin/python -m ruff check .                # Linux / macOS: lint
.venv/bin/python -m ruff format --check .       # Linux / macOS: formatting check
```

What each command covers:

- `npm run lint:eslint` lints the frontend TypeScript/TSX files (`src`, tests,
  `e2e`, and `.ts` configs; `dist`, `test-results`, `playwright-report` and
  `coverage` are ignored). The `react-hooks` and `jsx-a11y` accessibility rules
  are errors. Unused
  `eslint-disable` directives are reported as errors too.
- `npm run lint` is the TypeScript type-check; it does not run ESLint.
- `ruff check` applies the rule families `E, F, W, I, B, UP, C4, SIM, BLE, S, PGH,
  RUF` configured in `apps/backend/pyproject.toml` (`E501` is ignored: the formatter
  controls line width, 100 columns). `UP042` is also ignored on purpose: it would
  turn `(str, Enum)` into `StrEnum` and change `str(member)`, and these enums are
  persisted and serialized.
- `ruff format --check` only verifies formatting. To apply it, run
  `ruff format .` (same directory and interpreter).

Conventions:

- **Suppressions need a reason.** Name the rule and explain why after ` -- `, for
  example `# noqa: BLE001 -- <reason>` (Python) or
  `// eslint-disable-next-line <rule> -- <reason>` (frontend). Never disable a
  whole file or turn a rule off in the config. `# type: ignore[code]` also keeps
  its code and reason.
- **mypy is not used.** Python type checking is not part of the tooling; Ruff
  covers lint and format only.
- **`ops/` is never scanned.** It holds the vendored Caldera. The commands above
  run only inside `apps/frontend` and `apps/backend`, so they never reach it.
- There is no CI or pre-commit hook (the repository has no `.github`,
  `.pre-commit-config.yaml` or `.husky`), so these checks are run by hand.
- Dependencies: the frontend lint tools are pinned `devDependencies`, and `ruff`
  is pinned in the backend `dev` extra, so `npm run setup` installs them.
- `apps/frontend/package-lock.json` is kept in sync with `apps/frontend/package.json`
  because the frontend `Dockerfile` uses `npm ci`. `npm run setup` uses the root
  `package-lock.json` (npm workspaces). After changing frontend dependencies,
  update both lockfiles.

The backend is covered by example, integration, and **property-based** tests
(Hypothesis); the frontend by component tests (Vitest + Testing Library). The
project is **spec-driven**: each feature is planned under `.kiro/specs/` as
requirements -> design -> tasks before implementation.

---

## Research goal

STICKS exists to **empirically measure procedural sufficiency** in structured CTI:
evaluate the structural completeness of STIX datasets, quantify the operational
knowledge required to execute a campaign, surface what is missing to reproduce
APT behavior, and support automated adversary-emulation experiments - ultimately
to compare human curation against future AI-generated curation.

---

## License

GNU GPLv3.
