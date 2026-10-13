import { beforeEach, describe, expect, it, vi } from "vitest"
import { waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Caso, CasoEstados } from "@/lib/casos"
import type { Stage1Result } from "@/lib/estagios"

/**
 * Feature: entrada-estagio1-campanha-stix — propagação da versão (Task 6.2).
 * Validates: Requirements 3.1, 6.2
 *
 * O ExecutionVizard recebe `{ casoId, versao? }` e, no Estágio 1 (estágio
 * inicial), renderiza `<Stage1View casoId versao />`. A asserção da propagação é
 * feita no wrapper real `getEstagio1`: o Stage1View (filho real, não stubado
 * aqui) chama `getEstagio1(casoId, versao)` com a versão recebida pelo wizard;
 * sem versão, chama sem o parâmetro.
 *
 * Diferente de `ExecutionWizard.test.tsx` (que stuba o Stage1View para focar na
 * estrutura do wizard), aqui o Stage1View é renderizado de verdade — só o
 * wrapper de rede `@/lib/estagios` e o `@/lib/wizard` (getCaso) são mockados.
 */

// Mock o wrapper de rede do Estágio 1: é por ele que asseveramos a propagação.
// Mock parcial — `@/lib/wizard` reexporta outros wrappers de `@/lib/estagios`
// (ex. `executarEstagio1`), que precisam permanecer definidos.
vi.mock("@/lib/estagios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/estagios")>()
  return {
    ...actual,
    getEstagio1: vi.fn(),
  }
})

// Mock os wrappers de rede do wizard; o wizard lê `getCaso` na montagem para
// derivar os estados e renderizar o Estágio 1 (estágio inicial = 1).
vi.mock("@/lib/wizard", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/wizard")>()
  return {
    ...actual,
    getCaso: vi.fn(),
    executarModelagem: vi.fn(),
    concluirTraducao: vi.fn(),
    resetCaso: vi.fn(),
  }
})

import { getEstagio1 } from "@/lib/estagios"
import { getCaso } from "@/lib/wizard"

const getEstagio1Mock = vi.mocked(getEstagio1)
const getCasoMock = vi.mocked(getCaso)

function estados(concluidos: number[]): CasoEstados {
  const todos = [1, 2, 3, 4, 5] as const
  return {
    concluido_total: concluidos.length === 5,
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

function makeStage1Result(): Stage1Result {
  return {
    caso_id: "shadowray",
    estado: "nao_iniciado",
    entrada: { caso_id: "shadowray", nome: "ShadowRay" },
    saida: null,
  }
}

describe("ExecutionWizard — propaga a versão ao Estágio 1 (Req. 3.1, 6.2)", () => {
  beforeEach(() => {
    getEstagio1Mock.mockReset()
    getEstagio1Mock.mockResolvedValue(makeStage1Result())
    getCasoMock.mockReset()
    getCasoMock.mockResolvedValue(makeCaso([]))
  })

  it("com `versao`: o Stage1View do Estágio 1 chama getEstagio1(casoId, versao)", async () => {
    renderWithI18n(<ExecutionWizard casoId="shadowray" versao="18.0" />)

    await waitFor(() => expect(getEstagio1Mock).toHaveBeenCalledTimes(1))
    expect(getEstagio1Mock).toHaveBeenCalledWith("shadowray", "18.0")
  })

  it("sem `versao`: o Stage1View do Estágio 1 chama getEstagio1(casoId, undefined)", async () => {
    renderWithI18n(<ExecutionWizard casoId="shadowray" />)

    await waitFor(() => expect(getEstagio1Mock).toHaveBeenCalledTimes(1))
    expect(getEstagio1Mock).toHaveBeenCalledWith("shadowray", undefined)
  })
})
