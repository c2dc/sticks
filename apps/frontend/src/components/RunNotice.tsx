import { Info } from "lucide-react"
import { useTranslation } from "react-i18next"

import type { AiNoticeCode } from "@/lib/ai"

/** Informational banner for a non-fatal run notice. Unknown codes show the raw code. */
export function RunNotice({ code }: { code: AiNoticeCode }) {
  const { t } = useTranslation("pipeline")
  return (
    <div
      className="ml-14 flex items-start gap-2 rounded-xl border border-team-purple/30 bg-team-purple/5 px-3 py-2 text-sm"
      role="status"
      data-section="run-notice"
      data-code={code}
    >
      <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>{t(`guided.notice.${code}`, { defaultValue: code })}</span>
    </div>
  )
}
