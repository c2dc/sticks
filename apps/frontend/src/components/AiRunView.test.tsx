import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { AiRunView } from "@/components/AiRunView"
import type {
  AiEvaluation,
  AiIteration,
  AiRun,
  AiRunDetail,
  AiStageOutput,
  GroundTruthComparison,
} from "@/lib/ai"

// Mock the AI run wrappers so no network is used and the test fully controls the
// run summary + the detail the view polls (startAiRun / getAiRun) and the
// on-demand Ground_Truth_Comparison (compareAiRunWithCuration). The view imports
// all three from `@/lib/ai`, so mocking this module is enough.
vi.mock("@/lib/ai", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/ai")>()
  return {
    ...actual,
    startAiRun: vi.fn(),
    getAiRun: vi.fn(),
    compareAiRunWithCuration: vi.fn(),
  }
})

import { compareAiRunWithCuration, getAiRun, startAiRun } from "@/lib/ai"

const startMock = vi.mocked(startAiRun)
const getMock = vi.mocked(getAiRun)
const compareMock = vi.mocked(compareAiRunWithCuration)

const CAMPAIGN = "shadowray"

// ---------------------------------------------------------------------------
// Fixtures.
// ---------------------------------------------------------------------------

function makeRun(overrides: Partial<AiRun> = {}): AiRun {
  return {
    id: 1,
    campaign_id: CAMPAIGN,
    status: "running",
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
    decision: "finish",
    rationale: "Cobertura procedural suficiente.",
    ...overrides,
  }
}

function makeStageOutput(overrides: Partial<AiStageOutput> = {}): AiStageOutput {
  return {
    id: 1000,
    stage: "dag",
    origin: "ai_generated",
    payload: { nodes: ["T1190"] },
    trace: {
      agent_input: { modeling: "entrada de modelagem" },
      processing: { reasoning: "raciocínio", llm_prompt: "PROMPT", llm_response: "RESPOSTA" },
    },
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
    stage_outputs: [],
    failing_agent: null,
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
  compareMock.mockReset()
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
    const run = makeRun({ status: "completed", iterations_used: 1, final_verdict_success: true })
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

    // Per-iteration navigation renders exactly the single selected iteration
    // (index shown 1-based) and a navigator position readout "1 de 1".
    const iterations = Array.from(
      container.querySelectorAll('[data-section="ai-run-iteration"]'),
    ) as HTMLElement[]
    expect(iterations).toHaveLength(1)
    expect(iterations[0]).toHaveTextContent("Iteração 1")
    expect(screen.getByText("Iteração 1 de 1")).toBeInTheDocument()

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
    const runningRun = makeRun({ status: "running", iterations_used: 1 })
    const terminalRun = makeRun({
      status: "completed",
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
            refined_stage: "operationalization",
            evaluation: makeEvaluation({ id: 11, decision: "finish", verdict_success: true }),
          }),
        ],
      }),
    )

    const { container } = renderWithI18n(<AiRunView campaignId={CAMPAIGN} />)

    await userEvent.click(screen.getByRole("button", { name: "Iniciar execução de IA" }))

    const currentIteration = () =>
      container.querySelector('[data-section="ai-run-iteration"]') as HTMLElement | null

    // Initially one iteration (navigator "1 de 1") with the "verdict not
    // available yet" note; the view keeps polling because the run is non-terminal.
    await waitFor(() => expect(currentIteration()).not.toBeNull())
    expect(screen.getByText("Iteração 1 de 1")).toBeInTheDocument()
    expect(
      screen.getByText("Veredito do avaliador ainda não disponível."),
    ).toBeInTheDocument()

    // After the next poll fires (the view re-reads on its ≤5s interval), a brand
    // new iteration appears; the navigator grows to "2 de 2" and — because the
    // Researcher was parked on the last iteration — auto-advances to it so its
    // input/processing/output and evaluator verdict surface (Req. 13.3, 13.4).
    // The timeout comfortably exceeds the view's 3s poll interval.
    await waitFor(() => expect(screen.getByText("Iteração 2 de 2")).toBeInTheDocument(), {
      timeout: 8000,
    })
    const iteration = currentIteration() as HTMLElement
    expect(iteration).toHaveTextContent("Iteração 2")
    // The refined stage is shown via its meaningful English name, not the raw enum.
    expect(iteration).toHaveTextContent("Agente de operacionalização")
    // The newly-surfaced iteration carries a rendered evaluator verdict.
    expect(iteration.querySelector('[data-section="ai-run-evaluation"]')).not.toBeNull()

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

// ---------------------------------------------------------------------------
// Task 9.8 — per-iteration Input/Processing/Output, view toggle to the Audit
// tab, and the curated-only "Compare with curation" action (Req. 1.1, 1.5, 5.1).
// ---------------------------------------------------------------------------

describe("AiRunView — per-iteration trace, Audit toggle, curated compare (Req. 1.1, 1.5, 5.1)", () => {
  async function startWithDetail(detail: AiRunDetail, curated = false) {
    startMock.mockResolvedValue(detail.run)
    getMock.mockResolvedValue(detail)
    const utils = renderWithI18n(<AiRunView campaignId={CAMPAIGN} curated={curated} />)
    await userEvent.click(screen.getByRole("button", { name: "Iniciar execução de IA" }))
    await waitFor(() => expect(getMock).toHaveBeenCalled())
    return utils
  }

  it("renders the selected iteration's agent steps as Input/Processing/Output trace panels", async () => {
    const run = makeRun({ status: "completed", iterations_used: 1, final_verdict_success: true })
    const { container } = await startWithDetail(
      makeDetail({
        run,
        iterations: [
          makeIteration({
            evaluation: makeEvaluation(),
            stage_outputs: [
              makeStageOutput({ id: 1000, stage: "dag" }),
              makeStageOutput({ id: 1001, stage: "operationalization" }),
            ],
          }),
        ],
      }),
    )

    // The iteration renders its agent steps as three-part trace panels.
    const steps = await waitFor(() => {
      const found = container.querySelectorAll('[data-section="agent-trace-panel"]')
      expect(found.length).toBe(2)
      return Array.from(found) as HTMLElement[]
    })
    // Ordered by the meaningful agent-step sequence and three-part each.
    expect(steps.map((s) => s.getAttribute("data-stage"))).toEqual([
      "dag",
      "operationalization",
    ])
    expect(steps[0].querySelector('[data-part="input"]')).not.toBeNull()
    expect(steps[0].querySelector('[data-part="processing"]')).not.toBeNull()
    expect(steps[0].querySelector('[data-part="output"]')).not.toBeNull()
    // The raw-LLM disclosure is closed by default (Req. 6.2, 6.3).
    const details = steps[0].querySelector('[data-part="raw-details"]') as HTMLDetailsElement
    expect(details).not.toBeNull()
    expect(details.open).toBe(false)
  })

  it("toggles between the per-iteration view and the consolidated Audit tab", async () => {
    const run = makeRun({ status: "completed", iterations_used: 1, final_verdict_success: true })
    const { container } = await startWithDetail(
      makeDetail({
        run,
        iterations: [
          makeIteration({
            index: 0,
            stage_outputs: [makeStageOutput({ id: 1000, stage: "dag" })],
          }),
          makeIteration({
            id: 101,
            index: 1,
            stage_outputs: [makeStageOutput({ id: 1001, stage: "environment" })],
          }),
        ],
      }),
    )

    // Default view: per-iteration navigation (one iteration at a time).
    await waitFor(() =>
      expect(container.querySelector('[data-section="ai-run-iteration"]')).not.toBeNull(),
    )
    expect(container.querySelector('[data-section="ai-run-audit"]')).toBeNull()

    // Switch to the Audit tab: the WHOLE trace is shown grouped by iteration.
    await userEvent.click(screen.getByRole("tab", { name: "Auditoria" }))
    const audit = await waitFor(() => {
      const el = container.querySelector('[data-section="ai-run-audit"]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    expect(
      audit.querySelectorAll('[data-section="ai-run-audit-iteration"]'),
    ).toHaveLength(2)
    // The per-iteration navigator is no longer shown.
    expect(container.querySelector('[data-section="ai-run-iteration-nav"]')).toBeNull()

    // Switch back to the per-iteration view.
    await userEvent.click(screen.getByRole("tab", { name: "Por iteração" }))
    await waitFor(() =>
      expect(container.querySelector('[data-section="ai-run-iteration"]')).not.toBeNull(),
    )
  })

  it("renders the curated-only 'Compare with curation' action for a completed curated run", async () => {
    const run = makeRun({ status: "completed", iterations_used: 1, final_verdict_success: true })
    const comparison: GroundTruthComparison = {
      campaign_id: CAMPAIGN,
      run_id: run.id,
      stages: [
        {
          stage: "dag",
          ai_items: ["T1190", "T1059"],
          curated_items: ["T1190"],
          matched: ["T1190"],
          missing: [],
          extra: ["T1059"],
        },
      ],
    }
    compareMock.mockResolvedValue(comparison)

    const { container } = await startWithDetail(
      makeDetail({ run, iterations: [makeIteration({ evaluation: makeEvaluation() })] }),
      true,
    )

    // The compare action is offered for a completed curated run.
    const action = await screen.findByRole("button", { name: "Comparar com a curadoria" })
    expect(action).toBeInTheDocument()

    await userEvent.click(action)

    await waitFor(() => expect(compareMock).toHaveBeenCalledWith(CAMPAIGN, run.id))
    // The per-stage comparison result renders.
    const result = await waitFor(() => {
      const el = container.querySelector('[data-section="ai-run-ground-truth-result"]')
      expect(el).not.toBeNull()
      return el as HTMLElement
    })
    expect(
      result.querySelectorAll('[data-section="ai-run-ground-truth-stage"]'),
    ).toHaveLength(1)
    expect(within(result).getByText("Geração do DAG")).toBeInTheDocument()
  })

  it("does NOT render the compare action for a non-curated run", async () => {
    const run = makeRun({ status: "completed", iterations_used: 1, final_verdict_success: true })
    await startWithDetail(
      makeDetail({ run, iterations: [makeIteration({ evaluation: makeEvaluation() })] }),
      false,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    // Curated-only: a non-curated run never offers the comparison (Req. 5.1).
    expect(
      screen.queryByRole("button", { name: "Comparar com a curadoria" }),
    ).toBeNull()
    expect(compareMock).not.toHaveBeenCalled()
  })

  it("does NOT render the compare action for a curated run that did not complete", async () => {
    // Terminal but aborted (not completed): polling stops, yet the action stays
    // hidden because the gate requires `status === "completed"`.
    const run = makeRun({ status: "aborted", iterations_used: 1 })
    await startWithDetail(
      makeDetail({ run, iterations: [makeIteration()] }),
      true,
    )

    await waitFor(() => expect(getMock).toHaveBeenCalled())
    // The action is gated on BOTH curated AND a completed run.
    expect(
      screen.queryByRole("button", { name: "Comparar com a curadoria" }),
    ).toBeNull()
  })
})
