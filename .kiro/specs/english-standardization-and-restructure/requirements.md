# Requirements Document

## Introduction

**STICKS** is a research tool that turns ATT&CK-in-STIX threat intelligence into contained APT emulation, driven by a six-stage guided wizard UI (React/Vite frontend + FastAPI backend). The repository will be published **publicly** on GitHub for a worldwide audience.

This spec defines a **standardization and restructuring** effort with two intertwined goals:

1. **English-only default across the whole project.** Everything in the project — file names, directory names, code identifiers, API route paths, docstrings, comments, and documentation — MUST default to English. The single explicit exception is the **rendered graphical interface**, which stays bilingual (pt-BR and en) through i18n. Only the UI's rendered copy and its i18n resource files may contain Portuguese.
2. **A clean, publicly-presentable directory/file layout.** Now that the UI is consolidated, the repository SHALL be reorganized into a conventional structure that cleanly separates frontend, backend, scripts, Docker assets, and docs, and that removes clutter (stray root-level log files and the obsolete `architecture.png`).

A central naming principle is **STIX alignment**: when renaming, STIX-standard vocabulary is the source of truth. The flagship rename is the project-wide `caso`/`casos` → `campaign`, because in STIX a **Campaign** is the operation run by an adversary group. The same logic applies to other names where a STIX term is the natural fit (adversary, ability/attack-pattern, technique, intrusion-set, operation).

This is a **mechanical, non-behavioral refactor**. The emulation logic, security/containment gates, the sequential-locking pipeline semantics, and the recently fixed Stage 6 behavior MUST remain unchanged. The full automated test suite (backend `pytest` + frontend `vitest`) and the end-to-end six-stage ShadowRay flow MUST still pass after the work.

### Out-of-band note (repository/VCS migration — not a code requirement)

The user intends to migrate this working repository into the main repository and continue working directly on `main` afterward, deleting this working repo. This is a version-control/migration concern handled outside the codebase and is **not** a functional requirement of this spec. It is recorded here so the design phase treats the current repo as the authoritative source of the rename map, and so no requirement assumes long-lived branch history in this working repo.

## Glossary

- **STICKS**: The research tool comprising `ui/backend` (FastAPI + SQLAlchemy + Alembic + PostgreSQL) and `ui/frontend` (React + TypeScript + Vite + i18next). It turns ATT&CK-in-STIX intelligence into contained APT emulation via a six-stage wizard.
- **Project**: The entire repository rooted at the workspace root, including backend, frontend, Docker assets, scripts, docs, and any legacy packages.
- **English-only default**: The rule that every project artifact defaults to English, with the UI rendered copy and i18n resources as the single exception.
- **Rendered UI copy**: The user-facing text displayed in the graphical interface at runtime.
- **i18n resources**: The internationalization resource files under `ui/frontend/src/i18n/` that hold the pt-BR and en translation strings for the rendered UI copy.
- **STIX**: Structured Threat Information Expression — the standardized CTI format whose vocabulary is the source of truth for naming.
- **STIX-aligned term**: A name taken from STIX-standard vocabulary (for example `Campaign`, `Intrusion Set`, `Attack Pattern`, `Course of Action`) chosen in preference to a project-local or Portuguese name.
- **Campaign**: The STIX object representing an operation run by an adversary group. The target replacement for the current `caso`/`casos` vocabulary across the project.
- **API contract**: The externally observable HTTP surface of the backend — route paths, request/response JSON field names, and status codes — consumed by the frontend.
- **Frontend API wrappers**: The TypeScript modules under `ui/frontend/src/lib/` that call the backend API and mirror its DTOs and field names.
- **DTO**: A Pydantic request/response model exposed by the backend API.
- **DB schema object**: A database table name, column name, index, or constraint, together with the Alembic migrations that define them.
- **Legacy CLI package**: The top-level `sticks/` package (`main.py`, `lib/`, `config/`, `data/`, `tools/`) that predates the consolidated UI.
- **Rename map**: The design-phase artifact that enumerates every old name and its new English/STIX-aligned name across backend, frontend, DB, and docs.
- **Directory-tree proposal**: A candidate top-level repository layout presented in the design phase for the Maintainer to choose from.
- **Maintainer**: The person performing the standardization and restructuring and owning the decisions it requires.
- **Behavior-preserving**: A change that alters names, locations, or text only, leaving runtime behavior, data, and observable outputs identical.
- **ShadowRay flow**: The end-to-end six-stage emulation path exercised with the `ShadowRay` curated campaign, used as the acceptance path for the full pipeline.
- **Containment gates**: The isolation/containment protections in `ui/backend/app/services/containment/` that keep attack commands confined to the provisioned isolated environment.

## Requirements

### Requirement 1: English-only default across the whole project

**User Story:** As a Maintainer preparing a public release, I want every project artifact to default to English, so that a worldwide audience can read and contribute to the codebase without a language barrier.

#### Acceptance Criteria

1. THE Project SHALL use English for all file names, directory names, code identifiers, API route paths, docstrings, and inline comments.
2. THE Project SHALL use English for all documentation under `docs/` and for all README files.
3. WHERE an artifact is the rendered UI copy or an i18n resource file under `ui/frontend/src/i18n/`, THE Project SHALL retain bilingual content in Portuguese (pt-BR) and English (en).
4. THE Project SHALL treat the rendered UI copy and i18n resources as the single exception to the English-only default, and SHALL NOT introduce any other non-English artifact.
5. WHEN an artifact outside the rendered UI copy and i18n resources contains Portuguese identifiers, names, route paths, docstrings, or comments, THE Project SHALL replace that Portuguese content with an English equivalent.
6. THE Project SHALL keep the frontend's bilingual capability functional, so that the rendered UI continues to switch between pt-BR and en after the standardization.

### Requirement 2: STIX-aligned terminology

**User Story:** As a Maintainer, I want names to follow STIX-standard vocabulary, so that the codebase speaks the same language as the threat-intelligence domain it models.

#### Acceptance Criteria

1. WHEN choosing an English replacement for a renamed artifact, THE Project SHALL prefer the STIX-standard term over a project-local or literal translation where a STIX term applies.
2. THE Project SHALL replace the `caso`/`casos` vocabulary with `campaign`/`campaigns` across code identifiers, file names, route paths, DTO/field names, and documentation (subject to the DB decision in Requirement 5).
3. WHERE a concept maps to a STIX object (for example adversary/intrusion-set, ability/attack-pattern, technique, operation), THE Project SHALL align the name with that STIX object's vocabulary.
4. IF a current name's natural STIX term differs from the existing name, THEN THE Project SHALL record the divergence in the rename map together with the chosen term and a short rationale.
5. WHERE a term is already correct English and STIX-consistent, THE Project SHALL leave that term unchanged to avoid churn.

### Requirement 3: Backend standardization (routes, modules, symbols, docstrings)

**User Story:** As a Maintainer, I want the backend code and its public HTTP surface to be English and STIX-aligned, so that the API is self-describing to external consumers.

#### Acceptance Criteria

1. THE Project SHALL rename the Portuguese API module files in `ui/backend/app/api/` (`casos.py`, `emulacao.py`, `auditoria.py`, `preferencias.py`, `topologia.py`, `estado_sessao.py`) to English names and update all imports and the router registration accordingly.
2. THE Project SHALL rename the Portuguese service modules in `ui/backend/app/services/environment/` (`provisionador.py`, `operacionalizacao.py`, `operacionalizador.py`, `conclusao.py`, `execucao.py`, `medicao.py`, `relatorio.py`, `saude.py`, `topologia_builder.py`) to English names and update all imports accordingly.
3. THE Project SHALL rename Portuguese API route paths to English, STIX-aligned paths, including the `/casos` family (`/casos`, `/casos/{caso}`, `/casos/{caso}/estagio/{n}`, `.../provisionar`, `.../concluir`, `.../escolher-dag`, `.../operacao`, `.../relatorio`, `/casos/{caso}/topologia`), `/auditoria/{operacao}`, `/preferencias`, `/estado-sessao`, and the STIX sub-paths (`.../baixar`, `.../campanhas`, `.../atores`, `.../versao`).
4. THE Project SHALL rename Portuguese Python symbols — DTO classes (for example `ResultadoEstagio1`, `Estagio2Response`, `ResultadoSelecaoCampanha`, `ProvisionarEstagio5Response`, `ConclusaoEstagio6Result`, `RelatorioExecucao`, `Stage4ProvisionamentoResponse`, `Estagio5LogsResponse`), functions (for example `obter_auditoria`, `obter_topologia`, `listar_casos`), and payload field names (for example `caso_id`, `versao`, `estagio`, `concluido`, `mensagem_erro`, `etapa_falha`, `saude`, `isolamento`, `redes`, `hosts`) — to English, STIX-aligned names.
5. THE Project SHALL rewrite Portuguese docstrings and inline comments in backend modules into English.
6. WHEN a backend route path or JSON field name changes, THE Project SHALL treat the change as a breaking API-contract change and update the corresponding frontend API wrappers in the same change so the two sides stay consistent.
7. WHEN the backend standardization is complete, THE Project SHALL keep the backend test suite (`pytest`) passing with no behavioral change to request handling.

### Requirement 4: Frontend standardization without touching bilingual UI copy

**User Story:** As a Maintainer, I want the frontend source to be English while the rendered UI stays bilingual, so that the code is readable publicly without losing the pt-BR/en experience.

#### Acceptance Criteria

1. THE Project SHALL rename the Portuguese lib files in `ui/frontend/src/lib/` (`casos.ts`, `emulacao.ts`, `estagios.ts`, `preferencias.ts`) to English names and update all imports.
2. THE Project SHALL rename the Portuguese component files in `ui/frontend/src/components/` (`CampanhaView.tsx`, `ParametrosAmbienteView.tsx`, `RelatorioView.tsx`) to English names and update all imports and references.
3. THE Project SHALL rename Portuguese test-file suffixes (for example `ExecutionWizard.navegacao.test.tsx`, `ExecutionWizard.propagacao.test.tsx`) to English suffixes.
4. THE Project SHALL rename Portuguese TypeScript identifiers and types in the frontend (including those mirroring backend DTOs and fields) to the matching English, STIX-aligned names.
5. THE Project SHALL NOT rename, remove, or translate the i18n resource files under `ui/frontend/src/i18n/`, and SHALL leave the rendered UI copy bilingual.
6. WHEN the frontend standardization is complete, THE Project SHALL keep the frontend test suite (`vitest`) passing with no behavioral change to the rendered UI.

### Requirement 5: Database schema naming decision

**User Story:** As a Maintainer, I want an explicit, recorded decision about the Portuguese database names, so that the public-facing contract and code are English while data integrity is preserved.

#### Acceptance Criteria

1. THE Project SHALL make an explicit decision, recorded in the design phase, on whether to rename the Portuguese DB table and column names (for example `casos`, `execucoes_estagio`, `operacoes`, `resultados_ability`, `relatorios_execucao`, `auditoria`, `preferencias`, `parametros_ambiente`, `ambiente_provisionado`, `estado_sessao`, `versoes_stix`) via Alembic migrations, or to keep the physical DB names while mapping them to English identifiers in code.
2. WHERE the decision is to rename DB schema objects, THE Project SHALL provide Alembic migrations under `ui/backend/alembic/` that perform the rename without data loss.
3. WHERE the decision is to keep physical DB names, THE Project SHALL map those names to English identifiers in the ORM models so that application code and the API contract expose only English names.
4. THE Project SHALL ensure that regardless of the DB decision, the public-facing API contract and all application code use English, STIX-aligned names.
5. WHEN DB-related changes are complete, THE Project SHALL keep the backend migration and test suite passing against the configured database.

### Requirement 6: Repository restructuring with candidate layouts

**User Story:** As a Maintainer, I want a clean, conventional repository layout chosen from clear options, so that the public project is easy to navigate and present.

#### Acceptance Criteria

1. THE Project SHALL define a target layout that cleanly separates frontend, backend, scripts, Docker assets, and documentation into conventional top-level locations.
2. THE design phase SHALL present at least three distinct candidate directory-tree proposals for the Maintainer to choose from, and SHALL NOT finalize a single layout during the requirements phase.
3. WHEN presenting the candidate proposals, THE design phase SHALL describe, for each proposal, the trade-offs and the migration impact on imports and references.
4. THE Project SHALL make an explicit in-scope or out-of-scope decision for the legacy CLI package (`sticks/`), recording whether its names are aligned in this effort or deferred.
5. WHEN the chosen layout is applied, THE Project SHALL update all import paths, references, and configuration so the backend and frontend build and run from the new locations.

### Requirement 7: Clutter and obsolete-asset removal

**User Story:** As a Maintainer, I want obsolete files removed and references cleaned up, so that the public repository contains no dead artifacts.

#### Acceptance Criteria

1. THE Project SHALL remove the obsolete `architecture.png` file at the repository root.
2. WHEN `architecture.png` is removed, THE Project SHALL remove or update its references in `README.md`, `docker/README.md`, and `sticks/README.md` so that no README links to a missing file.
3. THE Project SHALL remove the stray root-level log files (for example `backend-dev.err.log`, `backend-dev.out.log`, `backend-run.log`, `dev-app.log`, `dev-run.err.log`, `dev-run.out.log`).
4. THE Project SHALL ensure that patterns matching the removed log files are ignored by version control so equivalent clutter does not reappear.

### Requirement 8: Behavior preservation and verification

**User Story:** As a Maintainer, I want proof that nothing behavioral changed, so that I can publish with confidence that the tool still works end to end.

#### Acceptance Criteria

1. THE Project SHALL keep every change behavior-preserving, limited to names, file locations, and human-language text.
2. WHEN the standardization and restructuring are complete, THE Project SHALL have the full backend test suite (`pytest`) passing.
3. WHEN the standardization and restructuring are complete, THE Project SHALL have the full frontend test suite (`vitest`) passing.
4. WHEN the standardization and restructuring are complete, THE Project SHALL have the end-to-end six-stage ShadowRay flow completing successfully.
5. THE Project SHALL leave the containment gates in `ui/backend/app/services/containment/` functionally unchanged.
6. THE Project SHALL leave the sequential-locking pipeline semantics and the Stage 6 behavior functionally unchanged.

### Requirement 9: Non-goals

**User Story:** As a Maintainer, I want the scope strictly bounded to standardization and restructuring, so that no behavioral risk is introduced during a public-release cleanup.

#### Acceptance Criteria

1. THE Project SHALL NOT add new product features as part of this effort.
2. THE Project SHALL NOT change the emulation logic, the DAG generation, the operationalization, or the environment provisioning behavior.
3. THE Project SHALL NOT change the containment/security gates or the recently fixed Stage 6 behavior.
4. THE Project SHALL NOT change the bilingual (pt-BR/en) content or meaning of the rendered UI copy beyond what is required to rename non-UI identifiers.
