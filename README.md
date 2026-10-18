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

- It drives a curated campaign through a **six-stage pipeline** with explicit
  Input / Processing / Output per stage and strict sequential gating.
- It executes the campaign against an **isolated Docker lab** through
  MITRE Caldera, tracking each technique live.
- It measures **procedural sufficiency** and produces a consistent execution
  report (success rate, per-technique telemetry, failed nodes, verdict).

Security containment is **non-negotiable**: adversary commands never run on the
host or reach external targets — they execute only inside internal-only Docker
networks, verified before and during every run.

---

## The six-stage pipeline

| # | Stage | Role |
|---|-------|------|
| 1 | **STIX base selection** | Pick a STIX package version; download it from MITRE if absent; show a bundle dashboard. |
| 2 | **Campaign selection** | List the bundle's campaigns; pick one; extract its attributes. |
| 3 | **DAG generation** | Build the directed acyclic graph of techniques and their ordering/dependencies. |
| 4 | **Operationalization** | Produce the executable Abilities (commands) in DAG order plus the environment parameters they require. |
| 5 | **Environment provisioning** | Create and verify the isolated container lab (health, loaded Abilities, isolation gate). |
| 6 | **Execution, measurement & report** | Auto-run the campaign in the isolated lab, track it live, measure sufficiency, generate the report, and deprovision. |

For the **8 human-curated campaigns**, stages 3-5 consume curated artifacts read
directly from disk (no AI, no network in development). This is a transitional
design: an `origin` extension point (`human_curation` today, `ai_generated`
later) lets an AI agent replace the curation without a redesign.

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

- **UI:** http://localhost:8080
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
