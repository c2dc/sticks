# ops/kubernetes

Goal: run STICKS on a Kubernetes cluster.

**Status: not implemented yet (planned).** This folder intentionally holds no manifests, Helm charts or
Kustomize overlays. They will be added when this target is designed.
## Component map

| Component | Intended workload |
| --- | --- |
| backend | Deployment + Service |
| frontend | Deployment + Service (or static hosting behind an Ingress) |
| postgres + pgvector | StatefulSet with a persistent volume, using a pgvector-enabled PostgreSQL image; see `../database/` |
| caldera | Deployment + Service, reachable only from the control plane |
| lab (Kali attacker, nginx target, database target) | Isolated namespace with deny-all NetworkPolicies |

Whatever the platform, the lab networks must stay isolated (no route from the lab to the internet or
to the control plane), as the Docker setup enforces today. See `../docker/CONTAINMENT_CHANGES.md`.