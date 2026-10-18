import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import App from "@/App"
import type { BaseStixInfo } from "@/lib/stix"
import type { Campaign, CasesResponse } from "@/lib/campaigns"

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

vi.mock("@/lib/campaigns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/campaigns")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return { ...actual, getCampaign: vi.fn(), resetCampaign: vi.fn() }
})

import { getBases } from "@/lib/stix"
import { fetchCases } from "@/lib/campaigns"
import { getCampaign } from "@/lib/wizard"

const getBasesMock = vi.mocked(getBases)
const fetchCasesMock = vi.mocked(fetchCases)
const getCampaignMock = vi.mocked(getCampaign)

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

function makeCases(): CasesResponse {
  return {
    campaigns: [{ id: "shadowray", name: "ShadowRay" }],
    progress: { completed_campaigns: 0, total_campaigns: 8, percentage: 0 },
  }
}

function makeCampaign(): Campaign {
  return {
    id: "shadowray",
    name: "ShadowRay",
    states: {
      fully_completed: false,
      stages: [1, 2, 3, 4, 5, 6].map((stage) => ({
        stage: stage as 1 | 2 | 3 | 4 | 5 | 6,
        state: "nao_iniciado" as const,
        progress: 0,
        blocked: stage > 1,
      })),
    },
  }
}

describe("App flow integration (landing → wizard routing)", () => {
  beforeEach(() => {
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(makeBases())
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
    getCampaignMock.mockReset()
    getCampaignMock.mockResolvedValue(makeCampaign())
  })

  it("mounts the Landing first, then enters the ExecutionWizard on the CTA click", async () => {
    const user = userEvent.setup()
    renderWithI18n(<App />)

    // Landing is the entry screen: its CTA is present and the wizard is not.
    const cta = await screen.findByRole("button", { name: "Iniciar campanha" })
    expect(cta).toBeInTheDocument()
    expect(screen.queryByTestId("execution-wizard")).toBeNull()

    // START via the CTA switches the view landing → wizard, straight into
    // Stage 1 (STIX base selection) — no standalone bases/dashboard screens.
    await user.click(cta)

    expect(await screen.findByTestId("execution-wizard")).toBeInTheDocument()
    expect(await screen.findByText("Passo 1 de 6")).toBeInTheDocument()
    // The Landing CTA is gone once we entered the wizard.
    expect(
      screen.queryByRole("button", { name: "Iniciar campanha" }),
    ).toBeNull()
  })
})
