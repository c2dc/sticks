import { useTranslation } from "react-i18next"

import { LlmCheckList } from "@/components/llm/LlmCheckList"
import { useLlmStatus } from "@/components/llm/LlmStatusProvider"
import { Button } from "@/components/ui/button"
import { formatGigabytes } from "@/lib/llm"
import { cn } from "@/lib/utils"

/** Profile list, selection, checks of the selected profile and the recheck button. */
export function LlmPanel() {
  const { t } = useTranslation("llm")
  const { profiles, selectedId, localDiscovery, preflight, preflightError, rechecking, select, recheck } =
    useLlmStatus()

  const showVpnHint = preflight?.hints.includes("vpn") ?? false

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-sm font-semibold">{t("panel.title")}</h2>
        <p className="text-xs text-muted-foreground">{t("panel.description")}</p>
      </div>

      <div role="radiogroup" aria-label={t("panel.profiles")} className="space-y-1.5">
        {profiles.map((profile) => {
          const selected = profile.id === selectedId
          const disabled = !profile.configured
          return (
            <button
              key={profile.id}
              type="button"
              role="radio"
              aria-checked={selected}
              disabled={disabled}
              onClick={() => void select(profile.id)}
              className={cn(
                "w-full rounded-md border p-2 text-left text-sm transition-colors",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                "disabled:cursor-not-allowed disabled:opacity-60",
                selected ? "border-primary bg-accent" : "hover:bg-accent/50",
              )}
            >
              <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className="font-medium break-all">{profile.model_id}</span>
                {profile.is_default && (
                  <span className="rounded bg-muted px-1.5 text-xs">{t("profile.default")}</span>
                )}
                {selected && (
                  <span className="rounded bg-primary px-1.5 text-xs text-primary-foreground">
                    {t("profile.inUse")}
                  </span>
                )}
              </span>
              <span className="block text-xs text-muted-foreground">
                {t(`profile.kind.${profile.kind}`)}
                {profile.size_bytes !== null &&
                  ` · ${t("profile.size", { size: formatGigabytes(profile.size_bytes) })}`}
                {profile.memory_fit !== null && ` · ${t(`check.memory.${profile.memory_fit}`)}`}
                {disabled && ` · ${t("profile.notConfigured")}`}
              </span>
            </button>
          )
        })}
      </div>

      {localDiscovery === "ollama_down" && (
        <p className="text-xs text-muted-foreground">{t("panel.ollamaDown")}</p>
      )}

      <div aria-live="polite" className="space-y-2">
        {preflight && !rechecking && <LlmCheckList checks={preflight.checks} />}
        {rechecking && <p className="text-sm">{t("panel.rechecking")}</p>}
        {preflightError && !rechecking && <p className="text-sm">{t("panel.loadError")}</p>}
        {showVpnHint && !rechecking && <p className="text-sm font-medium">{t("hint.vpn")}</p>}
      </div>

      <Button type="button" variant="outline" size="sm" onClick={recheck} disabled={rechecking}>
        {t("panel.recheck")}
      </Button>
    </div>
  )
}
