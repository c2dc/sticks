import { useCallback, useEffect, useRef, useState } from "react"

import {
  abortAiRun,
  approveAiPhase,
  getActiveAiRun,
  getAiRun,
  regenerateAiFlow,
  selectAiFlowProposal,
  startAiRun,
  type AiRun,
  type ApprovableAiPhase,
} from "@/lib/ai"
import { ApiError } from "@/lib/api"

/** Interval between run rereads while a phase is running in the background. */
export const AI_RUN_POLL_INTERVAL_MS = 3000

export interface UseAiRun {
  /** Current run (running, awaiting approval or finished), or `null` when there is none. */
  run: AiRun | null
  /** True until the first restore attempt settles. */
  loading: boolean
  /** True while an action (start, approve, ...) is in flight. */
  busy: boolean
  /** Last error from restoring, polling or an action, or `null`. */
  error: unknown
  /** Rereads the run from the backend. */
  reload: () => Promise<void>
  start: (maxIterations?: number) => Promise<AiRun | null>
  approve: (phase: ApprovableAiPhase) => Promise<AiRun | null>
  regenerateFlow: () => Promise<AiRun | null>
  selectFlowProposal: (proposalId: number) => Promise<AiRun | null>
  abort: () => Promise<AiRun | null>
}

/**
 * State of the interactive AI run of a campaign. Restores the active run on mount,
 * polls every 3s while it is `running`, and stops at `awaiting_approval` and terminal
 * states. Any 409 from an action or from polling reloads the run, since its state
 * changed concurrently. A `null` campaign disables the hook.
 */
export function useAiRun(campaign: string | null): UseAiRun {
  const [run, setRun] = useState<AiRun | null>(null)
  const [settledCampaign, setSettledCampaign] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)

  const runRef = useRef<AiRun | null>(null)
  const campaignRef = useRef(campaign)
  useEffect(() => {
    runRef.current = run
    campaignRef.current = campaign
  })

  /** Reads the freshest run: the active one, else the last known run by id (now finished). */
  const fetchRun = useCallback(async (name: string): Promise<AiRun | null> => {
    const active = await getActiveAiRun(name)
    if (active) return active
    const known = runRef.current
    if (known && known.campaign_id === name) return (await getAiRun(name, known.id)).run
    return null
  }, [])

  const reload = useCallback(async () => {
    if (campaign === null) return
    try {
      const next = await fetchRun(campaign)
      if (campaignRef.current !== campaign) return
      setRun(next)
      setError(null)
    } catch (e) {
      if (campaignRef.current === campaign) setError(e)
    }
  }, [campaign, fetchRun])

  // Restore on mount / campaign change.
  useEffect(() => {
    if (campaign === null) return
    let active = true
    getActiveAiRun(campaign).then(
      (next) => {
        if (!active) return
        setRun(next)
        setError(null)
        setSettledCampaign(campaign)
      },
      (e: unknown) => {
        if (!active) return
        setError(e)
        setSettledCampaign(campaign)
      },
    )
    return () => {
      active = false
    }
  }, [campaign])

  // Poll only while running.
  const running = run?.status === "running"
  const runId = run?.id
  useEffect(() => {
    if (campaign === null || !running || runId === undefined) return
    let active = true
    const timer = setInterval(() => {
      getAiRun(campaign, runId).then(
        (detail) => {
          if (!active) return
          setRun(detail.run)
          setError(null)
        },
        (e: unknown) => {
          if (!active) return
          if (e instanceof ApiError && e.status === 409) void reload()
          else setError(e)
        },
      )
    }, AI_RUN_POLL_INTERVAL_MS)
    return () => {
      active = false
      clearInterval(timer)
    }
  }, [campaign, running, runId, reload])

  const perform = useCallback(
    async (action: (name: string, current: AiRun | null) => Promise<AiRun>): Promise<AiRun | null> => {
      if (campaign === null) return null
      setBusy(true)
      try {
        const next = await action(campaign, runRef.current)
        setRun(next)
        setError(null)
        return next
      } catch (e) {
        setError(e)
        if (e instanceof ApiError && e.status === 409) await reload()
        return null
      } finally {
        setBusy(false)
      }
    },
    [campaign, reload],
  )

  const requireRun = (current: AiRun | null): AiRun => {
    if (!current) throw new Error("No active AI run")
    return current
  }

  const start = useCallback(
    (maxIterations?: number) => perform((name) => startAiRun(name, maxIterations)),
    [perform],
  )
  const approve = useCallback(
    (phase: ApprovableAiPhase) =>
      perform((name, current) => approveAiPhase(name, requireRun(current).id, phase)),
    [perform],
  )
  const regenerateFlow = useCallback(
    () => perform((name, current) => regenerateAiFlow(name, requireRun(current).id)),
    [perform],
  )
  const selectFlowProposal = useCallback(
    (proposalId: number) =>
      perform((name, current) => selectAiFlowProposal(name, requireRun(current).id, proposalId)),
    [perform],
  )
  const abort = useCallback(
    () => perform((name, current) => abortAiRun(name, requireRun(current).id)),
    [perform],
  )

  return {
    run: campaign !== null && settledCampaign === campaign ? run : null,
    loading: campaign !== null && settledCampaign !== campaign,
    busy,
    error,
    reload,
    start,
    approve,
    regenerateFlow,
    selectFlowProposal,
    abort,
  }
}
