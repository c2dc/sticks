import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { UserCheck } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import {
  getStageOperationalization,
  type Ability,
  type Stage4Response,
  type TranslationSource,
  type Stage2Output,
} from "@/lib/stages"

/**
 * Stage2View — Curated Translation (Task 12.2, Req. 3, 11).
 *
 * - Entrada: the abstract ATT&CK techniques from Stage 1 (Req. 3.1). When Stage
 *   1 produced none, the Saída is suppressed and an absence message is shown
 *   (Req. 3.6).
 * - Processamento: shows the explicit, extensible translation-source indicator
 *   ("curadoria humana") as a first-class attribute (Req. 3.4, 11.1, 11.2).
 * - Saída: the curated Abilities (technique / tactic / description / command per
 *   executor — Req. 3.2), their ordering / dependencies (Req. 3.3) and the
 *   Adversary grouping (Req. 3.5). Absence states are handled: no Abilities
 *   (Req. 3.7) and no Adversary (Req. 3.8).
 */
export function Stage2View({ campaignId }: { campaignId: string }) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage4Response | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    getStageOperationalization(campaignId)
      .then((data) => {
        if (active) setResult(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [campaignId, t])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!result) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  // Stage 4 (Operationalization): `abilities` carries the curated Abilities
  // Output (same shape as the former Stage-2 output); it is null when the
  // operationalization is suppressed (no upstream technique).
  const suppressed = result.abilities === null
  const output = result.abilities

  return (
    <div className="grid gap-4 md:grid-cols-2">
      {/* Processamento — explicit translation-source indicator (Req. 3.4, 11). */}
      <StageSection
        data-section="processamento"
        label={t("sections.processamento")}
        kind="purple"
      >
        {output ? (
          <>
            <TranslationSourceBadge
              origin={output.translation_source}
              humanCuration={output.human_curation}
            />
            <p className="text-xs text-muted-foreground">
              {t("stage2.processamento.description")}
            </p>
          </>
        ) : (
          <EmptyNote>{t("stage2.saida.suppressed")}</EmptyNote>
        )}
      </StageSection>

      {/* Output — curated Abilities, ordering, Adversary; or absence (3.6–3.8). */}
      <StageSection data-section="saida" label={t("sections.saida")} kind="neutral">
        {suppressed || !output ? (
          <EmptyNote>{t("stage2.saida.suppressed")}</EmptyNote>
        ) : !output.has_abilities ? (
          <EmptyNote>{output.note || t("stage2.saida.noAbilities")}</EmptyNote>
        ) : (
          <div className="flex flex-col gap-4">
            <AbilitiesList abilities={output.abilities} />
            <OrderingBlock ordering={output.ordering} />
            <AdversaryBlock
              hasAdversary={output.has_adversary}
              adversary={output.adversary}
              note={output.adversary_note}
            />
          </div>
        )}
      </StageSection>
    </div>
  )
}

/**
 * The translation-source indicator (Req. 3.4, 11.1). Rendered as an explicit,
 * labelled attribute whose value comes from the extensible translation-source
 * set — falling back to the raw value for future, not-yet-translated origins
 * so a new AI-assisted source shows without a model change (Req. 11.2).
 */
function TranslationSourceBadge({
  origin,
  humanCuration,
}: {
  origin: TranslationSource
  humanCuration: boolean
}) {
  const { t } = useTranslation("pipeline")
  const key = `stage2.translationSource.${origin}`
  const translated = t(key)
  const label = translated === key ? origin : translated

  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-muted-foreground">
        {t("stage2.translationSource.label")}
      </span>
      <span
        className={cn(
          "inline-flex w-fit items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
          "border-border bg-muted text-foreground",
        )}
        data-origem={origin}
      >
        {humanCuration ? <UserCheck className="size-3.5" aria-hidden /> : null}
        {label}
      </span>
    </div>
  )
}

/** Curated Abilities with technique / tactic / description / per-executor cmd. */
function AbilitiesList({ abilities }: { abilities: Ability[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage2.saida.abilities", { count: abilities.length })}
      </p>
      <ul className="flex flex-col gap-2">
        {abilities.map((ability) => (
          <li
            key={ability.ability_id}
            className="rounded-md border p-2"
            data-ability={ability.ability_id}
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-sm font-medium">{ability.name}</span>
              <span className="shrink-0 rounded-full bg-team-blue/10 px-2 py-0.5 text-xs text-team-blue">
                {ability.technique_id}
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              {t("stage2.saida.tactic")}: {ability.tactic}
            </p>
            {ability.description ? (
              <p className="mt-1 text-xs">{ability.description}</p>
            ) : null}
            {ability.executors.length > 0 ? (
              <ul className="mt-2 flex flex-col gap-1">
                {ability.executors.map((executor, index) => (
                  <li key={`${ability.ability_id}-${index}`} className="flex flex-col">
                    <span className="text-xs text-muted-foreground">
                      {executor.name} · {executor.platform}
                    </span>
                    <code className="break-all rounded bg-muted px-1.5 py-1 text-xs">
                      {executor.command}
                    </code>
                  </li>
                ))}
              </ul>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Sequential order / dependencies across the Abilities (Req. 3.3). */
function OrderingBlock({ ordering }: { ordering: string[] }) {
  const { t } = useTranslation("pipeline")
  if (!ordering || ordering.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage2.saida.ordering")}
      </p>
      <ol className="flex flex-col gap-0.5">
        {ordering.map((abilityId, index) => (
          <li key={`${abilityId}-${index}`} className="text-xs">
            <span className="text-muted-foreground tabular-nums">{index + 1}.</span>{" "}
            {abilityId}
          </li>
        ))}
      </ol>
    </div>
  )
}

/** Adversary grouping (Req. 3.5), or its absence indication (Req. 3.8). */
function AdversaryBlock({
  hasAdversary,
  adversary,
  note,
}: {
  hasAdversary: boolean
  adversary: Stage2Output["adversary"]
  note?: string | null
}) {
  const { t } = useTranslation("pipeline")
  if (!hasAdversary || !adversary) {
    return <EmptyNote>{note || t("stage2.saida.noAdversary")}</EmptyNote>
  }
  return (
    <div className="flex flex-col gap-1 rounded-md border border-team-red/30 bg-team-red/5 p-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-team-red">
        {t("stage2.saida.adversary")}
      </p>
      <span className="text-sm font-medium">{adversary.name}</span>
      {adversary.description ? (
        <p className="text-xs text-muted-foreground">{adversary.description}</p>
      ) : null}
      {adversary.atomic_ordering.length > 0 ? (
        <p className="text-xs text-muted-foreground">
          {t("stage2.saida.adversaryOrdering", {
            count: adversary.atomic_ordering.length,
          })}
        </p>
      ) : null}
    </div>
  )
}
