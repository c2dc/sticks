import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { TerminalSquare } from "lucide-react"

import { StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import { connectStages } from "@/lib/campaigns"
import { getStage5Logs, type LogLineView } from "@/lib/stages"

/**
 * ProvisioningConsole — live log terminal of Stage 5 (Req. 2.1–2.5, 2.7).
 *
 * Shows, as a monospaced console, the log lines of the active environment
 * provisioning — the executed commands and their output (stdout/stderr) live
 * (Req. 2.1, 2.2). The data flow is dual and idempotent by `seq`:
 *
 * - **Recovery on mount (Req. 2.5, 2.7):** calls `getStage5Logs(campaignId)` to
 *   seed the current buffer — so reopening the screen (or a reload) does not
 *   lose the already-accumulated history of the in-progress provisioning.
 * - **Live subscription (Req. 2.2):** subscribes to the Stage 5 channel via
 *   `connectStages(campaignId, 5, …)`; each frame carries the **new** lines
 *   since the last frame in `logs` (and, on connect, the full buffer — which,
 *   together with the recovery above, keeps the history intact even after a
 *   reconnection — Req. 2.7).
 *
 * The lines are accumulated in a `Map<seq, LogLineView>` (dedupe/reorder by
 * `seq`) and **always** rendered in increasing `seq` order (Req. 2.3). Each
 * line shows a step label (Req. 2.4) and is colored by `stream` (Req. 2.4:
 * `stderr` → team-red, `comando` → accent/bold, `info` → muted,
 * `stdout` → default color). The console auto-scrolls to the latest line
 * whenever the lines change.
 */
export function ProvisioningConsole({
  campaignId,
  className,
}: {
  campaignId: string
  className?: string
}) {
  const { t } = useTranslation("pipeline")
  // Buffer accumulated by `seq` — idempotent dedupe/reorder (Req. 2.3).
  const [linesBySeq, setLinesBySeq] = useState<Map<number, LogLineView>>(
    () => new Map(),
  )
  const scrollRef = useRef<HTMLDivElement>(null)

  // Merge new lines into the buffer by `seq` (recovery + live frames).
  const accumulate = (newLines: readonly LogLineView[] | undefined) => {
    if (!newLines || newLines.length === 0) return
    setLinesBySeq((previous) => {
      const next = new Map(previous)
      for (const line of newLines) next.set(line.seq, line)
      return next
    })
  }

  useEffect(() => {
    let active = true
    // Reset the buffer when switching case.
    setLinesBySeq(new Map())

    // Recover the current buffer on mount (Req. 2.5, 2.7).
    getStage5Logs(campaignId)
      .then((response) => {
        if (active) accumulate(response.logs)
      })
      .catch(() => {
        // Recovery is best-effort: the live channel fills the full buffer on
        // connect anyway (Req. 2.7).
      })

    // Subscribe to the Stage 5 channel; each frame carries the new lines (Req. 2.2).
    const connection = connectStages(campaignId, 5, (frame) => {
      if (active) accumulate(frame.logs)
    })

    return () => {
      active = false
      connection.close()
    }
  }, [campaignId])

  // Lines always in increasing `seq` order (Req. 2.3).
  const lines = useMemo(
    () => Array.from(linesBySeq.values()).sort((a, b) => a.seq - b.seq),
    [linesBySeq],
  )

  // Auto-scroll to the latest line whenever the lines change.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [lines])

  return (
    <StageSection
      data-section="console"
      label={t("stage5.provisionamento.console.titulo")}
      kind="neutral"
      className={className}
    >
      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-label={t("stage5.provisionamento.console.titulo")}
        className="h-72 overflow-auto rounded-md border bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-100"
      >
        {lines.length === 0 ? (
          <p className="flex items-center gap-1.5 text-zinc-400">
            <TerminalSquare className="size-3.5" aria-hidden />
            {t("stage5.provisionamento.console.vazio")}
          </p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {lines.map((line) => (
              <ConsoleLine key={line.seq} line={line} />
            ))}
          </ul>
        )}
      </div>
    </StageSection>
  )
}

/** Color per line `stream` (Req. 2.4). `info` muted, `stdout` default. */
const STREAM_COLOR: Record<string, string> = {
  comando: "text-team-blue font-bold",
  stderr: "text-team-red",
  info: "text-zinc-400",
  stdout: "text-zinc-100",
}

/**
 * One console line: optional **sub-stage** badge ("5.1 Build/online" or "5.2
 * Runtime/isolado", from `line.substage` — stage5-target-provisioning Req. 6.3,
 * 6.4), the step badge (Req. 2.4, distinguishes the steps) and the text colored
 * by `stream` (Req. 2.4). Labels via i18n with a fallback to the raw
 * substage/step/stream string when the key does not exist yet. Rendering order
 * follows the increasing-`seq` order of the parent list, so sub-stage markers
 * appear in emission order.
 */
function ConsoleLine({ line }: { line: LogLineView }) {
  const { t } = useTranslation("pipeline")
  const streamColor = STREAM_COLOR[line.stream] ?? "text-zinc-100"
  const stepLabel = t(`stage5.provisionamento.console.etapas.${line.step}`, {
    defaultValue: line.step,
  })
  // Sub-stage marker — rendered only when the backend tagged the line (5.1/5.2).
  // The backend sends `"5.1"`/`"5.2"`; the i18n key avoids dots (`s51`/`s52`).
  const substageKey = line.substage ? `s${line.substage.replace(/\./g, "")}` : null
  const substageLabel =
    line.substage && substageKey
      ? t(`stage5.provisionamento.console.subetapas.${substageKey}`, {
          defaultValue: line.substage,
        })
      : null

  return (
    <li className="flex items-start gap-2">
      {substageLabel ? (
        <span
          data-substage={line.substage}
          className="mt-px shrink-0 rounded bg-team-blue/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-team-blue"
          title={substageLabel}
        >
          {substageLabel}
        </span>
      ) : null}
      <span
        className="mt-px shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-300"
        title={stepLabel}
      >
        {stepLabel}
      </span>
      <span className={cn("whitespace-pre-wrap break-all", streamColor)}>
        {line.text}
      </span>
    </li>
  )
}
