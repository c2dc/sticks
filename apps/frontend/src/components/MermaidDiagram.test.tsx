import { beforeEach, describe, expect, it, vi } from "vitest"
import { screen } from "@testing-library/react"

import i18n from "@/i18n"
import { renderWithI18n } from "@/test/render"
import { MermaidDiagram } from "@/components/MermaidDiagram"
import { ThemeProvider } from "@/components/theme-provider"

/**
 * MermaidDiagram accessible labels come from i18n (no hard-coded strings): the
 * rendered diagram is an `img` with a localized label, and the fallback source
 * `<pre>` has its own localized label. Mermaid itself is mocked.
 */

const renderMock = vi.fn()

vi.mock("mermaid", () => ({
  default: {
    initialize: vi.fn(),
    render: (...args: unknown[]) => renderMock(...args),
  },
}))

function renderDiagram() {
  return renderWithI18n(
    <ThemeProvider>
      <MermaidDiagram definition="graph TD; A-->B" />
    </ThemeProvider>,
  )
}

describe("MermaidDiagram i18n labels", () => {
  beforeEach(async () => {
    renderMock.mockReset()
    await i18n.changeLanguage("pt-BR")
  })

  it.each([
    ["pt-BR", "Diagrama do attack flow"],
    ["en", "Attack flow diagram"],
  ])("labels the rendered diagram in %s", async (language, label) => {
    await i18n.changeLanguage(language)
    renderMock.mockResolvedValue({ svg: "<svg></svg>" })
    renderDiagram()

    expect(await screen.findByRole("img", { name: label })).toBeInTheDocument()
  })

  it.each([
    ["pt-BR", "Código-fonte do diagrama do attack flow"],
    ["en", "Attack flow diagram source"],
  ])("labels the fallback source in %s", async (language, label) => {
    await i18n.changeLanguage(language)
    renderMock.mockRejectedValue(new Error("boom"))
    renderDiagram()

    const source = await screen.findByLabelText(label)
    expect(source.tagName).toBe("PRE")
    expect(source).toHaveTextContent("graph TD; A-->B")
    // The raw error message is not leaked into the UI.
    expect(screen.queryByText("boom")).toBeNull()
  })
})
