# Project status — meeting of October 1, 2026

## Executive summary

The Pipeline UI is implemented end to end in mocked mode and its three stages
were inspected in the browser with the ShadowRay campaign. The frontend, the
backend, persistence, containment, the Caldera integration, and the real-time
channels all exist and have automated coverage. The only milestone still
unproven is the full execution in the real Docker lab after fixing the payload
sent to Caldera.

## What can be demonstrated

1. Selection among the eight curated cases and progress per stage.
2. Structural modeling of ShadowRay in Stage 1.
3. Abilities, commands, dependencies, and Adversary in Stage 2.
4. Safe preview of the commands and targets in Stage 3.
5. Explicit confirmation before any execution.
6. Blocking of external targets and of non-isolated containers.
7. Light/dark theme (following the operating system and persistent across
   reloads), pt-BR/en with SVG flags, and session persistence.
8. Full mocked flow with results, aggregation, and audit.
9. A development environment with a single command at the root (`npm run dev`)
   that brings up the backend, frontend, and prepares the database all at once.

## Status by area

| Area | Status | Evidence |
|---|---|---|
| Scaffold, database, and migrations | Complete | FastAPI, React/Vite, SQLAlchemy, and Alembic |
| Eight curated cases | Complete | API lists and loads all cases |
| Containment and isolation | Complete | Properties 1 and 2 + security suite |
| Caldera API | Implemented | Mocked client and contracts; real payload fixed |
| Three interface stages | Complete | Visual audit of ShadowRay |
| WebSocket | Fixed | Three subscriptions; no loop after close 1000 |
| Theme and internationalization | Complete | Theme follows the OS and persists on reload; SVG flags; tests 11/12 |
| Developer experience | Complete | Single `npm run dev` at the root (backend+frontend+migration), unified node_modules |
| Mocked E2E | Complete | Preview → confirmation → execution → audit |
| Real Docker E2E | Pending | Kali/Caldera build and a new ShadowRay execution |

## Prioritized open items

1. Stabilize the Kali image build against the mirror's HTTP 403 errors.
2. Repeat the real ShadowRay E2E after adding `technique_name`.
3. Confirm the 11 results and the audit trail in the database.
4. Implement the optional property tests still missing: Properties 3, 4,
   7, 8, 9, and 10, plus the real local Stage 1 test.
5. Close the final checkpoint only after the real E2E.

## Risks and decisions

- The lab stays contained in the networks `172.20/21/22.0.0/24`; the
  `192.168.40.0/24` network is exclusive to Caldera's management plane.
- Port 8000 will not be used by this project. The application uses 15173/8010.
- The real test is opt-in and does not run automatically in development or CI.
- The detailed, resumable checklist is in `.kiro/specs/pipeline-ui/tasks.md`.
