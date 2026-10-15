import { useEffect, useState } from "react"

import { AppLayout } from "@/components/AppLayout"
import { EmulationConfirmModal } from "@/components/EmulationConfirmModal"
import { ExecutionWizard } from "@/components/ExecutionWizard"
import { Landing } from "@/components/Landing"
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
  // Guided flow view state. The reducer in `lib/flow.ts` holds only the current
  // screen: Landing or the ExecutionWizard. Clicking "Start" goes straight into
  // the wizard at Stage 1 — base and campaign selection are stages inside the
  // wizard, not standalone screens.
  const { estado, enviar } = useAppFlow()

  // Emulation confirm modal state is owned here. The Stage 6 view asks to
  // emulate via the `onRequestEmulation` seam forwarded by the wizard, passing
  // the selected campaign id; App opens the modal for it. Closing returns to the
  // prior state without executing anything. It stays mounted at the App root.
  const [emulationCampaignId, setEmulationCampaignId] = useState<string | null>(
    null,
  )

  // Theme preference. Local storage is the source of truth so the choice
  // survives a reload instantly; on first visit it follows the OS preference.
  // Light is the last-resort default. The backend is secondary, cross-machine
  // persistence.
  const [theme, setTheme] = useState<Theme>(resolveInitialTheme)

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
        // Restore the persisted language and apply it in place; no preference
        // defaults to pt-BR, and an invalid persisted value normalizes to pt-BR.
        void applyLanguage(prefs?.idioma)
      })
      .catch(() => {
        // Restore is best-effort: on failure we keep the local/OS theme and the
        // pt-BR language default so the UI stays usable.
      })
    return () => {
      active = false
    }
  }, [])

  // Persist the chosen theme. The user's explicit choice is written locally
  // (instant, survives reload, wins over the OS from now on) and forwarded to
  // the backend for cross-machine persistence. Runs only on an actual user
  // change, forwarded by the ThemeProvider.
  const handleThemeChange = (next: Theme) => {
    setTheme(next)
    writeStoredTheme(next)
    void putPreferencias({ tema: next }).catch(() => {
      // Backend persistence is best-effort; the switch already applied
      // instantly and is remembered locally, so the user is not blocked on the
      // network.
    })
  }

  return (
    <ThemeProvider theme={theme} onThemeChange={handleThemeChange}>
      <AppLayout>{renderView()}</AppLayout>

      {/* Pre-confirmation command/target list + explicit confirmation gate.
          Mounted at the root, keyed by the selected campaign id. */}
      <EmulationConfirmModal
        casoId={emulationCampaignId}
        open={emulationCampaignId !== null}
        onClose={() => setEmulationCampaignId(null)}
      />
    </ThemeProvider>
  )

  function renderView() {
    switch (estado.view) {
      case "landing":
        return <Landing onIniciar={() => enviar({ tipo: "START" })} />

      case "wizard":
        // The wizard conducts all six stages, one screen per stage. It resolves
        // the base (Stage 1) and the campaign (Stage 2) internally; the chosen
        // campaign id then drives Stages 3..6. "Back" from Stage 1 returns to
        // the Landing.
        return (
          <ExecutionWizard
            onRequestEmulation={setEmulationCampaignId}
            onExitToStart={() => enviar({ tipo: "BACK" })}
          />
        )

      default:
        return null
    }
  }
}

export default App
