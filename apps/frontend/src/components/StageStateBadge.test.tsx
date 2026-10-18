import { describe, expect, it } from "vitest"

import { renderWithI18n } from "@/test/render"
import { StageStateBadge } from "@/components/StageStateBadge"

/**
 * UI test for the "pre-completed (curated)" adornment of StageStateBadge
 * (wizard-progresso-estagios, Req. 2.1, 7.4). When a completed stage was
 * reached automatically by curation (`preCompleted`), the badge must be
 * visually distinct from the "concluded by you" state: a different
 * `data-state`, the `state.preConcluido` label, and a distinct style.
 */
describe("StageStateBadge — pre-completed (curated)", () => {
  it("renders the pre-concluded adornment with data-state='pre_concluido' and the i18n label", () => {
    const { container } = renderWithI18n(
      <StageStateBadge state="concluido" preCompleted />,
    )

    const badge = container.querySelector("[data-state]")
    expect(badge).not.toBeNull()
    expect(badge!.getAttribute("data-state")).toBe("pre_concluido")

    // pt-BR text for `state.preConcluido`.
    expect(badge!.textContent).toContain("Pré-concluído (curado)")

    // aria-label matches the pre-concluded label.
    expect(badge!.getAttribute("aria-label")).toBe("Pré-concluído (curado)")
  })

  it("renders the plain completed badge with data-state='concluido' when preCompleted is not set", () => {
    const { container } = renderWithI18n(<StageStateBadge state="concluido" />)

    const badge = container.querySelector("[data-state]")
    expect(badge).not.toBeNull()
    expect(badge!.getAttribute("data-state")).toBe("concluido")
    expect(badge!.textContent).toContain("Concluído")
  })

  it("renders the pre-concluded variant distinctly from the completed-by-action badge", () => {
    const pre = renderWithI18n(
      <StageStateBadge state="concluido" preCompleted />,
    )
    const preBadge = pre.container.querySelector("[data-state]")!

    const done = renderWithI18n(<StageStateBadge state="concluido" />)
    const doneBadge = done.container.querySelector("[data-state]")!

    // Distinct data-state attributes.
    expect(preBadge.getAttribute("data-state")).not.toBe(
      doneBadge.getAttribute("data-state"),
    )

    // Distinct styling: team-blue variation vs team-purple.
    expect(preBadge.className).not.toBe(doneBadge.className)
    expect(preBadge.className).toContain("team-blue")
    expect(doneBadge.className).toContain("team-purple")
  })
})
