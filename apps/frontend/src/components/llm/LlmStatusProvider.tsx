import * as React from "react"

import {
  deriveLlmState,
  getLlmProfiles,
  runLlmPreflight,
  selectLlmProfile,
  type LlmProfile,
  type LlmState,
  type PreflightResult,
} from "@/lib/llm"
import { useAsyncData } from "@/lib/use-async-data"

interface LlmStatusContextValue {
  profiles: LlmProfile[]
  /** Null until the profiles query settles. */
  selectedId: string | null
  localDiscovery: "ok" | "ollama_down" | null
  preflight: PreflightResult | null
  /** True when the Preflight query itself failed (distinct from a `blocked` result). */
  preflightError: boolean
  state: LlmState
  rechecking: boolean
  panelOpen: boolean
  openPanel: () => void
  closePanel: () => void
  /** Persists the choice, then reloads the profiles and the Preflight. Failures keep the old selection. */
  select: (profileId: string) => Promise<void>
  /** Runs the Preflight again, ignoring the backend cache. */
  recheck: () => void
}

const LlmStatusContext = React.createContext<LlmStatusContextValue | undefined>(undefined)

/** While the model is blocked or unknown, check again this often (milliseconds). */
const AUTO_RECHECK_INTERVAL_MS = 12_000

export function LlmStatusProvider({ children }: { children: React.ReactNode }) {
  const profilesData = useAsyncData("llm-profiles", getLlmProfiles)
  const selectedId = profilesData.data?.selected_id ?? null

  const [nonce, setNonce] = React.useState(0)
  const [panelOpen, setPanelOpen] = React.useState(false)
  const forceRef = React.useRef(false)

  const preflightData = useAsyncData(
    selectedId === null ? null : JSON.stringify([selectedId, nonce]),
    () => {
      const force = forceRef.current
      forceRef.current = false
      return runLlmPreflight(selectedId, force)
    },
  )

  const reloadProfiles = profilesData.reload
  const select = React.useCallback(
    async (profileId: string) => {
      try {
        await selectLlmProfile(profileId)
      } catch {
        // Unknown id or network failure: the previous selection stays.
      }
      reloadProfiles()
    },
    [reloadProfiles],
  )

  const recheck = React.useCallback(() => {
    forceRef.current = true
    setNonce((n) => n + 1)
  }, [])
  const openPanel = React.useCallback(() => setPanelOpen(true), [])
  const closePanel = React.useCallback(() => setPanelOpen(false), [])

  const state = deriveLlmState(
    preflightData.data,
    profilesData.loading || preflightData.loading,
    profilesData.error ?? preflightData.error,
  )

  // Notice on its own when a blocked model becomes reachable (for example the VPN
  // connects): re-run the Preflight in the background, without flipping the icon
  // to "checking". It also runs when the user returns to the window.
  const reloadPreflight = preflightData.reload
  const needsRecheck = state === "blocked" || state === "unknown"
  React.useEffect(() => {
    if (!needsRecheck) return
    const run = () => {
      if (document.hidden) return
      forceRef.current = true
      reloadPreflight()
    }
    const timer = window.setInterval(run, AUTO_RECHECK_INTERVAL_MS)
    window.addEventListener("focus", run)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener("focus", run)
    }
  }, [needsRecheck, reloadPreflight])

  const value: LlmStatusContextValue = {
    profiles: profilesData.data?.profiles ?? [],
    selectedId,
    localDiscovery: profilesData.data?.local_discovery ?? null,
    preflight: preflightData.data,
    preflightError: Boolean(profilesData.error ?? preflightData.error),
    state,
    rechecking: preflightData.loading,
    panelOpen,
    openPanel,
    closePanel,
    select,
    recheck,
  }

  return <LlmStatusContext.Provider value={value}>{children}</LlmStatusContext.Provider>
}

export function useLlmStatus(): LlmStatusContextValue {
  const context = React.useContext(LlmStatusContext)
  if (!context) {
    throw new Error("useLlmStatus must be used within an LlmStatusProvider")
  }
  return context
}
