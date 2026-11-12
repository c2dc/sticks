# ops/aws

Goal: run STICKS on AWS.

**Status: not implemented yet (planned).** This folder intentionally holds no infrastructure-as-code.
The options below are generic candidates, not decisions.
## Component map

| Component | Intended workload |
| --- | --- |
| backend | Container service |
| frontend | Container service or static hosting |
| postgres + pgvector | Managed PostgreSQL with pgvector, or a container with a persistent volume; see `../database/` |
| caldera | Container service in the control-plane network |
| lab (Kali attacker, nginx target, database target) | Isolated network segment without internet egress |

Whatever the platform, the lab networks must stay isolated (no route from the lab to the internet or
to the control plane), as the Docker setup enforces today. See `../docker/CONTAINMENT_CHANGES.md`.
## Generic options to evaluate

- Containers on a single EC2 instance running the Docker Compose stack from `../docker/`.
- A container orchestrator (ECS or EKS) for the services.
- A managed PostgreSQL service, provided the pgvector extension is supported.
- A dedicated, isolated network segment for the lab.