import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Caso, CasoEstados, CasesResponse } from "@/lib/casos"
import type { ResultadoEstagio1 } from "@/lib/estagios"
import type { BaseStixInfo } from "@/lib/stix"

/**
 * Feature: pipeline-seis-estagios — propagation of the chosen base version to
 * Stage 1 (Req. 4.1, 5.1, 5.2).
 *
 * The wizard is entered with no campaign chosen; it resolves a probe slug via
 * `fetchCases` and renders the real `<BaseStixView casoId={probe} />` as the
 * body of Stage 1. With no base selected yet, Stage 1 shows the bases list
 * (StixBasesList → `getBases`) and does NOT read the completion verdict. Once a
 * base is selected, BaseStixView reads `getEstagioBaseStix(probe, versao)` with
 * the chosen version — that call is the propagation assertion.
 *
 * Only the network wrappers (`@/lib/estagios`, `@/lib/stix`, `@/lib/casos`) and
 * `@/lib/wizard` (getCaso) are mocked; BaseStixView renders for real.
 */

vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return { ...actual, getEstagioBaseStix: vi.fn() }
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

import { getEstagioBaseStix } from "@/lib/estagios"
import { getBases } from "@/lib/stix"
import { fetchCases } from "@/lib/casos"
import { getCaso } from "@/lib/wizard"

const getEstagioBaseStixMock = vi.mocked(getEstagioBaseStix)
const getBasesMock = vi.mocked(getBases)
const fetchCasesMock = vi.mocked(fetchCases)
const getCasoMock = vi.mocked(getCaso)

function estados(concluidos: number[]): CasoEstados {
  const todos = [1, 2, 3, 4, 5, 6] as const
  return {
    concluido_total: concluidos.length === 6,
    estagios: todos.map((estagio) => ({
      estagio,
      estado: concluidos.includes(estagio) ? "concluido" : "nao_iniciado",
      progresso: concluidos.includes(estagio) ? 100 : 0,
      bloqueado: estagio > 1 && !concluidos.includes(estagio - 1),
    })),
  }
}

function makeCaso(concluidos: number[]): Caso {
  return { id: "shadowray", nome: "ShadowRay", estados: estados(concluidos) }
}

function makeCases(): CasesResponse {
  return {
    casos: [{ id: "shadowray", nome: "ShadowRay" }],
    progresso: { casos_concluidos: 0, total_casos: 8, percentual: 0 },
  }
}

function makeResultadoEstagio1(versao: string): ResultadoEstagio1 {
  return {
    versao,
    concluido: true,
    bundle_em_disco: true,
    dashboard: null,
    campanhas: [],
    curada: true,
  }
}

const bases: BaseStixInfo[] = [
  {
    versao: "18.0",
    nome: "MITRE ATT&CK v18.0",
    baixada: true,
    selecionavel: true,
    arquivo: "enterprise-attack-18.0.json",
  },
]

describe("ExecutionWizard — propaga a versão ao Estágio 1 (Req. 4.1, 5.1, 5.2)", () => {
  beforeEach(() => {
    getEstagioBaseStixMock.mockReset()
    getEstagioBaseStixMock.mockResolvedValue(makeResultadoEstagio1("18.0"))
    getBasesMock.mockReset()
    getBasesMock.mockResolvedValue(bases)
    fetchCasesMock.mockReset()
    fetchCasesMock.mockResolvedValue(makeCases())
    getCasoMock.mockReset()
    getCasoMock.mockResolvedValue(makeCaso([]))
  })

  it("ao selecionar uma base: o BaseStixView chama getEstagioBaseStix(probe, versao)", async () => {
    renderWithI18n(<ExecutionWizard />)

    // No base selected yet: the bases list loads, no verdict read.
    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getEstagioBaseStixMock).not.toHaveBeenCalled()

    // Select the base 18.0 from the list; the verdict read fires with the probe
    // id resolved from fetchCases.
    const selecionar = await screen.findByRole("button", { name: "Selecionar" })
    await userEvent.click(selecionar)

    await waitFor(() =>
      expect(getEstagioBaseStixMock).toHaveBeenCalledWith("shadowray", "18.0"),
    )
  })

  it("sem base selecionada: o Estágio 1 mostra a lista de bases e não consulta o veredito", async () => {
    renderWithI18n(<ExecutionWizard />)

    await waitFor(() => expect(getBasesMock).toHaveBeenCalled())
    expect(getEstagioBaseStixMock).not.toHaveBeenCalled()
  })
})
