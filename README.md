<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0b0f14,50:3b1e54,100:c8102e&height=210&section=header&text=STICKS&fontSize=60&fontColor=ffffff&animation=fadeIn&fontAlignY=36&desc=Measuring%20procedural%20sufficiency%20of%20ATT%26CK-in-STIX%20for%20APT%20emulation&descAlignY=58&descSize=17" alt="STICKS" width="100%" />

<a href="#-quick-start">
  <img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=21&pause=1100&color=FF4D6D&center=true&vCenter=true&width=760&lines=From+raw+STIX+to+a+measured+execution+report;Five+agentic+phases%2C+human+approval+at+each+gate;Isolated+Docker+lab+driven+by+MITRE+Caldera;Human+curation+vs.+AI-generated+attack+flows" alt="STICKS" />
</a>

<br />

<img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+" />
<img src="https://img.shields.io/badge/FastAPI-backend-009688?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
<img src="https://img.shields.io/badge/LangGraph-agents-1C3C3C?style=for-the-badge" alt="LangGraph" />
<img src="https://img.shields.io/badge/React-19-61DAFB?style=for-the-badge&logo=react&logoColor=black" alt="React 19" />
<br />
<img src="https://img.shields.io/badge/PostgreSQL-pgvector-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL + pgvector" />
<img src="https://img.shields.io/badge/MITRE-Caldera-C8102E?style=for-the-badge" alt="MITRE Caldera" />
<img src="https://img.shields.io/badge/Docker-lab-2496ED?style=for-the-badge&logo=docker&logoColor=white" alt="Docker" />
<img src="https://img.shields.io/badge/License-GPLv3-2EA043?style=for-the-badge" alt="GPLv3" />

<br /><br />

**[Quick start](#-quick-start)** ·
**[How it works](#-how-it-works)** ·
**[Safety](#-safety)** ·
**[Docs](#-documentation)** ·
**[Layout](#-repository-layout)**

</div>

---

## 🔎 About

Structured CTI says **which** ATT&CK techniques a campaign used, but not the **procedure**: the commands, their
order and their dependencies. STICKS makes that gap measurable. A researcher picks a STIX campaign, a multi-agent AI
loop turns it into commands, an attack flow and a lab, and the campaign runs in an isolated Docker lab through
MITRE Caldera. The output is a report with success rate, per-technique telemetry and a verdict.

> This repository accompanies the paper *"The Procedural Semantics Gap in ATT&CK-in-STIX: Measuring Procedural
> Sufficiency for APT Emulation."*

<div align="center">

| 🎯 Curated campaigns | 🧩 Abilities | 🤖 Agentic phases | 📚 Knowledge sources | 🧪 Tests |
|:---:|:---:|:---:|:---:|:---:|
| **8** | **108** | **5** | **5** | **10** |

</div>

## 🧭 How it works

```mermaid
flowchart LR
    SRC[("📚 5 sources<br/>STIX · Atomic · Docker Hub<br/>Attack Flow · curated seeds")]
    DB[("🐘 PostgreSQL<br/>tables + pgvector")]
    IN["👤 input<br/>STIX version + campaign"]
    OP["🧩 operationalization"]
    FL["🕸️ flow"]
    EN["🐳 environment"]
    EX["⚔️ execution"]
    EV["📊 evaluation"]

    SRC -- "setup / ingestion" --> DB
    DB --> IN --> OP --> FL --> EN --> EX --> EV
    EV -. "refine (max 3)" .-> OP
    EV --> R(["📄 report"])
```

| Concept | When | What it is |
|---------|------|------------|
| **Setup** | First start, verified on every start | Background, idempotent ingestion that fills the database. Not a screen, not a run phase |
| **Input** | Per run | You pick the STIX version and the campaign. Not an agent phase |
| **Agentic phases** | Per run | `operationalization → flow → environment → execution → evaluation`. The run pauses for your approval after the first three |

Per-phase inputs, processing and outputs: [`docs/pipeline.md`](docs/pipeline.md).

## 🚀 Quick start

> [!NOTE]
> Needs Docker (with Compose), Node.js 18+ and Python 3.11+. The first start downloads about 100 MB of STIX data
> and builds embeddings with [Ollama](https://ollama.com) (`nomic-embed-text`). The lab image build (Caldera,
> about 7 GB) happens later, in the first run's `environment` phase.

```bash
npm run db:up      # PostgreSQL + pgvector on 127.0.0.1:5432
npm run setup      # once: backend venv + frontend dependencies
npm run dev        # migrations, then backend + frontend with hot reload
```

| Service | URL |
|---------|-----|
| 🖥️ UI | <http://localhost:8000> |
| ⚙️ API | <http://localhost:8010> (`GET /health`) |
| 🧪 Caldera | <http://localhost:8888>, only while a run has the lab up |

On the first start, ingestion runs in the background. Watch it in the UI or at `GET /api/ingestion/status`;
when every source is `ready`, setup is done. You also need a language model: the work profile
(`SEPINF_*` in `.env`) or a local Ollama model. Details in [`docs/operations.md`](docs/operations.md).

> [!WARNING]
> Do not edit backend files during a run. `uvicorn --reload` restarts the backend and kills the run in progress.

## 🛡️ Safety

Containment is non-negotiable. Adversary commands never run on the host and never reach external targets.

- `environment` is the only phase with external egress, and only to pull images.
- An **isolation gate** cuts egress and proves it. `execution` cannot start without that proof.
- Every command destination is checked by `is_internal_ip`, and every executed command is audited 1:1.
- Agents only propose. The same containment code that guards a human-curated run guards an AI run.

## 📖 Documentation

| Where | Answers | Read it when |
|-------|---------|--------------|
| **`README.md`** (here) | What is this, how do I run it, where is everything | First contact |
| [`docs/pipeline.md`](docs/pipeline.md) | What each phase and agent takes in, does and puts out | You want to understand the system |
| [`docs/storage.md`](docs/storage.md) | Which databases, which tables, who reads and writes them | You touch data or ingestion |
| [`docs/operations.md`](docs/operations.md) | Config, ingestion details, model profiles, migrations, dev checks | You install, configure or debug |
| [`docs/agentic-phases-literature.md`](docs/agentic-phases-literature.md) | Academic sources behind the phase model | You write or review the paper |
| [`docs/history.md`](docs/history.md) | How the project got here, on one page | You meet an old name or want context |
| [`ops/database/README.md`](ops/database/README.md) | Database helper scripts, curated seed export | You maintain the seeds |


## 📁 Repository layout

```
.
├── apps/
│   ├── backend/     # FastAPI, LangGraph agents, Alembic migrations, tests
│   └── frontend/    # React + TypeScript + Vite + Tailwind + shadcn/ui (pt-BR / en)
├── ops/
│   ├── docker/      # Contained lab: Caldera, Kali attacker, nginx and MariaDB targets, PostgreSQL
│   └── database/    # Platform-agnostic database scripts
├── scripts/         # Cross-platform dev, setup and migrate runners (Node)
└── docs/            # Documentation
```

## 🔬 Research goal

Empirically measure procedural sufficiency in structured CTI: how complete STIX datasets are, how much operational
knowledge a campaign needs to run, what is missing to reproduce APT behavior, and how **human curation** compares with
**AI-generated** curation.

## 📄 License

GNU GPLv3.