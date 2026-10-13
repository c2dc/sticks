import { beforeEach, describe, expect, it, vi } from "vitest"

// Mock the HTTP client so no real network is used; we only assert the URL the
// wrapper builds and hands to `api.get`.
vi.mock("@/lib/api", () => ({
  api: {
    get: vi.fn(() => Promise.resolve({})),
    post: vi.fn(() => Promise.resolve({})),
    put: vi.fn(() => Promise.resolve({})),
    delete: vi.fn(() => Promise.resolve({})),
  },
}))

import { api } from "@/lib/api"
import { getEstagio1 } from "@/lib/estagios"

const getMock = vi.mocked(api.get)

/**
 * Feature: entrada-estagio1-campanha-stix — wrapper `getEstagio1(caso, versao?)`.
 * Validates: Requirements 5.2, 6.2
 *
 * O wrapper monta a URL do Estágio 1 com `?versao=<encoded>` sse a versão da
 * base STIX selecionada for informada; sem versão, usa a rota crua (fallback
 * retrocompatível no backend). Valores são sempre percent-encoded.
 */
describe("getEstagio1 — montagem da URL (com/sem ?versao=)", () => {
  beforeEach(() => {
    getMock.mockClear()
  })

  it("sem versão: chama api.get com a rota crua (sem query string)", () => {
    void getEstagio1("campanha-x")
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith("/casos/campanha-x/estagio/1")
  })

  it("com versão: acrescenta ?versao=<valor>", () => {
    void getEstagio1("campanha-x", "18.0")
    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock).toHaveBeenCalledWith(
      "/casos/campanha-x/estagio/1?versao=18.0",
    )
  })

  it("percent-encoda o caso e a versão quando precisam de encoding", () => {
    void getEstagio1("caso com espaço", "17.1/rc 2")
    expect(getMock).toHaveBeenCalledWith(
      "/casos/caso%20com%20espa%C3%A7o/estagio/1?versao=17.1%2Frc%202",
    )
  })
})
