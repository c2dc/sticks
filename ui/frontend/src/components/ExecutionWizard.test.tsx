import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import type { Caso, CasoEstados } from "@/lib/casos"

/**
 * UI tests for the five-stage ExecutionWizard (pipeline-cinco-estagios,
 * Req. 1.x/3.x/8.9). The wizard reads the backend as the source of truth, so
 * we mock `@/lib/wizard` (network) and stub the stage body components to keep
 * the assertions focused on the wizard's own structure and gating:
 * - the step indicator ("Passo X de 5", total:5);
 * - the badge track with five [data-stage] entries (1..5);
 * - the Estágio-1 body being Stage1View (Modelagem);
 * - the body mapping across all five stages
 *   (1=Stage1View, 2=DagStageView, 3=Stage2View, 4=EnvironmentStageView,
 *   5=Stage3View);
 * - sequential blocking (Avançar disabled while the current stage isn't done);
 * - no "Avançar" on the last stage (Estágio 5).
 */

// Mock the wizard network wrappers so no real HTTP is used.
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

// Stub the stage body components so their own effects / Mermaid bundle never
// load here; each stub advertises which stage body rendered.
vi.mock("@/components/Stage1View", () => ({
  Stage1View: ({ casoId }: { casoId: string }) => (
    <div data-testid="stage1-view">{casoId}</div>
  ),
}))
vi.mock("@/components/DagStageView", () => ({
  DagStageView: ({ casoId }: { casoId: string }) => (
    <div data-testid="dag-stage-view">{casoId}</div>
  ),
}))
vi.mock("@/components/Stage2View", () => ({
  Stage2View: ({ casoId }: { casoId: string }) => (
    <div data-testid="stage2-view">{casoId}</div>
  ),
}))
vi.mock("@/components/EnvironmentStageView", () => ({
  EnvironmentStageView: ({ casoId }: { casoId: string }) => (
    <div data-testid="environment-stage-view">{casoId}</div>
  ),
}))
vi.mock("@/components/Stage3View", () => ({
  Stage3View: ({ casoId }: { casoId: string }) => (
    <div data-testid="stage3-view">{casoId}</div>
  ),
}))

import {
  getCaso,
  concluirTraducao,
  executarModelagem,
  resetCaso,
} from "@/lib/wizard"

const getCasoMock = vi.mocked(getCaso)
const concluirTraducaoMock = vi.mocked(concluirTraducao)
const executarModelagemMock = vi.mocked(executarModelagem)
const resetCasoMock = vi.mocked(resetCaso)

function estados(concluidos: number[]): CasoEstados {
  const todos = [1, 2, 3, 4, 5] as const
  return {
    concluido_total: concluidos.length === 5,
    estagios: todos.map((estagio) => ({
      estagio,
      estado: concluidos.includes(estagio) ? "concluido" : "nao_iniciado",
      progresso: concluidos.includes(estagio) ? 100 : 0,
      // Blocked when the previous stage is not concluded (stage 1 never blocked).
      bloqueado: estagio > 1 && !concluidos.includes(estagio - 1),
    })),
  }
}

function makeCaso(concluidos: number[]): Caso {
  return { id: "shadowray", nome: "ShadowRay", estados: estados(concluidos) }
}

describe("ExecutionWizard (5 Estágios — Req. 1.x/3.x/8.9)", () => {
  beforeEach(() => {
    getCasoMock.mockReset()
    concluirTraducaoMock.mockReset()
    executarModelagemMock.mockReset()
    resetCasoMock.mockReset()
  })

  it("shows the step indicator 'Passo 1 de 5' and a five-badge track (1..5)", async () => {
    getCasoMock.mockResolvedValue(makeCaso([]))

    const { container } = renderWithI18n(<ExecutionWizard casoId="shadowray" />)

    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    // Step indicator reflects the current stage out of a total of five.
    expect(screen.getByText("Passo 1 de 5")).toBeInTheDocument()

    // The badge track has exactly five [data-stage] entries, numbered 1..5.
    const badges = Array.from(
      container.querySelectorAll("[data-stage]"),
    ) as HTMLElement[]
    expect(badges).toHaveLength(5)
    expect(badges.map((b) => b.getAttribute("data-stage"))).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
    ])
  })

  it("renders Stage1View (Modelagem) as the body of Estágio 1", async () => {
    getCasoMock.mockResolvedValue(makeCaso([]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)

    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    // Estágio 1 body is the Modelagem view; the other stage bodies are absent.
    expect(await screen.findByTestId("stage1-view")).toBeInTheDocument()
    expect(screen.queryByTestId("dag-stage-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("stage2-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("environment-stage-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("stage3-view")).not.toBeInTheDocument()
  })

  it("blocks 'Avançar' at Estágio 1 while the modeling has not run (stage not concluded)", async () => {
    getCasoMock.mockResolvedValue(makeCaso([]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)

    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(avancar).toBeDisabled()
    expect(avancar).toHaveAttribute("aria-disabled", "true")

    // Clicking the blocked control does not move the wizard forward.
    await userEvent.click(avancar)
    expect(screen.getByText("Passo 1 de 5")).toBeInTheDocument()
    expect(screen.getByTestId("stage1-view")).toBeInTheDocument()
  })

  it("enables 'Avançar' once Estágio 1 is concluded and advances to Estágio 2 (Geração do DAG)", async () => {
    // Backend reports Estágio 1 concluded (modeling ran).
    getCasoMock.mockResolvedValue(makeCaso([1]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)

    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await waitFor(() => expect(avancar).not.toBeDisabled())

    await userEvent.click(avancar)

    // Now on Estágio 2: step indicator updates and the DAG body shows.
    expect(await screen.findByText("Passo 2 de 5")).toBeInTheDocument()
    expect(screen.getByTestId("dag-stage-view")).toBeInTheDocument()
    expect(screen.queryByTestId("stage1-view")).not.toBeInTheDocument()
  })

  it("maps each stage number to its body component across the five stages", async () => {
    // All stages concluded so the wizard allows walking forward 1→2→3→4→5.
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4, 5]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)

    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Estágio 1 — Stage1View (Modelagem).
    expect(await screen.findByTestId("stage1-view")).toBeInTheDocument()

    // Estágio 2 — DagStageView (Geração do DAG).
    await userEvent.click(avancar())
    expect(await screen.findByTestId("dag-stage-view")).toBeInTheDocument()

    // Estágio 3 — Stage2View (Operacionalização).
    await userEvent.click(avancar())
    expect(await screen.findByTestId("stage2-view")).toBeInTheDocument()

    // Estágio 4 — EnvironmentStageView (Provisionamento do Ambiente).
    await userEvent.click(avancar())
    expect(await screen.findByTestId("environment-stage-view")).toBeInTheDocument()

    // Estágio 5 — Stage3View (Execução); no "Avançar" button on the last stage.
    await userEvent.click(avancar())
    expect(await screen.findByTestId("stage3-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 5 de 5")).toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: /Avançar para o próximo estágio/ }),
    ).not.toBeInTheDocument()
  })

  it("enables 'Próximo' on Estágio 3 when it is available but NOT yet concluded, and clicking concludes it", async () => {
    // Estágios 1 and 2 are concluded; Estágio 3 (Operacionalização) is thus
    // AVAILABLE but not concluded. Clicking "Próximo" must be enabled and POST
    // /estagio/3/concluir (concluirTraducao), then advance — this is the curated
    // "avançar = concluir" case that must not dead-end.
    getCasoMock.mockResolvedValue(makeCaso([1, 2]))
    // After concluding, the backend reports stage 3 concluded too.
    concluirTraducaoMock.mockImplementation(async () => {
      getCasoMock.mockResolvedValue(makeCaso([1, 2, 3]))
      return makeCaso([1, 2, 3]).estados as never
    })

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Walk to Estágio 3 (1→2→3), each hop enabled by the concluded predecessor.
    await userEvent.click(avancar()) // 1 → 2
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("stage2-view")
    expect(screen.getByText("Passo 3 de 5")).toBeInTheDocument()

    // The key assertion: on Estágio 3 (available, not concluded) the button is
    // ENABLED (not a dead end).
    const botao3 = avancar()
    expect(botao3).not.toBeDisabled()

    // Clicking concludes the Operacionalização and advances to Estágio 4.
    await userEvent.click(botao3)
    await waitFor(() => expect(concluirTraducaoMock).toHaveBeenCalledWith("shadowray"))
    expect(await screen.findByTestId("environment-stage-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 4 de 5")).toBeInTheDocument()
  })

  it("shows the pre-concluído note when opening a curated (pre-completed) Estágio 2 (R2.2)", async () => {
    // Curated case: the backend reports Estágios 1 and 2 as already concluded
    // (Estágio 2 auto-concluded by curation). On Estágio 1 the note is NOT
    // shown (estagioAtual=1 is not preConcluido). After clicking Avançar to
    // reach Estágio 2 we have maxEstagioVisitado=2, estagioAtual=2, and
    // concluidos includes both 1 and 2 → Estágio 2 is preConcluido and the
    // explanatory note must be visible.
    getCasoMock.mockResolvedValue(makeCaso([1, 2]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    // On Estágio 1 the note is absent.
    await screen.findByTestId("stage1-view")
    expect(screen.queryByTestId("pre-concluido-nota")).not.toBeInTheDocument()

    // Advancing from Estágio 1 is enabled because Estágio 1 is concluded.
    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await waitFor(() => expect(avancar).not.toBeDisabled())
    await userEvent.click(avancar)

    // Now on Estágio 2 (pre-completed by curation): the note appears with the
    // pt-BR copy.
    expect(await screen.findByTestId("dag-stage-view")).toBeInTheDocument()
    const nota = await screen.findByTestId("pre-concluido-nota")
    expect(nota).toBeInTheDocument()
    expect(nota).toHaveTextContent(
      "Este estágio já estava pronto por curadoria: o resultado existia antes de você agir.",
    )
  })

  it("does NOT show the pre-concluído note for a non-curated Estágio 2 (R4.3)", async () => {
    // Non-curated case: only Estágio 1 is concluded in the real state; Estágio 2
    // is not concluded. Advancing from Estágio 1 is still allowed (predecessor
    // concluded), but Estágio 2 is never preConcluido because it is not
    // concluded in the backend — so the explanatory note must be absent.
    getCasoMock.mockResolvedValue(makeCaso([1]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    await screen.findByTestId("stage1-view")
    expect(screen.queryByTestId("pre-concluido-nota")).not.toBeInTheDocument()

    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await waitFor(() => expect(avancar).not.toBeDisabled())
    await userEvent.click(avancar)

    // On Estágio 2, with stage 2 not concluded in the real state, the note is
    // not rendered.
    expect(await screen.findByTestId("dag-stage-view")).toBeInTheDocument()
    expect(screen.getByText("Passo 2 de 5")).toBeInTheDocument()
    expect(screen.queryByTestId("pre-concluido-nota")).not.toBeInTheDocument()
  })

  it("keeps 'Avançar' ENABLED at Estágio 1 even though Estágio 2 is only presented as 'disponivel' — the gate is governed by the real state (R3.4)", async () => {
    // Curated case: the backend reports Estágios 1 AND 2 as concluded in the
    // real state. On first load estagioAtual=1 and maxEstagioVisitado=1, so the
    // presentation reveals Estágio 2 gradually: since 2 > maxEstagioVisitado=1,
    // Estágio 2 shows as "disponivel" (its real conclusion is hidden until the
    // Pesquisador reaches it). Despite that presentation, the advance GATE is
    // governed by the REAL state (derivarEstados): Estágio 1 is concluded in
    // {1,2}, so "Avançar" must be ENABLED. This proves maxEstagioVisitado (the
    // presentation layer) does not alter the gate.
    getCasoMock.mockResolvedValue(makeCaso([1, 2]))

    const { container } = renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    // The wizard is on Estágio 1 (no navigation performed).
    await screen.findByTestId("stage1-view")
    expect(screen.getByText("Passo 1 de 5")).toBeInTheDocument()

    // Presentation differs from the gate: with maxEstagioVisitado=1, Estágio 2
    // is only revealed as "disponivel" (not yet shown as concluded), even
    // though it is concluded in the real state.
    const stage2 = container.querySelector('[data-stage="2"]') as HTMLElement
    expect(stage2).not.toBeNull()
    expect(stage2.getAttribute("data-apresentado")).toBe("disponivel")

    // The gate is governed by the real state: Estágio 1 is concluded in {1,2},
    // so the advance button is ENABLED regardless of the presentation.
    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(avancar).not.toBeDisabled()
    expect(avancar).toHaveAttribute("aria-disabled", "false")
  })

  it("renders the next button's visible label as 'Próximo'", async () => {
    getCasoMock.mockResolvedValue(makeCaso([1]))
    renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    // The accessible name stays descriptive (aria-label), but the VISIBLE text
    // is just "Próximo".
    const botao = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    expect(botao).toHaveTextContent("Próximo")
  })

  it("recomputes the badge track when the re-read of the backend changes 'concluidos' (R6.1/3.1)", async () => {
    // Start from a backend that reports NOTHING concluded: Estágio 1 is not
    // concluded, so the "Executar modelagem" action is available and the badge
    // track shows Estágio 1 as not-yet-concluded.
    getCasoMock.mockResolvedValue(makeCaso([]))
    // Running the modeling action makes the backend report Estágio 1 AND 2 as
    // concluded on the follow-up re-read (handleRunModelagem calls
    // executarModelagem then getCaso again). The Estado_Apresentado is derived
    // fresh from the re-read 'concluidos' on every render, so the trail must
    // recompute: with maxEstagioVisitado=1, Estágio 1 flips to "concluido"
    // (1 <= 1) while the just-auto-concluded Estágio 2 is only revealed as
    // "disponivel" (2 > maxEstagioVisitado).
    executarModelagemMock.mockImplementation(async () => {
      getCasoMock.mockResolvedValue(makeCaso([1, 2]))
      return makeCaso([1, 2]).estados as never
    })

    const { container } = renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    // Before the re-read: Estágio 1 is not concluded in the real state, so the
    // badge is presented as "disponivel".
    await screen.findByTestId("stage1-view")
    const stage1Before = container.querySelector(
      '[data-stage="1"]',
    ) as HTMLElement
    expect(stage1Before.getAttribute("data-apresentado")).toBe("disponivel")

    // Trigger the re-read via the Estágio 1 modeling action.
    const executar = screen.getByRole("button", { name: /Executar modelagem/ })
    await userEvent.click(executar)

    await waitFor(() =>
      expect(executarModelagemMock).toHaveBeenCalledWith("shadowray", undefined),
    )

    // After the re-read the trail recomputes from the new 'concluidos': Estágio
    // 1 is now presented as "concluido" (reached and concluded) while Estágio 2
    // (auto-concluded) is still only "disponivel" until the Pesquisador reaches
    // it — the gradual reveal governed by maxEstagioVisitado.
    await waitFor(() => {
      const stage1After = container.querySelector(
        '[data-stage="1"]',
      ) as HTMLElement
      expect(stage1After.getAttribute("data-apresentado")).toBe("concluido")
    })
    const stage2After = container.querySelector(
      '[data-stage="2"]',
    ) as HTMLElement
    expect(stage2After.getAttribute("data-apresentado")).toBe("disponivel")
    // The gate's data-status (source of truth) also reflects the re-read:
    // Estágio 2 is "concluido" in the real state even though its presentation
    // is still "disponivel".
    expect(stage2After.getAttribute("data-status")).toBe("concluido")
  })

  it("issues NO mutation POST as a side effect of marking a stage preConcluido — presentation is derived, never persisted (R2.3)", async () => {
    // Curated case where Estágio 2 becomes preConcluido once reached. Navigate
    // Estágio 1 → 2 via "Avançar" (a 1→2 hop does NOT POST; only the 3→4 hop
    // concludes the Operacionalização). Marking Estágio 2 as preConcluido is a
    // purely derived presentation effect, so no mutation endpoint must fire.
    getCasoMock.mockResolvedValue(makeCaso([1, 2]))

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    await screen.findByTestId("stage1-view")
    const avancar = screen.getByRole("button", {
      name: /Avançar para o próximo estágio/,
    })
    await waitFor(() => expect(avancar).not.toBeDisabled())
    await userEvent.click(avancar)

    // On Estágio 2, pre-completed by curation, the explanatory note is shown —
    // this is the derived preConcluido presentation.
    expect(await screen.findByTestId("dag-stage-view")).toBeInTheDocument()
    expect(await screen.findByTestId("pre-concluido-nota")).toBeInTheDocument()

    // Crucially, deriving/showing preConcluido persisted NOTHING: no mutation
    // endpoint was called. concluirTraducao (POST /estagio/3/concluir) and
    // executarModelagem (POST /estagio/1/executar) must never fire as a side
    // effect of the presentation.
    expect(concluirTraducaoMock).not.toHaveBeenCalled()
    expect(executarModelagemMock).not.toHaveBeenCalled()
  })

  it("resets the trail to Estágio 1 and recomputes when the casoId changes (R6.2)", async () => {
    // Fully concluded case so the Pesquisador can walk forward through the
    // stages. We advance to Estágio 3 (estagioAtual=3, maxEstagioVisitado=3),
    // then switch to a different casoId. The useEffect keyed on casoId must
    // reset estagioAtual=1 AND maxEstagioVisitado=1 and re-read the backend for
    // the NEW case (recompute of the Estado_Apresentado from the new state).
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4, 5]))

    const { rerender } = renderWithI18n(
      <ExecutionWizard casoId="shadowray" />,
    )
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Walk 1 → 2 → 3 so estagioAtual > 1 and maxEstagioVisitado > 1.
    await screen.findByTestId("stage1-view")
    await userEvent.click(avancar()) // 1 → 2
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("stage2-view")
    expect(screen.getByText("Passo 3 de 5")).toBeInTheDocument()

    // Switch the selected case. The casoId-keyed effect resets the trail.
    rerender(<ExecutionWizard casoId="other-case" />)

    // The re-read fires for the NEW casoId — this is the recompute from the new
    // Estado_Real (R6.2).
    await waitFor(() =>
      expect(getCasoMock).toHaveBeenCalledWith("other-case"),
    )

    // The wizard is back on Estágio 1 with Stage1View as the body.
    expect(await screen.findByText("Passo 1 de 5")).toBeInTheDocument()
    expect(await screen.findByTestId("stage1-view")).toBeInTheDocument()
    expect(screen.queryByTestId("stage2-view")).not.toBeInTheDocument()
    expect(screen.queryByTestId("dag-stage-view")).not.toBeInTheDocument()
  })

  it("resets the trail to Estágio 1 and recomputes from the re-read when the campaign is restarted (R6.3)", async () => {
    // Fully concluded case so the Pesquisador can walk forward and reach a
    // later stage, then press "Reiniciar". handleRestart POSTs resetCaso with
    // the casoId, resets estagioAtual=1 and maxEstagioVisitado=1, and re-reads
    // the backend — which now reports NOTHING concluded, so the trail
    // recomputes from the fresh (empty) state.
    getCasoMock.mockResolvedValue(makeCaso([1, 2, 3, 4, 5]))
    resetCasoMock.mockImplementation(async () => {
      // After the reset the backend reports a fresh campaign (nothing done).
      getCasoMock.mockResolvedValue(makeCaso([]))
      return undefined as never
    })

    renderWithI18n(<ExecutionWizard casoId="shadowray" />)
    await waitFor(() => expect(getCasoMock).toHaveBeenCalledWith("shadowray"))

    const avancar = () =>
      screen.getByRole("button", { name: /Avançar para o próximo estágio/ })

    // Walk 1 → 2 → 3 so the wizard is on a later stage before restarting.
    await screen.findByTestId("stage1-view")
    await userEvent.click(avancar()) // 1 → 2
    await screen.findByTestId("dag-stage-view")
    await userEvent.click(avancar()) // 2 → 3
    await screen.findByTestId("stage2-view")
    expect(screen.getByText("Passo 3 de 5")).toBeInTheDocument()

    // Press "Reiniciar" (pt-BR label).
    const reiniciar = screen.getByRole("button", { name: /Reiniciar/ })
    await userEvent.click(reiniciar)

    // The restart POSTs resetCaso with the casoId.
    await waitFor(() =>
      expect(resetCasoMock).toHaveBeenCalledWith("shadowray"),
    )

    // The wizard returns to Estágio 1 with Stage1View and recomputes the trail
    // from the re-read: with the backend now reporting nothing concluded,
    // Estágio 1 is presented as "disponivel" (not concluded).
    expect(await screen.findByText("Passo 1 de 5")).toBeInTheDocument()
    expect(await screen.findByTestId("stage1-view")).toBeInTheDocument()
    expect(screen.queryByTestId("stage2-view")).not.toBeInTheDocument()
  })
})
