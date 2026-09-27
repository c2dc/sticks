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
  connectEstagios,
  getCasos,
  type Caso,
  type EstagioEstado,
  type NumeroEstagio,
  type ProgressoAgregado,
} from "@/lib/casos"

const STAGE_NUMBERS: readonly NumeroEstagio[] = [1, 2, 3] as const
const SECTION_KEYS = ["entrada", "processamento", "saida"] as const

export interface StageSelection {
  casoId: string
  estagio: NumeroEstagio
}

export interface PipelineOverviewProps {
  /** Currently selected case; defaults to the first loaded case. */
  selectedCaseId?: string
  /** Notifies the host when the active case changes. */
  onSelectCase?: (casoId: string) => void
  /**
   * Selection seam consumed by the dedicated stage views (task 12.2). When the
   * researcher picks a stage, the host renders the matching Stage{1,2,3}View
   * (Req. 1.3). Passing no handler keeps the overview read-only.
   */
  onSelectStage?: (selection: StageSelection) => void
  /** The active {case, stage} selection, for visual highlight. */
  selectedStage?: StageSelection | null
}

/**
 * Data-driven pipeline overview (Req. 1, 5.5). Presents the three stages
 * simultaneously, visually separated and named (Req. 1.1); each stage shows
 * labelled Entrada / Processamento / Saída sections (Req. 1.2), a single state
 * indicator (Req. 1.4, with a distinct error state — Req. 1.6) and, while
 * `em_andamento`, its 0–100% processing progress (Req. 1.5). It also shows the
 * aggregate replication progress across the 8 cases (Req. 5.5) and reflects
 * real-time updates from `WS /ws/estagios/{caso}` (Req. 1.4, 1.5).
 *
 * The concrete Stage1/2/3 views (task 12.2), CaseSelector (12.3) and confirm
 * modal (12.4) plug in through `onSelectCase` / `onSelectStage`.
 */
export function PipelineOverview({
  selectedCaseId,
  onSelectCase,
  onSelectStage,
  selectedStage,
}: PipelineOverviewProps) {
  const { t } = useTranslation("pipeline")

  const [casos, setCasos] = useState<Caso[]>([])
  const [progresso, setProgresso] = useState<ProgressoAgregado | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // Load the curated cases and aggregate progress once.
  useEffect(() => {
    let active = true
    getCasos()
      .then((data) => {
        if (!active) return
        setCasos(data.casos)
        setProgresso(data.progresso)
      })
      .catch(() => {
        if (active) setLoadError(t("error.loadCases"))
      })
    return () => {
      active = false
    }
  }, [t])

  // Resolve the active case (controlled by the host, or first loaded case).
  const activeCaseId = selectedCaseId ?? casos[0]?.id
  const activeCase = useMemo(
    () => casos.find((c) => c.id === activeCaseId),
    [casos, activeCaseId],
  )

  // Live per-stage state for the active case, seeded from the REST snapshot and
  // then updated in real time via the WebSocket channel (Req. 1.4, 1.5).
  const [liveStages, setLiveStages] = useState<Record<number, EstagioEstado>>({})

  useEffect(() => {
    if (!activeCase) return
    const seed: Record<number, EstagioEstado> = {}
    for (const stage of activeCase.estados.estagios) seed[stage.estagio] = stage
    setLiveStages(seed)
  }, [activeCase])

  useEffect(() => {
    if (!activeCaseId) return
    const connection = connectEstagios(activeCaseId, (frame) => {
      setLiveStages((prev) => ({
        ...prev,
        [frame.estagio]: {
          estagio: frame.estagio,
          estado: frame.estado,
          progresso: frame.progresso,
          bloqueado: prev[frame.estagio]?.bloqueado ?? false,
          erro: frame.erro ?? null,
        },
      }))
    })
    return () => connection.close()
  }, [activeCaseId])

  const handleSelectStage = useCallback(
    (estagio: NumeroEstagio) => {
      if (!activeCaseId) return
      onSelectStage?.({ casoId: activeCaseId, estagio })
    },
    [activeCaseId, onSelectStage],
  )

  return (
    <section className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-4">
          <h2 className="text-xl font-semibold">{t("overview.title")}</h2>
        </div>
        <AggregateProgress progresso={progresso} />
      </div>

      {loadError ? (
        <Card className="border-team-red/50 bg-team-red/10">
          <CardContent className="pt-6 text-sm text-team-red">{loadError}</CardContent>
        </Card>
      ) : null}

      {!activeCase && !loadError ? (
        <p className="text-sm text-muted-foreground">{t("overview.selectCasePrompt")}</p>
      ) : null}

      {/* Three stages presented simultaneously, separated and named (Req. 1.1). */}
      <div className="grid gap-4 md:grid-cols-3">
        {STAGE_NUMBERS.map((estagio) => {
          const stageState =
            liveStages[estagio] ??
            activeCase?.estados.estagios.find((s) => s.estagio === estagio)
          const isSelected =
            selectedStage?.casoId === activeCaseId &&
            selectedStage?.estagio === estagio
          return (
            <StageCard
              key={estagio}
              estagio={estagio}
              state={stageState}
              selectable={Boolean(activeCaseId && onSelectStage)}
              selected={isSelected}
              onSelect={() => handleSelectStage(estagio)}
            />
          )
        })}
      </div>

      {/* Kept for potential host-driven case switching without a CaseSelector
          yet (task 12.3 replaces this with the real selector). */}
      {onSelectCase && casos.length > 1 ? (
        <div className="flex flex-wrap gap-2">
          {casos.map((c) => (
            <Button
              key={c.id}
              size="sm"
              variant={c.id === activeCaseId ? "default" : "outline"}
              onClick={() => onSelectCase(c.id)}
            >
              {c.nome}
            </Button>
          ))}
        </div>
      ) : null}
    </section>
  )
}

function AggregateProgress({ progresso }: { progresso: ProgressoAgregado | null }) {
  const { t } = useTranslation("pipeline")
  const concluidos = progresso?.casos_concluidos ?? 0
  const total = progresso?.total_casos ?? 8
  const percentual = progresso?.percentual ?? 0

  return (
    <Card>
      <CardContent className="flex flex-col gap-2 pt-6">
        <div className="flex items-center justify-between text-sm">
          <span className="font-medium">{t("overview.aggregate.label")}</span>
          <span className="text-muted-foreground">
            {concluidos > 0
              ? t("overview.aggregate.summary", { concluidos, total })
              : t("overview.aggregate.empty")}
          </span>
        </div>
        <ProgressBar
          value={percentual}
          label={t("overview.aggregate.label")}
          indicatorClassName="bg-team-purple"
        />
      </CardContent>
    </Card>
  )
}

function StageCard({
  estagio,
  state,
  selectable,
  selected,
  onSelect,
}: {
  estagio: NumeroEstagio
  state?: EstagioEstado
  selectable: boolean
  selected: boolean
  onSelect: () => void
}) {
  const { t } = useTranslation("pipeline")
  const estado = state?.estado ?? "nao_iniciado"
  const bloqueado = state?.bloqueado ?? false
  const emAndamento = estado === "em_andamento"

  return (
    <Card
      className={cn(
        "flex flex-col transition-colors",
        selected && "border-team-blue ring-1 ring-team-blue",
      )}
      data-stage={estagio}
      data-selected={selected}
    >
      <CardHeader className="gap-2">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-base">{t(`stages.${estagio}.name`)}</CardTitle>
          <StageStateBadge estado={estado} bloqueado={bloqueado} />
        </div>
        {estado === "erro" ? (
          <CardDescription className="text-team-red">
            {state?.erro || t("error.unknown")}
          </CardDescription>
        ) : null}
      </CardHeader>

      <CardContent className="flex flex-1 flex-col gap-3">
        {/* Processing progress while em_andamento (Req. 1.5). */}
        {emAndamento ? (
          <div className="flex flex-col gap-1">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>{t("progress.label")}</span>
              <span>{t("progress.value", { value: Math.round(state?.progresso ?? 0) })}</span>
            </div>
            <ProgressBar value={state?.progresso ?? 0} label={t("progress.label")} />
          </div>
        ) : null}

        {/* Labelled Entrada / Processamento / Saída sections (Req. 1.2). The
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
              : t("actions.openStage", { stage: t(`stages.${estagio}.short`) })}
          </Button>
        ) : null}
      </CardContent>
    </Card>
  )
}
