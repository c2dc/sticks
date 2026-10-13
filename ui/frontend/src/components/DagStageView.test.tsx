import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { DagStageView } from "@/components/DagStageView"
import type { CaseStageStates } from "@/lib/casos"
import type { DagDetalhe, DagListResponse, DagResumo } from "@/lib/dag"

// Mock the DAG API wrappers so no network is used.
vi.mock("@/lib/dag", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/dag")>()
  return {
    ...actual,
    getDags: vi.fn(),
    getDagPorId: vi.fn(),
    escolherDag: vi.fn(),
  }
})

// Stub MermaidDiagram so jsdom never imports the heavy `mermaid` bundle; the
// stub just exposes the definition it received for assertions.
vi.mock("@/components/MermaidDiagram", () => ({
  MermaidDiagram: ({ definition }: { definition: string }) => (
    <div data-testid="mermaid-stub">{definition}</div>
  ),
}))

import { escolherDag, getDagPorId, getDags } from "@/lib/dag"

const getDagsMock = vi.mocked(getDags)
const getDagPorIdMock = vi.mocked(getDagPorId)
const escolherDagMock = vi.mocked(escolherDag)

function makeResumo(): DagResumo {
  return {
    id: 1,
    caso_id: "shadowray",
    origem: "curadoria_humana",
    identificador: "shadowray-v1",
    rotulo: "ShadowRay (curado)",
    criado_em: "2024-01-01T00:00:00Z",
  }
}

function makeList(dags: DagResumo[]): DagListResponse {
  return { caso_id: "shadowray", dags }
}

function makeDetalhe(): DagDetalhe {
  return {
    caso_id: "shadowray",
    dag_id: 1,
    origem: "curadoria_humana",
    identificador: "shadowray-v1",
    rotulo: "ShadowRay (curado)",
    nos: [],
    arestas: [],
    mermaid: 'flowchart TD\n  n_n1["T1190"]',
  }
}

// Backend-reported stage states after a DAG choice: under the five-stage
// scheme, choosing the DAG concludes Estágio 2 (Geração do DAG) and unlocks
// Estágio 3 (Operacionalização). Estágio 1 was already concluded (the gate).
function makeEstados(): CaseStageStates {
  return {
    caso_id: "shadowray",
    estagios: [
      { estagio: 1, estado: "concluido", progresso: 100, bloqueado: false, iniciavel: true },
      { estagio: 2, estado: "concluido", progresso: 100, bloqueado: false, iniciavel: true },
      { estagio: 3, estado: "nao_iniciado", progresso: 0, bloqueado: false, iniciavel: true },
    ],
    concluido_total: false,
  }
}

describe("DagStageView (Estágio 2 — Geração do DAG, Req. 3.3–3.5, 3.7)", () => {
  beforeEach(() => {
    getDagsMock.mockReset()
    getDagPorIdMock.mockReset()
    escolherDagMock.mockReset()
  })

  it("lists persisted DAGs, renders the selected DAG's graph and chooses it (Req. 3.3, 3.7)", async () => {
    getDagsMock.mockResolvedValue(makeList([makeResumo()]))
    getDagPorIdMock.mockResolvedValue(makeDetalhe())
    const estados = makeEstados()
    escolherDagMock.mockResolvedValue(estados)
    const onDagEscolhido = vi.fn()

    renderWithI18n(
      <DagStageView casoId="shadowray" onDagEscolhido={onDagEscolhido} />,
    )

    await waitFor(() => {
      expect(getDagsMock).toHaveBeenCalledWith("shadowray")
    })

    // The selected DAG's graph renders with the backend-provided definition.
    const stub = await screen.findByTestId("mermaid-stub")
    expect(stub).toHaveTextContent("flowchart TD")

    // Choosing the DAG calls escolherDag and reports the backend stage states.
    await userEvent.click(screen.getByRole("button", { name: /Selecionar DAG/ }))
    await waitFor(() => {
      expect(escolherDagMock).toHaveBeenCalledWith("shadowray", 1)
    })
    await waitFor(() => {
      expect(onDagEscolhido).toHaveBeenCalledWith(estados)
    })
  })

  it("shows the 'sem DAG' state with 'Gerar DAG' visible but disabled, no-op on click (Req. 3.4, 3.5)", async () => {
    getDagsMock.mockResolvedValue(makeList([]))
    const onDagEscolhido = vi.fn()

    renderWithI18n(
      <DagStageView casoId="shadowray" onDagEscolhido={onDagEscolhido} />,
    )

    await screen.findByText(/não possui um DAG/)

    const gerar = screen.getByRole("button", { name: /Gerar DAG|Gerar com IA/ })
    expect(gerar).toBeDisabled()
    expect(gerar).toHaveAttribute("aria-disabled")

    // There is no DAG to choose, so advancing past Estágio 2 stays blocked:
    // the "Selecionar DAG" action is absent and no graph is requested.
    expect(
      screen.queryByRole("button", { name: /Selecionar DAG/ }),
    ).not.toBeInTheDocument()

    // Clicking the disabled button is a no-op: no AI, no choice, no advance.
    await userEvent.click(gerar)
    expect(onDagEscolhido).not.toHaveBeenCalled()
    expect(getDagPorIdMock).not.toHaveBeenCalled()
    expect(escolherDagMock).not.toHaveBeenCalled()
  })

  it("surfaces the pt-BR error message when the DAG list fails to load", async () => {
    getDagsMock.mockRejectedValue(new Error("boom"))

    renderWithI18n(<DagStageView casoId="shadowray" />)

    await screen.findByText("Não foi possível carregar o DAG deste caso.")
  })

  it("renders the origin label (stix.dag.origem.*) for each DAG resumo (Req. 3.7)", async () => {
    // One curated and one AI-generated DAG so both origin labels are exercised.
    const curado = makeResumo()
    const geradoIa: DagResumo = {
      ...makeResumo(),
      id: 2,
      origem: "gerado_ia",
      identificador: "shadowray-ia",
      rotulo: "ShadowRay (IA)",
    }
    getDagsMock.mockResolvedValue(makeList([curado, geradoIa]))
    getDagPorIdMock.mockResolvedValue(makeDetalhe())

    renderWithI18n(<DagStageView casoId="shadowray" />)

    await waitFor(() => expect(getDagsMock).toHaveBeenCalledWith("shadowray"))

    // The source line uses the pt-BR label and the per-origin translations.
    expect(await screen.findByText(/Origem do DAG: Curadoria humana/)).toBeInTheDocument()
    expect(screen.getByText(/Origem do DAG: Gerado por IA/)).toBeInTheDocument()
  })

  it("defaults selection to the first DAG and chooses it, reporting the backend CaseStageStates (Req. 3.3)", async () => {
    const first = makeResumo()
    const second: DagResumo = { ...makeResumo(), id: 2, identificador: "shadowray-v2" }
    getDagsMock.mockResolvedValue(makeList([first, second]))
    getDagPorIdMock.mockResolvedValue(makeDetalhe())
    const estados = makeEstados()
    escolherDagMock.mockResolvedValue(estados)
    const onDagEscolhido = vi.fn()

    renderWithI18n(
      <DagStageView casoId="shadowray" onDagEscolhido={onDagEscolhido} />,
    )

    await waitFor(() => expect(getDagsMock).toHaveBeenCalledWith("shadowray"))

    // Without changing the selection, choosing picks the first DAG (id 1) and
    // reports exactly the backend-provided stage states (Estágio 2 concluído,
    // Estágio 3 liberado).
    await userEvent.click(screen.getByRole("button", { name: /Selecionar DAG/ }))
    await waitFor(() => expect(escolherDagMock).toHaveBeenCalledWith("shadowray", 1))
    await waitFor(() => expect(onDagEscolhido).toHaveBeenCalledWith(estados))

    const reported = onDagEscolhido.mock.calls[0][0] as CaseStageStates
    expect(reported.estagios.find((e) => e.estagio === 2)?.estado).toBe("concluido")
    expect(reported.estagios.find((e) => e.estagio === 3)?.bloqueado).toBe(false)
  })
})
