import { describe, expect, it } from "vitest"
import { render, screen } from "@testing-library/react"

import { FlagBR, FlagUS } from "@/components/FlagIcon"

/** Flags are decorative unless a localized title is passed by the caller. */
describe.each([
  ["FlagBR", FlagBR],
  ["FlagUS", FlagUS],
])("%s", (_name, Flag) => {
  it("is hidden from assistive technology without a title (no hard-coded label)", () => {
    const { container } = render(<Flag />)
    const svg = container.querySelector("svg")!
    expect(svg).toHaveAttribute("aria-hidden", "true")
    expect(svg).not.toHaveAttribute("aria-label")
    expect(svg).not.toHaveAttribute("role")
    expect(screen.queryByRole("img")).toBeNull()
  })

  it("is exposed as an image with the given title", () => {
    render(<Flag title="Some language" />)
    const img = screen.getByRole("img", { name: "Some language" })
    expect(img).not.toHaveAttribute("aria-hidden")
  })
})
