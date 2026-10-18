import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Campaign, CampaignStates, CasesResponse } from "@/lib/campaigns"
import type { Stage1ResultStix } from "@/lib/stages"
import type { BaseStixInfo } from "@/lib/stix"

/**
 * Feature: pipeline-seis-estagios — propagation of the chosen base version to
 * Stage 1 (Req. 4.1, 5.1, 5.2).
 *
 * The wizard is entered with no campaign chosen; it resolves a probe slug via
 * `fetchCases` and renders the real `<BaseStixView campaignId={probe} />` as the
 * body of Stage 1. With no base selected yet, Stage 1 shows the bases list
 * (StixBasesList → `getBases`) and does NOT read the completion verdict. Once a
 * base is selected, BaseStixView reads `getStageBaseStix(probe, version)` with
 * the chosen version — that call is the propagation assertion.
 *
 * Only the network wrappers (`@/lib/stages`, `@/lib/stix`, `@/lib/campaigns`) and
 * `@/lib/wizard` (getCampaign) are mocked; BaseStixView renders for real.
 */

vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return { ...actual, getStageBaseStix: vi.fn() }
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

import { getStageBaseStix } from "@/lib/stages"
import { getBases } from "@/lib/stix"
import { fetchCases } from "@/lib/campaigns"
import { getCampaign } from "@/lib/wizard"

const getStageBaseStixMock = vi.mocked(getStageBaseStix)
const getBasesMock = vi.mocked(getBases)
const fetchCasesMock = vi.mocked(fetchCases)
const getCampaignMock = vi.mocked(getCampaign)

function states(completed: number[]): CampaignStates {
  const all = [1, 2, 3, 4, 5, 6] as const
  return {
    fully_completed: completed.length === 6,
    stages: all.map((stage) => ({
      stage,
      state: completed.includes(stage) ? "concluido" : "nao_iniciado",
      progress: completed.includes(stage) ? 100 : 0,
      blocked: stage > 1 && !completed.includes(stage - 1),
    })),
  }
}

function makeCampaign(completed: number[]): Campaign {
  return { id: "shadowray", name: "ShadowRay", states: states(completed) }
}

function makeCases(): CasesResponse {
  return {
    campaigns: [{ id: "shadowray", name: "ShadowRay" }],
    progress: { completed_campaigns: 0, total_campaigns: 8, percentage: 0 },
  }
}

function makeStage1Result(version: string): Stage1ResultStix {
  return {
    version,
    completed: true,
    bundle_on_disk: true,
    dashboard: null,
    campaigns: [],
    curated: true,
  }
}

const bases: BaseStixInfo[] = [
  {
    version: "18.0",
    name: "MITRE ATT&CK v18.0",
    downloaded: true,
    selectable: true,
    file: "enterprise-attack-18.0.json",
  },
]

describe("ExecutionWizard — propagates the version to Stage 1 (Req. 4.1, 5.1, 5.2)", () => {
  beforeEach(() => {
    getStageBaseStixMock.mockReset()
    getStageBaseStixMock.mockResolvedValue(makeStage1Result("18.0"))
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(bases)
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
    getCampaignMock.mockReset()
    getCampaignMock.mockResolvedValue(makeCampaign([]))
  })

  it("on selecting a base: BaseStixView calls getStageBaseStix(probe, version)", async () => {
    renderWithI18n(<ExecutionWizard />)

    // No base selected yet: the bases list loads, no verdict read.
    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getStageBaseStixMock).not.toHaveBeenCalled()

    // Select the base 18.0 from the list; the verdict read fires with the probe
    // id resolved from fetchCases.
    const selectButton = await screen.findByRole("button", { name: "Selecionar" })
    await userEvent.click(selectButton)

    await waitFor(() =>
      expect(getStageBaseStixMock).toHaveBeenCalledWith("shadowray", "18.0"),
    )
  })

  it("with no base selected: Stage 1 shows the bases list and does not query the verdict", async () => {
    renderWithI18n(<ExecutionWizard />)

    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getStageBaseStixMock).not.toHaveBeenCalled()
  })
})
