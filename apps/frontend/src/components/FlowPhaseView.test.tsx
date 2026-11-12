import { describe, expect, it, vi } from "vitest"
import { screen } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { FlowPhaseView } from "@/components/FlowPhaseView"
import type { FlowDetail } from "@/lib/attackFlow"

/**
 * FlowPhaseView: provenance badge (exact/adapted/created) and the cycles
 * warning (Req. 3.6, 2.6). The diagram and the API client are mocked.
 */

vi.mock("@/components/MermaidDiagram", () => ({
  MermaidDiagram: ({ definition }: { definition: string }) => (
    <pre data-testid="mermaid-stub">{definition}</pre>
  ),
}))
vi.mock("@/lib/attackFlow", () => ({ getFlow: vi.fn(), getFlows: vi.fn() }))

function flow(overrides: Partial<FlowDetail> = {}): FlowDetail {
  return {
    id: 1,
    campaign_id: "shadowray",
    origin: "ai_generated",
    identifier: "f1",
    label: "Flow 1",
    scope: null,
    has_cycles: false,
    provenance: {
      kind: "adapted",
      similarity: 0.82,
      references: [9],
      reference_flows: [{ id: 9, label: "Ref flow", source_key: "mitre-9" }],
    },
    created_at: "2024-01-01T00:00:00",
    description: null,
    nodes: [],
    edges: [],
    mermaid: "flowchart TD\nA-->B",
    cycles: [],
    back_edges: [],
    ...overrides,
  }
}

describe("FlowPhaseView", () => {
  it("shows the provenance badge with its kind and the reference flows", () => {
    renderWithI18n(<FlowPhaseView flow={flow()} />)

    const badge = document.querySelector('[data-part="provenance-badge"]')
    expect(badge).toHaveAttribute("data-kind", "adapted")
    expect(badge).toHaveTextContent("Adaptado")
    expect(screen.getByText("Ref flow")).toBeInTheDocument()
    expect(screen.getByTestId("mermaid-stub")).toHaveTextContent("flowchart TD")
    expect(document.querySelector('[data-part="cycles"]')).toBeNull()
  })

  it("warns about cycles and lists them with their back edges", () => {
    renderWithI18n(
      <FlowPhaseView
        flow={flow({
          provenance: null,
          has_cycles: true,
          cycles: [["A", "B", "A"]],
          back_edges: [["B", "A"]],
        })}
      />,
    )

    expect(screen.getByRole("alert")).toHaveTextContent("Ciclos detectados")
    expect(screen.getByText("A → B → A")).toBeInTheDocument()
    expect(screen.getByText("Aresta de retorno: B → A")).toBeInTheDocument()
    expect(document.querySelector('[data-part="provenance-badge"]')).toBeNull()
  })
})
