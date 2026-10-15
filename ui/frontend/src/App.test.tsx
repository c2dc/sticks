import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import App from "@/App"
import type { BaseStixInfo } from "@/lib/stix"
import type { Caso, CasesResponse } from "@/lib/casos"

// Mock every API wrapper the App + wizard touch so no network is used and the
// routing can be driven deterministically.
vi.mock("@/lib/preferencias", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/preferencias")>()
  return {
    ...actual,
    getPreferencias: vi.fn().mockResolvedValue(null),
    putPreferencias: vi.fn().mockResolvedValue({ tema: "claro", idioma: "pt-BR" }),
  }
})

vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return { ...actual, getBases: vi.fn() }
})

vi.mock("@/lib/casos", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/casos")>()
  return { ...actual, fetchCases: vi.fn() }
})

vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return { ...actual, getCaso: vi.fn(), resetCaso: vi.fn() }
})

import { getBases } from "@/lib/stix"
import { fetchCases } from "@/lib/casos"
import { getCaso } from "@/lib/wizard"

const getBasesMock = vi.mocked(getBases)
const fetchCasesMock = vi.mocked(fetchCases)
const getCasoMock = vi.mocked(getCaso)

function makeBases(): BaseStixInfo[] {
  return [
    {
      versao: "18.0",
      nome: "Enterprise ATT&CK v18.0",
      baixada: true,
      selecionavel: true,
      arquivo: "enterprise-attack-18.0.json",
    },
  ]
}

function makeCases(): CasesResponse {
  return {
    casos: [{ id: "shadowray", nome: "ShadowRay" }],
    progresso: { casos_concluidos: 0, total_casos: 8, percentual: 0 },
  }
}

function makeCaso(): Caso {
  return {
    id: "shadowray",
    nome: "ShadowRay",
    estados: {
      concluido_total: false,
      estagios: [1, 2, 3, 4, 5, 6].map((estagio) => ({
        estagio: estagio as 1 | 2 | 3 | 4 | 5 | 6,
        estado: "nao_iniciado" as const,
        progresso: 0,
        bloqueado: estagio > 1,
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
    getCasoMock.mockReset()
    getCasoMock.mockResolvedValue(makeCaso())
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
