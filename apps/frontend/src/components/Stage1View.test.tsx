import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { Stage1View } from "@/components/Stage1View"
import type {
  CampaignStixInput,
  Stage1Result,
  Stage1Output,
} from "@/lib/stages"

/**
 * Feature: stage1-campaign-stix input — version propagation (Task 6.2).
 * Validates: Requirements 3.1, 6.2
 *
 * Stage1View receives `{ campaignId, version? }` and, in the mount effect, calls
 * `getStage1(campaignId, version)` with the STIX base version selected in the
 * flow. When the version is not provided, it calls without the parameter (the
 * backend applies the backward-compatible fallback `versao_ausente`). We mock
 * `@/lib/stages` to assert exactly the arguments passed to the wrapper.
 */

// Mock the stage-detail wrapper so no real HTTP is used; we only assert the
// arguments the view hands to `getStage1`.
vi.mock("@/lib/stages", () => ({
  getStage1: vi.fn(),
}))

import { getStage1 } from "@/lib/stages"

const getStage1Mock = vi.mocked(getStage1)

function makeResult(): Stage1Result {
  return {
    campaign_id: "shadowray",
    state: "nao_iniciado",
    input: { campaign_id: "shadowray", name: "ShadowRay" },
    output: null,
  }
}

describe("Stage1View — version propagation to getStage1 (Req. 3.1, 6.2)", () => {
  beforeEach(() => {
    getStage1Mock.mockReset()
    getStage1Mock.mockResolvedValue(makeResult())
  })

  it("with `version`: calls getStage1(campaignId, version) with that version", async () => {
    renderWithI18n(<Stage1View campaignId="shadowray" version="18.0" />)

    await waitFor(() => expect(getStage1Mock).toHaveBeenCalledTimes(1))
    expect(getStage1Mock).toHaveBeenCalledWith("shadowray", "18.0")
  })

  it("without `version`: calls getStage1(campaignId, undefined) (no version)", async () => {
    renderWithI18n(<Stage1View campaignId="shadowray" />)

    await waitFor(() => expect(getStage1Mock).toHaveBeenCalledTimes(1))
    expect(getStage1Mock).toHaveBeenCalledWith("shadowray", undefined)
  })
})
/**
 * Feature: stage1-campaign-stix input — Input render (Task 7.3).
 * Validates: Requirements 2.3, 2.4, 4.2 (Property 1, 3)
 *
 * The Stage 1 Input becomes the slice of the `campaign` object of the selected
 * STIX base (`input.campaign_stix`):
 * - found → renders the rich fields (name, STIX id, external_id, description,
 *   aliases, dates, base version);
 * - not found → `role="alert"` warning + curated identifier (name ·
 *   external_id), WITHOUT the rich block.
 *
 * Two product invariants are asserted here:
 * - Property 1: the structural extraction (Output) stays intact — the Input
 *   never breaks/affects the Output, regardless of the campaign state.
 * - Property 3: a Caldera path/file (e.g. `data/caldera_adversaries`, `*.yml`)
 *   or the old "STIX reference" never appears as input, in any state.
 */

/** Fixture of the found campaign, with rich fields filled in. */
function foundCampaign(): CampaignStixInput {
  return {
    found: true,
    base_version: "18.0",
    stix_id: "campaign--12345678-abcd-ef00-1234-567890abcdef",
    name: "ShadowRay",
    external_id: "C0045",
    description: "Campanha que explora clusters Ray expostos na internet.",
    aliases: ["A", "B"],
    first_seen: "2023-09-05T00:00:00.000Z",
    last_seen: "2023-11-15T00:00:00.000Z",
    unavailable_reason: null,
  }
}

/**
 * Fixture of the NOT-found campaign: only the curated identifier (name +
 * external_id) survives; the rich fields are absent (null).
 */
function notFoundCampaign(): CampaignStixInput {
  return {
    found: false,
    base_version: null,
    stix_id: null,
    name: "ShadowRay",
    external_id: "C0045",
    description: null,
    aliases: [],
    first_seen: null,
    last_seen: null,
    unavailable_reason: "campanha_nao_encontrada",
  }
}

/** Non-empty structural Output, with one extracted technique. */
function outputWithTechniques(): Stage1Output {
  return {
    techniques: [
      {
        technique_id: "T1190",
        name: "Exploit Public-Facing Application",
        tactic: "initial-access",
      },
    ],
    relationships: [],
    indicators: [],
    infrastructure: [],
    malware: [],
    metadata: {},
    is_empty: false,
  }
}

/** Builds a complete `Stage1Result` for the desired scenario. */
function makeResultWithCampaign(
  campaign: CampaignStixInput,
  output: Stage1Output | null = null,
): Stage1Result {
  return {
    campaign_id: "shadowray",
    state: output ? "concluido" : "nao_iniciado",
    input: {
      campaign_id: "shadowray",
      name: "ShadowRay",
      campaign_stix: campaign,
    },
    output,
  }
}

describe("Stage1View — campaign_stix Input render (Req. 2.3, 2.4, 4.2)", () => {
  beforeEach(() => {
    getStage1Mock.mockReset()
  })

  it("found: renders the rich fields (name, STIX id, external_id, description, aliases, dates, version)", async () => {
    getStage1Mock.mockResolvedValue(makeResultWithCampaign(foundCampaign()))

    renderWithI18n(<Stage1View campaignId="shadowray" />)

    // The rich block is present; the fallback warning is not.
    const block = await screen.findByTestId("campaign-stix")
    expect(screen.queryByTestId("campaign-stix-not-found")).toBeNull()

    // Rich fields rendered.
    expect(block).toHaveTextContent("ShadowRay")
    expect(block).toHaveTextContent(
      "campaign--12345678-abcd-ef00-1234-567890abcdef",
    )
    expect(block).toHaveTextContent("C0045")
    expect(block).toHaveTextContent(
      "Campanha que explora clusters Ray expostos na internet.",
    )
    // Aliases as chips.
    expect(block).toHaveTextContent("A")
    expect(block).toHaveTextContent("B")
    // Dates and base version.
    expect(block).toHaveTextContent("2023-09-05T00:00:00.000Z")
    expect(block).toHaveTextContent("2023-11-15T00:00:00.000Z")
    expect(block).toHaveTextContent("18.0")
  })

  it("not found: shows the warning (role=alert) + curated identifier and does NOT render the rich block", async () => {
    getStage1Mock.mockResolvedValue(
      makeResultWithCampaign(notFoundCampaign()),
    )

    renderWithI18n(<Stage1View campaignId="shadowray" />)

    // Fallback warning with alert role and the pt-BR key text.
    const alert = await screen.findByRole("alert")
    expect(alert).toHaveTextContent("Campanha não encontrada nesta base STIX.")

    // The curated identifier (name · external_id) stays visible.
    expect(alert).toHaveTextContent("ShadowRay")
    expect(alert).toHaveTextContent("C0045")

    // The rich-fields block is NOT present.
    expect(screen.queryByTestId("campaign-stix")).toBeNull()
    expect(screen.getByTestId("campaign-stix-not-found")).toBeInTheDocument()
  })

  it("Property 1: the Output (structural extraction) stays intact with a found campaign", async () => {
    getStage1Mock.mockResolvedValue(
      makeResultWithCampaign(foundCampaign(), outputWithTechniques()),
    )

    const { container } = renderWithI18n(<Stage1View campaignId="shadowray" />)

    await screen.findByTestId("campaign-stix")
    const output = container.querySelector('[data-section="saida"]')
    expect(output).not.toBeNull()
    // The extracted technique appears in the Output, regardless of the Input.
    expect(output).toHaveTextContent("T1190")
    expect(output).toHaveTextContent("Exploit Public-Facing Application")
  })

  it("Property 1: the Output stays intact even when the campaign is not found", async () => {
    getStage1Mock.mockResolvedValue(
      makeResultWithCampaign(notFoundCampaign(), outputWithTechniques()),
    )

    const { container } = renderWithI18n(<Stage1View campaignId="shadowray" />)

    // Input fell into the fallback, but the structural Output keeps rendering.
    await screen.findByTestId("campaign-stix-not-found")
    const output = container.querySelector('[data-section="saida"]')
    expect(output).not.toBeNull()
    expect(output).toHaveTextContent("T1190")
    expect(output).toHaveTextContent("Exploit Public-Facing Application")
  })

  it("Property 3: never shows a Caldera path/file nor 'STIX reference' (found and not found)", async () => {
    // Found state.
    getStage1Mock.mockResolvedValue(
      makeResultWithCampaign(foundCampaign(), outputWithTechniques()),
    )
    const found = renderWithI18n(<Stage1View campaignId="shadowray" />)
    await screen.findByTestId("campaign-stix")

    const foundText = found.container.textContent ?? ""
    expect(foundText).not.toMatch(/data\/caldera_adversaries/)
    expect(foundText).not.toMatch(/\.yml\b/)
    // The old "STIX reference" (stix_reference) is no longer rendered.
    expect(foundText).not.toContain("Referência STIX")
    expect(found.container.querySelector("input")).toBeNull()
    found.unmount()

    // Not-found state.
    getStage1Mock.mockResolvedValue(
      makeResultWithCampaign(notFoundCampaign(), outputWithTechniques()),
    )
    const notFound = renderWithI18n(<Stage1View campaignId="shadowray" />)
    await screen.findByTestId("campaign-stix-not-found")

    const notFoundText = notFound.container.textContent ?? ""
    expect(notFoundText).not.toMatch(/data\/caldera_adversaries/)
    expect(notFoundText).not.toMatch(/\.yml\b/)
    expect(notFoundText).not.toContain("Referência STIX")
    expect(notFound.container.querySelector("input")).toBeNull()
  })
})
