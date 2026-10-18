import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { StageSection } from "@/components/StageSection"
import { Stage1View } from "@/components/Stage1View"
import type { Stage1Result } from "@/lib/stages"

// Mock the stage-detail API wrapper so no real backend is needed (Req. 1.2).
vi.mock("@/lib/stages", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stages")>()
  return { ...actual, getStage1: vi.fn() }
})

import { getStage1 } from "@/lib/stages"

const getStage1Mock = vi.mocked(getStage1)

/** A minimal, non-empty Stage-1 result fixture. */
function makeStage1Result(): Stage1Result {
  return {
    campaign_id: "apt41-dust",
    state: "concluido",
    input: {
      campaign_id: "apt41-dust",
      name: "APT41-DUST",
      // The Stage 1 Input is now the slice of the `campaign` object of the STIX
      // base (`campaign_stix`); the legacy fields `dag_file`/`stix_reference`/
      // `campaign_name` remain in the contract for compatibility, but the UI no
      // longer displays them (Property 3).
      dag_file: "data/dag/apt41-dust_dag.json",
      stix_reference: "attack-pattern--abc123",
      campaign_name: "APT41 DUST",
      campaign_stix: {
        found: true,
        base_version: "18.0",
        stix_id: "campaign--apt41-dust",
        name: "APT41 DUST",
        external_id: "C0040",
        description: null,
        aliases: [],
        first_seen: null,
        last_seen: null,
        unavailable_reason: null,
      },
    },
    output: {
      techniques: [
        { technique_id: "T1059", name: "Command and Scripting Interpreter" },
      ],
      relationships: [{ source: "a", target: "b", type: "uses" }],
      indicators: [],
      infrastructure: [],
      malware: [],
      metadata: { first_seen: "2023" },
      is_empty: false,
    },
  }
}

describe("StageSection (Req. 1.2 — labelled Entrada/Processamento/Saída panels)", () => {
  it("renders its label and children in a distinct panel", () => {
    renderWithI18n(
      <StageSection label="Entrada" data-section="entrada">
        <span>conteúdo da entrada</span>
      </StageSection>,
    )

    expect(screen.getByText("Entrada")).toBeInTheDocument()
    expect(screen.getByText("conteúdo da entrada")).toBeInTheDocument()
  })

  it("keeps three sections visually separate via data-section", () => {
    const { container } = renderWithI18n(
      <div>
        <StageSection label="Entrada" data-section="entrada">
          <span>in</span>
        </StageSection>
        <StageSection label="Processamento" data-section="processamento">
          <span>proc</span>
        </StageSection>
        <StageSection label="Saída" data-section="saida">
          <span>out</span>
        </StageSection>
      </div>,
    )

    const sections = container.querySelectorAll("[data-section]")
    const kinds = Array.from(sections).map((el) => el.getAttribute("data-section"))
    // Three distinct sections, each rendered once.
    expect(kinds).toEqual(["entrada", "processamento", "saida"])
    expect(new Set(kinds).size).toBe(3)
  })
})

describe("Stage1View (Req. 1.2 — three labelled sections render for a stage)", () => {
  beforeEach(() => {
    getStage1Mock.mockReset()
  })

  it("renders the Entrada, Processamento and Saída sections with distinct labels", async () => {
    getStage1Mock.mockResolvedValue(makeStage1Result())

    const { container } = renderWithI18n(<Stage1View campaignId="apt41-dust" />)

    // Wait for the mocked fetch to resolve and the sections to render.
    await waitFor(() => {
      expect(screen.getByText("Entrada")).toBeInTheDocument()
    })

    // All three section labels appear...
    expect(screen.getByText("Entrada")).toBeInTheDocument()
    expect(screen.getByText("Processamento")).toBeInTheDocument()
    expect(screen.getByText("Saída")).toBeInTheDocument()

    // ...and they are three distinct, separated panels. Scoped to the three
    // top-level stage panels (`CampaignStixBlock` carries its own nested
    // `data-section="campanha-stix"` inside the Entrada, which is not a panel).
    const sections = container.querySelectorAll(
      '[data-section="entrada"], [data-section="processamento"], [data-section="saida"]',
    )
    expect(sections).toHaveLength(3)
    const kinds = Array.from(sections).map((el) => el.getAttribute("data-section"))
    expect(new Set(kinds)).toEqual(new Set(["entrada", "processamento", "saida"]))

    // Sanity: the Input carries the STIX campaign identity (rich fields), and
    // NOT the old Caldera `stix_reference` (Property 3).
    const entrada = container.querySelector('[data-section="entrada"]')!
    expect(within(entrada as HTMLElement).getByTestId("campanha-stix")).toBeInTheDocument()
    expect(within(entrada as HTMLElement).getByText("APT41 DUST")).toBeInTheDocument()
    expect(entrada.textContent ?? "").not.toContain("attack-pattern--abc123")

    expect(getStage1Mock).toHaveBeenCalledWith("apt41-dust", undefined)
  })
})
