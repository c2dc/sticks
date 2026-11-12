# The Procedural Semantics Gap in ATT\&CK-in-STIX: 
### Measuring Procedural Sufficiency for APT Emulation

## Overview

This repository accompanies the paper:

> **The Procedural Semantics Gap in ATT\&CK-in-STIX: Measuring Procedural Sufficiency for APT Emulation**

It provides an experimental environment to evaluate **procedural completeness of STIX-based threat intelligence** and its ability to reproduce **APT campaign behaviors** in a controlled laboratory environment.

The repository includes:

- A **Docker-based adversary emulation environment**
- Automated generation of attacker commands
- Integration with **MITRE Caldera**
- Tools for evaluating the procedural gaps in structured CTI

---

# Architecture

The environment uses multiple Docker containers connected through static networks.

---

# Static Network Configuration

| Network | Subnet | Host | IP |
|------|------|------|------|
| caldera-kali-network | 192.168.10.0/24 | caldera | 192.168.10.10 |
| caldera-kali-network | 192.168.10.0/24 | kali | 192.168.10.20 |
| kali-nginx-network | 192.168.20.0/24 | kali | 192.168.20.20 |
| kali-nginx-network | 192.168.20.0/24 | nginx | 192.168.20.30 |
| nginx-db-network | 192.168.30.0/24 | nginx | 192.168.30.30 |
| nginx-db-network | 192.168.30.0/24 | db | 192.168.30.40 |
| caldera-mgmt-network (not internal) | 192.168.40.0/24 | caldera (alias `caldera-c2`) | dynamic |
| caldera-mgmt-network (not internal) | 192.168.40.0/24 | backend (opt-in `containers` profile) | dynamic |

`caldera-mgmt-network` is the control-plane bridge that publishes Caldera on `127.0.0.1:8888`. The backend joins it (plus `app-network`) but never the three internal attack networks.

---

# Repository Structure

```
.
├── apps/                   # Runnable applications (Pipeline_UI)
│   ├── backend/            # FastAPI backend (API, services, Alembic migrations)
│   └── frontend/           # React + TypeScript (Vite) frontend
│
├── ops/                    # Operations (per deployment platform)
│   └── docker/             # Docker environment and network configuration
│       ├── docker-compose.yml
│       ├── kali-data/      # Persistent data for Kali container
│       └── .docker/        # Container build contexts
│           ├── caldera/    # MITRE Caldera container
│           ├── db/         # Database container
│           ├── kali/       # Kali attacker container
│           └── nginx/      # Target web server container
│
├── scripts/                # Cross-platform dev/setup/migrate runners
└── docs/                   # Project documentation
```
---

# Configuration

The backend reads its settings from `PIPELINE_UI_*` environment variables (see `apps/backend/README.md`). The Caldera URL and API key default to `http://localhost:8888` / `ADMIN123` for the native backend (Caldera is published on `127.0.0.1:8888`). The containerized backend (`containers` profile) joins `caldera-mgmt-network` and reaches Caldera at `http://caldera-c2:8888`. Both can be overridden with `PIPELINE_UI_CALDERA_URL` and `PIPELINE_UI_CALDERA_KEY`. Runtime data lives in the database; the curated cases are loaded once from the versioned read-only seeds in `apps/backend/app/seeds/data`.

---

# Running the Environment

## 1️⃣ Start the Docker infrastructure

```bash
cd docker
docker-compose up --build
```

Wait until all containers are built and running.

---

## 2. Run the backend

Apply migrations and start the API (`npm run dev` from the repository root, or see `apps/backend/README.md`). Abilities and adversaries are loaded into Caldera by the pipeline from the database.

---

# Accessing the Adversary Emulation Platform

Open your browser and navigate to:

```
http://localhost:8888
```

Login credentials:

```
user: red
password: admin
```

Then go to:

```
Operations
```

to observe the running campaigns:
APT41 – DUST
ATT&CK Campaign C0010
ATT&CK Campaign C0026
CostaRicto
Operation MidnightEclipse
Operation Outer Space
Salesforce Data Exfiltration
ShadowRay

---

# Experimental Goal

The purpose of this framework is to:

- Evaluate **structural completeness of STIX CTI datasets**
- Measure the **procedural requirements needed for campaign execution**
- Identify **missing operational knowledge** required to reproduce APT campaigns
- Support **automated adversary emulation experiments**

---

# Experiment Workflow

The framework implements a pipeline that converts **structured CTI (STIX campaigns)** into executable adversary behaviors in a controlled emulation environment.

The workflow is divided into three stages:

- **Stage 1** performs automated structural modeling of CTI data.  
- **Stage 2** introduces a human-in-the-loop translation from abstract behavioral descriptions to minimal executable steps.  
- **Stage 3** integrates these steps into an adversary emulation environment.

The diagram below is **conceptual rather than an implementation diagram** and does not assume any specific serialization.

```mermaid
flowchart TD

A[STIX Campaign Dataset]

B[Stage 1<br>Structural Modeling<br><br>Automated parsing of CTI data<br>Technique extraction<br>Relationship modeling]

C[Stage 2<br>Human-in-the-Loop Translation<br><br>Abstract behavior → executable steps<br>Command identification<br>Dependency resolution]

D[Stage 3<br>Adversary Emulation<br><br>Integration into emulation environment<br>Campaign execution<br>Experimental evaluation]

A --> B
B --> C
C --> D
```

---

# Stage 1 — Structural Modeling of CTI

In the first stage, the framework performs **automated structural modeling** of cyber threat intelligence data.

The system parses STIX campaign datasets and extracts relevant structural elements, including:

- Techniques  
- Relationships  
- Indicators  
- Infrastructure  
- Malware  
- Campaign metadata  

This process constructs an intermediate representation of the campaign by identifying:

- attack techniques  
- dependencies between actions  
- structural relationships within the campaign  

The result is a **machine-readable model of adversary activity derived directly from CTI data**.

---

# Stage 2 — Human-in-the-Loop Translation

The second stage introduces a **human-in-the-loop translation process**.

Structured CTI often describes adversary behavior at an abstract level (e.g., techniques or tactics) rather than providing precise execution procedures. In this stage, an analyst translates these abstract behavioral descriptions into **minimal executable steps**.

This step includes:

- identifying the concrete commands required to reproduce a technique  
- resolving implicit environmental assumptions  
- defining execution order and dependencies  
- specifying required privileges or infrastructure  

The outcome is a set of **minimal operational procedures** that can be executed by an adversary emulation platform.

---

# Stage 3 — Adversary Emulation

In the final stage, the translated procedures are integrated into an **adversary emulation environment**.

The generated steps are converted into executable components and deployed within a controlled laboratory infrastructure built with Docker containers.

The environment includes multiple hosts representing a simplified enterprise architecture, such as:

- attacker node  
- command and control infrastructure  
- intermediary services  
- backend systems  

Campaign execution is then orchestrated using an adversary emulation platform, allowing the system to observe whether the CTI-derived behaviors can be successfully reproduced.

This stage enables **empirical evaluation of procedural completeness in structured CTI datasets**.

# License
GNU GPLv3.
