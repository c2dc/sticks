import { useTranslation } from "react-i18next"
import { ArrowLeft, ArrowRight, CheckCircle2, Package, Target } from "lucide-react"

import { BaseStixView } from "@/components/BaseStixView"
import { CampaignView } from "@/components/CampaignView"
import { OrchestratorAvatar } from "@/components/OrchestratorAvatar"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

/**
 * InputScreen — the single, unified selection screen of the AI-first flow.
 *
 * Minimalist by design: ONE compact page where the Researcher picks the STIX
 * package version (a small dropdown) and then the campaign (a simple list), with
 * a lightweight overview panel appearing once a campaign is selected. The
 * campaign step is revealed only after a base is ready, so the two choices read
 * as one short guided flow — not a page-filling grid of cards.
 */
export function InputScreen({
  selectedVersion,
  baseReady,
  onVersionSelected,
  onBaseCompletionChange,
  onCampaignSelected,
  campaignReady,
  onAdvanceToRun,
  onExitToStart,
}: {
  selectedVersion?: string
  baseReady: boolean
  onVersionSelected: (version: string) => void
  onBaseCompletionChange: (completed: boolean) => void
  /** Called with the chosen `campaign_key` once its overview has loaded. */
  onCampaignSelected: (campaignKey: string) => void
  /** True once a campaign is selected and its overview loaded. */
  campaignReady: boolean
  /** Explicit advance to the operationalization screen (the Next button). */
  onAdvanceToRun: () => void
  onExitToStart?: () => void
}) {
  const { t } = useTranslation("pipeline")

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-5" data-section="input-screen">
      {/* Compact hero — the robot introduces the two-in-one selection. */}
      <div className="flex items-center gap-3">
        <OrchestratorAvatar mood="talking" pulse className="size-11 shrink-0" />
        <div className="flex flex-col">
          <h1 className="text-lg font-bold tracking-tight">{t("input.heroTitle")}</h1>
          <p className="text-sm text-muted-foreground">{t("input.heroDescription")}</p>
        </div>
      </div>

      {/* Step 1 — STIX package / version: a single compact row. */}
      <StepRow
        index={1}
        icon={<Package className="size-4" aria-hidden />}
        title={t("input.card.packageTitle")}
        done={baseReady}
      >
        <BaseStixView
          version={selectedVersion}
          onVersionSelected={onVersionSelected}
          onCompletionChange={onBaseCompletionChange}
        />
      </StepRow>

      {/* Step 2 — campaign within the package. Revealed once the base is ready. */}
      <StepRow
        index={2}
        icon={<Target className="size-4" aria-hidden />}
        title={t("input.card.campaignTitle")}
        disabled={!baseReady}
        disabledHint={t("input.card.campaignBlocked")}
      >
        {baseReady && selectedVersion ? (
          <CampaignView version={selectedVersion} onCompleted={onCampaignSelected} />
        ) : null}
      </StepRow>

      {/* Footer — Back to Landing on the left; the explicit Next advance on
          the right, enabled only once a campaign is selected so the
          Researcher can review the campaign overview before proceeding. */}
      <div className="flex items-center justify-between border-t pt-3">
        <Button variant="outline" size="sm" onClick={onExitToStart} disabled={!onExitToStart}>
          <ArrowLeft className="size-4" aria-hidden />
          {t("input.back")}
        </Button>
        {campaignReady ? (
          <Button
            size="sm"
            onClick={onAdvanceToRun}
            data-testid="advance-to-run"
          >
            {t("input.next")}
            <ArrowRight className="size-4" aria-hidden />
          </Button>
        ) : (
          <p className="text-xs text-muted-foreground">{t("input.nextHint")}</p>
        )}
      </div>
    </div>
  )
}

/**
 * A compact, numbered step row: a small index badge + title on one line, with
 * the step body directly below. Dims to a locked state with a hint when
 * `disabled` (the campaign step before a base is ready).
 */
function StepRow({
  index,
  icon,
  title,
  done = false,
  doneLabel,
  disabled = false,
  disabledHint,
  children,
}: {
  index: number
  icon: React.ReactNode
  title: string
  done?: boolean
  doneLabel?: string
  disabled?: boolean
  disabledHint?: string
  children: React.ReactNode
}) {
  return (
    <section
      className={cn("flex flex-col gap-2 transition-opacity", disabled && "opacity-60")}
      data-section="input-panel"
      data-index={index}
      aria-disabled={disabled}
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "inline-flex size-5 items-center justify-center rounded-full border text-xs font-bold",
            done
              ? "border-team-blue bg-team-blue/10 text-team-blue"
              : "border-team-purple/50 bg-team-purple/10 text-team-purple",
          )}
        >
          {done ? <CheckCircle2 className="size-3" aria-hidden /> : index}
        </span>
        <span className="inline-flex items-center gap-1.5 text-sm font-semibold">
          {icon}
          {title}
        </span>
        {done && doneLabel ? (
          <span className="ml-auto text-xs font-medium text-team-blue">{doneLabel}</span>
        ) : null}
      </div>

      <div className="pl-7">
        {disabled ? (
          <p className="text-xs text-muted-foreground">{disabledHint}</p>
        ) : (
          children
        )}
      </div>
    </section>
  )
}
