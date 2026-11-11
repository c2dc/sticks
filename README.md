# STICKS

### The Procedural Semantics Gap in ATT&CK-in-STIX — Measuring Procedural Sufficiency for APT Emulation

STICKS is a research platform that evaluates the **procedural completeness** of
STIX-based cyber threat intelligence (CTI) and its ability to **reproduce APT
campaign behavior** inside a fully contained laboratory. It pairs a modern,
guided **web application (Pipeline_UI)** with a MITRE Caldera adversary-emulation
lab, so a researcher can carry a curated campaign end to end — from raw STIX to a
measured execution report — one explicit stage at a time.

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

## Three distinct concepts: Ingestion, Input, and the Agentic phases

STICKS has **three** separate concepts. They run at different times, for
different consumers — do not treat them as synonyms. The phase names are
**semantic and number-free**; the legacy `stage_N` numbering has been removed.
Every phase decision (how many phases, their names, splitting execution from
evaluation, and the network boundary) is traceable to the academic sources in
[`docs/agentic-phases-literature.md`](docs/agentic-phases-literature.md).

| Concept | When | Nature |
|---------|------|--------|
| **Ingestion** | System setup / maintenance | Administrative, offline, **idempotent** process that populates the databases from the knowledge sources. |
| **Input** (`input`) | Per run | The researcher selects a STIX base + campaign. **Not an agent phase** — it precedes the agentic loop. |
| **Agentic phases** | Per run | The agent loop: `dag → operationalization → environment → execution → evaluation`. |

Relationship: **Ingestion fills the databases → Input reads from them** to build
the initial context → the agentic phases run. Ingestion is a prerequisite for
everything else.

### Ingestion (setup) — operations, inputs, persistence

A one-time, idempotent bootstrap (re-running the same sources does not duplicate
rows or embeddings). It populates both stores:

| Source | Operation | Persistence |
|--------|-----------|-------------|
| **STIX bundles** (ATT&CK-in-STIX, e.g. v18.0 / v19.0 / v19.1) | Parse and persist campaigns, techniques, relationships. | Relational `stix_*` tables + STIX-context embeddings. |
| **Atomic Red Team** (`redcanaryco/atomic-red-team`) | Index per-technique commands. | Relational `technique_commands` + command affinities in the Vector store (`experience_memory`). |
| **Docker image catalog** | Fetch **text-only** image descriptions via the public Docker Hub API (no image layers; caps ~10k images / ~10 MB). | Relational `image_catalog` keyed by `(image, target_os)` + best-effort description/services embeddings (a Vector-store failure never aborts the relational write). |

Ingestion requires **no campaign run** and isolates per-source failures: a
failure in one source preserves what the others already persisted, without
corrupting the databases.

### Input (`input`, per run) — operations, inputs, persistence

The researcher picks a STIX base version and a campaign from it. The system
extracts the campaign's attributes (techniques, relationships, indicators,
infrastructure, malware) by **reading from the ingested databases** — no disk,
no network at request time. `input` is the human choosing the target; it is
**not counted or numbered as an agent phase**. Its output (campaign identity +
techniques) is the initial context the agentic loop consumes.

### Agentic phases — operations, inputs, persistence

The agent loop runs five semantic phases. `evaluation` observes the **real**
execution result and either finishes (success) or routes a refinement back to
`dag`, `operationalization`, or `environment`, bounded by a max-iterations guard.

| Phase | Operation | Input | Persistence |
|-------|-----------|-------|-------------|
| **`dag`** | Build the directed acyclic graph of techniques — ordering and dependencies. | Campaign identity + techniques (from `input`). | DAG nodes/edges; per-stage trace + experience in the Vector store. |
| **`operationalization`** | Resolve concrete, ordered executable Abilities per technique (from the Atomic Red Team index; relational match, then semantic RAG — never invented). | The DAG. | Ordered Abilities + Adversary; audit trace. |
| **`environment`** | Provision the contained lab. Resolve images **catalog-first** (Docker image catalog, then Docker Hub online only as fallback). **External egress is allowed here, automatically**, to pull images. | Environment parameters derived from operationalization. | Provisioning map; environment state + audit trace. |
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

For the **8 human-curated campaigns**, the `dag`, `operationalization`, and
`environment` phases can consume curated artifacts (the AI-generated curation is
compared against this ground truth). An `origin` extension point
(`human_curation` / `ai_generated`) lets the AI agent produce the curation
without a redesign.

### STIX base selection — where the bundle is stored

The STIX bundle (`enterprise-attack-{version}.json`, ~50 MB) is a **runtime
artifact**, not a versioned file. It is downloaded from the official MITRE source
on demand during STIX base selection (part of the `input` phase), into a
**persistent user cache** outside the repository tree, so the repository stays
small and clean:

- **Windows:** `%LOCALAPPDATA%\sticks\stix`
- **Linux / macOS:** `$XDG_CACHE_HOME/sticks/stix` when `XDG_CACHE_HOME` is set,
  otherwise `~/.cache/sticks/stix`
- **Fallback:** `~/.sticks/stix` when no base environment variable is available

Override the location with the `PIPELINE_UI_STIX_DIR` environment variable (or an
entry in `.env`); when set, it takes precedence over the OS default.

**Curated campaigns:** APT41 - DUST, ATT&CK Campaign C0010, ATT&CK Campaign
C0026, CostaRicto, Operation MidnightEclipse, Operation Outer Space,
Salesforce Data Exfiltration, ShadowRay.

---

## Architecture

- **Backend** — FastAPI + SQLAlchemy + Alembic, **PostgreSQL only**. Orchestrates
  the stages, exposes the REST/WebSocket API, enforces containment, and persists
  state. Live technique-by-technique progress streams over WebSocket.
- **Frontend** — React 19 + TypeScript + Vite + Tailwind + shadcn/ui + i18next
  (pt-BR / en). A guided wizard renders every stage with Input/Processing/Output,
  live execution tracking, and the consolidated report.
- **CLI** — the STIX analysis and campaign-generation framework (`packages/cli`).
- **Infra** — the contained Docker lab (Caldera, Kali attacker, nginx target, DB)
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
|-- packages/
|   `-- cli/                # STIX analysis & campaign-generation framework
|-- infra/
|   `-- docker/             # Contained lab: compose + Caldera / Kali / nginx / db
|-- scripts/                # Cross-platform dev / setup / migrate runners (Node)
|-- docs/                   # Project documentation & journal
|-- .kiro/specs/            # Spec-driven plans (requirements -> design -> tasks)
`-- package.json            # npm workspaces + single dev commands
```

---

## Quick start

**Prerequisites:** Docker (+ Compose), Node.js 18+, Python 3.11+.

The lab infrastructure (including PostgreSQL) runs in Docker; the backend and
frontend run natively on the host with hot-reload against the containerized
Postgres.

```bash
# 1. Start the contained infrastructure
#    (Caldera, lab targets, PostgreSQL on 127.0.0.1:5432)
cd infra/docker && docker compose up -d

# 2. Prepare the native environment once (backend venv + frontend deps)
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

The backend is covered by example, integration, and **property-based** tests
(Hypothesis); the frontend by component tests (Vitest + Testing Library). The
project is **spec-driven**: each phase is planned under `.kiro/specs/` as
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
