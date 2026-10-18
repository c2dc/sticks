# Task 1 — Terminology Freeze & Completion Baseline

Status: **DONE** (analysis/baseline task — no code changes).
Spec type: `feature` (requirements-first). No PBT.

This record freezes the naming authority for every rename in this spec and captures
the repo-wide Portuguese-term baseline that task 9 (final verification) must drive to
**zero** (outside the two permanently-excluded zones).

---

## 1. Single source of truth (Req 2.1, 2.2, 2.3, 2.5)

The **§2 STIX Terminology Table in `design.md` is the single, authoritative source of
truth** for every rename across backend, frontend, DB, and docs. No implementer may
invent a replacement term: each Portuguese artifact is resolved by looking its concept
up in that table, and only if no entry exists is the plain English translation used
(design "Rule: how a Portuguese artifact becomes English").

### Morpheme resolution rule (the long tail)

Every Portuguese identifier **not** explicitly listed is renamed by resolving each of
its morphemes through §2, **preserving the surrounding casing convention**
(PascalCase for classes, snake_case for functions/fields, kebab-case for multi-word
route segments):

| Morpheme (any casing) | Resolves to | Example |
|-----------------------|-------------|---------|
| `*Estagio*` / `*estagio*` | `*Stage*` / `*stage*` | `ResultadoEstagio1` → `Stage1Result` |
| `*Resultado*` / `*resultado*` | `*Result*` / `*result*` | `resultados_ability` → `ability_results` |
| `*Campanha*` / `*campanha*` | `*Campaign*` / `*campaign*` | `CampanhaView` → `CampaignView` |
| `caso` / `casos` (flagship) | `campaign` / `campaigns` | `caso_id` → `campaign_id` |
| `*Versao*` / `versao` | `*Version*` / `version` | `versoes_stix` → `stix_versions` |
| `*Relatorio*` / `relatorio` | `*Report*` / `report` | `RelatorioExecucao` → `ExecutionReport` |
| `*Operacao*` / `operacao` | `*Operation*` / `operation` | `operacoes` → `operations` |
| `*Provisionamento*` | `*Provisioning*` | `Stage4ProvisionamentoResponse` → `Stage4ProvisioningResponse` |
| `*Conclusao*` / `conclusao` | `*Completion*` / `completion` | `ConclusaoEstagio6Result` → `CompletionStage6Result` |
| `concluido` | `completed` (default; `done` only where terser reads better) | — |

Casing is **preserved**, never flattened: a PascalCase class stays PascalCase, a
snake_case field stays snake_case, a kebab-case route segment stays kebab-case.

### Already-English / STIX-consistent terms are LEFT UNCHANGED (Req 2.5)

To avoid churn, these are **not** touched: `hosts`, `ability`/`abilities`,
`adversary`/`adversaries`, `stix`, `health.py`/`router.py`/`stix.py` (backend api),
`api.ts`/`dag.ts`/`stix.ts`/`flow.ts`/`wizard.ts`/`ws.ts`/`utils.ts`/`theme-storage.ts`
(frontend lib), `compose_executor.py`/`log_buffer.py`/`stage_service.py` (env services).

---

## 2. §2.4 Divergences — recorded explicitly for consistent application (Req 2.4)

These are the cases where the chosen STIX/English term **diverges** from a literal
translation. Implementers MUST apply them exactly as stated so the two surfaces do not
drift.

| # | Portuguese | Literal | **Chosen term** | Rationale / rule |
|---|-----------|---------|-----------------|------------------|
| D1 | `atores` | `actors` | **`adversaries`** in domain/DTOs/fields; **`actors`** in the URL path segment | STIX models the group as an **Intrusion Set**; the project's established English domain term is `adversary`/`adversaries`. **Split by surface:** the returned data, DTOs and JSON field keys use `adversaries`; the *route path segment* is standardized to the literal `/actors` to stay predictable in the URL. (`/stix/bases/{version}/actors` ↔ response payload field `adversaries`.) |
| D2 | `caso` / `casos` | `case` / `cases` | **`campaign` / `campaigns`** | Flagship. A STIX **Campaign** is the operation run by an adversary group — not the literal "case". Applies to code, files, routes, DTO/field names, DB (Option A), and docs. |
| D3 | `baixar` (`casos.py`) + `download` (`stix.py`) | `download` | **single canonical `/stix/bases/{version}/download`** | The repo exposes the same "download the bundle" action under two spellings. Reconciled onto the one already-English, STIX-scoped route; the Portuguese `/baixar` route is **removed** and its caller repointed. Behavior-preserving consolidation. |
| D4 | `concluido` / `concluir` | `concluded` | **`completed`** (field) / **`complete`** (route verb) | Default to `completed`/`complete`; `done` only where it reads terser. Pick once, apply consistently. |
| D5 | `escolher-dag` | `choose-dag` / `select-dag` | **`choose-dag`** | Both are valid; `choose-dag` is the locked default so routes/wrappers match. |

Note on D1 reconciliation vs. design §2 table: the §2 table row for `atores` lists
`adversaries` (domain), while §3a routes the *path* as `/actors`. Task 2.1/3.4
implement the **path = `/actors`, payload = `adversaries`** split above.

---

## 3. Completion baseline (grep) — the metric task 9 drives to zero

### Method (reproducible)

- Tool: `git grep -I -i -E` (case-insensitive, skip binary) from the repo root.
- **Permanent exclusions** (the only zones where Portuguese is allowed to survive):
  - `ui/frontend/src/i18n/**` (bilingual UI copy / i18n resources — Req 1.3, 4.5, 9.4)
  - legacy CLI: `sticks/**` and future `packages/cli/**` (Req 6.4 — relocate, do not rename)
- **Operational exclusions** (not rename targets / noise): `.kiro/**` (the spec itself
  quotes the Portuguese terms), `node_modules/**`, `*.log` (stray logs removed in task 6),
  `architecture.png`, `package-lock.json`.
- Pattern (the §2 Portuguese terms, alternation):

```
caso|casos|versao|versoes|estagio|estagios|concluido|concluir|conclusao|relatorio|relatorios|operacao|operacoes|auditoria|preferencias|estado_sessao|estado-sessao|topologia|provisionar|provisionador|provisionamento|operacionalizacao|operacionalizador|execucao|execucoes|medicao|saude|isolamento|redes|campanha|campanhas|atores|parametros|ambiente|baixar|baixada|mensagem_erro|etapa_falha|escolher|selecionar|emulacao|resultado|resultados|navegacao|propagacao|apresentacao|diario
```

### Baseline result (captured this task)

| Metric | Value |
|--------|-------|
| **Files containing ≥1 §2 Portuguese term** | **201** |
| **Matching lines (completion metric → target 0)** | **10860** |

Distribution by area:

| Area | Files | Matching lines |
|------|------:|---------------:|
| `ui/backend` | 126 | 8002 |
| `ui/frontend` (excl. `src/i18n/**`) | 61 | 2771 |
| `docs/diario` | 4 | 40 |
| `docs/apresentacao-progresso.md` | 1 | 24 |
| `docs/status-projeto.md` | 1 | 10 |
| `docker/.docker` | 2 | 4 |
| `docker/preflight-network-check.ps1` | 1 | 2 |
| `scripts/dev.mjs` | 1 | 2 |
| `scripts/setup.mjs` | 1 | 2 |
| `.gitignore` | 1 | 1 |
| `docker/docker-compose.yml` | 1 | 1 |
| `README.md` | 1 | 1 |

### Target for task 8/9

After the full refactor, re-running the identical `git grep` (with the directory move
applied, so the i18n exclusion becomes `apps/frontend/src/i18n/**` and the CLI
exclusion becomes `packages/cli/**`) MUST report **0 matching lines**. Any residual
match outside the two permanently-excluded zones is an incomplete rename.

### Reproduce

```powershell
$pattern = 'caso|casos|versao|versoes|estagio|estagios|concluido|concluir|conclusao|relatorio|relatorios|operacao|operacoes|auditoria|preferencias|estado_sessao|estado-sessao|topologia|provisionar|provisionador|provisionamento|operacionalizacao|operacionalizador|execucao|execucoes|medicao|saude|isolamento|redes|campanha|campanhas|atores|parametros|ambiente|baixar|baixada|mensagem_erro|etapa_falha|escolher|selecionar|emulacao|resultado|resultados|navegacao|propagacao|apresentacao|diario'
$excludes = @(':(exclude)ui/frontend/src/i18n/**', ':(exclude)sticks/**', ':(exclude)packages/cli/**', ':(exclude).kiro/**', ':(exclude)node_modules/**', ':(exclude)*.log', ':(exclude)architecture.png', ':(exclude)package-lock.json')
$perFile = git grep -I -i -E -c $pattern -- $excludes
"Files: $($perFile.Count)  Lines: $(($perFile | % { ($_ -split ':')[-1] -as [int] } | Measure-Object -Sum).Sum)"
```
