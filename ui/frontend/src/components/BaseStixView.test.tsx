import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { BaseStixView } from "@/components/BaseStixView"
import type { ResultadoEstagio1 } from "@/lib/estagios"

/**
 * BaseStixView — Estágio 1 (Seleção da base STIX) (Task 14.1, Req. 4.1, 5.7,
 * 5.8, 5.10).
 *
 * Mockamos o veredito de conclusão (`getEstagioBaseStix`) e as duas telas
 * reusadas do fluxo fluxo-stix-dashboard (`StixBasesList`,
 * `StixIntelligenceDashboard`) por stubs mínimos, para focar na lógica própria
 * do BaseStixView: alternância lista↔dashboard conforme a `versao` e a
 * propagação do veredito de conclusão (`onConclusaoChange`) que habilita o
 * "Próximo" do wizard (tarefa 14.4).
 */

vi.mock("@/lib/estagios", () => ({
  getEstagioBaseStix: vi.fn(),
}))

vi.mock("@/components/StixBasesList", () => ({
  StixBasesList: ({
    onSelecionarBase,
  }: {
    onSelecionarBase: (versao: string) => void
  }) => (
    <button type="button" onClick={() => onSelecionarBase("18.0")}>
      stub-selecionar-18
    </button>
  ),
}))

vi.mock("@/components/StixIntelligenceDashboard", () => ({
  StixIntelligenceDashboard: ({ versao }: { versao: string }) => (
    <div data-testid="dashboard-stub">dashboard-{versao}</div>
  ),
}))

import { getEstagioBaseStix } from "@/lib/estagios"

const getEstagioBaseStixMock = vi.mocked(getEstagioBaseStix)

function makeResultado(concluido: boolean): ResultadoEstagio1 {
  return {
    versao: "18.0",
    concluido,
    bundle_em_disco: concluido,
    dashboard: concluido ? { versao: "18.0", nome: "v18.0", contagens: [] } : null,
    campanhas: [],
    curada: true,
    mensagem_erro: concluido ? null : undefined,
  }
}

describe("BaseStixView (Estágio 1 — Seleção da base STIX)", () => {
  beforeEach(() => {
    getEstagioBaseStixMock.mockReset()
  })

  it("lists bases when no version is selected and reports not-concluded", async () => {
    const onConclusaoChange = vi.fn()
    const onVersaoSelecionada = vi.fn()

    renderWithI18n(
      <BaseStixView
        casoId="shadowray"
        onConclusaoChange={onConclusaoChange}
        onVersaoSelecionada={onVersaoSelecionada}
      />,
    )

    // Sem versão: a lista de bases é exibida, o dashboard não (Req. 5.1–5.4).
    expect(screen.getByText("stub-selecionar-18")).toBeInTheDocument()
    expect(screen.queryByTestId("dashboard-stub")).toBeNull()

    // Sem base selecionada o Estágio 1 não pode ser concluído (Req. 5.10).
    await waitFor(() => {
      expect(onConclusaoChange).toHaveBeenCalledWith(false)
    })
    // E não resolve o veredito do backend sem versão.
    expect(getEstagioBaseStixMock).not.toHaveBeenCalled()

    // Selecionar uma base propaga a versão escolhida para o wizard (Req. 5.1).
    await userEvent.setup().click(screen.getByText("stub-selecionar-18"))
    expect(onVersaoSelecionada).toHaveBeenCalledWith("18.0")
  })

  it("shows the bundle dashboard and enables conclusion when the bundle is on disk (Req. 5.7, 5.8, 5.10)", async () => {
    getEstagioBaseStixMock.mockResolvedValue(makeResultado(true))
    const onConclusaoChange = vi.fn()

    renderWithI18n(
      <BaseStixView
        casoId="shadowray"
        versao="18.0"
        onConclusaoChange={onConclusaoChange}
      />,
    )

    // Com versão: o dashboard do bundle é exibido, a lista não.
    expect(await screen.findByTestId("dashboard-stub")).toHaveTextContent(
      "dashboard-18.0",
    )
    expect(screen.queryByText("stub-selecionar-18")).toBeNull()
    expect(getEstagioBaseStixMock).toHaveBeenCalledWith("shadowray", "18.0")

    // Bundle em disco + dashboard exibido → conclusão habilitada (Req. 5.8, 5.10).
    await waitFor(() => {
      expect(onConclusaoChange).toHaveBeenLastCalledWith(true)
    })
    const banner = await screen.findByText(
      "Base em disco e dashboard exibido — o Estágio 1 pode ser concluído.",
    )
    expect(banner).toBeInTheDocument()
  })

  it("keeps Stage 1 not-concluded and reports the cause on a load failure (Req. 5.9)", async () => {
    getEstagioBaseStixMock.mockRejectedValue(new Error("boom"))
    const onConclusaoChange = vi.fn()

    renderWithI18n(
      <BaseStixView
        casoId="shadowray"
        versao="18.0"
        onConclusaoChange={onConclusaoChange}
      />,
    )

    // A causa da falha é mostrada e o Estágio permanece não concluído.
    await screen.findByText("Não foi possível concluir a Seleção da base STIX.")
    await waitFor(() => {
      expect(onConclusaoChange).toHaveBeenLastCalledWith(false)
    })
  })
})
