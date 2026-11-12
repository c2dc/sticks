# ops/

Deployment and operations assets for STICKS, organized by target platform.

| Folder | Purpose | State |
| --- | --- | --- |
| `docker/` | Docker Compose stack: postgres+pgvector, Caldera, attack lab (Kali, nginx target, database target). | Implemented |
| `kubernetes/` | Kubernetes deployment of the same components. | Planned, not implemented |
| `aws/` | Cloud deployment on AWS. | Planned, not implemented |
| `database/` | Platform-agnostic PostgreSQL bootstrap, backup and restore scripts. | Implemented |

## ops/ versus setup

`ops/` is about *running* the infrastructure: where the containers or pods live, how the database is
bootstrapped, backed up and restored. The setup (installation and data ingestion, such as loading
the ATT&CK/STIX catalog and seed data) is about *preparing the application* once the infrastructure
is up. Setup is not part of `ops/`; it can run against any platform listed here.

## Conventions

- No secrets are stored here. Credentials come from environment variables or a `.env` file that is not committed.
- Compose project name (`sticks`), network names and container names do not depend on this folder layout.