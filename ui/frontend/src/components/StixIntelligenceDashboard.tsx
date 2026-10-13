import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { EmptyNote } from "@/components/StageSection"
import { ApiError } from "@/lib/api"
import { getDashboard, type DashboardStix } from "@/lib/stix"

export interface StixIntelligenceDashboardProps {
  /** Downloaded base version whose intelligence dashboard is shown (Req. 3.1). */
  versao: string
  /** Advances the flow to the campaign/actor selection step (Req. 3.6). */
  onIrParaSelecao: () => void
}

/**
 * Intelligence dashboard of a selected (downloaded) STIX base (Req. 3.1–3.3,
 * 3.5, 3.6). On mount — and whenever `versao` changes — it loads
 * `getDashboard(versao)` and renders:
 *
 * - the base identification with its version (Req. 3.1);
 * - a grid of per-type count cards, each showing the count, the pt-BR label and
 *   the description explaining what that STIX type means (Req. 3.2, 3.3). The
 *   six types (campanhas, técnicas, táticas, intrusion sets, malware,
 *   relacionamentos) come straight from the backend `contagens`;
 * - a prominent "Selecionar campanha/ator" button advancing the flow (Req. 3.6).
 *
 * On an unreadable/malformed base the backend answers with an `ApiError`
 * (e.g. the 422 `bundle_invalido`); in that case a descriptive pt-BR error is
 * shown and NO partial counts are rendered (Req. 3.5).
 *
 * The per-type labels/descriptions are backend data (`tipo`/`descricao`) and are
 * rendered as-is; only the chrome strings use the `stix` i18n namespace.
 */
export function StixIntelligenceDashboard({
  versao,
  onIrParaSelecao,
}: StixIntelligenceDashboardProps) {
  const { t } = useTranslation("stix")

  const [dashboard, setDashboard] = useState<DashboardStix | null>(null)
  const [loading, setLoading] = useState(true)
  const [erro, setErro] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setLoading(true)
    setErro(null)
    // Never keep stale counts around while (re)loading or on failure, so a
    // malformed base can't surface partial data as if it were complete.
    setDashboard(null)

    getDashboard(versao)
      .then((data) => {
        if (!active) return
        setDashboard(data)
      })
      .catch((error: unknown) => {
        if (!active) return
        const motivo =
          error instanceof ApiError &&
          isRecord(error.body) &&
          isRecord(error.body.detail) &&
          typeof error.body.detail.mensagem === "string"
            ? error.body.detail.mensagem
            : null
        setErro(motivo ?? t("dashboard.erro"))
        setDashboard(null)
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => {
      active = false
    }
  }, [versao, t])

  if (loading) {
    return (
      <section className="flex flex-col gap-6">
        <p className="text-sm text-muted-foreground">{t("dashboard.carregando")}</p>
      </section>
    )
  }

  // On error, show a descriptive pt-BR message and render no partial counts
  // (Req. 3.5).
  if (erro || !dashboard) {
    return (
      <section className="flex flex-col gap-6">
        <Card className="border-team-red/50 bg-team-red/10">
          <CardContent className="pt-6 text-sm text-team-red">
            {erro ?? t("dashboard.erro")}
          </CardContent>
        </Card>
      </section>
    )
  }

  return (
    <section className="flex flex-col gap-6">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2 className="text-xl font-semibold">{t("dashboard.titulo")}</h2>
            {/* Base identification with its version (Req. 3.1). */}
            <p className="text-sm text-muted-foreground">
              {t("dashboard.base", {
                nome: dashboard.nome,
                versao: dashboard.versao,
              })}
            </p>
          </div>
          {/* Prominent CTA advancing to campaign/actor selection (Req. 3.6). */}
          <Button size="lg" onClick={onIrParaSelecao}>
            {t("dashboard.irParaSelecao")}
          </Button>
        </div>
      </div>

      {/* One count card per ContagemTipo (Req. 3.2, 3.3). The six types come
          from the backend `contagens`. */}
      {dashboard.contagens.length === 0 ? (
        <EmptyNote>{t("dashboard.semContagens")}</EmptyNote>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {dashboard.contagens.map((contagem) => (
            <Card key={contagem.stix_type} className="flex flex-col">
              <CardHeader className="gap-2">
                <div className="flex items-start justify-between gap-3">
                  {/* pt-BR label straight from the backend (Req. 3.2). */}
                  <CardTitle className="text-base">{contagem.tipo}</CardTitle>
                  <span className="text-2xl font-semibold tabular-nums text-team-purple">
                    {contagem.quantidade}
                  </span>
                </div>
                {/* Description explaining what the type means (Req. 3.3). */}
                <CardDescription>{contagem.descricao}</CardDescription>
              </CardHeader>
              <CardContent className="pt-0" />
            </Card>
          ))}
        </div>
      )}
    </section>
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}
