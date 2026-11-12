import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { BaseStixInfo } from "@/lib/stix"

/**
 * Feature: ai-first-campaign-flow (task 9.10) — propagation of the chosen STIX
 * base version into Input step A ({@link BaseStixView}).
 *
 * The two-layer shell renders the real `<BaseStixView />` as the first Input
 * step. With no base selected yet, step A shows the bases list (`getBases`) and
 * does NOT request the package overview or the campaigns. Once a base is
 * selected, the overview (`getPackageOverview`) and the campaign listing
 * (`getCampaigns`) are requested with the chosen version — those calls are the
 * propagation assertion.
 *
 * Only the `@/lib/stix` network wrappers are mocked; BaseStixView and
 * CampaignView render for real. The Run view is stubbed since it is never
 * reached in these tests.
 */

vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return {
    ...actual,
    getBases: vi.fn(),
    getPackageOverview: vi.fn(),
    getPackageAnalytics: vi.fn(),
    getCampaigns: vi.fn(),
  }
})

vi.mock("@/components/GuidedRunView", () => ({
  GuidedRunView: ({ campaignId }: { campaignId: string; version: string }) => (
    <div data-testid="guided-run-view">{campaignId}</div>
  ),
}))

import {
  getBases,
  getCampaigns,
  getPackageAnalytics,
  getPackageOverview,
  type StixPackageOverview,
} from "@/lib/stix"

const getBasesMock = vi.mocked(getBases)
const getCampaignsMock = vi.mocked(getCampaigns)
const getPackageAnalyticsMock = vi.mocked(getPackageAnalytics)
const getPackageOverviewMock = vi.mocked(getPackageOverview)

function makeOverview(version: string): StixPackageOverview {
  return {
    version,
    name: "MITRE ATT&CK v18.0",
    downloaded_at: null,
    counts: [],
    campaigns_total: 0,
    actors_total: 0,
    techniques_total: 0,
    tactics_total: 0,
    malware_total: 0,
    relationships_total: 0,
  }
}

const bases: BaseStixInfo[] = [
  {
    version: "18.0",
    name: "MITRE ATT&CK v18.0",
    downloaded: true,
    selectable: true,
    file: null,
    completed: true,
  },
]

describe("ExecutionWizard — propagates the version to Input step A (task 9.10)", () => {
  beforeEach(() => {
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(bases)
    getPackageOverviewMock.mockReset()
    getPackageOverviewMock.mockResolvedValue(makeOverview("18.0"))
    getPackageAnalyticsMock.mockReset()
    getPackageAnalyticsMock.mockRejectedValue(new Error("not needed"))
    getCampaignsMock.mockReset()
    getCampaignsMock.mockResolvedValue([])
  })

  it("on selecting a base: the overview and campaign listing use the chosen version", async () => {
    renderWithI18n(<ExecutionWizard />)

    // No base selected yet: the bases list loads, no overview/campaign read.
    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getPackageOverviewMock).not.toHaveBeenCalled()
    expect(getCampaignsMock).not.toHaveBeenCalled()

    // The picker starts empty: open the dropdown and click 18.0. A downloaded
    // version is selected directly on the click (Req. 7.1) — no separate
    // "Selecionar" button.
    const user = userEvent.setup()
    // Open the dropdown (trigger carries the "Bases STIX" aria-label).
    await user.click(await screen.findByRole("button", { name: /Bases STIX/ }))
    // The options render as buttons inside the listbox; clicking 18.0 selects.
    const option = await screen.findByRole("option", { name: /18\.0/ })
    await user.click(option.querySelector("button") as HTMLButtonElement)

    await waitFor(() => expect(getPackageOverviewMock).toHaveBeenCalledWith("18.0"))
    await waitFor(() => expect(getCampaignsMock).toHaveBeenCalledWith("18.0"))
  })

  it("with no base selected: Input step A shows the bases list and does not query the overview", async () => {
    renderWithI18n(<ExecutionWizard />)

    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getPackageOverviewMock).not.toHaveBeenCalled()
  })
})
