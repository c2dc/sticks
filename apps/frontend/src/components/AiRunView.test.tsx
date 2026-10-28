import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { AiRunView } from "@/components/AiRunView"
import type { AiEvaluation, AiIteration, AiRun, AiRunDetail } from "@/lib/ai"

// Mock the AI run wrappers so no network is used and the test fully controls the
// run summary + the detail the view polls (startAiRun / getAiRun). The view
// imports both from `@/lib/ai`, so mocking this module is enough.
vi.mock("@/lib/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ai")>()
  return {
    ...actual,
    startAiRun: vi.fn(),
    getAiRun: vi.fn(),
  }
})

import { getAiRun, startAiRun } from "@/lib/ai"

const startMock = vi.mocked(startAiRun)
const getMock = vi.mocked(getAiRun)

const CAMPAIGN = "shadowray"

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function makeRun(overrides: Partial<AiRun> = {}): AiRun {
  return {
    id: 1,
    campaign_id: CAMPAIGN,
    status: "em_execucao",
    thread_id: "thread-1",
    max_iterations: 3,
    iterations_used: 0,
    final_verdict_success: null,
    ...overrides,
  }
}

function makeEvaluation(overrides: Partial<AiEvaluation> = {}): AiEvaluation {
  return {
    id: 10,
    report_id: 5,
    success_rate: 0.8,
    procedural_coverage: 0.6,
    verdict_success: true,
    decision: "finalizar",
    rationale: "Cobertura procedural suficiente.",
    ...overrides,
  }
}

function makeIteration(overrides: Partial<AiIteration> = {}): AiIteration {
  return {
    id: 100,
    index: 0,
    refined_stage: null,
    notes: null,
    created_at: "2024-01-02T03:04:05",
    evaluation: null,
    ...overrides,
  }
}

function makeDetail(overrides: Partial<AiRunDetail> = {}): AiRunDetail {
  return {
    run: makeRun(),
    iterations: [],
    ...overrides,
  }
}

beforeEach(() => {
  startMock.mockReset()
  getMock.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// Task 17.3 / Req. 13.3, 13.4 — the view starts a run and renders the iterations
// list and the evaluator verdict (and surfaces new iterations/verdict on poll).
// ---------------------------------------------------------------------------

describe("AiRunView — starts a run and renders iterations/verdict (Req. 13.3, 13.4)", () => {
  it("shows the neutral empty state before any run is started", () => {
    renderWithI18n(<AiRunView campaignId={CAMPAIGN} />)

    // pt-BR source-of-truth string: no run started yet.
    expect(screen.getByText("Nenhuma execução de IA iniciada ainda.")).toBeInTheDocument()
    // The start control is present and no poll fired before the click.
    expect(screen.getByRole("button", { name: "Iniciar execução de IA" })).toBeInTheDocument()
    expect(startMock).not.toHaveBeenCalled()
    expect(getMock).not.toHaveBeenCalled()
  })

  it("starts a run on click and renders a terminal run's iterations and evaluator verdict", async () => {
    const run = makeRun({ status: "concluido", iterations_used: 1, final_verdict_success: true })
    startMock.mockResolvedValue(run)
    // A single terminal read returns one iteration carrying the evaluator verdict.
    getMock.mockResolvedValue(
      makeDetail({
        run,
        iterations: [makeIteration({ evaluation: makeEvaluation() })],
      }),
    )

    const { container } = renderWithI18n(<AiRunView campaignId={CAMPAIGN} />)

    await userEvent.click(screen.getByRole("button", { name: "Iniciar execução de IA" }))

    await waitFor(() => expect(startMock).toHaveBeenCalledWith(CAMPAIGN))
    await waitFor(() => expect(getMock).toHaveBeenCalledWith(CAMPAIGN, run.id))

    // The run summary reflects the terminal status and the final verdict.
    const summary = await waitFor(() => {
      const el = container.querySelector('[data-section="ai-run-summary"]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    expect(summary).toHaveTextContent("Concluído")
    expect(within(summary).getByText("Aprovado")).toBeInTheDocument()

    // The iterations list renders exactly one iteration (index shown 1-based).
    const iterations = Array.from(
      container.querySelectorAll('[data-section="ai-run-iteration"]'),
    ) as HTMLElement[]
    expect(iterations).toHaveLength(1)
    expect(iterations[0]).toHaveTextContent("Iteração 1")

    // The evaluator verdict is rendered for that iteration: decision + metrics.
    const evaluation = iterations[0].querySelector(
      '[data-section="ai-run-evaluation"]',
    ) as HTMLElement
    expect(evaluation).not.toBeNull()
    expect(within(evaluation).getByText("Veredito do avaliador")).toBeInTheDocument()
    expect(within(evaluation).getByText("Finalizar")).toBeInTheDocument()
    expect(within(evaluation).getByText("80%")).toBeInTheDocument()
    expect(within(evaluation).getByText("60%")).toBeInTheDocument()
    expect(within(evaluation).getByText("Cobertura procedural suficiente.")).toBeInTheDocument()
  })

  it("surfaces a NEW iteration and verdict appearing on a later poll (Req. 13.3, 13.4)", async () => {
    const runningRun = makeRun({ status: "em_execucao", iterations_used: 1 })
    const terminalRun = makeRun({
      status: "concluido",
      iterations_used: 2,
      final_verdict_success: true,
    })
    startMock.mockResolvedValue(runningRun)

    // First poll: a single iteration whose evaluator verdict is not available
    // yet (the view keeps polling because the run is non-terminal).
    getMock.mockResolvedValueOnce(
      makeDetail({
        run: runningRun,
        iterations: [makeIteration({ id: 100, index: 0, evaluation: null })],
      }),
    )
    // Second poll: a brand-new iteration appears AND the evaluator verdict is now
    // present; the run turns terminal so polling stops after this read.
    getMock.mockResolvedValue(
      makeDetail({
        run: terminalRun,
        iterations: [
          makeIteration({ id: 100, index: 0, evaluation: null }),
          makeIteration({
            id: 101,
            index: 1,
            refined_stage: "estagio_4_operacionalizacao",
            evaluation: makeEvaluation({ id: 11, decision: "finalizar", verdict_success: true }),
          }),
        ],
      }),
    )

    const { container } = renderWithI18n(<AiRunView campaignId={CAMPAIGN} />)

    await userEvent.click(screen.getByRole("button", { name: "Iniciar execução de IA" }))

    const iterationEls = () =>
      Array.from(
        container.querySelectorAll('[data-section="ai-run-iteration"]'),
      ) as HTMLElement[]

    // Initially one iteration with the "verdict not available yet" note.
    await waitFor(() => expect(iterationEls()).toHaveLength(1))
    expect(
      screen.getByText("Veredito do avaliador ainda não disponível."),
    ).toBeInTheDocument()

    // After the next poll fires (the view re-reads on its ≤5s interval), the new
    // iteration and its evaluator verdict appear (Req. 13.3, 13.4). The timeout
    // comfortably exceeds the view's 3s poll interval.
    await waitFor(() => expect(iterationEls()).toHaveLength(2), { timeout: 8000 })
    const iterations = iterationEls()
    expect(iterations[1]).toHaveTextContent("Iteração 2")
    expect(iterations[1]).toHaveTextContent("estagio_4_operacionalizacao")
    // The second iteration now carries a rendered evaluator verdict.
    expect(
      iterations[1].querySelector('[data-section="ai-run-evaluation"]'),
    ).not.toBeNull()

    // The view polled at least twice (initial + follow-up) and stopped once the
    // run reached a terminal status.
    expect(getMock.mock.calls.length).toBeGreaterThanOrEqual(2)
  }, 15000)

  it("renders the pt-BR start error when startAiRun fails", async () => {
    startMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<AiRunView campaignId={CAMPAIGN} />)

    await userEvent.click(screen.getByRole("button", { name: "Iniciar execução de IA" }))

    await screen.findByText("Não foi possível iniciar a execução de IA.")
    // A failed start never polls the read endpoint.
    expect(getMock).not.toHaveBeenCalled()
  })
})
