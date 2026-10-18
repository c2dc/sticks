import { expect, test } from "@playwright/test"

/**
 * Full guided-flow E2E for the STIX journey, driven end-to-end against the
 * already-running stack (frontend :8080, backend :8010). Unlike the smoke test
 * — which stops at the local landing → bases portion — this spec walks the
 * WHOLE flow using the DOWNLOADED base 18.0 and the curated "ShadowRay"
 * campaign under the renumbered **5-Estágio scheme** (pipeline-cinco-estagios):
 *
 *   landing → bases → dashboard → selecao → wizard
 *
 * The standalone "dag" view NO LONGER EXISTS: after the ShadowRay campaign is
 * picked, the app goes STRAIGHT to the ExecutionWizard. Under the five-Estágio
 * scheme the pipeline is 1..5:
 *   - Estágio 1 — Modelagem Estrutural (body `Stage1View`; "Executar modelagem"
 *     in the wizard footer drives POST /estagio/1/executar).
 *   - Estágio 2 — Geração do DAG (body `DagStageView`; the single "Gerar DAG"
 *     button is disabled "em breve"; "Selecionar DAG" chooses a curated DAG).
 *   - Estágio 3 — Operacionalização (body `Stage2View`).
 *   - Estágio 4 — Provisionamento do Ambiente (body `EnvironmentStageView`;
 *     topology / "Diagrama de rede").
 *   - Estágio 5 — Execução da Campanha (body `Stage3View` + emulation modal).
 *
 * It captures a screenshot at every step under e2e/__screenshots__/full-*.png.
 * Assertions mirror the exact rendered pt-BR text/roles from the components:
 * Landing.tsx, StixBasesList.tsx, StixIntelligenceDashboard.tsx,
 * CampaignActorSelector.tsx, ExecutionWizard.tsx (5 `[data-stage]` badges +
 * "Passo 1 de 5", stage title from i18n `stages.{n}.name`), Stage1View.tsx
 * (Entrada / Processamento / Saída sections), DagStageView.tsx (DAG section
 * title, "Selecionar DAG", "Gerar com IA" disabled "em breve") and
 * MermaidDiagram.tsx (container aria-label "Diagrama de rede"). It walks up to
 * (at most) Estágio 4 and NEVER confirms the Estágio 5 emulation modal.
 */
test("fluxo completo: landing → wizard (base 18.0 / ShadowRay curada), 5 Estágios", async ({
  page,
}) => {
  test.setTimeout(60_000)

  // --- 1. Landing --------------------------------------------------------
  await page.goto("/")

  await expect(page).toHaveTitle("STICKS")

  const cta = page.getByRole("button", { name: /Iniciar campanha/i })
  await expect(cta).toBeVisible()

  await page.screenshot({
    path: "e2e/__screenshots__/full-01-landing.png",
    fullPage: true,
  })

  await cta.click()

  // --- 2. Bases ----------------------------------------------------------
  await expect(
    page.getByRole("heading", { name: /Bases STIX/i }),
  ).toBeVisible()

  // The 18.0 base is already downloaded: its card carries data-baixada="true",
  // shows the "Já baixada" badge, and exposes a "Selecionar" button.
  const base18 = page.locator('[data-versao="18.0"]')
  await expect(base18).toBeVisible()
  await expect(base18).toHaveAttribute("data-baixada", "true")
  await expect(base18.getByText(/Já baixada/i)).toBeVisible()

  await page.screenshot({
    path: "e2e/__screenshots__/full-02-bases.png",
    fullPage: true,
  })

  await base18.getByRole("button", { name: /Selecionar/i }).click()

  // --- 3. Dashboard ------------------------------------------------------
  // Base identification shows the selected version "v18.0" (Req. 3.1).
  await expect(page.getByText(/v18\.0/)).toBeVisible()

  // A per-type count card labeled "campanhas" with a numeric count visible
  // (Req. 3.2). The card title is the backend pt-BR label; scope to the card
  // and assert it carries a number.
  const campanhasCard = page
    .locator("div")
    .filter({ has: page.getByText(/campanhas/i) })
    .filter({ hasText: /\d/ })
    .first()
  await expect(campanhasCard).toBeVisible()

  await page.screenshot({
    path: "e2e/__screenshots__/full-03-dashboard.png",
    fullPage: true,
  })

  await page
    .getByRole("button", { name: /Selecionar campanha\/ator/i })
    .click()

  // --- 4. Selection (campaign view is the default) -----------------------
  // Campaign cards exist; the ShadowRay card is curated and shows "Já curada".
  const shadowRayTitle = page.getByText("ShadowRay", { exact: true }).first()
  await expect(shadowRayTitle).toBeVisible()

  // Scope the "Já curada" badge to the ShadowRay card itself.
  const shadowRayCard = page
    .locator('[data-curada="true"]')
    .filter({ hasText: "ShadowRay" })
    .first()
  await expect(shadowRayCard).toBeVisible()
  await expect(shadowRayCard.getByText(/Já curada/i)).toBeVisible()

  await page.screenshot({
    path: "e2e/__screenshots__/full-04-selecao.png",
    fullPage: true,
  })

  // Picking the campaign goes STRAIGHT to the wizard (no standalone DAG view).
  await shadowRayTitle.click()

  // --- 5. Execution wizard mounts (5-Estágio scheme) ---------------------
  // The ExecutionWizard mounts directly on selecao → wizard. Assert its root
  // test id, the "Passo 1 de 5" step indicator and the 5-badge stage track
  // (Req. 8.9).
  const wizard = page.getByTestId("execution-wizard")
  await expect(wizard).toBeVisible({ timeout: 15_000 })

  // Step indicator — rendered pt-BR "Passo {{atual}} de {{total}}" with total 5.
  await expect(wizard.getByText(/Passo\s*1\s*de\s*5/i)).toBeVisible()

  // The badge track has exactly 5 `[data-stage]` entries (Estágios 1..5).
  const badges = wizard.locator("[data-stage]")
  await expect(badges).toHaveCount(5)
  await expect(wizard.locator('[data-stage="1"]')).toBeVisible()
  await expect(wizard.locator('[data-stage="5"]')).toBeVisible()
  // Estágio 1 is the current step (aria-current="step").
  await expect(wizard.locator('[data-stage="1"]')).toHaveAttribute(
    "aria-current",
    "step",
  )

  // --- 6. Estágio 1 is the Modelagem Estrutural --------------------------
  // The wizard title comes from i18n `stages.1.name` and is deterministic:
  // "Estágio 1 — Modelagem Estrutural" (Req. 2.x / 8.9). This is the stable
  // anchor that the body is the Modelagem view, NOT the DAG list.
  await expect(
    wizard.getByText(/Estágio 1\s*—\s*Modelagem Estrutural/i),
  ).toBeVisible({ timeout: 15_000 })

  // Stage1View renders its three labelled sections (i18n `sections.*`):
  // Entrada / Processamento / Saída. Assert the Entrada section is present as a
  // further confirmation that the Modelagem body (not the DAG) is mounted.
  await expect(
    wizard.locator('[data-section="entrada"]'),
  ).toBeVisible({ timeout: 15_000 })

  await page.screenshot({
    path: "e2e/__screenshots__/full-05-wizard-estagio1-modelagem.png",
    fullPage: true,
  })

  // --- 7. Conclude Estágio 1 (Modelagem) → advance to Estágio 2 ----------
  // Estágio 1 is the pipeline start and is never "bloqueado". For a curated
  // ShadowRay case it may already be concluido (backend state) or may need the
  // "Executar modelagem" action. Keep the e2e resilient: if the Estágio 1 badge
  // is not yet "concluido", trigger the modeling and wait for the backend to
  // report it concluded, then advance.
  const estagio1Badge = wizard.locator('[data-stage="1"]')
  const estagio1Status = await estagio1Badge.getAttribute("data-status")
  if (estagio1Status !== "concluido") {
    await wizard.getByRole("button", { name: /Executar modelagem/i }).click()
    // The wizard re-reads the backend after running; Estágio 1 flips to
    // "concluido" once the modeling persists.
    await expect(estagio1Badge).toHaveAttribute("data-status", "concluido", {
      timeout: 20_000,
    })
  }

  // "Avançar para o próximo estágio" is enabled once Estágio 1 is concluido
  // (sequential gate). Advancing moves the wizard to Estágio 2.
  await wizard
    .getByRole("button", { name: /Avançar para o próximo estágio/i })
    .click()

  // --- 8. Estágio 2 is the Geração do DAG --------------------------------
  // Step indicator advances to "Passo 2 de 5"; the title comes from
  // `stages.2.name` ("Estágio 2 — Geração do DAG").
  await expect(wizard.getByText(/Passo\s*2\s*de\s*5/i)).toBeVisible({
    timeout: 15_000,
  })
  await expect(wizard.locator('[data-stage="2"]')).toHaveAttribute(
    "aria-current",
    "step",
  )

  // The DagStageView body shows the DAG section title and the DAG list
  // (role=list labeled "Selecionar DAG"). The curated ShadowRay DAG is
  // backfilled by GET /dags, so at least one selectable DAG is present.
  await expect(
    wizard.getByText(/Geração do Grafo Acíclico Dirigido \(DAG\)/i),
  ).toBeVisible({ timeout: 15_000 })

  // The single "Gerar DAG" button is rendered as "Gerar com IA", visible but
  // disabled ("em breve") — a no-op in this iteration (Req. 3.4, 3.5).
  const gerarDag = wizard.getByRole("button", { name: /Gerar com IA/i })
  await expect(gerarDag).toBeVisible()
  await expect(gerarDag).toBeDisabled()

  // The Mermaid diagram of the (pre-)selected curated DAG renders an <svg>
  // inside the container whose aria-label is "Diagrama de rede". Mermaid is
  // loaded lazily (dynamic import), so allow a generous timeout.
  const diagramaDag = wizard.locator('[aria-label="Diagrama de rede"]')
  await expect(diagramaDag).toBeVisible({ timeout: 20_000 })
  await expect(diagramaDag.locator("svg")).toBeVisible({ timeout: 20_000 })

  await page.screenshot({
    path: "e2e/__screenshots__/full-06-wizard-estagio2-dag.png",
    fullPage: true,
  })

  // --- 9. Choose the curated DAG → Estágio 2 concluido -------------------
  // Choosing a DAG completes Estágio 2 and unlocks Estágio 3 (Req. 3.3). This
  // depends on the backend having a persisted curated DAG for ShadowRay — the
  // GET /dags backfill guarantees one. Click "Selecionar DAG"; the wizard
  // re-reads the backend and flips Estágio 2 to concluido.
  await wizard.getByRole("button", { name: /Selecionar DAG/i }).click()

  // Estágio 2 badge reports its backend-derived status as "concluido" once the
  // choice is persisted; Estágio 3 is no longer "bloqueado".
  await expect(wizard.locator('[data-stage="2"]')).toHaveAttribute(
    "data-status",
    "concluido",
    { timeout: 15_000 },
  )
  await expect(wizard.locator('[data-stage="3"]')).not.toHaveAttribute(
    "data-status",
    "bloqueado",
  )

  await page.screenshot({
    path: "e2e/__screenshots__/full-07-wizard-estagio2-concluido.png",
    fullPage: true,
  })

  // --- 10. Walk to Estágio 4 (Provisionamento) if the gate allows --------
  // For a curated case the backend auto-concludes Estágio 2 (DAG) and Estágio 4
  // (Ambiente). Advance Estágio 2 → 3, conclude Operacionalização (Estágio 3),
  // then land on Estágio 4 and assert its topology renders. These advances are
  // gated by the backend; this section is best-effort and stops BEFORE the
  // Estágio 5 emulation modal is ever opened or confirmed.
  const avancar = wizard.getByRole("button", {
    name: /Avançar para o próximo estágio/i,
  })

  // Estágio 2 → Estágio 3 (Operacionalização).
  await avancar.click()
  await expect(wizard.getByText(/Passo\s*3\s*de\s*5/i)).toBeVisible({
    timeout: 15_000,
  })
  await expect(wizard.locator('[data-stage="3"]')).toHaveAttribute(
    "aria-current",
    "step",
  )

  // Estágio 3 → Estágio 4 (Provisionamento do Ambiente). Advancing from
  // Estágio 3 concludes the Operacionalização (POST /estagio/3/concluir).
  await avancar.click()
  await expect(wizard.getByText(/Passo\s*4\s*de\s*5/i)).toBeVisible({
    timeout: 15_000,
  })
  await expect(wizard.locator('[data-stage="4"]')).toHaveAttribute(
    "aria-current",
    "step",
  )

  // Estágio 4 body is the EnvironmentStageView: the contained-lab topology
  // renders a Mermaid network diagram (container aria-label "Diagrama de rede").
  const diagramaTopologia = wizard.locator('[aria-label="Diagrama de rede"]')
  await expect(diagramaTopologia).toBeVisible({ timeout: 20_000 })
  await expect(diagramaTopologia.locator("svg")).toBeVisible({ timeout: 20_000 })

  await page.screenshot({
    path: "e2e/__screenshots__/full-08-wizard-estagio4-topologia.png",
    fullPage: true,
  })

  // Stop here: do NOT advance into or confirm the Estágio 5 emulation modal.
})
