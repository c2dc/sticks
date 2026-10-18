# Implementation Plan: English Standardization and Restructure

## Overview

This plan implements the mechanical, non-behavioral refactor that makes STICKS English-only (STIX-aligned), reorganizes the repository into the locked **Proposal 2** layout (`apps/` + `packages/` + `infra/`), and removes clutter. It follows the design's recommended sequencing to minimize churn and avoid wire-contract drift: freeze the terminology table, rename the backend code layer, rename the frontend code layer **in lockstep** with the backend contract, apply the Option A database rename migration, translate docs, remove clutter, and perform the directory move **last** as a single mechanical step, then run the full verification gate.

Every rename resolves through the authoritative §2 STIX Terminology Table in `design.md`. All renames and moves preserve runtime behavior; correctness is proven by the pre-existing `pytest`/`vitest` suites, the unweakened i18n parity/terminology tests, and the end-to-end six-stage ShadowRay flow (design §7 Testing Strategy). The design uses a specific stack (FastAPI/Python backend, React/TypeScript frontend) rather than pseudocode, so no implementation-language choice is needed. There is no Correctness Properties section in the design (it is a rename-and-move refactor), so there are no property-based test tasks — verification is by the existing suites plus the end-to-end flow.

Hard boundaries carried through every task:
- Never touch `ui/frontend/src/i18n/**` or rendered UI copy (Req 1.3, 4.5, 9.4).
- Legacy CLI (`sticks/`): move and fix its `architecture.png` reference only — **its identifiers are explicitly OUT OF SCOPE for renaming** (Req 6.4).
- Backend-contract (§3a route + §3c field) renames and the mirrored frontend-wrapper/type renames are **coupled in lockstep** — never done in isolation (Req 3.6).

## Tasks

- [x] 1. Freeze terminology and establish a completion baseline
  - Confirm the §2 STIX Terminology Table in `design.md` is the single source of truth for every rename across backend, frontend, DB, and docs; resolve each Portuguese artifact's morphemes through it (e.g. `*Estagio*`→`*Stage*`, `*Resultado*`→`*Result*`, `*Campanha*`→`*Campaign*`, `caso_id`→`campaign_id`), preserving casing conventions.
  - Record the §2.4 divergences explicitly (e.g. `atores`→`adversaries` as STIX Intrusion Set; URL path segment `actors` vs. domain `adversaries`) so implementers apply them consistently.
  - Run a repo-wide grep baseline for the §2 Portuguese terms **excluding** `ui/frontend/src/i18n/**` and the legacy CLI (`sticks/` / future `packages/cli/**`); capture the match count as the completion metric to be driven to zero by task 9.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 2. Backend code layer — routes, modules, symbols, docstrings (paths still `ui/backend`)
  - [x] 2.1 Rename backend API route paths to English/STIX paths (§3a)
    - In `ui/backend/app/api/`, rename the `/casos` family to the `/campaigns` family and the remaining Portuguese segments per §3a: `/casos`→`/campaigns`, `.../estagio/{n}`→`.../stage/{n}`, `.../provisionar`→`.../provision`, `.../concluir`→`.../complete`, `.../escolher-dag`→`.../choose-dag`, `.../operacao`→`.../operation`, `.../relatorio`→`.../report`, `.../topologia`→`.../topology`, `/auditoria/{operacao}`→`/audit/{operation}`, `/preferencias`→`/preferences`, `/estado-sessao`→`/session-state`, and the STIX sub-paths `.../{versao}`→`.../{version}`, `.../campanhas`→`.../campaigns`, `.../atores`→`.../actors`.
    - Reconcile `/baixar` onto `/download`: remove the Portuguese `/stix/bases/{versao}/baixar` route in `casos.py` and point its single caller at the canonical `/stix/bases/{version}/download` in `stix.py` (behavior-preserving consolidation).
    - Preserve the Stage 6 fix: the topology route rename (`/casos/.../topologia`→`/campaigns/.../topology`) must keep the Stage 6 topology lookup intact.
    - COUPLED WITH 3.4 (frontend wrappers): apply each route rename and its frontend caller change together; do not merge this task without 3.4.
    - _Requirements: 3.3, 3.6, 8.6, 9.3_
  - [x] 2.2 Rename backend API module files and update router registration (§3b)
    - Rename `casos.py`→`campaigns.py`, `emulacao.py`→`emulation.py`, `auditoria.py`→`audit.py`, `preferencias.py`→`preferences.py`, `topologia.py`→`topology.py`, `estado_sessao.py`→`session_state.py` in `ui/backend/app/api/`; leave already-English `stix.py`, `health.py`, `router.py` unchanged.
    - Update every import and the `router.py` registration to the new module names.
    - _Requirements: 3.1_
  - [x] 2.3 Rename backend environment service module files (§3b)
    - In `ui/backend/app/services/environment/`, rename `provisionador.py`→`provisioner.py`, `operacionalizacao.py`→`operationalization.py`, `operacionalizador.py`→`operationalizer.py`, `conclusao.py`→`completion.py`, `execucao.py`→`execution.py`, `medicao.py`→`measurement.py`, `relatorio.py`→`report.py`, `saude.py`→`health.py`, `topologia_builder.py`→`topology_builder.py`; leave `compose_executor.py`, `log_buffer.py`, `stage_service.py` unchanged.
    - Rename `case_service.py`→`campaign_service.py` under the flagship rule and update all imports.
    - Keep the HOST_CONTAINERS isolation resolution of the Stage 6 fix intact through the renames.
    - _Requirements: 3.2, 8.6, 9.3_
  - [x] 2.4 Rename backend Python symbols, DTOs, functions, and payload fields; rewrite docstrings/comments to English (§3c)
    - Rename DTO classes (e.g. `ResultadoEstagio1`→`Stage1Result`, `Estagio2Response`→`Stage2Response`, `ResultadoSelecaoCampanha`→`CampaignSelectionResult`, `ProvisionarEstagio5Response`→`ProvisionStage5Response`, `ConclusaoEstagio6Result`→`CompletionStage6Result`, `RelatorioExecucao`→`ExecutionReport`, `Stage4ProvisionamentoResponse`→`Stage4ProvisioningResponse`, `Estagio5LogsResponse`→`Stage5LogsResponse`) and functions (e.g. `obter_auditoria`→`get_audit`, `obter_topologia`→`get_topology`, `listar_casos`→`list_campaigns`), resolving the long tail through §2.
    - Rename Pydantic/JSON wire-contract field keys (e.g. `caso_id`→`campaign_id`, `versao`→`version`, `estagio`→`stage`, `concluido`→`completed`, `mensagem_erro`→`error_message`, `etapa_falha`→`failed_step`, `saude`→`health`, `isolamento`→`isolation`, `redes`→`networks`, `atores`→`adversaries`, `campanhas`→`campaigns`; `hosts` unchanged). Introduce no `alias=` shims — the frontend is updated in lockstep.
    - Rewrite all Portuguese docstrings and inline comments in backend modules into English, including any inside `ui/backend/app/services/containment/` (names/comments only — gates stay functionally unchanged).
    - COUPLED WITH 3.5 (frontend types/field keys): each field-key rename lands with its mirrored frontend type change; do not do in isolation.
    - _Requirements: 3.4, 3.5, 3.6, 8.5_
  - [x] 2.5 Verify the backend code layer
    - Run `pytest` from `ui/backend` with the project venv; confirm green with no behavioral change to request handling before moving on.
    - _Requirements: 3.7, 8.2_

- [x] 3. Frontend code layer — files, components, test suffixes, TS identifiers (IN LOCKSTEP with task 2)
  - [x] 3.1 Rename frontend lib files and update imports (§3d)
    - In `ui/frontend/src/lib/`, rename `casos.ts`→`campaigns.ts`, `emulacao.ts`→`emulation.ts`, `estagios.ts`→`stages.ts`, `preferencias.ts`→`preferences.ts`; leave already-English lib files unchanged. Update all imports.
    - _Requirements: 4.1_
  - [x] 3.2 Rename frontend component files and update imports/references (§3d)
    - In `ui/frontend/src/components/`, rename `CampanhaView.tsx`→`CampaignView.tsx`, `ParametrosAmbienteView.tsx`→`EnvironmentParametersView.tsx`, `RelatorioView.tsx`→`ReportView.tsx`; update all imports and JSX references.
    - _Requirements: 4.2_
  - [x] 3.3 Rename Portuguese test-file suffixes (§3d)
    - Rename `ExecutionWizard.navegacao.test.tsx`→`ExecutionWizard.navigation.test.tsx` and `ExecutionWizard.propagacao.test.tsx`→`ExecutionWizard.propagation.test.tsx`; update any references.
    - _Requirements: 4.3_
  - [x] 3.4 Retarget frontend API wrappers to the renamed routes (lockstep with 2.1)
    - Update the TypeScript API wrappers that call the backend so every route string matches the §3a renames (incl. `/campaigns...`, `/stage/{n}`, `/provision`, `/complete`, `/choose-dag`, `/operation`, `/report`, `/topology`, `/audit/{operation}`, `/preferences`, `/session-state`, STIX `/{version}`, `/campaigns`, `/actors`, canonical `/download`).
    - Retarget the Stage 6 topology call (formerly `getTopologia`/`topologia`) to `/campaigns/.../topology` so the Stage 6 fix keeps working.
    - This task MUST land together with 2.1 — the route change is breaking and the two sides must not drift.
    - _Requirements: 3.6, 4.4, 8.6, 9.3_
  - [x] 3.5 Rename frontend TypeScript identifiers, types, and mirrored field keys (lockstep with 2.4) (§3e)
    - Rename Portuguese TS identifiers, type names, and variables (e.g. `caso`, `versao`, `estagio`, `concluido`) and the interfaces mirroring backend DTOs so they stay structurally identical to the §3c symbols.
    - Rename object field keys that mirror the renamed JSON wire contract (e.g. `caso_id`→`campaign_id`, `versao`→`version`, `mensagem_erro`→`error_message`, `atores`→`adversaries`) to match task 2.4 exactly.
    - HARD BOUNDARY: do not rename, remove, or translate anything under `ui/frontend/src/i18n/**`; leave the rendered UI copy bilingual. Portuguese identifiers in TS code are in scope; i18n keys and translation strings are not.
    - _Requirements: 4.4, 4.5, 9.4_
  - [x] 3.6 Verify the frontend code layer and i18n guard
    - Run `vitest` in `ui/frontend`; confirm green with no behavioral change to the rendered UI.
    - Confirm the i18n parity/terminology tests still pass **unweakened** (`ui/frontend/src/i18n/i18n.property.test.ts`, `pipeline-i18n-parity.property.test.ts`, `pipeline-parity.test.ts`, `stages.terminology.test.ts`); any need to edit them signals an in-scope change leaked into the i18n layer.
    - _Requirements: 4.6, 1.6, 8.3_

- [x] 4. Database layer — Option A reversible English rename migration
  - [x] 4.1 Create the reversible Alembic rename migration (§4, Option A)
    - Add a new migration under `ui/backend/alembic/` that renames all Portuguese tables to English/STIX names via pure `ALTER TABLE ... RENAME` / `RENAME COLUMN` (no data movement): `casos`→`campaigns`, `execucoes_estagio`→`stage_runs`, `resultados_ability`→`ability_results`, `operacoes`→`operations`, `relatorios_execucao`→`execution_reports`, `versoes_stix`→`stix_versions`, `parametros_ambiente`→`environment_parameters`, `ambiente_provisionado`→`provisioned_environment`, `auditoria`→`audit`, `estado_sessao`→`session_state`, `preferencias`→`preferences`, plus the Portuguese columns.
    - Rename any index/constraint/FK names that embed the old table names; provide a working `downgrade` that restores the Portuguese names exactly.
    - _Requirements: 5.1, 5.2_
  - [x] 4.2 Update the ORM to the English physical names (no `name=` fallback)
    - In `ui/backend/app/models/domain.py`, set `__tablename__` and `mapped_column` names to the English physical names so they map 1:1; the application-visible attributes and the API contract expose only English, STIX-aligned names.
    - _Requirements: 5.3, 5.4_
  - [x] 4.3 Verify the migration round-trip and backend suite
    - On a seeded dev DB run `alembic upgrade head` → `downgrade` → `upgrade` and confirm the round-trip is data-loss-free and reversible; then run `pytest` from `ui/backend` and confirm green against the configured database.
    - _Requirements: 5.2, 5.5, 8.2_

- [x] 5. Docs layer — rename and translate docs and READMEs to English
  - [x] 5.1 Rename docs files to English (§3f)
    - Rename `docs/apresentacao-progresso.md`→`docs/progress-presentation.md`, `docs/status-projeto.md`→`docs/project-status.md`, and `docs/diario/`→`docs/journal/`; update any internal links/references.
    - _Requirements: 1.2_
  - [x] 5.2 Rewrite docs and README contents into English
    - Rewrite the contents of the renamed `docs/` files and all README files into English (Req 1.2); the only preserved bilingual copy is rendered UI copy, which `docs/` does not contain.
    - _Requirements: 1.1, 1.2_

- [x] 6. Clutter and obsolete-asset removal (§6)
  - [x] 6.1 Remove `architecture.png` and fix its references
    - `git rm` the root `architecture.png`; remove the image tag and the directory-tree line referencing it in `README.md`, `docker/README.md`, and `sticks/README.md` (the latter becomes `packages/cli/README.md` after the move in task 7) so no README links to a missing file.
    - _Requirements: 7.1, 7.2_
  - [x] 6.2 Remove stray root log files
    - `git rm` the tracked stray logs: `backend-dev.err.log`, `backend-dev.out.log`, `backend-run.log`, `dev-app.log`, `dev-run.err.log`, `dev-run.out.log`.
    - _Requirements: 7.3_
  - [x] 6.3 Update `.gitignore` with English comments and dev-runner globs
    - Rewrite the Portuguese `.gitignore` comments into English and add explicit dev-runner globs (`*-dev.*.log`, `*-run.log`, `dev-*.log`, `dev-run.*.log`) so equivalent clutter cannot reappear.
    - _Requirements: 1.1, 7.4_

- [x] 7. Directory restructure LAST — Proposal 2, single mechanical step
  - [x] 7.1 Move directories into the Proposal 2 layout
    - Move `ui/backend`→`apps/backend`, `ui/frontend`→`apps/frontend`, `docker/`→`infra/docker/`, and the legacy CLI `sticks/`→`packages/cli/`; keep `scripts/` and `docs/` at root. Relocating the legacy CLI is move-only — **do NOT rename its identifiers** (Req 6.4); only fix its `architecture.png` README reference (handled in 6.1).
    - _Requirements: 6.1, 6.4, 6.5_
  - [x] 7.2 Update build/config path couplings to the new layout
    - Update root `package.json` workspaces to `["apps/frontend", "packages/*"]`; rewrite the hard-coded `ui/backend`/`ui/frontend` paths in `scripts/dev.mjs`, `scripts/migrate.mjs`, `scripts/setup.mjs`; set the Alembic working directory to `apps/backend`; adjust Vite/`tsconfig` roots and import aliases; retarget Dockerfiles/compose build contexts and `docker/.docker/**` relative assets to `infra/docker/`.
    - Update README directory-tree snippets to reflect the new layout.
    - _Requirements: 6.5_

- [x] 8. Final verification gate
  - Run `pytest` from `apps/backend` and `vitest` in `apps/frontend`; confirm both green from the new locations.
  - Confirm the i18n parity/terminology tests still pass unweakened (`i18n.property.test.ts`, `pipeline-i18n-parity.property.test.ts`, `pipeline-parity.test.ts`, `stages.terminology.test.ts`).
  - Run the end-to-end six-stage ShadowRay flow through the UI across the renamed routes/fields and confirm it completes successfully, with the Stage 6 behavior and containment gates intact.
  - Run the final repo-wide grep (from task 1) and confirm no §2 Portuguese term survives outside `ui/frontend/src/i18n/**` (now `apps/frontend/src/i18n/**`) and `packages/cli/**`.
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 1.4, 1.5_

- [~] 9. (Optional) Commit the refactor on the feature branch
  - Commit the completed standardization/restructure on this working feature branch. The VCS migration into the main repository is out-of-band per the requirements' out-of-band note and is NOT code work.
  - _Requirements: 8.1_

## Notes

- The design has no Correctness Properties section (a rename-and-move refactor has no "for all inputs" invariant), so there are no property-based test tasks. Correctness is proven by the pre-existing `pytest`/`vitest` suites, the unweakened i18n parity/terminology tests, and the end-to-end ShadowRay flow (design §7).
- Lockstep coupling: task 2.1 ↔ 3.4 (route contract) and task 2.4 ↔ 3.5 (JSON field contract) are breaking API-contract changes and must land together so the two sides never drift (Req 3.6).
- Hard boundary: nothing under `ui/frontend/src/i18n/**` (later `apps/frontend/src/i18n/**`) or rendered UI copy is renamed, removed, or translated (Req 1.3, 4.5, 9.4).
- Legacy CLI identifier renaming is explicitly OUT OF SCOPE (Req 6.4) — move to `packages/cli/` and fix the `architecture.png` README reference only.
- The directory move is performed last, in a single reviewable step, then everything is re-run (design §5 mitigation).
- Task 9 is optional and non-code; it records the branch commit only.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "2.2", "2.3", "3.1", "3.2", "3.3"] },
    { "id": 2, "tasks": ["2.4", "3.4", "3.5"] },
    { "id": 3, "tasks": ["2.5", "3.6"] },
    { "id": 4, "tasks": ["4.1", "4.2"] },
    { "id": 5, "tasks": ["4.3"] },
    { "id": 6, "tasks": ["5.1", "6.1", "6.2", "6.3"] },
    { "id": 7, "tasks": ["5.2"] },
    { "id": 8, "tasks": ["7.1"] },
    { "id": 9, "tasks": ["7.2"] },
    { "id": 10, "tasks": ["8"] },
    { "id": 11, "tasks": ["9"] }
  ]
}
```
