# Pipeline UI — Backend (FastAPI)

Backend for the project's **Pipeline UI** (`sticks`). It orchestrates the
existing three-stage pipeline, exposes the REST/WebSocket API to the frontend,
enforces the containment checks, and persists state.

This project is **additive**: it reuses (without duplicating) the code and
configuration of the existing `sticks/` package at the repository root.

## Structure

```
ui/backend/
  app/
    core/       # configuration + integration with the existing sticks pipeline
    models/     # SQLAlchemy domain models
    services/   # application services (CampaignService, StageService, ...)
    api/        # REST routes
    ws/         # WebSocket/SSE channels
    main.py     # app factory + root router
  tests/
  pyproject.toml
```

## Development (native hot-reload)

The project uses **PostgreSQL only** — there is no SQLite anywhere. The
infrastructure (including Postgres) runs via Docker; the backend and frontend
run natively on the host with hot-reload, pointing at the containerized Postgres.

```bash
# 1. start the infrastructure (includes Postgres published on 127.0.0.1:5432)
cd docker && docker compose up -d

# 2. prepare the native environment once (backend venv + node_modules)
#    from the repository root
npm run setup

# 3. run frontend + backend natively with hot-reload, against the containerized
#    Postgres (applies the Alembic migrations before serving)
npm run dev
```

- UI: http://localhost:8080
- API: http://localhost:8010 — health check: `GET /health` → `{"status": "ok"}`

The default database (`PIPELINE_UI_DATABASE_URL`) already points at the
containerized Postgres on `localhost:5432`. The compose `backend` service
(`containers` profile, opt-in) overrides the host to `postgres:5432`.

No real Caldera or Docker attack network is used in development; everything
uses mocks/fixtures.

## Tests

```powershell
# from ui/backend, with the venv active
pytest
```

The suite uses only mocks/fixtures and does not touch Caldera or the Docker
attack network. The end-to-end integration test against the **REAL**
environment (`tests/test_e2e_real_lab.py`, Task 15.3) is **skipped by default** —
it sits behind the `lab` marker (deselected via `-m "not lab"` in
`pyproject.toml`) and behind `skipif(PIPELINE_UI_LAB != "1")`, so it never runs
in dev/CI. To run it on the **lab machine** (real Caldera + hardened compose,
containment satisfied), see [`LAB.md`](./LAB.md).
