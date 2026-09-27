import { describe, expect, it, vi } from "vitest"
import { within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { CaseSelector } from "@/components/CaseSelector"
import type { CasesResponse } from "@/lib/casos"

/**
 * A single case where Stage 1 is concluded, Stage 2 is BLOCKED
 * (bloqueado: true, iniciavel: false) and Stage 3 is blocked behind it.
 * This mirrors `GET /api/casos` for a case midway through the pipeline.
 */
function makeBlockedStageData(): CasesResponse {
  return {
    casos: [
      {
        id: "c0010",
        nome: "C0010",
        origem_traducao: "curadoria_humana",
        estados: {
          caso_id: "c0010",
          concluido_total: false,
          estagios: [
            {
              estagio: 1,
              estado: "concluido",
              progresso: 100,
              bloqueado: false,
              iniciavel: false,
            },
            {
              estagio: 2,
              estado: "nao_iniciado",
              progresso: 0,
              bloqueado: true,
              iniciavel: false,
            },
            {
              estagio: 3,
              estado: "nao_iniciado",
              progresso: 0,
              bloqueado: true,
              iniciavel: false,
            },
          ],
        },
      },
    ],
    progresso: {
      casos_concluidos: 0,
      total_casos: 8,
      percentual: 0,
    },
  }
}

/**
 * The three stage rows of the (single) rendered case, in stage order 1→2→3.
 * Each stage is rendered as an `<li>` inside the card's `<ol>`, so the ordered
 * list items map directly to stages 1, 2, 3.
 */
function stageRows(container: HTMLElement): HTMLElement[] {
  const ol = container.querySelector("ol")
  expect(ol).not.toBeNull()
  return Array.from((ol as HTMLElement).querySelectorAll(":scope > li")) as HTMLElement[]
}

describe("CaseSelector (Req. 5.4 — blocked stages cannot be started)", () => {
  it("visually marks blocked stages (dimmed rows) and blocks their controls", () => {
    const { container } = renderWithI18n(<CaseSelector data={makeBlockedStageData()} />)

    const rows = stageRows(container)
    expect(rows).toHaveLength(3)

    // Stage 1 (index 0) is not blocked; stages 2 and 3 are blocked and dimmed.
    const stage1Inner = rows[0].querySelector("div")!
    const stage2Inner = rows[1].querySelector("div")!
    const stage3Inner = rows[2].querySelector("div")!
    expect(stage1Inner.className).not.toContain("opacity-60")
    expect(stage2Inner.className).toContain("opacity-60")
    expect(stage3Inner.className).toContain("opacity-60")
  })

  it("disables the start control of a blocked stage and never triggers onSelectStage", async () => {
    const onSelectStage = vi.fn()
    const user = userEvent.setup()

    const { container } = renderWithI18n(
      <CaseSelector data={makeBlockedStageData()} onSelectStage={onSelectStage} />,
    )

    const rows = stageRows(container)
    // Stage 2 is blocked: its action control is disabled (Req. 5.4).
    const stage2Button = within(rows[1]).getByRole("button")
    expect(stage2Button).toBeDisabled()
    expect(stage2Button).toHaveAttribute("aria-disabled", "true")

    // Clicking the disabled control must not start the blocked stage.
    await user.click(stage2Button)
    expect(onSelectStage).not.toHaveBeenCalled()

    // Stage 3 is also blocked and disabled.
    const stage3Button = within(rows[2]).getByRole("button")
    expect(stage3Button).toBeDisabled()
    await user.click(stage3Button)
    expect(onSelectStage).not.toHaveBeenCalled()
  })

  it("allows opening a non-blocked (concluded) stage", async () => {
    const onSelectStage = vi.fn()
    const user = userEvent.setup()

    const { container } = renderWithI18n(
      <CaseSelector data={makeBlockedStageData()} onSelectStage={onSelectStage} />,
    )

    const rows = stageRows(container)
    // Stage 1 is concluded and not blocked, so its control can be actioned.
    const stage1Button = within(rows[0]).getByRole("button")
    expect(stage1Button).not.toBeDisabled()

    await user.click(stage1Button)
    expect(onSelectStage).toHaveBeenCalledTimes(1)
    // The second argument is the stage number that was opened (stage 1).
    expect(onSelectStage.mock.calls[0][1]).toBe(1)
  })

  it("keeps other cases selectable when one case failed to load (Req. 5.6)", () => {
    const data: CasesResponse = {
      casos: [
        {
          id: "broken",
          nome: "Broken Case",
          erro: {
            slug: "broken",
            nome: "Broken Case",
            error_type: "json_malformado",
            arquivo: "data/api/broken_dag-ability.json",
            detail: "Invalid JSON",
          },
        },
        makeBlockedStageData().casos[0],
      ],
      progresso: { casos_concluidos: 0, total_casos: 8, percentual: 0 },
    }

    const { container } = renderWithI18n(<CaseSelector data={data} />)
    // The healthy case still renders its stage rows (ordered list); the broken
    // case renders an alert instead — the others stay usable.
    const orderedLists = container.querySelectorAll("ol")
    expect(orderedLists.length).toBe(1)
    expect(stageRows(container)).toHaveLength(3)
  })
})
