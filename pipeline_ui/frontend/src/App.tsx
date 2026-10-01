import { useEffect, useState } from "react"

import { AppLayout } from "@/components/AppLayout"
import { EmulationConfirmModal } from "@/components/EmulationConfirmModal"
import {
  PipelineOverview,
  type StageSelection,
} from "@/components/PipelineOverview"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import { ThemeProvider, type Theme } from "@/components/theme-provider"
import { applyLanguage } from "@/i18n"
import { getPreferencias, putPreferencias } from "@/lib/preferencias"
import {
  readStoredTheme,
  resolveInitialTheme,
  systemPrefersDark,
  writeStoredTheme,
} from "@/lib/theme-storage"

function App() {
  // Selection seam wired here: the overview reports the active case and the
  // selected stage; the dedicated Stage1/2/3 views (task 12.2) render from
  // this state (Req. 1.3).
  const [selectedCaseId, setSelectedCaseId] = useState<string | undefined>(undefined)
  const [selectedStage, setSelectedStage] = useState<StageSelection | null>(null)

  // Emulation confirm modal state is owned here (task 12.4). Stage3View asks to
  // emulate via the `onRequestEmulation` seam forwarded by StageViewHost; App
  // opens the modal for that case. Closing returns to the prior state without
  // executing anything (Req. 6.6, 6.7).
  const [emulationCaseId, setEmulationCaseId] = useState<string | null>(null)

  // Theme preference (Req. 7). The local storage is the source of truth so the
  // choice survives a reload instantly and, on first visit, follows the OS
  // preference (prefers-color-scheme). Modo_Claro is only the last-resort
  // default (Req. 7.6). The backend is secondary, cross-machine persistence.
  const [theme, setTheme] = useState<Theme>(resolveInitialTheme)

  // Follow the OS theme live, but only while the user hasn't made an explicit
  // choice yet. Once they pick a theme (stored locally), we stop overriding it.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return
    const mql = window.matchMedia("(prefers-color-scheme: dark)")
    const onChange = () => {
      if (readStoredTheme() === null) {
        setTheme(systemPrefersDark() ? "escuro" : "claro")
      }
    }
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [])

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
        <PipelineOverview
          selectedCaseId={selectedCaseId}
          onSelectCase={(casoId) => {
            setSelectedCaseId(casoId)
            setSelectedStage(null)
          }}
          selectedStage={selectedStage}
          onSelectStage={setSelectedStage}
        />
        {/* Fluxo guiado (wizard): conduz os 3 estágios da campanha selecionada
            (Avançar → Avançar → Executar/Finish). O pedido de emulação do
            Estágio 3 abre o modal de confirmação abaixo. */}
        {selectedCaseId ? (
          <ExecutionWizard
            casoId={selectedCaseId}
            onRequestEmulation={setEmulationCaseId}
          />
        ) : null}
      </AppLayout>

      {/* Pre-confirmation command/target list + explicit confirmation gate
          (Req. 6.5, 6.6, 6.7, 6.2, 6.4, 4.6). */}
      <EmulationConfirmModal
        casoId={emulationCaseId}
        open={emulationCaseId !== null}
        onClose={() => setEmulationCaseId(null)}
      />
    </ThemeProvider>
  )
}

export default App
