import { describe, expect, it, vi } from "vitest"
import { within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { CaseSelector } from "@/components/CaseSelector"
import type { CasesResponse } from "@/lib/casos"

/**
 * A single case in the 1..6 scheme (Seleção da base STIX = Stage 1): Stage 1 is
 * concluded, and Stages 2 (Seleção da campanha), 3 (Geração do DAG),
 * 4 (Operacionalização), 5 (Criação do ambiente) and 6 (Execução, medição e
 * relatório) are BLOCKED behind it (bloqueado: true, iniciavel: false). This
 * mirrors `GET /api/casos` for a case midway through the six-stage pipeline.
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
            {
              estagio: 4,
              estado: "nao_iniciado",
              progresso: 0,
              bloqueado: true,
              iniciavel: false,
            },
            {
              estagio: 5,
              estado: "nao_iniciado",
              progresso: 0,
              bloqueado: true,
              iniciavel: false,
            },
            {
              estagio: 6,
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
 * The six stage rows of the (single) rendered case, in stage order
 * 1→2→3→4→5→6. Each stage is rendered as an `<li>` inside the card's `<ol>`, so
 * the ordered list items map directly to stages 1, 2, 3, 4, 5, 6.
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
    expect(rows).toHaveLength(6)

    // Stage 1 (index 0) is not blocked; stages 2..6 are blocked and dimmed.
    const stage1Inner = rows[0].querySelector("div")!
    const stage2Inner = rows[1].querySelector("div")!
    const stage3Inner = rows[2].querySelector("div")!
    const stage4Inner = rows[3].querySelector("div")!
    const stage5Inner = rows[4].querySelector("div")!
    const stage6Inner = rows[5].querySelector("div")!
    expect(stage1Inner.className).not.toContain("opacity-60")
    expect(stage2Inner.className).toContain("opacity-60")
    expect(stage3Inner.className).toContain("opacity-60")
    expect(stage4Inner.className).toContain("opacity-60")
    expect(stage5Inner.className).toContain("opacity-60")
    expect(stage6Inner.className).toContain("opacity-60")
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

    // Stage 4 (Provisionamento do Ambiente) is blocked behind the others and disabled too.
    const stage4Button = within(rows[3]).getByRole("button")
    expect(stage4Button).toBeDisabled()
    await user.click(stage4Button)
    expect(onSelectStage).not.toHaveBeenCalled()

    // Stage 5 (Execução) is blocked behind the others and disabled too.
    const stage5Button = within(rows[4]).getByRole("button")
    expect(stage5Button).toBeDisabled()
    await user.click(stage5Button)
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
    expect(stageRows(container)).toHaveLength(6)
  })
})
