import { defineConfig } from "@playwright/test"

/**
 * Playwright config for the STIX-flow smoke tests.
 *
 * There is intentionally NO `webServer` entry: these smoke tests run against an
 * already-running stack (frontend on :8080, backend on :8010). The tests only
 * exercise the local, no-network portion of the flow (landing → bases); they do
 * not trigger a STIX download or any MITRE network access.
 */
export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://localhost:8080",
    headless: true,
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
})
