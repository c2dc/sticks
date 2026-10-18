import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { CampaignView } from "@/components/CampaignView"
import type {
  ListedCampaign,
  Stage2Response,
  CampaignSelectionResult,
} from "@/lib/stages"

/**
 * CampaignView — Stage 2 (Campaign selection) of the six-stage pipeline.
 * Validates: Requirements 4.1, 6.2, 6.3, 6.5
 *
 * We mock `@/lib/stages` to control `getStageCampaign` (list + read-only
 * selection) and `selectCampaign` (attribute extraction). We assert:
 * - the list of the 8 selectable curated campaigns + disabled non-selectable
 *   ones (Req. 6.2, 6.5);
 * - the propagation of the version to the wrappers (Req. 4.1);
 * - the selection that extracts and shows the attributes (identity + techniques)
 *   and completes the stage when `completed` (Req. 6.3, 6.5);
 * - the extraction failure surfacing as `completed === false` with the cause
 *   (Req. 6.7).
 */

vi.mock("@/lib/stages", () => ({
  getStageCampaign: vi.fn(),
  selectCampaign: vi.fn(),
}))

import { getStageCampaign, selectCampaign } from "@/lib/stages"

const getStageCampaignMock = vi.mocked(getStageCampaign)
const selectCampaignMock = vi.mocked(selectCampaign)

function campaign(
  name: string,
  slug: string | null,
  selectable: boolean,
): ListedCampaign {
  return {
    stix_id: `campaign--${name}`,
    name,
    description: `Descrição de ${name}`,
    curated: selectable,
    curated_slug: slug,
    selectable,
  }
}

/** Eight selectable curated campaigns + one non-selectable (version 18). */
function listWithEightCurated(): ListedCampaign[] {
  const curated = Array.from({ length: 8 }, (_, i) =>
    campaign(`Curada${i + 1}`, `curada-${i + 1}`, true),
  )
  const nonCurated = campaign("Externa", null, false)
  return [...curated, nonCurated]
}

function emptySelection(): CampaignSelectionResult {
  return {
    campaign_id: "shadowray",
    version: "18.0",
    completed: false,
    identity: null,
    techniques: [],
  }
}

function response(
  campaigns: ListedCampaign[],
  selection: CampaignSelectionResult = emptySelection(),
): Stage2Response {
  return {
    campaign_id: "shadowray",
    version: "18.0",
    campaigns,
    selection,
  }
}

function completedSelection(): CampaignSelectionResult {
  return {
    campaign_id: "shadowray",
    version: "18.0",
    completed: true,
    identity: {
      found: true,
      base_version: "18.0",
      stix_id: "campaign--abc",
      name: "ShadowRay",
      external_id: "C0045",
      description: "Campanha que explora clusters Ray expostos.",
      aliases: ["RayHunter"],
      first_seen: null,
      last_seen: null,
      unavailable_reason: null,
    },
    techniques: [
      {
        technique_id: "T1190",
        technique_name: "Exploit Public-Facing Application",
        tactic: "initial-access",
      },
    ],
  }
}

describe("CampaignView — Stage 2 (Req. 4.1, 6.2, 6.3, 6.5)", () => {
  beforeEach(() => {
    getStageCampaignMock.mockReset()
    selectCampaignMock.mockReset()
  })

  it("lists the 8 selectable curated campaigns and disables the non-selectable ones", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    // The 9 campaigns appear; the 8 curated ones have an enabled button.
    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    expect(curada1).not.toBeDisabled()

    const curated = screen.getAllByRole("button", { name: /Curada\d/ })
    expect(curated).toHaveLength(8)
    curated.forEach((btn) => expect(btn).not.toBeDisabled())

    // The non-curated campaign appears but disabled.
    const externa = screen.getByRole("button", { name: /Externa/ })
    expect(externa).toBeDisabled()
  })

  it("propagates the version to getStageCampaign", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    await waitFor(() =>
      expect(getStageCampaignMock).toHaveBeenCalledWith("shadowray", "18.0"),
    )
  })

  it("selecting a campaign extracts and shows the attributes and completes the stage", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))
    selectCampaignMock.mockResolvedValue(completedSelection())
    const onCompleted = vi.fn()

    renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" onCompleted={onCompleted} />,
    )

    // Pick the first curated campaign and confirm the selection.
    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    fireEvent.click(curada1)
    const confirmar = screen.getByRole("button", { name: "Selecionar campanha" })
    fireEvent.click(confirmar)

    // The wrapper receives the curated slug + the version.
    await waitFor(() =>
      expect(selectCampaignMock).toHaveBeenCalledWith(
        "shadowray",
        "curada-1",
        "18.0",
      ),
    )

    // The extracted attributes are shown (identity reusing the Stage1View block
    // + techniques), and the completion indicator appears.
    await screen.findByTestId("campanha-concluida")
    const attributes = screen
      .getByTestId("campanha-concluida")
      .closest('[data-section="atributos-extraidos"]') as HTMLElement
    expect(attributes).not.toBeNull()
    expect(attributes).toHaveTextContent("ShadowRay")
    expect(attributes).toHaveTextContent("C0045")
    expect(attributes).toHaveTextContent("T1190")
    expect(attributes).toHaveTextContent("Exploit Public-Facing Application")

    expect(onCompleted).toHaveBeenCalledTimes(1)
    // onCompleted now reports the chosen curated slug plus the typed result so
    // the wizard can adopt the slug as the campaignId for Stages 3..6.
    expect(onCompleted).toHaveBeenCalledWith("curada-1", completedSelection())
  })

  it("extraction failure surfaces as completed=false with the cause (without completing)", async () => {
    getStageCampaignMock.mockResolvedValue(response(listWithEightCurated()))
    selectCampaignMock.mockResolvedValue({
      campaign_id: "shadowray",
      version: "18.0",
      completed: false,
      identity: null,
      techniques: [],
      error_message: "Artefato curado ausente.",
      failed_step: "extracao_atributos",
    })
    const onCompleted = vi.fn()

    renderWithI18n(
      <CampaignView campaignId="shadowray" version="18.0" onCompleted={onCompleted} />,
    )

    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    fireEvent.click(curada1)
    fireEvent.click(screen.getByRole("button", { name: "Selecionar campanha" }))

    const alert = await screen.findByTestId("campanha-falha")
    expect(alert).toHaveTextContent("Artefato curado ausente.")
    expect(screen.queryByTestId("campanha-concluida")).toBeNull()
    // Extraction failed → the stage did not complete.
    expect(onCompleted).not.toHaveBeenCalled()
  })

  it("shows the 'no campaigns' note when the bundle has no campaigns", async () => {
    getStageCampaignMock.mockResolvedValue(response([]))

    renderWithI18n(<CampaignView campaignId="shadowray" version="18.0" />)

    expect(
      await screen.findByText("Nenhuma campanha disponível neste bundle."),
    ).toBeInTheDocument()
  })
})
