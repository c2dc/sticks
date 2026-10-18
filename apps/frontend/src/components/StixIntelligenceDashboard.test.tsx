import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen } from "@testing-library/react"

import { renderWithI18n } from "@/test/render"
import { StixIntelligenceDashboard } from "@/components/StixIntelligenceDashboard"
import { ApiError } from "@/lib/api"
import type { DashboardStix } from "@/lib/stix"

// Mock the STIX API wrappers so no network is used.
vi.mock("@/lib/stix", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/stix")>()
  return {
    ...actual,
    getDashboard: vi.fn(),
  }
})

import { getDashboard } from "@/lib/stix"

const getDashboardMock = vi.mocked(getDashboard)

function makeDashboard(): DashboardStix {
  return {
    version: "14.0",
    name: "Enterprise ATT&CK v14.0",
    counts: [
      {
        label: "Campanhas",
        stix_type: "campaign",
        count: 12,
        description: "Operações atribuídas a um adversário ao longo do tempo.",
      },
      {
        label: "Técnicas",
        stix_type: "attack-pattern",
        count: 201,
        description: "Comportamentos usados pelo adversário para atingir objetivos.",
      },
    ],
  }
}

describe("StixIntelligenceDashboard (Req. 3 — intelligence counts, no partial on error)", () => {
  beforeEach(() => {
    getDashboardMock.mockReset()
  })

  it("renders the version id and every count card (label + count + description)", async () => {
    getDashboardMock.mockResolvedValue(makeDashboard())

    renderWithI18n(
      <StixIntelligenceDashboard version="14.0" onGoToSelection={vi.fn()} />,
    )

    // Base identification with its version (Req. 3.1).
    await screen.findByText("Enterprise ATT&CK v14.0 — v14.0")

    // Each count card: label, count and description (Req. 3.2, 3.3).
    expect(screen.getByText("Campanhas")).toBeInTheDocument()
    expect(screen.getByText("12")).toBeInTheDocument()
    expect(
      screen.getByText("Operações atribuídas a um adversário ao longo do tempo."),
    ).toBeInTheDocument()

    expect(screen.getByText("Técnicas")).toBeInTheDocument()
    expect(screen.getByText("201")).toBeInTheDocument()
    expect(
      screen.getByText("Comportamentos usados pelo adversário para atingir objetivos."),
    ).toBeInTheDocument()
  })

  it("shows a descriptive error and renders NO count cards on an ApiError(422) (Req. 3.5)", async () => {
    getDashboardMock.mockRejectedValue(
      new ApiError(422, "Bundle inválido", {
        detail: {
          motivo: "bundle_invalido",
          mensagem: "A base STIX selecionada está corrompida ou malformada.",
        },
      }),
    )

    renderWithI18n(
      <StixIntelligenceDashboard version="14.0" onGoToSelection={vi.fn()} />,
    )

    // The descriptive pt-BR message from the typed error body is shown.
    await screen.findByText("A base STIX selecionada está corrompida ou malformada.")

    // No partial counts are rendered (Req. 3.5).
    expect(screen.queryByText("Campanhas")).toBeNull()
    expect(screen.queryByText("Técnicas")).toBeNull()
    expect(screen.queryByText("12")).toBeNull()
    expect(screen.queryByText("201")).toBeNull()
  })
})
