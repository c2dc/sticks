import * as React from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { cn } from "@/lib/utils"
import {
  fetchCases,
  hasLoadError,
  isStageStartable,
  PIPELINE_STAGES,
  stageStateOf,
  TOTAL_CURATED_CASES,
  type AggregateProgress,
  type CaseListItem,
  type CaseLoadErrorType,
  type CasesResponse,
  type StageNumber,
  type StageState,
  type TranslationSource,
} from "@/lib/casos"

/**
 * Case selector (CaseSelector) — Task 12.3, Req. 5.1–5.6.
 *
 * Lists the 8 curated cases, shows each case's per-stage state, conducts the
 * researcher through the stages in order 1 → 2 → 3 by reflecting the backend's
 * `bloqueado` / `iniciavel` flags (a blocked stage is shown blocked and cannot
 * be started — Req. 5.2, 5.4), shows the aggregate replication progress
 * (cases with all 3 stages `concluido` / 8 — Req. 5.5), and renders a
 * descriptive per-case error while keeping the other cases selectable
 * (Req. 5.6).
 *
 * The component is composable: pass `data` to render in controlled mode (the
 * parent — e.g. PipelineOverview — owns the fetch), or omit it to let the
 * component fetch on mount. Selection is surfaced through `onSelectCase` /
 * `onSelectStage` so the parent coordinates navigation.
 */
export interface CaseSelectorProps {
  /**
   * Controlled data. When provided, the component renders it directly and does
   * not fetch. When omitted, it fetches `GET /api/casos` on mount.
   */
  data?: CasesResponse
  /** Highlight the currently selected case (controlled selection). */
  selectedCaseId?: string
  /** Called when the researcher selects a case (Req. 5.2). */
  onSelectCase?: (caso: CaseListItem) => void
  /**
   * Called when the researcher starts/opens a stage. Only ever called for a
   * stage the UI allows (never for a blocked stage — Req. 5.4).
   */
  onSelectStage?: (caso: CaseListItem, estagio: StageNumber) => void
  className?: string
}

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; data: CasesResponse }
  | { status: "error"; message: string }

export function CaseSelector({
  data,
  selectedCaseId,
  onSelectCase,
  onSelectStage,
  className,
}: CaseSelectorProps) {
  const { t } = useTranslation("pipeline")
  const controlled = data !== undefined

  const [state, setState] = React.useState<LoadState>(
    controlled ? { status: "ready", data: data! } : { status: "idle" },
  )

  const load = React.useCallback(() => {
    let cancelled = false
    setState({ status: "loading" })
    fetchCases()
      .then((res) => {
        if (!cancelled) setState({ status: "ready", data: res })
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          const message =
            err instanceof Error ? err.message : t("caseSelector.loadError")
          setState({ status: "error", message })
        }
      })
    return () => {
      cancelled = true
    }
  }, [t])

  React.useEffect(() => {
    if (controlled) {
      setState({ status: "ready", data: data! })
      return
    }
    const cancel = load()
    return cancel
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [controlled, data])

  if (state.status === "loading" || state.status === "idle") {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("caseSelector.loading")}
      </p>
    )
  }

  if (state.status === "error") {
    return (
      <div className="flex flex-col items-start gap-2" role="alert">
        <p className="text-sm text-destructive">{state.message}</p>
        {!controlled && (
          <Button variant="outline" size="sm" onClick={() => load()}>
            {t("caseSelector.retry")}
          </Button>
        )}
      </div>
    )
  }

  const { casos, progresso } = state.data

  return (
    <section className={cn("flex flex-col gap-4", className)} aria-label={t("caseSelector.title")}>
      <header className="flex flex-col gap-1">
        <h2 className="text-lg font-semibold tracking-tight">
          {t("caseSelector.title")}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t("caseSelector.subtitle")}
        </p>
      </header>

      <AggregateProgressBar progresso={progresso} />

      {casos.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("caseSelector.empty")}</p>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2">
          {casos.map((caso) => (
            <li key={caso.id}>
              <CaseCard
                caso={caso}
                selected={caso.id === selectedCaseId}
                onSelectCase={onSelectCase}
                onSelectStage={onSelectStage}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/** Aggregate replication progress: cases fully concluído / 8 (Req. 5.5). */
function AggregateProgressBar({ progresso }: { progresso: AggregateProgress }) {
  const { t } = useTranslation("pipeline")
  const total = progresso.total_casos || TOTAL_CURATED_CASES
  const percent = Math.max(0, Math.min(100, progresso.percentual))

  return (
    <div className="flex flex-col gap-1.5" aria-label={t("caseSelector.progress.label")}>
      <div className="flex items-center justify-between text-sm">
        <span className="font-medium">{t("caseSelector.progress.label")}</span>
        <span className="text-muted-foreground">
          {t("caseSelector.progress.summary", {
            done: progresso.casos_concluidos,
            total,
          })}
        </span>
      </div>
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
      >
        <div
          className="h-full rounded-full bg-team-purple transition-[width]"
          style={{ width: `${percent}%` }}
        />
      </div>
    </div>
  )
}

interface CaseCardProps {
  caso: CaseListItem
  selected: boolean
  onSelectCase?: (caso: CaseListItem) => void
  onSelectStage?: (caso: CaseListItem, estagio: StageNumber) => void
}

function CaseCard({ caso, selected, onSelectCase, onSelectStage }: CaseCardProps) {
  const { t } = useTranslation("pipeline")
  const failed = hasLoadError(caso)
  const complete = caso.estados?.concluido_total ?? false

  return (
    <Card
      className={cn(
        "h-full",
        selected && "ring-2 ring-ring",
        failed && "border-destructive/50",
      )}
    >
      <CardHeader className="gap-1 pb-3">
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            className="text-left"
            onClick={() => onSelectCase?.(caso)}
            disabled={failed}
          >
            <CardTitle className="text-base hover:underline disabled:no-underline">
              {caso.nome}
            </CardTitle>
          </button>
          {complete && (
            <span className="shrink-0 rounded-full bg-team-blue/15 px-2 py-0.5 text-xs font-medium text-team-blue">
              {t("caseSelector.completeBadge")}
            </span>
          )}
        </div>
        {!failed && caso.origem_traducao && (
          <CardDescription className="text-xs">
            {t("caseSelector.translationSource.label")}:{" "}
            {translationSourceLabel(caso.origem_traducao, t)}
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="pt-0">
        {failed ? (
          <CaseError caso={caso} />
        ) : (
          <ol className="flex flex-col gap-2">
            {PIPELINE_STAGES.map((estagio) => (
              <li key={estagio}>
                <StageRow
                  caso={caso}
                  estagio={estagio}
                  onSelectStage={onSelectStage}
                />
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  )
}

interface StageRowProps {
  caso: CaseListItem
  estagio: StageNumber
  onSelectStage?: (caso: CaseListItem, estagio: StageNumber) => void
}

function StageRow({ caso, estagio, onSelectStage }: StageRowProps) {
  const { t } = useTranslation("pipeline")
  const state = stageStateOf(caso, estagio)
  const estado: StageState = state?.estado ?? "nao_iniciado"
  const blocked = state?.bloqueado ?? false
  const startable = isStageStartable(caso, estagio)
  // A completed / in-progress (but not blocked) stage can be opened for review;
  // a blocked or errored stage cannot be started (Req. 5.4).
  const canOpen = estado === "concluido" || estado === "em_andamento"
  const actionable = startable || canOpen

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-md border p-2",
        blocked && "opacity-60",
      )}
    >
      <StageStateDot estado={estado} />
      <div className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-sm font-medium">
          {t(`caseSelector.stage.name.${estagio}`)}
        </span>
        <span className="text-xs text-muted-foreground">
          {blocked ? (
            <span title={t("caseSelector.stage.blockedHint")}>
              {t("caseSelector.stage.blocked")}
            </span>
          ) : (
            <>
              {t(`caseSelector.stage.state.${estado}`)}
              {estado === "em_andamento" &&
                ` · ${t("caseSelector.stage.progressPercent", {
                  percent: Math.max(0, Math.min(100, state?.progresso ?? 0)),
                })}`}
              {estado === "erro" && state?.mensagem_erro
                ? ` · ${state.mensagem_erro}`
                : ""}
            </>
          )}
        </span>
      </div>
      <Button
        variant={startable ? "default" : "outline"}
        size="sm"
        // Blocked stages can never be started (Req. 5.4); non-actionable stages
        // (e.g. errored, or not-started while blocked) are disabled entirely.
        disabled={!actionable || blocked}
        aria-disabled={!actionable || blocked}
        onClick={() => {
          if (blocked || !actionable) return
          onSelectStage?.(caso, estagio)
        }}
      >
        {startable ? t("caseSelector.stage.start") : t("caseSelector.stage.open")}
      </Button>
    </div>
  )
}

/** Small colored dot conveying the stage state. */
function StageStateDot({ estado }: { estado: StageState }) {
  const color: Record<StageState, string> = {
    nao_iniciado: "bg-muted-foreground/40",
    em_andamento: "bg-team-purple",
    concluido: "bg-team-blue",
    erro: "bg-team-red",
  }
  return (
    <span
      className={cn("h-2.5 w-2.5 shrink-0 rounded-full", color[estado])}
      aria-hidden="true"
    />
  )
}

/** Descriptive per-case error; other cases stay selectable (Req. 5.6). */
function CaseError({ caso }: { caso: CaseListItem & { erro: NonNullable<CaseListItem["erro"]> } }) {
  const { t } = useTranslation("pipeline")
  const { erro } = caso
  return (
    <div
      className="flex flex-col gap-1 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
      role="alert"
    >
      <span className="font-medium text-destructive">
        {t("caseSelector.caseError.title")}
      </span>
      <span className="text-xs text-muted-foreground">
        {errorTypeLabel(erro.error_type, t)}
        {erro.detail ? ` · ${erro.detail}` : ""}
      </span>
      {erro.arquivo && (
        <span className="text-xs text-muted-foreground">
          {t("caseSelector.caseError.file", { file: erro.arquivo })}
        </span>
      )}
      <span className="text-xs text-muted-foreground">
        {t("caseSelector.caseError.keepOthers")}
      </span>
    </div>
  )
}

function translationSourceLabel(
  source: TranslationSource,
  t: ReturnType<typeof useTranslation>["t"],
): string {
  const key = `caseSelector.translationSource.${source}`
  const translated = t(key)
  // Fall back to the raw value for future, not-yet-translated sources (Req. 11.2).
  return translated === key ? source : translated
}

function errorTypeLabel(
  type: CaseLoadErrorType,
  t: ReturnType<typeof useTranslation>["t"],
): string {
  const key = `caseSelector.caseError.${type}`
  const translated = t(key)
  return translated === key ? type : translated
}
