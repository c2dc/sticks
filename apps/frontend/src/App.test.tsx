import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import App from "@/App"
import type { BaseStixInfo } from "@/lib/stix"

// Mock every API wrapper the App + wizard touch so no network is used and the
// routing can be driven deterministically.
vi.mock("@/lib/preferences", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/preferences")>()
  return {
    ...actual,
    getPreferences: vi.fn().mockResolvedValue(null),
    putPreferences: vi.fn().mockResolvedValue({ theme: "claro", language: "pt-BR" }),
  }
})

vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return { ...actual, getBases: vi.fn() }
})

import { getBases } from "@/lib/stix"

const getBasesMock = vi.mocked(getBases)

function makeBases(): BaseStixInfo[] {
  return [
    {
      version: "18.0",
      name: "Enterprise ATT&CK v18.0",
      downloaded: true,
      selectable: true,
      file: "enterprise-attack-18.0.json",
    },
  ]
}

describe("App flow integration (landing → wizard routing)", () => {
  beforeEach(() => {
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(makeBases())
  })

  it("mounts the Landing first, then enters the ExecutionWizard on the CTA click", async () => {
    const user = userEvent.setup()
    renderWithI18n(<App />)

    // Landing is the entry screen: its CTA is present and the wizard is not.
    const cta = await screen.findByRole("button", { name: "Iniciar campanha" })
    expect(cta).toBeInTheDocument()
    expect(screen.queryByTestId("execution-wizard")).toBeNull()

    // START via the CTA switches the view landing → wizard, straight into the
    // unified Input screen (package + campaign on one screen).
    await user.click(cta)

    expect(await screen.findByTestId("execution-wizard")).toBeInTheDocument()
    // The flow enters at the unified Input screen (two-layer shell), not the
    // old 1..6 ruler.
    expect(
      await screen.findByText(/Vamos preparar sua emulação/),
    ).toBeInTheDocument()
    // The Landing CTA is gone once we entered the wizard.
    expect(
      screen.queryByRole("button", { name: "Iniciar campanha" }),
    ).toBeNull()
  })
})
