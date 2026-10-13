import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { StageSection } from "@/components/StageSection"
import { Stage1View } from "@/components/Stage1View"
import type { Stage1Result } from "@/lib/estagios"

// Mock the stage-detail API wrapper so no real backend is needed (Req. 1.2).
vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return { ...actual, getEstagio1: vi.fn() }
})

import { getEstagio1 } from "@/lib/estagios"

const getEstagio1Mock = vi.mocked(getEstagio1)

/** A minimal, non-empty Stage-1 result fixture. */
function makeStage1Result(): Stage1Result {
  return {
    caso_id: "apt41-dust",
    estado: "concluido",
    entrada: {
      caso_id: "apt41-dust",
      nome: "APT41-DUST",
      // Entrada do Estágio 1 agora é o recorte do objeto `campaign` da base
      // STIX (`campanha_stix`); os campos legados `dag_file`/`stix_reference`/
      // `campaign_name` permanecem no contrato por compatibilidade, mas a UI
      // não os exibe mais (Property 3).
      dag_file: "data/dag/apt41-dust_dag.json",
      stix_reference: "attack-pattern--abc123",
      campaign_name: "APT41 DUST",
      campanha_stix: {
        encontrada: true,
        versao_base: "18.0",
        stix_id: "campaign--apt41-dust",
        nome: "APT41 DUST",
        external_id: "C0040",
        descricao: null,
        aliases: [],
        first_seen: null,
        last_seen: null,
        motivo_indisponivel: null,
      },
    },
    saida: {
      techniques: [
        { technique_id: "T1059", name: "Command and Scripting Interpreter" },
      ],
      relationships: [{ source: "a", target: "b", tipo: "uses" }],
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
    getEstagio1Mock.mockReset()
  })

  it("renders the Entrada, Processamento and Saída sections with distinct labels", async () => {
    getEstagio1Mock.mockResolvedValue(makeStage1Result())

    const { container } = renderWithI18n(<Stage1View casoId="apt41-dust" />)

    // Wait for the mocked fetch to resolve and the sections to render.
    await waitFor(() => {
      expect(screen.getByText("Entrada")).toBeInTheDocument()
    })

    // All three section labels appear...
    expect(screen.getByText("Entrada")).toBeInTheDocument()
    expect(screen.getByText("Processamento")).toBeInTheDocument()
    expect(screen.getByText("Saída")).toBeInTheDocument()

    // ...and they are three distinct, separated panels. Scoped to the three
    // top-level stage panels (`CampanhaStixBlock` carries its own nested
    // `data-section="campanha-stix"` inside the Entrada, which is not a panel).
    const sections = container.querySelectorAll(
      '[data-section="entrada"], [data-section="processamento"], [data-section="saida"]',
    )
    expect(sections).toHaveLength(3)
    const kinds = Array.from(sections).map((el) => el.getAttribute("data-section"))
    expect(new Set(kinds)).toEqual(new Set(["entrada", "processamento", "saida"]))

    // Sanity: a Entrada carrega a identidade da campanha STIX (campos ricos),
    // e NÃO a antiga `stix_reference` da Caldera (Property 3).
    const entrada = container.querySelector('[data-section="entrada"]')!
    expect(within(entrada as HTMLElement).getByTestId("campanha-stix")).toBeInTheDocument()
    expect(within(entrada as HTMLElement).getByText("APT41 DUST")).toBeInTheDocument()
    expect(entrada.textContent ?? "").not.toContain("attack-pattern--abc123")

    expect(getEstagio1Mock).toHaveBeenCalledWith("apt41-dust", undefined)
  })
})
