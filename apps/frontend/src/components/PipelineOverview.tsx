import { useCallback, useEffect, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ProgressBar } from "@/components/ProgressBar"
import { StageStateBadge } from "@/components/StageStateBadge"
import { cn } from "@/lib/utils"
import {
  connectStages,
  getCampaigns,
  type Campaign,
  type StageStateOverview,
  type StageNumber,
  type AggregateProgress,
} from "@/lib/campaigns"

const STAGE_NUMBERS: readonly StageNumber[] = [1, 2, 3] as const
const SECTION_KEYS = ["entrada", "processamento", "saida"] as const

export interface StageSelection {
  campaignId: string
  stage: StageNumber
}

export interface PipelineOverviewProps {
  /** Currently selected campaign; defaults to the first loaded campaign. */
  selectedCaseId?: string
  /** Notifies the host when the active campaign changes. */
  onSelectCase?: (campaignId: string) => void
  /**
   * Selection seam consumed by the dedicated stage views (task 12.2). When the
   * researcher picks a stage, the host renders the matching Stage{1,2,3}View
   * (Req. 1.3). Passing no handler keeps the overview read-only.
   */
  onSelectStage?: (selection: StageSelection) => void
  /** The active {campaign, stage} selection, for visual highlight. */
  selectedStage?: StageSelection | null
  /**
   * When true, hide the three per-stage cards (the guided ExecutionWizard owns
   * the detailed stage view in that case), keeping only the aggregate progress
   * and the campaign selector so there is no duplicated stage panel.
   */
  hideStageCards?: boolean
}

/**
 * Data-driven pipeline overview (Req. 1, 5.5). Presents the three stages
 * simultaneously, visually separated and named (Req. 1.1); each stage shows
 * labelled Input / Processing / Output sections (Req. 1.2), a single state
 * indicator (Req. 1.4, with a distinct error state — Req. 1.6) and, while
 * `em_andamento`, its 0–100% processing progress (Req. 1.5). It also shows the
 * aggregate replication progress across the 8 campaigns (Req. 5.5) and reflects
 * real-time updates from `WS /ws/stages/{campaign}` (Req. 1.4, 1.5).
 *
 * The concrete Stage1/2/3 views (task 12.2), CaseSelector (12.3) and confirm
 * modal (12.4) plug in through `onSelectCase` / `onSelectStage`.
 */
export function PipelineOverview({
  selectedCaseId,
  onSelectCase,
  onSelectStage,
  selectedStage,
  hideStageCards = false,
}: PipelineOverviewProps) {
  const { t } = useTranslation("pipeline")

  const [campaigns, setCampaigns] = useState<Campaign[]>([])
  const [progress, setProgress] = useState<AggregateProgress | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Load the curated campaigns and aggregate progress once.
  useEffect(() => {
    let active = true
    getCampaigns()
      .then((data) => {
        if (!active) return
        setCampaigns(data.campaigns)
        setProgress(data.progress)
      })
      .catch(() => {
        if (active) setLoadError(t("error.loadCases"))
      })
    return () => {
      active = false
    }
  }, [t])

  // Resolve the active campaign (controlled by the host, or first loaded one).
  const activeCaseId = selectedCaseId ?? campaigns[0]?.id
  const activeCase = useMemo(
    () => campaigns.find((c) => c.id === activeCaseId),
    [campaigns, activeCaseId],
  )

  // Live per-stage state for the active campaign, seeded from the REST snapshot
  // and then updated in real time via the WebSocket channel (Req. 1.4, 1.5).
  const [liveStages, setLiveStages] = useState<Record<number, StageStateOverview>>({})

  useEffect(() => {
    if (!activeCase) return
    const seed: Record<number, StageStateOverview> = {}
    for (const stage of activeCase.states.stages) seed[stage.stage] = stage
    setLiveStages(seed)
  }, [activeCase])

  useEffect(() => {
    if (!activeCaseId) return
    const connections = STAGE_NUMBERS.map((stage) =>
      connectStages(activeCaseId, stage, (frame) => {
        setLiveStages((prev) => {
          const next: Record<number, StageStateOverview> = {
            ...prev,
            [frame.stage]: {
              stage: frame.stage,
              state: frame.state,
              progress: frame.progress,
              blocked: false,
              error_message: frame.error_message ?? null,
            },
          }

          // Sequential blocking is derived from the latest state snapshot;
          // never retain a stale REST value after a WebSocket transition.
          for (const stageNumber of STAGE_NUMBERS) {
            const stage = next[stageNumber]
            if (!stage) continue
            next[stageNumber] = {
              ...stage,
              blocked:
                stageNumber > 1 && next[stageNumber - 1]?.state !== "concluido",
            }
          }
          return next
        })
      }),
    )
    return () => connections.forEach((connection) => connection.close())
  }, [activeCaseId])

  const handleSelectStage = useCallback(
    (stage: StageNumber) => {
      if (!activeCaseId) return
      onSelectStage?.({ campaignId: activeCaseId, stage })
    },
    [activeCaseId, onSelectStage],
  )

  return (
    <section className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-xl font-semibold">{t("overview.title")}</h2>
        </div>
        <AggregateProgressView progress={progress} />
      </div>

      {loadError ? (
        <Card className="border-team-red/50 bg-team-red/10">
          <CardContent className="pt-6 text-sm text-team-red">{loadError}</CardContent>
        </Card>
      ) : null}

      {!activeCase && !loadError ? (
        <p className="text-sm text-muted-foreground">{t("overview.selectCasePrompt")}</p>
      ) : null}

      {/* Three stages presented simultaneously, separated and named (Req. 1.1).
          Hidden when the guided wizard is active to avoid a duplicated panel. */}
      {!hideStageCards ? (
      <div className="grid gap-4 md:grid-cols-3">
        {STAGE_NUMBERS.map((stage) => {
          const stageState =
            liveStages[stage] ??
            activeCase?.states.stages.find((s) => s.stage === stage)
          const isSelected =
            selectedStage?.campaignId === activeCaseId &&
            selectedStage?.stage === stage
          return (
            <StageCard
              key={stage}
              stage={stage}
              state={stageState}
              selectable={Boolean(activeCaseId && onSelectStage)}
              selected={isSelected}
              onSelect={() => handleSelectStage(stage)}
            />
          )
        })}
      </div>
      ) : null}

      {/* Kept for potential host-driven campaign switching without a CaseSelector
          yet (task 12.3 replaces this with the real selector). */}
      {onSelectCase && campaigns.length > 1 ? (
        <div className="flex flex-wrap gap-2">
          {campaigns.map((c) => (
            <Button
              key={c.id}
              size="sm"
              variant={c.id === activeCaseId ? "default" : "outline"}
              onClick={() => onSelectCase(c.id)}
            >
              {c.name}
            </Button>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function AggregateProgressView({ progress }: { progress: AggregateProgress | null }) {
  const { t } = useTranslation("pipeline")
  const completed = progress?.completed_campaigns ?? 0
  const total = progress?.total_campaigns ?? 8
  const percentage = progress?.percentage ?? 0

  return (
    <Card>
      <CardContent className="flex flex-col gap-2 pt-6">
        <div className="flex items-center justify-between text-sm">
          <span className="font-medium">{t("overview.aggregate.label")}</span>
          <span className="text-muted-foreground">
            {completed > 0
              ? t("overview.aggregate.summary", { concluidos: completed, total })
              : t("overview.aggregate.empty")}
          </span>
        </div>
        <ProgressBar
          value={percentage}
          label={t("overview.aggregate.label")}
          indicatorClassName="bg-team-purple"
        />
      </CardContent>
    </Card>
  )
}

function StageCard({
  stage,
  state,
  selectable,
  selected,
  onSelect,
}: {
  stage: StageNumber
  state?: StageStateOverview
  selectable: boolean
  selected: boolean
  onSelect: () => void
}) {
  const { t } = useTranslation("pipeline")
  const stageState = state?.state ?? "nao_iniciado"
  const blocked = state?.blocked ?? false
  const inProgress = stageState === "em_andamento"

  return (
    <Card
      className={cn(
        "flex flex-col transition-colors",
        selected && "border-team-blue ring-1 ring-team-blue",
      )}
      data-stage={stage}
      data-selected={selected}
    >
      <CardHeader className="gap-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base">{t(`stages.${stage}.name`)}</CardTitle>
          <StageStateBadge state={stageState} blocked={blocked} />
        </div>
        {stageState === "erro" ? (
          <CardDescription className="text-team-red">
            {state?.error_message || t("error.unknown")}
          </CardDescription>
        ) : null}
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-3">
        {/* Processing progress while em_andamento (Req. 1.5). */}
        {inProgress ? (
          <div className="flex flex-col gap-1">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{t("progress.label")}</span>
              <span>{t("progress.value", { value: Math.round(state?.progress ?? 0) })}</span>
            </div>
            <ProgressBar value={state?.progress ?? 0} label={t("progress.label")} />
          </div>
        ) : null}

        {/* Labelled Input / Processing / Output sections (Req. 1.2). The
            dedicated stage views (task 12.2) fill these with real content. */}
        <div className="flex flex-col gap-2">
          {SECTION_KEYS.map((section) => (
            <div
              key={section}
              className="rounded-md border border-dashed p-3"
              data-section={section}
            >
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                {t(`sections.${section}`)}
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                {t("sections.placeholder")}
              </p>
            </div>
          ))}
        </div>

        {selectable ? (
          <Button
            variant={selected ? "default" : "outline"}
            size="sm"
            className="mt-auto"
            onClick={onSelect}
            aria-pressed={selected}
          >
            {selected
              ? t("actions.selected")
              : t("actions.openStage", { stage: t(`stages.${stage}.short`) })}
          </Button>
        ) : null}
      </CardContent>
    </Card>
  )
}
