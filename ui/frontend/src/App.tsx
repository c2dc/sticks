import { useEffect, useState } from "react"

import { AppLayout } from "@/components/AppLayout"
import { CampaignActorSelector } from "@/components/CampaignActorSelector"
import { EmulationConfirmModal } from "@/components/EmulationConfirmModal"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import { Landing } from "@/components/Landing"
import { StixBasesList } from "@/components/StixBasesList"
import { StixIntelligenceDashboard } from "@/components/StixIntelligenceDashboard"
import { ThemeProvider, type Theme } from "@/components/theme-provider"
import { applyLanguage } from "@/i18n"
import { useAppFlow } from "@/lib/flow"
import { getPreferencias, putPreferencias } from "@/lib/preferencias"
import {
  readStoredTheme,
  resolveInitialTheme,
  writeStoredTheme,
} from "@/lib/theme-storage"

function App() {
  // Linear STIX flow view state (task 17.2). The reducer in `lib/flow.ts` holds
  // the current screen plus the selections (base version, campaign) carried
  // across steps; `enviar` dispatches the pt-BR flow events (Req. 7.1–7.4, 9.3).
  const { estado, enviar } = useAppFlow()

  // Emulation confirm modal state is owned here (task 12.4). Stage3View asks to
  // emulate via the `onRequestEmulation` seam forwarded by the wizard; App opens
  // the modal for that case. Closing returns to the prior state without
  // executing anything (Req. 6.6, 6.7). It stays mounted at the App root.
  const [emulationCaseId, setEmulationCaseId] = useState<string | null>(null)

  // Theme preference (Req. 7). The local storage is the source of truth so the
  // choice survives a reload instantly and, on first visit, follows the OS
  // preference (prefers-color-scheme). Modo_Claro is only the last-resort
  // default (Req. 7.6). The backend is secondary, cross-machine persistence.
  const [theme, setTheme] = useState<Theme>(resolveInitialTheme)

  // Light (Modo_Claro) is the product default and is NOT overridden by the OS
  // preference. We intentionally do not follow `prefers-color-scheme`: without
  // an explicit user choice the app stays light. Once the user picks a theme it
  // is stored locally and respected.

  useEffect(() => {
    let active = true
    getPreferencias()
      .then((prefs) => {
        if (!active) return
        // The backend is cross-machine persistence. Only let it drive the theme
        // when the user hasn't made a local choice on this device yet, so a
        // stored local preference (or the OS default) is never clobbered by a
        // slower network read.
        if (prefs?.tema && readStoredTheme() === null) setTheme(prefs.tema)
        // Restore the persisted Idioma and apply it in-place (Req. 8.5). With
        // no preference this defaults to pt-BR (Req. 8.6); an invalid persisted
        // value normalizes to pt-BR (Req. 8.7) — both via `applyLanguage`.
        void applyLanguage(prefs?.idioma)
      })
      .catch(() => {
        // Restore is best-effort: on failure we keep the local/OS theme and the
        // pt-BR language default so the UI stays usable (Req. 7.6, 8.6).
      })
    return () => {
      active = false
    }
  }, [])

  // Persist the chosen theme. The user's explicit choice is written locally
  // (instant, survives reload, wins over the OS from now on) and forwarded to
  // the backend for cross-machine persistence (Req. 7.4). Runs only on an
  // actual user change, forwarded by the ThemeProvider.
  const handleThemeChange = (next: Theme) => {
    setTheme(next)
    writeStoredTheme(next)
    void putPreferencias({ tema: next }).catch(() => {
      // Backend persistence is best-effort; the switch already applied
      // instantly (Req. 7.2) and is remembered locally, so the user is not
      // blocked on the network.
    })
  }

  return (
    <ThemeProvider theme={theme} onThemeChange={handleThemeChange}>
      <AppLayout>
        {/* The six-screen linear flow is routed from `estado.view`. The reducer
            only reaches a view after the selection it depends on is recorded, so
            the required value is always present; the guards below keep this
            type-safe under tsc strict without non-null assertions, falling back
            to VOLTAR if an expected selection is somehow missing (Req. 7.1–7.4,
            9.3). */}
        {renderView()}
      </AppLayout>

      {/* Pre-confirmation command/target list + explicit confirmation gate
          (Req. 6.5, 6.6, 6.7, 6.2, 6.4, 4.6). Mounted at the root, unchanged. */}
      <EmulationConfirmModal
        casoId={emulationCaseId}
        open={emulationCaseId !== null}
        onClose={() => setEmulationCaseId(null)}
      />
    </ThemeProvider>
  )

  function renderView() {
    switch (estado.view) {
      case "landing":
        return <Landing onIniciar={() => enviar({ tipo: "INICIAR" })} />

      case "bases":
        return (
          <StixBasesList
            onSelecionarBase={(versao) =>
              enviar({ tipo: "SELECIONAR_BASE", versao })
            }
          />
        )

      case "dashboard": {
        const versao = estado.versaoSelecionada
        if (!versao) {
          enviar({ tipo: "VOLTAR" })
          return null
        }
        return (
          <StixIntelligenceDashboard
            versao={versao}
            onIrParaSelecao={() => enviar({ tipo: "IR_PARA_SELECAO" })}
          />
        )
      }

      case "selecao": {
        const versao = estado.versaoSelecionada
        if (!versao) {
          enviar({ tipo: "VOLTAR" })
          return null
        }
        return (
          <CampaignActorSelector
            versao={versao}
            onSelecionarCampanha={(campanha) =>
              enviar({ tipo: "SELECIONAR_CAMPANHA", campanha })
            }
          />
        )
      }

      case "wizard": {
        // The DAG generation is now Estágio 1 inside the ExecutionWizard
        // (DagStageView moves into the wizard — task 11.x). The advance gating
        // lives inside the wizard, derived from the backend.
        const slug = estado.campanhaSelecionada?.slug_curado
        if (!slug) {
          enviar({ tipo: "VOLTAR" })
          return null
        }
        return (
          <ExecutionWizard
            casoId={slug}
            versao={estado.versaoSelecionada}
            onRequestEmulation={setEmulationCaseId}
            onVoltarAntesDoEstagio1={() => enviar({ tipo: "VOLTAR" })}
          />
        )
      }

      default:
        return null
    }
  }
}

export default App
