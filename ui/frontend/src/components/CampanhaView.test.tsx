import { beforeEach, describe, expect, it, vi } from "vitest"
import { fireEvent, screen, waitFor } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { CampanhaView } from "@/components/CampanhaView"
import type {
  CampanhaListada,
  Estagio2Response,
  ResultadoSelecaoCampanha,
} from "@/lib/estagios"

/**
 * CampanhaView — Estágio 2 (Seleção da campanha) do pipeline de seis Estágios.
 * Validates: Requirements 4.1, 6.2, 6.3, 6.5
 *
 * Mockamos `@/lib/estagios` para controlar `getEstagioCampanha` (lista + seleção
 * read-only) e `selecionarCampanha` (extração dos atributos). Asserimos:
 * - a lista das 8 campanhas curadas selecionáveis + não selecionáveis
 *   desabilitadas (Req. 6.2, 6.5);
 * - a propagação da versão aos wrappers (Req. 4.1);
 * - a seleção que extrai e exibe os atributos (identidade + técnicas) e conclui
 *   o Estágio quando `concluido` (Req. 6.3, 6.5);
 * - a falha de extração surge como `concluido === false` com a causa (Req. 6.7).
 */

vi.mock("@/lib/estagios", () => ({
  getEstagioCampanha: vi.fn(),
  selecionarCampanha: vi.fn(),
}))

import { getEstagioCampanha, selecionarCampanha } from "@/lib/estagios"

const getEstagioCampanhaMock = vi.mocked(getEstagioCampanha)
const selecionarCampanhaMock = vi.mocked(selecionarCampanha)

function campanha(
  nome: string,
  slug: string | null,
  selecionavel: boolean,
): CampanhaListada {
  return {
    stix_id: `campaign--${nome}`,
    nome,
    descricao: `Descrição de ${nome}`,
    curada: selecionavel,
    slug_curado: slug,
    selecionavel,
  }
}

/** Oito campanhas curadas selecionáveis + uma não selecionável (versão 18). */
function listaComOitoCuradas(): CampanhaListada[] {
  const curadas = Array.from({ length: 8 }, (_, i) =>
    campanha(`Curada${i + 1}`, `curada-${i + 1}`, true),
  )
  const naoCurada = campanha("Externa", null, false)
  return [...curadas, naoCurada]
}

function selecaoVazia(): ResultadoSelecaoCampanha {
  return {
    caso_id: "shadowray",
    versao: "18.0",
    concluido: false,
    identidade: null,
    tecnicas: [],
  }
}

function resposta(
  campanhas: CampanhaListada[],
  selecao: ResultadoSelecaoCampanha = selecaoVazia(),
): Estagio2Response {
  return {
    caso_id: "shadowray",
    versao: "18.0",
    campanhas,
    selecao,
  }
}

function selecaoConcluida(): ResultadoSelecaoCampanha {
  return {
    caso_id: "shadowray",
    versao: "18.0",
    concluido: true,
    identidade: {
      encontrada: true,
      versao_base: "18.0",
      stix_id: "campaign--abc",
      nome: "ShadowRay",
      external_id: "C0045",
      descricao: "Campanha que explora clusters Ray expostos.",
      aliases: ["RayHunter"],
      first_seen: null,
      last_seen: null,
      motivo_indisponivel: null,
    },
    tecnicas: [
      {
        technique_id: "T1190",
        technique_name: "Exploit Public-Facing Application",
        tactic: "initial-access",
      },
    ],
  }
}

describe("CampanhaView — Estágio 2 (Req. 4.1, 6.2, 6.3, 6.5)", () => {
  beforeEach(() => {
    getEstagioCampanhaMock.mockReset()
    selecionarCampanhaMock.mockReset()
  })

  it("lista as 8 campanhas curadas selecionáveis e desabilita as não selecionáveis", async () => {
    getEstagioCampanhaMock.mockResolvedValue(resposta(listaComOitoCuradas()))

    renderWithI18n(<CampanhaView casoId="shadowray" versao="18.0" />)

    // As 9 campanhas aparecem; as 8 curadas têm botão habilitado.
    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    expect(curada1).not.toBeDisabled()

    const curadas = screen.getAllByRole("button", { name: /Curada\d/ })
    expect(curadas).toHaveLength(8)
    curadas.forEach((btn) => expect(btn).not.toBeDisabled())

    // A campanha não curada aparece porém desabilitada.
    const externa = screen.getByRole("button", { name: /Externa/ })
    expect(externa).toBeDisabled()
  })

  it("propaga a versão para getEstagioCampanha", async () => {
    getEstagioCampanhaMock.mockResolvedValue(resposta(listaComOitoCuradas()))

    renderWithI18n(<CampanhaView casoId="shadowray" versao="18.0" />)

    await waitFor(() =>
      expect(getEstagioCampanhaMock).toHaveBeenCalledWith("shadowray", "18.0"),
    )
  })

  it("selecionar uma campanha extrai e exibe os atributos e conclui o Estágio", async () => {
    getEstagioCampanhaMock.mockResolvedValue(resposta(listaComOitoCuradas()))
    selecionarCampanhaMock.mockResolvedValue(selecaoConcluida())
    const onConcluido = vi.fn()

    renderWithI18n(
      <CampanhaView casoId="shadowray" versao="18.0" onConcluido={onConcluido} />,
    )

    // Escolhe a primeira campanha curada e confirma a seleção.
    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    fireEvent.click(curada1)
    const confirmar = screen.getByRole("button", { name: "Selecionar campanha" })
    fireEvent.click(confirmar)

    // O wrapper recebe o slug curado + a versão.
    await waitFor(() =>
      expect(selecionarCampanhaMock).toHaveBeenCalledWith(
        "shadowray",
        "curada-1",
        "18.0",
      ),
    )

    // Os atributos extraídos são exibidos (identidade reusando o bloco do
    // Stage1View + técnicas), e o indicador de conclusão aparece.
    await screen.findByTestId("campanha-concluida")
    const atributos = screen
      .getByTestId("campanha-concluida")
      .closest('[data-section="atributos-extraidos"]') as HTMLElement
    expect(atributos).not.toBeNull()
    expect(atributos).toHaveTextContent("ShadowRay")
    expect(atributos).toHaveTextContent("C0045")
    expect(atributos).toHaveTextContent("T1190")
    expect(atributos).toHaveTextContent("Exploit Public-Facing Application")

    expect(onConcluido).toHaveBeenCalledTimes(1)
    // onConcluido now reports the chosen curated slug plus the typed result so
    // the wizard can adopt the slug as the campaignId for Stages 3..6.
    expect(onConcluido).toHaveBeenCalledWith("curada-1", selecaoConcluida())
  })

  it("falha de extração surge como concluido=false com a causa (sem concluir)", async () => {
    getEstagioCampanhaMock.mockResolvedValue(resposta(listaComOitoCuradas()))
    selecionarCampanhaMock.mockResolvedValue({
      caso_id: "shadowray",
      versao: "18.0",
      concluido: false,
      identidade: null,
      tecnicas: [],
      mensagem_erro: "Artefato curado ausente.",
      etapa_falha: "extracao_atributos",
    })
    const onConcluido = vi.fn()

    renderWithI18n(
      <CampanhaView casoId="shadowray" versao="18.0" onConcluido={onConcluido} />,
    )

    const curada1 = await screen.findByRole("button", { name: /Curada1/ })
    fireEvent.click(curada1)
    fireEvent.click(screen.getByRole("button", { name: "Selecionar campanha" }))

    const alerta = await screen.findByTestId("campanha-falha")
    expect(alerta).toHaveTextContent("Artefato curado ausente.")
    expect(screen.queryByTestId("campanha-concluida")).toBeNull()
    // Extração falhou → o Estágio não concluiu.
    expect(onConcluido).not.toHaveBeenCalled()
  })

  it("exibe a nota de 'nenhuma campanha' quando o bundle não tem campanhas", async () => {
    getEstagioCampanhaMock.mockResolvedValue(resposta([]))

    renderWithI18n(<CampanhaView casoId="shadowray" versao="18.0" />)

    expect(
      await screen.findByText("Nenhuma campanha disponível neste bundle."),
    ).toBeInTheDocument()
  })
})
