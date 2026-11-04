# Longhorn

### LLM-Driven Autonomous APT Campaign Analysis and Emulation

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![LLM](https://img.shields.io/badge/AI-LLM--Driven-purple.svg)](#architecture)
[![STIX](https://img.shields.io/badge/CTI-STIX%202.x-orange.svg)](https://oasis-open.github.io/cti-documentation/)
[![MITRE ATT&CK](https://img.shields.io/badge/Framework-MITRE%20ATT%26CK-red.svg)](https://attack.mitre.org/)
[![Docker](https://img.shields.io/badge/Environment-Docker-blue.svg)](https://www.docker.com/)
[![Caldera](https://img.shields.io/badge/Emulation-MITRE%20Caldera-green.svg)](https://caldera.mitre.org/)

**Longhorn** is an LLM-driven software framework designed to automate the analysis, reconstruction, and emulation of Advanced Persistent Threat (APT) campaigns.

The framework leverages Large Language Models (LLMs) to interpret structured Cyber Threat Intelligence (CTI), reconstruct adversary workflows, derive execution dependencies, identify environmental requirements, and generate the artifacts necessary for controlled adversary emulation.

Unlike conventional CTI processing pipelines that rely primarily on predefined extraction and transformation rules, Longhorn employs LLM-based reasoning to automate the transition from descriptive threat intelligence to structured and executable representations.

Its central objective is to establish an autonomous workflow capable of transforming APT campaign intelligence into reproducible experimental scenarios, minimizing manual intervention throughout the process.

---

## Table of Contents

- [Objectives](#objectives)
- [Design Principles](#design-principles)
- [Architecture](#architecture)
- [Main Components](#main-components)
  - [CTI Acquisition](#1-cti-acquisition)
  - [LLM-Based Intelligence Analysis](#2-llm-based-intelligence-analysis)
  - [Campaign Reconstruction](#3-campaign-reconstruction)
  - [Environment and SUT Generation](#4-environment-and-sut-generation)
  - [Automated Emulation Artifact Generation](#5-automated-emulation-artifact-generation)
  - [Validation and Execution](#6-validation-and-execution)
- [Technology Stack](#technology-stack)
- [Repository Structure](#repository-structure)
- [Installation](#installation)
- [Configuration](#configuration)
- [Automated Workflow](#automated-workflow)
- [LLM Orchestration](#llm-orchestration)
- [Experimental Environment](#experimental-environment)
- [Research Context](#research-context)
- [Security and Responsible Use](#security-and-responsible-use)
- [Development Status](#development-status)
- [Citation](#citation)
- [License](#license)

---

## Objectives

The primary objectives of Longhorn are:

- **Automated CTI Interpretation:** Use LLMs to interpret structured and unstructured threat intelligence.
- **APT Campaign Reconstruction:** Reconstruct adversary activities and their logical relationships from campaign descriptions.
- **Behavioral Modeling:** Automatically derive execution workflows from ATT&CK techniques and campaign context.
- **Dependency Inference:** Identify procedural dependencies, preconditions, and execution constraints.
- **Environment Generation:** Derive target system characteristics and infrastructure requirements.
- **Automated Artifact Generation:** Generate structured emulation artifacts from reconstructed campaign workflows.
- **Automated Validation:** Evaluate generated representations against predefined structural and environmental constraints.
- **End-to-End Automation:** Minimize manual intervention across the CTI-to-emulation pipeline.

## Design Principles

Longhorn is based on five fundamental design principles.

| Principle | Description |
|---|---|
| Autonomy | LLM-driven processing across the complete workflow |
| Context Awareness | Campaign-specific reasoning grounded in CTI |
| Structured Generation | Machine-readable intermediate representations |
| Iterative Validation | Automated verification and refinement |
| Reproducibility | Consistent generation of experimental scenarios |

## Architecture

Longhorn adopts an LLM-centered architecture in which intelligence processing, reasoning, generation, and validation are coordinated through an automated workflow.

```text
                 +-------------------------+
                 |       CTI Sources       |
                 |                         |
                 |  MITRE ATT&CK            |
                 |  STIX / TAXII            |
                 |  MISP / OpenCTI          |
                 |  APT Reports             |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |    CTI Acquisition      |
                 |    and Normalization   |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |    LLM Orchestrator     |
                 |                         |
                 |  Context Management     |
                 |  Task Decomposition     |
                 |  Agent Coordination     |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |   Intelligence Analysis |
                 |                         |
                 |  Technique Extraction   |
                 |  Context Interpretation|
                 |  Relationship Inference|
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |  Campaign Reconstruction|
                 |                         |
                 |  Workflow Generation    |
                 |  Dependency Modeling    |
                 |  Constraint Analysis    |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |   Environment Modeling |
                 |                         |
                 |  Platform Identification|
                 |  Software Requirements  |
                 |  SUT Specification       |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |   Artifact Generation   |
                 |                         |
                 |  Structured Workflows   |
                 |  Emulation Specifications|
                 |  Validation Metadata    |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |   Automated Validation |
                 |                         |
                 |  Schema Verification    |
                 |  Dependency Validation  |
                 |  Consistency Checking   |
                 +------------+------------+
                              |
                              v
                 +-------------------------+
                 |   Emulation Environment |
                 |                         |
                 |  MITRE Caldera          |
                 |  Docker-based SUTs       |
                 +-------------------------+
```

## Main Components

### 1. CTI Acquisition

Collects and normalizes threat intelligence from multiple sources.

Potential sources include:

- MITRE ATT&CK STIX repositories.
- STIX/TAXII-compatible services.
- MISP.
- OpenCTI.
- AlienVault Open Threat Exchange (OTX).
- Structured and unstructured APT reports.

### 2. LLM-Based Intelligence Analysis

Uses Large Language Models to interpret collected threat intelligence and extract campaign-relevant information.

Core responsibilities include:

- Campaign description interpretation.
- Technique identification and mapping.
- Adversary behavior extraction.
- Relationship identification.
- Contextual enrichment.
- Cross-source information correlation.

### 3. Campaign Reconstruction

Transforms extracted intelligence into structured representations of adversary activities.

The LLM-based analysis supports:

- Activity ordering.
- Dependency inference.
- Preconditions identification.
- Behavioral relationship modeling.
- Workflow reconstruction.
- Campaign-specific execution constraints.

The resulting representation is intended to capture both the behavioral characteristics and the procedural relationships necessary for subsequent processing.

### 4. Environment and SUT Generation

Derives the environmental specifications required to represent the target infrastructure.

Relevant characteristics include:

- Operating system requirements.
- Software dependencies.
- Service configurations.
- Platform compatibility.
- Vulnerability associations.
- Network topology.
- System Under Test (SUT) specifications.

The generated environment specifications provide the basis for constructing reproducible experimental scenarios.

### 5. Automated Emulation Artifact Generation

Transforms validated campaign representations into structured emulation artifacts.

This component supports:

- Workflow serialization.
- Technique-to-artifact association.
- Execution metadata generation.
- Dependency preservation.
- Adversary profile specification.
- Integration with emulation platforms.

### 6. Validation and Execution

Performs automated verification of generated artifacts and their consistency with the reconstructed campaign representation.

Validation dimensions include:

- Structural correctness.
- Schema compliance.
- Dependency consistency.
- Environmental compatibility.
- Technique traceability.
- Workflow completeness.

The framework is designed to integrate with MITRE Caldera for controlled execution and experimental evaluation.

## Technology Stack

| Technology | Purpose |
|---|---|
| Python | Core implementation and orchestration |
| Large Language Models | Intelligence interpretation and automated reasoning |
| STIX 2.x | Structured CTI representation |
| MITRE ATT&CK | Adversary tactics and techniques |
| TAXII 2.x | CTI collection and distribution |
| LangChain / LLM APIs | Potential model orchestration and integration |
| Pydantic | Structured output validation |
| JSON | Intermediate representations and artifact serialization |
| MITRE Caldera | Adversary emulation |
| Docker | Isolated experimental environments |

*The technology list should be adjusted to reflect the libraries and services actually implemented in the repository.*

## Repository Structure

The following is a conceptual representation of the project organization:

```text
Longhorn/
├── config/
│   └── config.py
│
├── data/
│   ├── stix/
│   ├── apt_campaigns/
│   ├── reports/
│   ├── generated_workflows/
│   └── emulation_artifacts/
│
├── lib/
│   ├── cti/
│   ├── llm/
│   ├── orchestration/
│   ├── agents/
│   ├── campaigns/
│   ├── workflows/
│   ├── environment/
│   ├── validation/
│   └── caldera/
│
├── prompts/
│   ├── analysis/
│   ├── reconstruction/
│   ├── environment/
│   └── validation/
│
├── schemas/
│   ├── campaign.schema.json
│   ├── workflow.schema.json
│   └── environment.schema.json
│
├── plugins/
│
├── scripts/
│
├── tests/
│
├── requirements.txt
├── README.md
└── main.py
```

> **Note:** This structure is illustrative and should be adapted to the actual repository.

## Installation

### Prerequisites

- Python 3.10 or later.
- Git.
- Docker and Docker Compose.
- Access to an LLM provider or locally hosted model.
- MITRE Caldera for adversary emulation.
- Access to the required CTI sources.

### Clone the Repository

```bash
git clone <repository-url>
cd Longhorn
```

### Create a Virtual Environment

```bash
python -m venv .venv
```

Activate it:

**Linux:**

```bash
source .venv/bin/activate
```

**Windows:**

```powershell
.venv\Scripts\activate
```

### Install Dependencies

```bash
pip install -r requirements.txt
```

## Configuration

Configure the project parameters in:

```text
config/config.py
```

The configuration should define:

- CTI source locations.
- STIX dataset directories.
- LLM provider and model selection.
- API credentials.
- Prompt and schema locations.
- Generated artifact directories.
- Caldera integration endpoints.
- Docker environment parameters.

API credentials should be provided through environment variables rather than committed to the repository.

Example:

```bash
export LLM_API_KEY="your-api-key"
export LLM_MODEL="your-model"
export CALDERA_URL="http://localhost:8888"
```

## Automated Workflow

Longhorn organizes its processing into three autonomous stages.

### Stage 1 — CTI Acquisition and Intelligence Interpretation

The framework collects structured and unstructured threat intelligence and uses LLM-based analysis to extract campaign information, identify ATT&CK techniques, and establish contextual relationships.

**Output:**
- Normalized CTI.
- Campaign profiles.
- Technique mappings.
- Contextual intelligence representations.

### Stage 2 — Autonomous Campaign and Environment Reconstruction

LLM-based reasoning reconstructs campaign workflows, infers dependencies, identifies execution constraints, and derives the characteristics of the required target environment.

**Output:**
- Structured campaign workflows.
- Dependency representations.
- Environment specifications.
- SUT descriptions.

### Stage 3 — Automated Artifact Generation and Validation

The framework transforms reconstructed representations into structured emulation artifacts, validates their consistency, and prepares them for integration with the experimental environment.

**Output:**
- Validated workflow representations.
- Emulation specifications.
- Execution metadata.
- Experimental environment definitions.

## LLM Orchestration

LLMs constitute the central reasoning and automation mechanism in Longhorn.

Rather than relying exclusively on predefined transformation rules, the framework uses model-driven processing to interpret campaign context and generate structured intermediate representations.

The orchestration layer is responsible for coordinating the following logical tasks:

1. Intelligence interpretation.
2. Campaign information extraction.
3. Technique relationship inference.
4. Procedural workflow reconstruction.
5. Environment requirement derivation.
6. Structured artifact generation.
7. Automated consistency validation.

The framework can employ different models and orchestration strategies depending on the computational requirements and complexity of each task.

### Structured Outputs

LLM-generated outputs should conform to predefined schemas to support interoperability and automated processing.

Examples include:

- Campaign representations.
- Technique mappings.
- Workflow graphs.
- Dependency specifications.
- Environment descriptions.
- Emulation metadata.

### Iterative Refinement

Generated representations can undergo automated validation and refinement.

When a representation fails predefined constraints, the orchestration layer can request additional analysis and regenerate the affected representation.

This process aims to improve consistency and reduce the need for manual correction.

## Experimental Environment

Longhorn supports the construction of controlled experimental environments using containerized infrastructure.

A typical environment may include:

- An emulation management server.
- Dedicated simulation hosts.
- Application servers.
- Database services.
- Isolated Docker networks.
- Target systems representing campaign-specific environments.

The generated specifications can be used to support reproducible experimental scenarios.

## Research Context

Longhorn investigates the application of Large Language Models to the autonomous transformation of structured and unstructured Cyber Threat Intelligence into executable adversary emulation representations.

Its central research premise is that LLMs can bridge part of the gap between descriptive threat intelligence and the procedural and environmental information required for emulation.

The framework explores whether autonomous reasoning can support:

- Reconstruction of adversary execution workflows.
- Inference of missing procedural relationships.
- Identification of environmental dependencies.
- Generation of structured emulation artifacts.
- Automated validation of generated representations.

A fundamental distinction is made between extracting information explicitly available in CTI sources and inferring additional information required to construct executable workflows.

Inferred information should remain distinguishable from source-grounded evidence to preserve traceability and support experimental reproducibility.

The framework provides an experimental foundation for evaluating the capabilities and limitations of LLM-driven automation in APT campaign emulation.

## Security and Responsible Use

Longhorn is intended for cybersecurity research, defensive security evaluation, and controlled adversary emulation.

All generated artifacts and experiments should be restricted to authorized and isolated environments.

The framework should enforce appropriate execution boundaries, validation requirements, and access controls when interacting with external systems.

Users are responsible for ensuring that their activities comply with applicable laws, institutional policies, and authorization requirements.

## Development Status

Longhorn is under active research and development.

Its architecture and capabilities may evolve as new LLM orchestration strategies, CTI sources, and automated validation mechanisms are incorporated.

## Citation

If you use Longhorn in academic research, please cite the associated research publication.

```bibtex
@misc{longhorn,
  title        = {Longhorn: An LLM-Driven Framework for
                  Autonomous APT Campaign Analysis and Emulation},
  author       = {Ferraz, Ágney Lopes Roth and collaborators},
  year         = {2026},
  note         = {Research software}
}
```

## License

License information will be provided in this repository.

## Acknowledgments

- [MITRE ATT&CK](https://attack.mitre.org/)
- [OASIS Cyber Threat Intelligence (CTI)](https://www.oasis-open.org/committees/cti/)
- [MITRE Caldera](https://caldera.mitre.org/)
- The open-source LLM research community.
