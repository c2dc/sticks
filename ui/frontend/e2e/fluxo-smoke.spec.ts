import { expect, test } from "@playwright/test"

/**
 * Smoke test of the new STIX guided flow, local (no-network) portion only:
 * landing → bases.
 *
 * It never triggers a base download, dashboard, or DAG step — those require
 * real MITRE network access and are covered by a separate step. The assertions
 * mirror the exact rendered pt-BR text from `src/components/Landing.tsx`,
 * `src/components/StixBasesList.tsx` and `src/i18n/locales/pt-BR/stix.json`.
 */
test("landing → bases", async ({ page }) => {
  // --- Landing -----------------------------------------------------------
  await page.goto("/")

  await expect(page).toHaveTitle("STICKS")

  const cta = page.getByRole("button", { name: /Iniciar campanha/i })
  await expect(cta).toBeVisible()

  await page.screenshot({ path: "e2e/__screenshots__/landing.png", fullPage: true })

  // --- Bases -------------------------------------------------------------
  await cta.click()

  // Heading "Bases STIX" (h2 in StixBasesList).
  await expect(
    page.getByRole("heading", { name: /Bases STIX/i }),
  ).toBeVisible()

  // A base card for version "19.1" must be present.
  const card191 = page.locator('[data-versao="19.1"]')
  await expect(card191).toBeVisible()

  // The first not-downloaded base shows a "Baixar" button and must NOT carry a
  // "Já baixada" badge. We pick the first card flagged as not-downloaded.
  const primeiroNaoBaixado = page.locator('[data-baixada="false"]').first()
  await expect(primeiroNaoBaixado).toBeVisible()
  await expect(
    primeiroNaoBaixado.getByRole("button", { name: /Baixar/i }),
  ).toBeVisible()
  await expect(primeiroNaoBaixado.getByText(/Já baixada/i)).toHaveCount(0)

  await page.screenshot({ path: "e2e/__screenshots__/bases.png", fullPage: true })
})
