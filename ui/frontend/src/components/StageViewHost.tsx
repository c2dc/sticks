import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import { Stage1View } from "@/components/Stage1View"
import { Stage2View } from "@/components/Stage2View"
import { Stage3View } from "@/components/Stage3View"
import type { StageSelection } from "@/components/PipelineOverview"

/**
 * Renders the dedicated Stage{1,2,3}View that matches the PipelineOverview
 * stage-selection seam (Req. 1.3). Given the active {case, stage} selection, it
 * shows the matching view with its Entrada / Processamento / Saída sections.
 *
 * The Stage-3 emulation trigger is deferred to Task 12.4 via the
 * `onRequestEmulation` seam, which this host forwards untouched.
 */
export function StageViewHost({
  selection,
  onClose,
  onRequestEmulation,
}: {
  selection: StageSelection | null
  /** Optional "back to overview" affordance. */
  onClose?: () => void
  /** Forwarded to Stage3View; wired by Task 12.4. */
  onRequestEmulation?: (casoId: string) => void
}) {
  const { t } = useTranslation("pipeline")

  if (!selection) return null

  const { casoId, estagio } = selection

  return (
    <section className="flex flex-col gap-3" data-stage-view={estagio}>
      <div className="flex items-center justify-between gap-4">
        <h3 className="text-lg font-semibold">{t(`stages.${estagio}.name`)}</h3>
        {onClose ? (
          <Button variant="outline" size="sm" onClick={onClose}>
            {t("stageView.back")}
          </Button>
        ) : null}
      </div>

      {estagio === 1 ? <Stage1View casoId={casoId} /> : null}
      {estagio === 2 ? <Stage2View casoId={casoId} /> : null}
      {estagio === 3 ? (
        <Stage3View casoId={casoId} onRequestEmulation={onRequestEmulation} />
      ) : null}
    </section>
  )
}
