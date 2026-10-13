import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { Stage1View } from "@/components/Stage1View"
import type {
  CampanhaStixEntrada,
  Stage1Result,
  Stage1Saida,
} from "@/lib/estagios"

/**
 * Feature: entrada-estagio1-campanha-stix — propagação da versão (Task 6.2).
 * Validates: Requirements 3.1, 6.2
 *
 * O Stage1View recebe `{ casoId, versao? }` e, no efeito de montagem, chama
 * `getEstagio1(casoId, versao)` com a versão da base STIX selecionada no fluxo.
 * Quando a versão não é informada, chama sem o parâmetro (o backend aplica o
 * fallback retrocompatível `versao_ausente`). Mockamos `@/lib/estagios` para
 * asseverar exatamente os argumentos passados ao wrapper.
 */

// Mock the stage-detail wrapper so no real HTTP is used; we only assert the
// arguments the view hands to `getEstagio1`.
vi.mock("@/lib/estagios", () => ({
  getEstagio1: vi.fn(),
}))

import { getEstagio1 } from "@/lib/estagios"

const getEstagio1Mock = vi.mocked(getEstagio1)

function makeResult(): Stage1Result {
  return {
    caso_id: "shadowray",
    estado: "nao_iniciado",
    entrada: { caso_id: "shadowray", nome: "ShadowRay" },
    saida: null,
  }
}

describe("Stage1View — propagação da versão para getEstagio1 (Req. 3.1, 6.2)", () => {
  beforeEach(() => {
    getEstagio1Mock.mockReset()
    getEstagio1Mock.mockResolvedValue(makeResult())
  })

  it("com `versao`: chama getEstagio1(casoId, versao) com essa versão", async () => {
    renderWithI18n(<Stage1View casoId="shadowray" versao="18.0" />)

    await waitFor(() => expect(getEstagio1Mock).toHaveBeenCalledTimes(1))
    expect(getEstagio1Mock).toHaveBeenCalledWith("shadowray", "18.0")
  })

  it("sem `versao`: chama getEstagio1(casoId, undefined) (sem a versão)", async () => {
    renderWithI18n(<Stage1View casoId="shadowray" />)

    await waitFor(() => expect(getEstagio1Mock).toHaveBeenCalledTimes(1))
    expect(getEstagio1Mock).toHaveBeenCalledWith("shadowray", undefined)
  })
})
/**
 * Feature: entrada-estagio1-campanha-stix — render da Entrada (Task 7.3).
 * Validates: Requirements 2.3, 2.4, 4.2 (Property 1, 3)
 *
 * A Entrada do Estágio 1 passa a ser o recorte do objeto `campaign` da base
 * STIX selecionada (`entrada.campanha_stix`):
 * - encontrada → renderiza os campos ricos (nome, ID STIX, external_id,
 *   descrição, aliases, datas, versão da base);
 * - não encontrada → aviso `role="alert"` + identificador curado (nome ·
 *   external_id), SEM o bloco rico.
 *
 * Duas invariantes de produto são asseveradas aqui:
 * - Property 1: a extração estrutural (Saída) permanece intacta — a Entrada
 *   nunca quebra/afeta a Saída, independentemente do estado da campanha.
 * - Property 3: nunca aparece caminho/arquivo da Caldera (ex.
 *   `data/caldera_adversaries`, `*.yml`) nem a antiga "Referência STIX" como
 *   entrada, em nenhum dos estados.
 */

/** Fixture da campanha encontrada, com campos ricos preenchidos. */
function campanhaEncontrada(): CampanhaStixEntrada {
  return {
    encontrada: true,
    versao_base: "18.0",
    stix_id: "campaign--12345678-abcd-ef00-1234-567890abcdef",
    nome: "ShadowRay",
    external_id: "C0045",
    descricao: "Campanha que explora clusters Ray expostos na internet.",
    aliases: ["A", "B"],
    first_seen: "2023-09-05T00:00:00.000Z",
    last_seen: "2023-11-15T00:00:00.000Z",
    motivo_indisponivel: null,
  }
}

/**
 * Fixture da campanha NÃO encontrada: só o identificador curado (nome +
 * external_id) sobrevive; os campos ricos ficam ausentes (null).
 */
function campanhaNaoEncontrada(): CampanhaStixEntrada {
  return {
    encontrada: false,
    versao_base: null,
    stix_id: null,
    nome: "ShadowRay",
    external_id: "C0045",
    descricao: null,
    aliases: [],
    first_seen: null,
    last_seen: null,
    motivo_indisponivel: "campanha_nao_encontrada",
  }
}

/** Saída estrutural não-vazia, com uma técnica extraída. */
function saidaComTecnicas(): Stage1Saida {
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

/** Monta um `Stage1Result` completo para o cenário desejado. */
function makeResultComCampanha(
  campanha: CampanhaStixEntrada,
  saida: Stage1Saida | null = null,
): Stage1Result {
  return {
    caso_id: "shadowray",
    estado: saida ? "concluido" : "nao_iniciado",
    entrada: {
      caso_id: "shadowray",
      nome: "ShadowRay",
      campanha_stix: campanha,
    },
    saida,
  }
}

describe("Stage1View — render da Entrada campanha_stix (Req. 2.3, 2.4, 4.2)", () => {
  beforeEach(() => {
    getEstagio1Mock.mockReset()
  })

  it("encontrada: renderiza os campos ricos (nome, ID STIX, external_id, descrição, aliases, datas, versão)", async () => {
    getEstagio1Mock.mockResolvedValue(makeResultComCampanha(campanhaEncontrada()))

    renderWithI18n(<Stage1View casoId="shadowray" />)

    // O bloco rico está presente; o aviso de fallback não.
    const bloco = await screen.findByTestId("campanha-stix")
    expect(screen.queryByTestId("campanha-stix-nao-encontrada")).toBeNull()

    // Campos ricos renderizados.
    expect(bloco).toHaveTextContent("ShadowRay")
    expect(bloco).toHaveTextContent(
      "campaign--12345678-abcd-ef00-1234-567890abcdef",
    )
    expect(bloco).toHaveTextContent("C0045")
    expect(bloco).toHaveTextContent(
      "Campanha que explora clusters Ray expostos na internet.",
    )
    // Aliases como chips.
    expect(bloco).toHaveTextContent("A")
    expect(bloco).toHaveTextContent("B")
    // Datas e versão da base.
    expect(bloco).toHaveTextContent("2023-09-05T00:00:00.000Z")
    expect(bloco).toHaveTextContent("2023-11-15T00:00:00.000Z")
    expect(bloco).toHaveTextContent("18.0")
  })

  it("não encontrada: mostra o aviso (role=alert) + identificador curado e NÃO renderiza o bloco rico", async () => {
    getEstagio1Mock.mockResolvedValue(
      makeResultComCampanha(campanhaNaoEncontrada()),
    )

    renderWithI18n(<Stage1View casoId="shadowray" />)

    // Aviso de fallback com papel de alerta e o texto pt-BR da chave.
    const alerta = await screen.findByRole("alert")
    expect(alerta).toHaveTextContent("Campanha não encontrada nesta base STIX.")

    // O identificador curado (nome · external_id) permanece visível.
    expect(alerta).toHaveTextContent("ShadowRay")
    expect(alerta).toHaveTextContent("C0045")

    // O bloco de campos ricos NÃO está presente.
    expect(screen.queryByTestId("campanha-stix")).toBeNull()
    expect(screen.getByTestId("campanha-stix-nao-encontrada")).toBeInTheDocument()
  })

  it("Property 1: a Saída (extração estrutural) permanece intacta com campanha encontrada", async () => {
    getEstagio1Mock.mockResolvedValue(
      makeResultComCampanha(campanhaEncontrada(), saidaComTecnicas()),
    )

    const { container } = renderWithI18n(<Stage1View casoId="shadowray" />)

    await screen.findByTestId("campanha-stix")
    const saida = container.querySelector('[data-section="saida"]')
    expect(saida).not.toBeNull()
    // A técnica extraída aparece na Saída, independentemente da Entrada.
    expect(saida).toHaveTextContent("T1190")
    expect(saida).toHaveTextContent("Exploit Public-Facing Application")
  })

  it("Property 1: a Saída permanece intacta mesmo quando a campanha não é encontrada", async () => {
    getEstagio1Mock.mockResolvedValue(
      makeResultComCampanha(campanhaNaoEncontrada(), saidaComTecnicas()),
    )

    const { container } = renderWithI18n(<Stage1View casoId="shadowray" />)

    // Entrada caiu no fallback, mas a Saída estrutural continua renderizando.
    await screen.findByTestId("campanha-stix-nao-encontrada")
    const saida = container.querySelector('[data-section="saida"]')
    expect(saida).not.toBeNull()
    expect(saida).toHaveTextContent("T1190")
    expect(saida).toHaveTextContent("Exploit Public-Facing Application")
  })

  it("Property 3: nunca exibe caminho/arquivo Caldera nem 'Referência STIX' (encontrada e não encontrada)", async () => {
    // Estado encontrada.
    getEstagio1Mock.mockResolvedValue(
      makeResultComCampanha(campanhaEncontrada(), saidaComTecnicas()),
    )
    const encontrada = renderWithI18n(<Stage1View casoId="shadowray" />)
    await screen.findByTestId("campanha-stix")

    const textoEncontrada = encontrada.container.textContent ?? ""
    expect(textoEncontrada).not.toMatch(/data\/caldera_adversaries/)
    expect(textoEncontrada).not.toMatch(/\.yml\b/)
    // A antiga "Referência STIX" (stix_reference) não é mais renderizada.
    expect(textoEncontrada).not.toContain("Referência STIX")
    expect(encontrada.container.querySelector("input")).toBeNull()
    encontrada.unmount()

    // Estado não encontrada.
    getEstagio1Mock.mockResolvedValue(
      makeResultComCampanha(campanhaNaoEncontrada(), saidaComTecnicas()),
    )
    const naoEncontrada = renderWithI18n(<Stage1View casoId="shadowray" />)
    await screen.findByTestId("campanha-stix-nao-encontrada")

    const textoNaoEncontrada = naoEncontrada.container.textContent ?? ""
    expect(textoNaoEncontrada).not.toMatch(/data\/caldera_adversaries/)
    expect(textoNaoEncontrada).not.toMatch(/\.yml\b/)
    expect(textoNaoEncontrada).not.toContain("Referência STIX")
    expect(naoEncontrada.container.querySelector("input")).toBeNull()
  })
})
