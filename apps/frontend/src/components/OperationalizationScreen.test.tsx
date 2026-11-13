import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { act, fireEvent, screen, within } from "@testing-library/react"

import i18n from "@/i18n"
import { renderWithI18n } from "@/test/render"
import {
  ATOMIC_POLL_INTERVAL_MS,
  OperationalizationScreen,
} from "@/components/OperationalizationScreen"
import type { OperationalizationListing } from "@/lib/operationalization"

/**
 * Deterministic tests for the OperationalizationScreen: techniques with their
 * commands, an explicit message for a technique without commands, and the
 * not-ready warning that rereads the listing until `atomic_status` is `ready`
 * (Req. 6.3, 6.4, 6.5). The API client is mocked; timers are fake.
 */

vi.mock("@/lib/operationalization", () => ({
  getOperationalization: vi.fn(),
}))

import { getOperationalization } from "@/lib/operationalization"

const getMock = vi.mocked(getOperationalization)

function listing(
  status: "ready" | "running",
  loaded: number | null = null,
): OperationalizationListing {
  return {
    campaign_key: "shadowray",
    atomic_status: { status, loaded_count: loaded, expected_count: loaded === null ? null : 10 },
    techniques: [
      {
        technique_id: "T1059",
        technique_name: "Command and Scripting Interpreter",
        commands: [
          { id: 1, command: "whoami /all", executor: "powershell", platform: "windows", source: "atomic" },
        ],
      },
      { technique_id: "T1003", technique_name: "OS Credential Dumping", commands: [] },
    ],
  }
}

/** Flush pending promise callbacks inside act. */
async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe("OperationalizationScreen", () => {
  beforeEach(async () => {
    vi.useFakeTimers()
    getMock.mockReset()
    // The shared i18n instance persists its language across tests.
    await i18n.changeLanguage("pt-BR")
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("lists commands per technique and flags a technique without commands", async () => {
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    expect(getMock).toHaveBeenCalledWith("18.0", "shadowray")
    const items = screen.getAllByTestId("operationalization-technique")
    expect(items).toHaveLength(2)
    expect(within(items[0]).getByText("whoami /all")).toBeInTheDocument()
    expect(within(items[1]).getByTestId("no-commands")).toBeInTheDocument()
    expect(screen.queryByTestId("atomic-notice")).toBeNull()
  })

  it("shows the campaign name in the header when provided", async () => {
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(
      <OperationalizationScreen version="18.0" campaignKey="outer_space" campaignName="Outer Space" />,
    )
    await flush()

    const header = screen.getByTestId("operationalization-campaign")
    expect(header).toHaveTextContent("Outer Space")
    expect(header.textContent).not.toContain("outer_space")
  })

  it("falls back to the humanized key without exposing the slug when the name is absent", async () => {
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="outer_space" />)
    await flush()

    const header = screen.getByTestId("operationalization-campaign")
    expect(header).toHaveTextContent("Outer Space")
    expect(header.textContent).not.toContain("_")
  })

  it.each([
    ["pt-BR", "O Atomic Red Team não possui teste para esta técnica."],
    ["en", "Atomic Red Team has no test for this technique."],
  ])("shows the localized zero-command note in %s", async (language, note) => {
    await i18n.changeLanguage(language)
    getMock.mockResolvedValue({
      campaign_key: "shadowray",
      atomic_status: { status: "ready", loaded_count: 10, expected_count: 10 },
      techniques: [{ technique_id: "T1584.004", technique_name: "Server", commands: [] }],
    })
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    expect(screen.getByTestId("no-commands")).toHaveTextContent(note)
    expect(screen.queryByTestId("copy-command")).toBeNull()
  })

  it("exposes a labelled region and a labelled, focusable command block", async () => {
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    const region = screen.getByRole("region", { name: "Operacionalização" })
    expect(region).toBe(screen.getByTestId("operationalization-screen"))

    const block = screen.getByLabelText("Comando do Atomic Red Team")
    expect(block.tagName).toBe("PRE")
    expect(block).toHaveTextContent("whoami /all")
    expect(block).toHaveAttribute("tabindex", "0")
    expect(block.className).toContain("whitespace-pre-wrap")
    expect(block.className).toContain("overflow-x-auto")
    expect(block.className).toContain("break-words")
  })

  it("copies the command to the clipboard and gives localized feedback", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    const button = screen.getByRole("button", { name: "Copiar comando de T1059" })
    expect(button).toHaveTextContent("Copiar")
    await act(async () => {
      fireEvent.click(button)
    })

    expect(writeText).toHaveBeenCalledWith("whoami /all")
    expect(button).toHaveTextContent("Copiado")

    // Feedback is temporary.
    await flush(3000)
    expect(button).toHaveTextContent("Copiar")
    expect(button).not.toHaveTextContent("Copiado")
  })

  it("reports a copy failure when the clipboard rejects", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"))
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })
    await i18n.changeLanguage("en")
    getMock.mockResolvedValue(listing("ready"))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    const button = screen.getByRole("button", { name: "Copy command for T1059" })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(button).toHaveTextContent("Could not copy")
  })

  describe("resolved arguments (Req. 11.9, 11.10)", () => {
    function withCommand(extra: Record<string, unknown>): OperationalizationListing {
      return {
        campaign_key: "shadowray",
        atomic_status: { status: "ready", loaded_count: 10, expected_count: 10 },
        techniques: [
          {
            technique_id: "T1059",
            technique_name: "Command and Scripting Interpreter",
            commands: [
              {
                id: 1,
                command: "copy #{input_file} #{output_file}",
                executor: "powershell",
                platform: "windows",
                source: "atomic",
                ...extra,
              },
            ],
          },
        ],
      }
    }

    it("shows the resolved command instead of the original one", async () => {
      getMock.mockResolvedValue(
        withCommand({
          resolved_command: "copy a.txt #{output_file}",
          arguments_used: { input_file: "a.txt" },
          unresolved_arguments: ["output_file"],
        }),
      )
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      const block = screen.getByLabelText("Comando do Atomic Red Team")
      expect(block).toHaveTextContent("copy a.txt #{output_file}")
      expect(block.textContent).not.toContain("#{input_file}")
    })

    it("falls back to the original command when resolved_command is absent or empty", async () => {
      getMock.mockResolvedValue(withCommand({ resolved_command: "" }))
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      expect(screen.getByLabelText("Comando do Atomic Red Team")).toHaveTextContent(
        "copy #{input_file} #{output_file}",
      )
    })

    it("lists unresolved argument names in a localized warning", async () => {
      getMock.mockResolvedValue(
        withCommand({ unresolved_arguments: ["input_file", "output_file"] }),
      )
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      expect(screen.getByTestId("unresolved-arguments")).toHaveTextContent(
        "Argumentos não resolvidos (mantidos como marcadores): input_file, output_file",
      )

      await act(async () => {
        await i18n.changeLanguage("en")
      })
      expect(screen.getByTestId("unresolved-arguments")).toHaveTextContent(
        "Unresolved arguments (kept as placeholders): input_file, output_file",
      )
    })

    it("shows the arguments used in a collapsible details element", async () => {
      getMock.mockResolvedValue(
        withCommand({
          resolved_command: "copy a.txt b.txt",
          arguments_used: { input_file: "a.txt", output_file: "b.txt" },
          unresolved_arguments: [],
        }),
      )
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      const details = screen.getByTestId("arguments-used")
      expect(details.tagName).toBe("DETAILS")
      expect(within(details).getByText("Argumentos usados")).toBeInTheDocument()
      expect(details).toHaveTextContent("input_file:")
      expect(details).toHaveTextContent("a.txt")
      expect(details).toHaveTextContent("output_file:")
      expect(details).toHaveTextContent("b.txt")
      expect(screen.queryByTestId("unresolved-arguments")).toBeNull()
    })

    it("renders an old response without the new fields as before", async () => {
      getMock.mockResolvedValue(withCommand({}))
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      expect(screen.getByLabelText("Comando do Atomic Red Team")).toHaveTextContent(
        "copy #{input_file} #{output_file}",
      )
      expect(screen.queryByTestId("unresolved-arguments")).toBeNull()
      expect(screen.queryByTestId("arguments-used")).toBeNull()
    })

    it("copies the resolved command", async () => {
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })
      getMock.mockResolvedValue(withCommand({ resolved_command: "copy a.txt b.txt" }))
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      await act(async () => {
        fireEvent.click(screen.getByTestId("copy-command"))
      })
      expect(writeText).toHaveBeenCalledWith("copy a.txt b.txt")
    })
  })

  describe("example of the canonical template (Req. 12.14, 12.15, 12.16)", () => {
    const TEMPLATE = "copy #{input_file} $PathToAtomicsFolder/out.zip"
    const EXAMPLE = "copy a.txt $PathToAtomicsFolder/out.zip"

    function withTemplate(extra: Record<string, unknown>): OperationalizationListing {
      return {
        campaign_key: "shadowray",
        atomic_status: { status: "ready", loaded_count: 10, expected_count: 10 },
        techniques: [
          {
            technique_id: "T1059",
            technique_name: "Command and Scripting Interpreter",
            commands: [
              {
                id: 1,
                command: TEMPLATE,
                executor: "powershell",
                platform: "windows",
                source: "atomic",
                ...extra,
              },
            ],
          },
        ],
      }
    }

    it.each([
      ["pt-BR", "Exemplo com valores padrão do Atomic", "O agente adapta esses valores ao ambiente da campanha.", "Copiar exemplo de T1059", "Template original"],
      ["en", "Example with Atomic default values", "The agent adapts these values to the campaign environment.", "Copy example for T1059", "Original template"],
    ])(
      "labels the example and offers the original template in %s",
      async (language, label, hint, copyLabel, summary) => {
        await i18n.changeLanguage(language)
        getMock.mockResolvedValue(withTemplate({ resolved_command: EXAMPLE }))
        renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
        await flush()

        const exampleLabel = screen.getByTestId("example-label")
        expect(exampleLabel).toHaveTextContent(label)
        expect(exampleLabel).toHaveTextContent(hint)
        expect(screen.getByRole("button", { name: copyLabel })).toBeInTheDocument()

        const details = screen.getByTestId("template-details")
        expect(details.tagName).toBe("DETAILS")
        expect(within(details).getByText(summary)).toBeInTheDocument()
        expect(details).toHaveTextContent(TEMPLATE)
      },
    )

    it("copies the displayed example, not the template", async () => {
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true })
      getMock.mockResolvedValue(withTemplate({ resolved_command: EXAMPLE }))
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      expect(screen.getByLabelText("Comando do Atomic Red Team")).toHaveTextContent(EXAMPLE)
      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Copiar exemplo de T1059" }))
      })
      expect(writeText).toHaveBeenCalledWith(EXAMPLE)
      expect(screen.queryByRole("button", { name: "Copiar comando de T1059" })).toBeNull()
    })

    it.each(["pt-BR", "en"])(
      "shows no example label or template details when resolved_command equals command (%s)",
      async (language) => {
        await i18n.changeLanguage(language)
        getMock.mockResolvedValue(withTemplate({ resolved_command: TEMPLATE }))
        renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
        await flush()

        expect(screen.queryByTestId("example-label")).toBeNull()
        expect(screen.queryByTestId("template-details")).toBeNull()
        const copyName = language === "en" ? "Copy command for T1059" : "Copiar comando de T1059"
        expect(screen.getByRole("button", { name: copyName })).toBeInTheDocument()
      },
    )

    it("shows no example label for an old response without the new fields", async () => {
      getMock.mockResolvedValue(withTemplate({}))
      renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
      await flush()

      expect(screen.queryByTestId("example-label")).toBeNull()
      expect(screen.queryByTestId("template-details")).toBeNull()
    })

    it.each([
      ["pt-BR", "Argumentos não resolvidos (mantidos como marcadores): PathToAtomicsFolder"],
      ["en", "Unresolved arguments (kept as placeholders): PathToAtomicsFolder"],
    ])(
      "warns about PathToAtomicsFolder in unresolved_arguments in %s",
      async (language, warning) => {
        await i18n.changeLanguage(language)
        getMock.mockResolvedValue(
          withTemplate({ resolved_command: EXAMPLE, unresolved_arguments: ["PathToAtomicsFolder"] }),
        )
        renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
        await flush()

        expect(screen.getByTestId("unresolved-arguments")).toHaveTextContent(warning)
      },
    )
  })

  it("warns while not ready and rereads until it becomes ready", async () => {
    getMock
      .mockResolvedValueOnce(listing("running", 4))
      .mockResolvedValue(listing("ready", 10))
    renderWithI18n(<OperationalizationScreen version="18.0" campaignKey="shadowray" />)
    await flush()

    expect(screen.getByTestId("atomic-notice")).toBeInTheDocument()
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "40")
    expect(getMock).toHaveBeenCalledTimes(1)

    await flush(ATOMIC_POLL_INTERVAL_MS)
    expect(getMock).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId("atomic-notice")).toBeNull()

    // Ready: no further polling.
    await flush(ATOMIC_POLL_INTERVAL_MS * 2)
    expect(getMock).toHaveBeenCalledTimes(2)
  })
})
