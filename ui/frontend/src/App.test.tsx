import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import App from "@/App"
import type { BaseStixInfo } from "@/lib/stix"

// Mock every API wrapper the App touches so no network is used and the flow
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
  return {
    ...actual,
    getBases: vi.fn(),
    baixarBase: vi.fn(),
    getDashboard: vi.fn(),
    getCampanhas: vi.fn(),
    getAtores: vi.fn(),
  }
})

vi.mock("@/lib/dag", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/dag")>()
  return { ...actual, getDag: vi.fn() }
})

import { getBases } from "@/lib/stix"

const getBasesMock = vi.mocked(getBases)

function makeBases(): BaseStixInfo[] {
  return [
    {
      versao: "14.0",
      nome: "Enterprise ATT&CK v14.0",
      baixada: true,
      selecionavel: true,
      arquivo: "enterprise-attack-14.0.json",
    },
  ]
}

describe("App flow integration (Req. 7.1, 9.3 — landing → bases routing)", () => {
  beforeEach(() => {
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(makeBases())
  })

  it("mounts the Landing first, then switches to the bases screen on the CTA click", async () => {
    const user = userEvent.setup()
    renderWithI18n(<App />)

    // Landing is the entry screen: its CTA is present.
    const cta = await screen.findByRole("button", { name: "Iniciar campanha" })
    expect(cta).toBeInTheDocument()
    // The bases screen is not mounted yet.
    expect(screen.queryByText("Bases STIX")).toBeNull()

    // Driving INICIAR via the CTA switches the view landing → bases.
    await user.click(cta)

    // The bases screen mounts and loads the (mocked) bases list.
    expect(await screen.findByText("Bases STIX")).toBeInTheDocument()
    expect(await screen.findByText("Enterprise ATT&CK v14.0")).toBeInTheDocument()
    expect(getBasesMock).toHaveBeenCalled()
    // The Landing CTA is gone once we advanced.
    expect(screen.queryByRole("button", { name: "Iniciar campanha" })).toBeNull()
  })
})
