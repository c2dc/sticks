# The Procedural Semantics Gap in Structured CTI  
### A Measurement-Driven STIX Analysis for APT Emulation

📄 Paper: https://arxiv.org/abs/2512.12078

---

## Authors

- **Ágney Lopes Roth Ferraz**  
- **Sidnei Barbieri**  
- **Murray Evangelista de Souza**  
- **Lourenço A. Pereira Júnior**

---

## Overview

This repository accompanies the paper:

> **The Procedural Semantics Gap in Structured CTI: A Measurement-Driven STIX Analysis for APT Emulation**

It provides an experimental environment to evaluate **procedural completeness of STIX-based threat intelligence** and its ability to reproduce **APT campaign behaviors** in a controlled laboratory environment.

The repository includes:

- A **Docker-based adversary emulation environment**
- Automated generation of attacker commands
- Integration with **MITRE Caldera**
- Tools for evaluating the procedural gaps in structured CTI

---

# Architecture

The environment uses multiple Docker containers connected through static networks.

<p align="center">
  <img src="docker/architecture.png" alt="Network Architecture" width="85%">
</p>

---

# Static Network Configuration

| Network | Subnet | Host | IP |
|------|------|------|------|
| caldera-kali-network | 172.20.0.0/24 | caldera | 172.20.0.10 |
| caldera-kali-network | 172.20.0.0/24 | kali | 172.20.0.20 |
| kali-nginx-network | 172.21.0.0/24 | kali | 172.21.0.10 |
| kali-nginx-network | 172.21.0.0/24 | nginx | 172.21.0.20 |
| nginx-db-network | 172.22.0.0/24 | nginx | 172.22.0.10 |
| nginx-db-network | 172.22.0.0/24 | db | 172.22.0.20 |

---

# Repository Structure

```
.
├── architecture.png        # Network architecture diagram
│
├── docker/                 # Docker environment and network configuration
│   ├── docker-compose.yml
│   ├── kali-data/          # Persistent data for Kali container
│   └── .docker/            # Container build contexts
│       ├── caldera/        # MITRE Caldera container
│       ├── db/             # Database container
│       ├── kali/           # Kali attacker container
│       └── nginx/          # Target web server container
│
├── sticks/                 # STIX analysis and campaign generation framework
│   ├── config/             # Configuration files
│   ├── data/               # Dataset and intermediate artifacts
│   ├── lib/                # Core libraries
│   ├── tools/              # Utility scripts
│   ├── main.py             # Main execution entry point
│   ├── requirements.txt    # Python dependencies
│   └── README.md           # STIX framework documentation
```
---

# Requirements

Before running the system, install the required Python dependencies:

```bash
pip install -r requirements.txt
```

---

# Configuration

Open the file:

```
config/config.py
```

and configure the following variables.

## GitHub Token

Insert your GitHub token:

```python
GITHUB_TOKEN = "your_token_here"
```

You can generate one here:

https://github.com/settings/tokens

---

## Optional: Azure Integration

If you want to generate attacker commands using Azure, configure:

```python
AZURE_SECRET_KEY
AZURE_ENDPOINT
AZURE_DEPLOYMENT
```

Otherwise, these fields can remain empty.

---

# Running the Environment

## 1️⃣ Start the Docker infrastructure

```bash
cd docker
docker-compose up --build
```

Wait until all containers are built and running.

---

## 2️⃣ Initialize the STIX environment

Move to the `sticks` directory and run:

```bash
python tools/empty_caldera.py
python main.py init
```

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

# Citation

If you use this repository in your research, please cite:

```bibtex
@article{ferraz2025procedural,
  title={The Procedural Semantics Gap in Structured CTI: A Measurement-Driven STIX Analysis for APT Emulation},
  author={Ferraz, Ágney Lopes Roth and Barbieri, Sidnei and de Souza, Murray Evangelista and Pereira Júnior, Lourenço A.},
  year={2025},
  journal={arXiv preprint arXiv:2512.12078}
}
```

---

# License
GNU GPLv3.
