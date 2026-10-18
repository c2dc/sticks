import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2 } from "lucide-react"

import { StageSection } from "@/components/StageSection"
import { StixBasesList } from "@/components/StixBasesList"
import { StixIntelligenceDashboard } from "@/components/StixIntelligenceDashboard"
import { Button } from "@/components/ui/button"
import { getStageBaseStix, type Stage1ResultStix } from "@/lib/stages"
import { cn } from "@/lib/utils"

export interface BaseStixViewProps {
  /** Campaign slug; used to resolve the Stage 1 result. */
  campaignId: string
  /**
   * STIX base version already selected in the flow (controlled by the wizard —
   * task 14.4). When absent, the screen shows the base list for the Researcher
   * to choose from. When present, it shows the bundle dashboard.
   */
  version?: string
  /**
   * Called when the Researcher picks a base from the list (Req. 5.1). The wizard
   * stores the selection and passes it back via `version`, so that the Stage 1
   * Output (bundle + dashboard) becomes the Stage 2 Input (Req. 2.2).
   */
  onVersionSelected?: (version: string) => void
  /**
   * Reported whenever the Stage 1 completion verdict is recomputed (Req. 5.8,
   * 5.10). `true` **only when** the selected version's bundle is on disk **and**
   * the dashboard was shown; the wizard uses this to enable the "Next" control.
   * Any failure keeps the stage not completed (`false`), with the cause reported
   * on screen (Req. 5.9).
   */
  onCompletionChange?: (completed: boolean) => void
}

/**
 * BaseStixView — body of **Stage 1 (STIX base selection)** of the six-stage
 * pipeline (Req. 4.1, 5). It is not a standalone routed phase: navigation
 * (Next/Back) and the sequential gate live in the `ExecutionWizard` (task 14.4)
 * and in the backend. Here we drive the base selection and show the bundle
 * dashboard, reusing the screens of the `fluxo-stix-dashboard` flow:
 *
 * - **No version selected** → {@link StixBasesList}: lists the known versions
 *   (none downloaded by default), downloads the missing version from the
 *   official MITRE source and lets the user select a base already on disk
 *   (Req. 5.1–5.4). Picking a base fires {@link BaseStixViewProps.onVersionSelected}.
 * - **Version selected** → {@link StixIntelligenceDashboard}: shows the bundle
 *   dashboard (per-type counts) of the base on disk (Req. 5.7, 5.8), with a
 *   "Change base" action that returns to the list.
 *
 * In parallel, it reads `getStageBaseStix(campaignId, version)` to obtain the
 * typed completion verdict ({@link Stage1ResultStix}): Stage 1 can only be
 * completed when `bundle_on_disk` and the dashboard were assembled — reflected
 * by `completed`. That verdict is propagated via
 * {@link BaseStixViewProps.onCompletionChange} so the wizard can enable "Next"
 * (Req. 5.8, 5.10). A download/read failure keeps the stage not completed and
 * shows the cause (Req. 5.9).
 */
export function BaseStixView({
  campaignId,
  version,
  onVersionSelected,
  onCompletionChange,
}: BaseStixViewProps) {
  const { t } = useTranslation("pipeline")

  const [result, setResult] = useState<Stage1ResultStix | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [checking, setChecking] = useState(false)

  // Keep the last reported completion so we only notify the wizard on real
  // changes (avoids render loops when the callback is recreated each render).
  const lastCompletion = useRef<boolean | null>(null)
  const notifyCompletion = useCallback(
    (completed: boolean) => {
      if (lastCompletion.current === completed) return
      lastCompletion.current = completed
      onCompletionChange?.(completed)
    },
    [onCompletionChange],
  )

  // Resolve the completion verdict whenever the selected version changes. With
  // no version there is no base on disk to confirm → stage not completed.
  useEffect(() => {
    let active = true
    setLoadError(null)
    setResult(null)

    if (!version) {
      setChecking(false)
      notifyCompletion(false)
      return
    }

    setChecking(true)
    getStageBaseStix(campaignId, version)
      .then((data) => {
        if (!active) return
        setResult(data)
        notifyCompletion(data.completed)
      })
      .catch(() => {
        if (!active) return
        setLoadError(t("baseStix.erro"))
        notifyCompletion(false)
      })
      .finally(() => {
        if (active) setChecking(false)
      })

    return () => {
      active = false
    }
  }, [campaignId, version, t, notifyCompletion])

  return (
    <div className="flex flex-col gap-4">
      <StageSection
        data-section="base-stix"
        label={t("baseStix.titulo")}
        kind="blue"
        description={t("baseStix.descricao")}
      >
        {!version ? (
          // No base selected: list + download (Req. 5.1–5.4).
          <StixBasesList
            onSelectBase={(v) => onVersionSelected?.(v)}
          />
        ) : (
          // Base selected: bundle dashboard (Req. 5.7, 5.8).
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-muted-foreground">
                {t("baseStix.selecionada", { versao: version })}
              </p>
              {onVersionSelected ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onVersionSelected("")}
                >
                  {t("baseStix.trocarBase")}
                </Button>
              ) : null}
            </div>

            {/* Embedded in Stage 1: the wizard owns navigation, so the
                dashboard's own "select campaign/actor" CTA is hidden. */}
            <StixIntelligenceDashboard version={version} hideCta />
          </div>
        )}
      </StageSection>

      {/* Stage 1 completion verdict (Req. 5.8, 5.10): only ready when the bundle
          is on disk and the dashboard was shown. */}
      <CompletionBanner
        checking={checking}
        loadError={loadError}
        result={result}
      />
    </div>
  )
}

/**
 * Stage 1 completion-state banner. Shows:
 * - the failure cause when the read/validation failed (`loadError` or
 *   `result.error_message`), keeping the stage not completed (Req. 5.9);
 * - the "checking" note while the verdict is being resolved;
 * - the "ready" indicator when `completed` (bundle on disk + dashboard —
 *   Req. 5.8, 5.10);
 * - the "waiting" note otherwise (e.g. no base selected).
 */
function CompletionBanner({
  checking,
  loadError,
  result,
}: {
  checking: boolean
  loadError: string | null
  result: Stage1ResultStix | null
}) {
  const { t } = useTranslation("pipeline")

  const error = loadError ?? result?.error_message ?? null
  if (error) {
    return (
      <p
        className="rounded-md border border-team-red/40 bg-team-red/10 px-3 py-2 text-sm text-team-red"
        role="alert"
        data-section="conclusao"
      >
        {error}
      </p>
    )
  }

  if (checking) {
    return (
      <p
        className="text-sm text-muted-foreground"
        role="status"
        data-section="conclusao"
      >
        {t("baseStix.verificando")}
      </p>
    )
  }

  const completed = result?.completed ?? false
  return (
    <p
      className={cn(
        "flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm",
        completed
          ? "border-team-blue/40 bg-team-blue/10 text-team-blue"
          : "text-muted-foreground",
      )}
      role="status"
      data-section="conclusao"
      data-concluido={completed}
    >
      {completed ? <CheckCircle2 className="size-4" aria-hidden /> : null}
      {completed ? t("baseStix.pronto") : t("baseStix.aguardando")}
    </p>
  )
}
