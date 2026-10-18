import { describe, expect, it, vi, afterEach } from "vitest"
import { screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { ThemeProvider } from "@/components/theme-provider"
import { Landing } from "@/components/Landing"

/**
 * Install a `window.matchMedia` stub so `usePrefersReducedMotion` resolves to a
 * known value. `reduce` controls whether `(prefers-reduced-motion: reduce)`
 * matches.
 */
function mockMatchMedia(reduce: boolean) {
  const mql = {
    matches: reduce,
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation(() => mql),
  )
}

function renderLanding(onStart = vi.fn()) {
  return renderWithI18n(
    <ThemeProvider theme="claro" onThemeChange={vi.fn()}>
      <Landing onStart={onStart} />
    </ThemeProvider>,
  )
}

describe("Landing (Req. 1.1–1.5 — entry point of the guided flow)", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("renders the CTA 'Iniciar campanha' and fires onStart when clicked", async () => {
    mockMatchMedia(false)
    const onStart = vi.fn()
    const user = userEvent.setup()

    renderLanding(onStart)

    const cta = screen.getByRole("button", { name: "Iniciar campanha" })
    expect(cta).toBeInTheDocument()

    await user.click(cta)
    expect(onStart).toHaveBeenCalledTimes(1)
  })

  it("applies the entry animation classes when motion is NOT reduced", () => {
    mockMatchMedia(false)
    renderLanding()

    // The CTA wrapper carries the Tailwind `animate-in` class in the normal path.
    const cta = screen.getByRole("button", { name: "Iniciar campanha" })
    expect(cta.className).toContain("animate-in")
  })

  it("renders without any animation classes when prefers-reduced-motion is set (Req. 1.5)", () => {
    mockMatchMedia(true)
    const { container } = renderLanding()

    // No element on the Landing keeps the `animate-in` class under reduced motion.
    const animated = container.querySelectorAll(".animate-in")
    expect(animated).toHaveLength(0)

    const cta = screen.getByRole("button", { name: "Iniciar campanha" })
    expect(cta.className).not.toContain("animate-in")
    // The reduced-motion path omits the inline animationDuration entirely.
    expect(cta.style.animationDuration).toBe("")
  })
})
