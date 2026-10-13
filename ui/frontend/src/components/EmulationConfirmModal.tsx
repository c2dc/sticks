import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertTriangle, Loader2, ServerCrash, ShieldAlert } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { cn } from "@/lib/utils"
import {
  classifyEmulacaoError,
  getEmulacaoPreview,
  runEmulacao,
  type EmulacaoErroOutcome,
  type EmulacaoPreview,
  type EmulacaoResultado,
} from "@/lib/emulacao"

/**
 * EmulationConfirmModal — the pre-confirmation gate for Stage-3 emulation
 * (Task 12.4, Req. 6.5, 6.6, 6.7, 6.2, 6.4, 4.6).
 *
 * Flow:
 * 1. On open it calls the preview endpoint and renders the COMPLETE list of
 *    concrete commands and the target container(s) of each — BEFORE any
 *    confirmation (Req. 6.5). External-destination rows are flagged red because
 *    they would cause the run to be refused (Req. 6.2).
 * 2. Running requires an EXPLICIT click on "Confirmar execução"; there is no
 *    pre-checked box and no auto-run. It is the only path that calls the run
 *    endpoint with `confirmado=true` (Req. 6.6).
 * 3. Cancel / close / Escape / backdrop just close the modal and return to the
 *    prior state — they NEVER call the run endpoint (Req. 6.7).
 * 4. On explicit confirm it POSTs `/emulacao` and maps outcomes: containment
 *    violation (409, Req. 6.2), isolation failure (409, Req. 6.4), Caldera
 *    unavailable (503, Req. 4.6), or the aggregate success result (Req. 4.5).
 *
 * The parent (App via StageViewHost) owns the open state and passes the target
 * `casoId`; `onClose` returns to the prior state and `onCompleted` lets the
 * parent refresh Stage-3 results after a run started.
 */
export function EmulationConfirmModal({
  casoId,
  open,
  onClose,
  onCompleted,
}: {
  /** The case to emulate; null when there is no active request. */
  casoId: string | null
  open: boolean
  /** Return to the prior state without executing anything (Req. 6.7). */
  onClose: () => void
  /** Called after a run actually started, so the parent can refresh Stage 3. */
  onCompleted?: (result: EmulacaoResultado) => void
}) {
  const { t } = useTranslation("pipeline")

  const [preview, setPreview] = useState<EmulacaoPreview | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [loadingPreview, setLoadingPreview] = useState(false)

  const [submitting, setSubmitting] = useState(false)
  const [runError, setRunError] = useState<EmulacaoErroOutcome | null>(null)
  const [result, setResult] = useState<EmulacaoResultado | null>(null)

  // Load the preview whenever the modal opens for a case (Req. 6.5). Resets any
  // prior run/error state so each open starts clean.
  useEffect(() => {
    if (!open || !casoId) return
    let active = true

    setPreview(null)
    setPreviewError(null)
    setRunError(null)
    setResult(null)
    setLoadingPreview(true)

    getEmulacaoPreview(casoId)
      .then((data) => {
        if (active) setPreview(data)
      })
      .catch(() => {
        if (active) setPreviewError(t("emulation.previewError"))
      })
      .finally(() => {
        if (active) setLoadingPreview(false)
      })

    return () => {
      active = false
    }
  }, [open, casoId, t])

  // Explicit confirmation — the ONLY call to the run endpoint (Req. 6.6).
  async function handleConfirm() {
    if (!casoId || submitting) return
    setSubmitting(true)
    setRunError(null)
    setResult(null)
    try {
      const res = await runEmulacao(casoId, true)
      setResult(res)
      if (res.iniciada) onCompleted?.(res)
    } catch (error) {
      setRunError(classifyEmulacaoError(error))
    } finally {
      setSubmitting(false)
    }
  }

  // Cancel/close returns to the prior state WITHOUT calling the run endpoint
  // (Req. 6.7). Ignored while a request is in flight to avoid a dangling run.
  function handleOpenChange(next: boolean) {
    if (next) return
    if (submitting) return
    onClose()
  }

  if (!casoId) return null

  const hasExternal = preview?.tem_externo ?? false
  // A run cannot proceed while loading, on preview error, or when the preview
  // already shows an external destination (it would be refused — Req. 6.2).
  const confirmDisabled =
    loadingPreview || Boolean(previewError) || submitting || hasExternal

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogClose label={t("emulation.close")} />
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-team-red">
            <ShieldAlert className="size-5" aria-hidden="true" />
            {t("emulation.title")}
          </DialogTitle>
          <DialogDescription>{t("emulation.description")}</DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
          {loadingPreview ? (
            <p className="flex items-center gap-2 text-sm text-muted-foreground" role="status">
              <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              {t("emulation.previewLoading")}
            </p>
          ) : previewError ? (
            <p className="rounded-md border border-team-red/40 bg-team-red/5 p-3 text-sm text-team-red">
              {previewError}
            </p>
          ) : result ? (
            <RunResult result={result} />
          ) : runError ? (
            <RunErrorMessage outcome={runError} />
          ) : preview ? (
            <PreviewList preview={preview} />
          ) : null}
        </div>

        <DialogFooter>
          {result?.iniciada ? (
            <Button variant="default" onClick={onClose}>
              {t("emulation.done")}
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={onClose} disabled={submitting}>
                {t("emulation.cancel")}
              </Button>
              <Button
                variant="destructive"
                onClick={handleConfirm}
                disabled={confirmDisabled}
                aria-disabled={confirmDisabled}
              >
                {submitting ? (
                  <>
                    <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                    {t("emulation.running")}
                  </>
                ) : (
                  t("emulation.confirm")
                )}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/**
 * The pre-confirmation command list: every concrete command with its target
 * container(s) and resolved destinations (Req. 6.5). External-destination rows
 * are flagged red (Req. 6.2).
 */
function PreviewList({ preview }: { preview: EmulacaoPreview }) {
  const { t } = useTranslation("pipeline")

  if (preview.abilities.length === 0) {
    return (
      <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        {t("emulation.noCommands")}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      {preview.tem_externo ? (
        <p className="flex items-start gap-2 rounded-md border border-team-red/40 bg-team-red/5 p-3 text-sm text-team-red">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <span>{t("emulation.externalWarning")}</span>
        </p>
      ) : null}

      {preview.abilities.map((ability) => (
        <section
          key={ability.ability_id}
          className={cn(
            "rounded-lg border p-3",
            ability.tem_externo && "border-team-red/40 bg-team-red/5",
          )}
        >
          <header className="mb-2 flex items-center justify-between gap-2">
            <div className="min-w-0">
              <h3 className="truncate text-sm font-semibold">
                {ability.ability_name}
              </h3>
              <p className="truncate text-xs text-muted-foreground">
                {ability.ability_id}
              </p>
            </div>
            {ability.tem_externo ? (
              <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-team-red/10 px-2 py-0.5 text-xs text-team-red">
                <AlertTriangle className="size-3" aria-hidden="true" />
                {t("emulation.externalBadge")}
              </span>
            ) : null}
          </header>

          <ul className="flex flex-col gap-2">
            {ability.comandos.map((comando, index) => (
              <li
                key={`${ability.ability_id}-${index}`}
                className={cn(
                  "rounded-md border p-2",
                  comando.tem_externo && "border-team-red/40",
                )}
              >
                <code className="block max-h-24 overflow-auto whitespace-pre-wrap break-all rounded bg-muted px-1.5 py-1 text-xs">
                  {comando.comando}
                </code>

                <div className="mt-2 flex flex-col gap-1 text-xs">
                  {/* Target container(s) of this command (Req. 6.5). */}
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="font-medium text-muted-foreground">
                      {t("emulation.targets")}:
                    </span>
                    {comando.alvos.length > 0 ? (
                      comando.alvos.map((alvo) => (
                        <span
                          key={alvo}
                          className="rounded-md border border-team-blue/30 bg-team-blue/5 px-1.5 py-0.5 text-team-blue"
                        >
                          {alvo}
                        </span>
                      ))
                    ) : (
                      <span className="text-muted-foreground">
                        {comando.local
                          ? t("emulation.localCommand")
                          : t("emulation.noTarget")}
                      </span>
                    )}
                  </div>

                  {/* Resolved destinations; external ones flagged red (Req. 6.2). */}
                  {comando.destinos.length > 0 ? (
                    <div className="flex flex-wrap items-center gap-1">
                      <span className="font-medium text-muted-foreground">
                        {t("emulation.destinations")}:
                      </span>
                      {comando.destinos.map((destino, di) => (
                        <span
                          key={`${destino.valor}-${di}`}
                          className={cn(
                            "rounded-md px-1.5 py-0.5",
                            destino.externo
                              ? "bg-team-red/10 text-team-red"
                              : "bg-muted text-muted-foreground",
                          )}
                          title={
                            destino.externo
                              ? t("emulation.destinationExternal")
                              : t("emulation.destinationInternal")
                          }
                        >
                          {destino.valor}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}

/** Renders a classified run failure: containment / isolation / unavailable. */
function RunErrorMessage({ outcome }: { outcome: EmulacaoErroOutcome }) {
  const { t } = useTranslation("pipeline")

  if (outcome.kind === "contencao_recusada") {
    return (
      <div className="flex flex-col gap-2 rounded-md border border-team-red/40 bg-team-red/5 p-3 text-sm text-team-red">
        <p className="flex items-center gap-2 font-medium">
          <ShieldAlert className="size-4" aria-hidden="true" />
          {t("emulation.error.contencao.title")}
        </p>
        <p>{outcome.mensagem ?? t("emulation.error.contencao.body")}</p>
        {outcome.violacoes.length > 0 ? (
          <ul className="flex flex-col gap-1.5">
            {outcome.violacoes.map((v, index) => (
              <li key={index} className="rounded-md border border-team-red/30 p-2">
                {v.ability_name || v.ability_id ? (
                  <p className="font-medium">
                    {v.ability_name ?? v.ability_id}
                    {v.ability_name && v.ability_id ? (
                      <span className="ml-1 font-normal opacity-70">
                        ({v.ability_id})
                      </span>
                    ) : null}
                  </p>
                ) : null}
                {v.comando ? (
                  <code className="mt-1 block whitespace-pre-wrap break-all rounded bg-team-red/10 px-1.5 py-1 text-xs">
                    {v.comando}
                  </code>
                ) : null}
                {v.destino ? (
                  <p className="mt-1 text-xs">
                    {t("emulation.error.contencao.destination", { destino: v.destino })}
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    )
  }

  if (outcome.kind === "isolamento_falhou") {
    return (
      <div className="flex flex-col gap-2 rounded-md border border-team-red/40 bg-team-red/5 p-3 text-sm text-team-red">
        <p className="flex items-center gap-2 font-medium">
          <ShieldAlert className="size-4" aria-hidden="true" />
          {t("emulation.error.isolamento.title")}
        </p>
        <p>{outcome.mensagem ?? t("emulation.error.isolamento.body")}</p>
        {outcome.containers.length > 0 ? (
          <ul className="flex flex-wrap gap-1.5">
            {outcome.containers.map((container) => (
              <li
                key={container}
                className="rounded-md border border-team-red/30 bg-team-red/10 px-2 py-0.5 text-xs"
              >
                {container}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    )
  }

  if (outcome.kind === "caldera_indisponivel") {
    return (
      <div className="flex flex-col gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm text-amber-600 dark:text-amber-400">
        <p className="flex items-center gap-2 font-medium">
          <ServerCrash className="size-4" aria-hidden="true" />
          {t("emulation.error.indisponivel.title")}
        </p>
        <p>{outcome.mensagem ?? t("emulation.error.indisponivel.body")}</p>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-team-red/40 bg-team-red/5 p-3 text-sm text-team-red">
      <p className="flex items-center gap-2 font-medium">
        <AlertTriangle className="size-4" aria-hidden="true" />
        {t("emulation.error.unknown.title")}
      </p>
      <p>{outcome.mensagem ?? t("emulation.error.unknown.body")}</p>
    </div>
  )
}

/** The aggregate success result shown after a run started (Req. 4.5). */
function RunResult({ result }: { result: EmulacaoResultado }) {
  const { t } = useTranslation("pipeline")

  if (!result.iniciada) {
    return (
      <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
        {result.mensagem || t("emulation.notStarted")}
      </p>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm">{result.mensagem || t("emulation.startedBody")}</p>
      <div className="flex items-center gap-4 text-sm">
        <span className="inline-flex items-center gap-1 text-team-blue">
          {t("stage3.saida.success", { count: result.total_sucesso })}
        </span>
        <span className="inline-flex items-center gap-1 text-team-red">
          {t("stage3.saida.failure", { count: result.total_falha })}
        </span>
      </div>
      {result.resultados.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {result.resultados.map((r) => (
            <li
              key={r.ability_id}
              className="flex items-center justify-between gap-2 rounded-md border p-2 text-sm"
            >
              <span className="min-w-0 flex-1 truncate">{r.ability_id}</span>
              <span
                className={cn(
                  "shrink-0 rounded-full px-2 py-0.5 text-xs",
                  r.status === "sucesso" && "bg-team-blue/10 text-team-blue",
                  r.status === "falha" && "bg-team-red/10 text-team-red",
                  (r.status === "pendente" || r.status === "em_execucao") &&
                    "bg-muted text-muted-foreground",
                )}
              >
                {t(`stage3.status.${r.status}`)}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
