# LAB.md — End-to-end integration with the REAL environment (Task 15.3)

> ⚠️ **This procedure runs EXCLUSIVELY on the user's lab machine.** It executes
> **real** adversary emulation against the real Caldera and the real Docker
> environment containers. **Never** run it in the development environment nor in
> CI.

The test `tests/test_e2e_real_lab.py` drives 1–2 curated cases end to end with
**real Caldera** at `http://localhost:8888` and the **real Docker environment**,
with containment effectively satisfied, validating the emulation and the
**persisted audit trail** (Requirements **4.2, 4.4, 6.3, 6.8**).

It uses the **real CalderaClient** (no mock) and the **real isolation inspector**
(read-only, via the Docker Engine API). Outside the lab machine it is **skipped**
by two independent guards:

- the `lab` marker, deselected by default via `addopts = -m "not lab"` in
  `pyproject.toml`; and
- `@pytest.mark.skipif(os.getenv("PIPELINE_UI_LAB") != "1", ...)`.

This way, the full suite stays green in dev/CI without any lab environment.

## Containment prerequisites

By design decision (*"Recorded decision — hardening via a separate compose
(Option A)"*), containment is only satisfied on the lab machine with the
**hardened** compose applied. The file `docker/docker-compose.hardened.yml` is a
**lab-only reference**: it does not change the `docker-compose.yml` used in
development by the other team members. The hardened compose must:

- remove `local-network` (bridge) from `kali`, `nginx` and `db`, leaving them
  **only** on the `internal: true` networks — `192.168.10.0/24`,
  `192.168.20.0/24`, `192.168.30.0/24`;
- not configure external DNS on those containers;
- pre-satisfy the egress dependencies of the curated commands offline
  (`apt-get`, `pip install`, `git clone`, external `wget`/`curl`), since the
  Operation is blocked if any destination falls outside the internal subnets.

The containment pre-flight (destinations + isolation) refuses execution while
that containment is not satisfied — that is what makes the real run safe.

## How to run on the lab machine

1. Bring up the hardened stack (lab-only reference file):

   ```powershell
   docker compose -f docker/docker-compose.yml -f docker/docker-compose.hardened.yml up -d
   ```

2. Confirm Caldera responds at `http://localhost:8888` (header `KEY`).

3. From `apps/backend`, with the venv active, set the opt-in variable and run
   only the lab test:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   $env:PIPELINE_UI_LAB = "1"
   pytest -m lab
   ```

   (POSIX alternative: `PIPELINE_UI_LAB=1 pytest -m lab`.)

### Options

- **Cases executed** — by default it runs 1 case (`shadowray`). To choose 1–2
  cases, set `PIPELINE_UI_LAB_CASES` with comma-separated slugs, for example
  `$env:PIPELINE_UI_LAB_CASES = "shadowray,c0010"`.
- **Database** — by default the audit trail is persisted in a temporary SQLite.
  To use the lab's real PostgreSQL, point `PIPELINE_UI_DATABASE_URL` at it before
  running.

If Caldera is not up when the test runs, it **skips** with a message asking to
bring up the hardened stack — instead of failing.

## What the test validates

- **Req. 4.2** — loads the case's Abilities + Adversary and starts a real
  Operation in Caldera against the Docker environment.
- **Req. 4.4/4.5** — persists the per-Ability result and the aggregate
  (`total_sucesso`/`total_falha`), consistent with each other.
- **Req. 6.3** — the **real** isolation pre-flight (read-only) confirms that the
  target containers are isolated before any execution.
- **Req. 6.8** — the audit trail is persisted **1:1**: exactly one record per
  executed command, with command + target container + result, with no duplicates.
