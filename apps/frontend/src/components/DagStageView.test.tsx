import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { DagStageView } from "@/components/DagStageView"
import type { CaseStageStates } from "@/lib/campaigns"
import type { DagDetail, DagListResponse, DagSummary } from "@/lib/dag"

// Mock the DAG API wrappers so no network is used.
vi.mock("@/lib/dag", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/dag")>()
  return {
    ...actual,
    getDags: vi.fn(),
    getDagById: vi.fn(),
    chooseDag: vi.fn(),
  }
})

// Stub MermaidDiagram so jsdom never imports the heavy `mermaid` bundle; the
// stub just exposes the definition it received for assertions.
vi.mock("@/components/MermaidDiagram", () => ({
  MermaidDiagram: ({ definition }: { definition: string }) => (
    <div data-testid="mermaid-stub">{definition}</div>
  ),
}))

import { chooseDag, getDagById, getDags } from "@/lib/dag"

const getDagsMock = vi.mocked(getDags)
const getDagByIdMock = vi.mocked(getDagById)
const chooseDagMock = vi.mocked(chooseDag)

function makeSummary(): DagSummary {
  return {
    id: 1,
    campaign_id: "shadowray",
    origin: "curadoria_humana",
    identifier: "shadowray-v1",
    label: "ShadowRay (curado)",
    created_at: "2024-01-01T00:00:00Z",
  }
}

function makeList(dags: DagSummary[]): DagListResponse {
  return { campaign_id: "shadowray", dags }
}

function makeDetail(): DagDetail {
  return {
    campaign_id: "shadowray",
    dag_id: 1,
    origin: "curadoria_humana",
    identifier: "shadowray-v1",
    label: "ShadowRay (curado)",
    nodes: [],
    edges: [],
    mermaid: 'flowchart TD\n  n_n1["T1190"]',
  }
}

// Backend-reported stage states after a DAG choice: under the five-stage
// scheme, choosing the DAG concludes Stage 2 (DAG generation) and unlocks
// Stage 3 (Operationalization). Stage 1 was already concluded (the gate).
function makeStates(): CaseStageStates {
  return {
    campaign_id: "shadowray",
    stages: [
      { stage: 1, state: "concluido", progress: 100, blocked: false, startable: true },
      { stage: 2, state: "concluido", progress: 100, blocked: false, startable: true },
      { stage: 3, state: "nao_iniciado", progress: 0, blocked: false, startable: true },
    ],
    fully_completed: false,
  }
}

describe("DagStageView (Stage 2 — DAG generation, Req. 3.3–3.5, 3.7)", () => {
  beforeEach(() => {
    getDagsMock.mockReset()
    getDagByIdMock.mockReset()
    chooseDagMock.mockReset()
  })

  it("auto-concludes when there is exactly one DAG (curated), renders the graph and reports stage states", async () => {
    getDagsMock.mockResolvedValue(makeList([makeSummary()]))
    getDagByIdMock.mockResolvedValue(makeDetail())
    const states = makeStates()
    chooseDagMock.mockResolvedValue(states)
    const onDagChosen = vi.fn()

    renderWithI18n(
      <DagStageView campaignId="shadowray" onDagChosen={onDagChosen} />,
    )

    await waitFor(() => {
      expect(getDagsMock).toHaveBeenCalledWith("shadowray")
    })

    // The DAG's graph renders with the backend-provided definition.
    const stub = await screen.findByTestId("mermaid-stub")
    expect(stub).toHaveTextContent("flowchart TD")

    // With a single DAG, the stage auto-concludes — no "Selecionar DAG" button.
    expect(
      screen.queryByRole("button", { name: /Selecionar DAG/ }),
    ).not.toBeInTheDocument()

    // chooseDag is called automatically and the stage states are reported.
    await waitFor(() => {
      expect(chooseDagMock).toHaveBeenCalledWith("shadowray", 1)
    })
    await waitFor(() => {
      expect(onDagChosen).toHaveBeenCalledWith(states)
    })
  })

  it("shows the 'sem DAG' state with 'Gerar DAG' visible but disabled, no-op on click (Req. 3.4, 3.5)", async () => {
    getDagsMock.mockResolvedValue(makeList([]))
    const onDagChosen = vi.fn()

    renderWithI18n(
      <DagStageView campaignId="shadowray" onDagChosen={onDagChosen} />,
    )

    await screen.findByText(/não possui um DAG/)

    const gerar = screen.getByRole("button", { name: /Gerar DAG|Gerar com IA/ })
    expect(gerar).toBeDisabled()
    expect(gerar).toHaveAttribute("aria-disabled")

    // There is no DAG to choose, so advancing past Stage 2 stays blocked:
    // the "Selecionar DAG" action is absent and no graph is requested.
    expect(
      screen.queryByRole("button", { name: /Selecionar DAG/ }),
    ).not.toBeInTheDocument()

    // Clicking the disabled button is a no-op: no AI, no choice, no advance.
    await userEvent.click(gerar)
    expect(onDagChosen).not.toHaveBeenCalled()
    expect(getDagByIdMock).not.toHaveBeenCalled()
    expect(chooseDagMock).not.toHaveBeenCalled()
  })

  it("surfaces the pt-BR error message when the DAG list fails to load", async () => {
    getDagsMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<DagStageView campaignId="shadowray" />)

    await screen.findByText("Não foi possível carregar o DAG deste caso.")
  })

  it("renders the origin label (stix.dag.origem.*) for each DAG resumo (Req. 3.7)", async () => {
    // One curated and one AI-generated DAG so both origin labels are exercised.
    const curado = makeSummary()
    const geradoIa: DagSummary = {
      ...makeSummary(),
      id: 2,
      origin: "gerado_ia",
      identifier: "shadowray-ia",
      label: "ShadowRay (IA)",
    }
    getDagsMock.mockResolvedValue(makeList([curado, geradoIa]))
    getDagByIdMock.mockResolvedValue(makeDetail())

    renderWithI18n(<DagStageView campaignId="shadowray" />)

    await waitFor(() => expect(getDagsMock).toHaveBeenCalledWith("shadowray"))

    // The source line uses the pt-BR label and the per-origin translations.
    expect(await screen.findByText(/Origem do DAG: Curadoria humana/)).toBeInTheDocument()
    expect(screen.getByText(/Origem do DAG: Gerado por IA/)).toBeInTheDocument()
  })

  it("defaults selection to the first DAG and chooses it, reporting the backend CaseStageStates (Req. 3.3)", async () => {
    const first = makeSummary()
    const second: DagSummary = { ...makeSummary(), id: 2, identifier: "shadowray-v2" }
    getDagsMock.mockResolvedValue(makeList([first, second]))
    getDagByIdMock.mockResolvedValue(makeDetail())
    const states = makeStates()
    chooseDagMock.mockResolvedValue(states)
    const onDagChosen = vi.fn()

    renderWithI18n(
      <DagStageView campaignId="shadowray" onDagChosen={onDagChosen} />,
    )

    await waitFor(() => expect(getDagsMock).toHaveBeenCalledWith("shadowray"))

    // Without changing the selection, choosing picks the first DAG (id 1) and
    // reports exactly the backend-provided stage states (Stage 2 concluded,
    // Stage 3 unlocked).
    await userEvent.click(screen.getByRole("button", { name: /Selecionar DAG/ }))
    await waitFor(() => expect(chooseDagMock).toHaveBeenCalledWith("shadowray", 1))
    await waitFor(() => expect(onDagChosen).toHaveBeenCalledWith(states))

    const reported = onDagChosen.mock.calls[0][0] as CaseStageStates
    expect(reported.stages.find((e) => e.stage === 2)?.state).toBe("concluido")
    expect(reported.stages.find((e) => e.stage === 3)?.blocked).toBe(false)
  })
})
