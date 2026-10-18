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
 */
export function StageViewHost({
  selection,
  onClose,
}: {
  selection: StageSelection | null
  /** Optional "back to overview" affordance. */
  onClose?: () => void
}) {
  const { t } = useTranslation("pipeline")

  if (!selection) return null

  const { campaignId, stage } = selection

  return (
    <section className="flex flex-col gap-3" data-stage-view={stage}>
      <div className="flex items-center justify-between gap-4">
        <h3 className="text-lg font-semibold">{t(`stages.${stage}.name`)}</h3>
        {onClose ? (
          <Button variant="outline" size="sm" onClick={onClose}>
            {t("stageView.back")}
          </Button>
        ) : null}
      </div>

      {stage === 1 ? <Stage1View campaignId={campaignId} /> : null}
      {stage === 2 ? <Stage2View campaignId={campaignId} /> : null}
      {stage === 3 ? <Stage3View campaignId={campaignId} /> : null}
    </section>
  )
}
