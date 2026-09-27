import { useEffect, useState } from "react"

import { AppLayout } from "@/components/AppLayout"
import { EmulationConfirmModal } from "@/components/EmulationConfirmModal"
import {
  PipelineOverview,
  type StageSelection,
} from "@/components/PipelineOverview"
import { PreferencesPanel } from "@/components/PreferencesPanel"
import { StageViewHost } from "@/components/StageViewHost"
import { ThemeProvider, type Theme } from "@/components/theme-provider"
import { applyLanguage } from "@/i18n"
import { getPreferencias, putPreferencias } from "@/lib/preferencias"

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

  // Theme preference (Req. 7). Starts at the default Modo_Claro (Req. 7.6);
  // on open we restore the last persisted theme, if any (Req. 7.5).
  const [theme, setTheme] = useState<Theme>("claro")

  useEffect(() => {
    let active = true
    getPreferencias()
      .then((prefs) => {
        if (!active) return
        // On first visit there is no persisted preference: keep Modo_Claro
        // (Req. 7.6). Otherwise apply the last persisted theme (Req. 7.5).
        if (prefs?.tema) setTheme(prefs.tema)
        // Restore the persisted Idioma and apply it in-place (Req. 8.5). With
        // no preference this defaults to pt-BR (Req. 8.6); an invalid persisted
        // value normalizes to pt-BR (Req. 8.7) — both via `applyLanguage`.
        void applyLanguage(prefs?.idioma)
      })
      .catch(() => {
        // Restore is best-effort: on failure we keep the defaults (Modo_Claro /
        // pt-BR) so the UI stays usable (Req. 7.6, 8.6).
      })
    return () => {
      active = false
    }
  }, [])

  // Persist the chosen theme through PUT /api/preferencias (Req. 7.4). This runs
  // only on an actual user change, forwarded by the ThemeProvider.
  const handleThemeChange = (next: Theme) => {
    setTheme(next)
    void putPreferencias({ tema: next }).catch(() => {
      // Persistence is best-effort here; the switch itself already applied
      // instantly (Req. 7.2) so the user is not blocked on the network.
    })
  }

  return (
    <ThemeProvider theme={theme} onThemeChange={handleThemeChange}>
      <AppLayout aside={<PreferencesPanel />}>
        <PipelineOverview
          selectedCaseId={selectedCaseId}
          onSelectCase={(casoId) => {
            setSelectedCaseId(casoId)
            setSelectedStage(null)
          }}
          selectedStage={selectedStage}
          onSelectStage={setSelectedStage}
        />
        {/* When a stage is selected, render its dedicated view (Req. 1.3). The
            Stage-3 emulation request opens the confirm modal below. */}
        <StageViewHost
          selection={selectedStage}
          onClose={() => setSelectedStage(null)}
          onRequestEmulation={setEmulationCaseId}
        />
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
