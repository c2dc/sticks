import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, ShieldAlert, ShieldQuestion } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { MermaidDiagram } from "@/components/MermaidDiagram"
import { cn } from "@/lib/utils"
import {
  getProvisionamento,
  type ProvisionamentoResponse,
  type TopologyHost,
} from "@/lib/estagios"

/**
 * EnvironmentStageView — Provisionamento do Ambiente (Estágio 4, Req. 5.4–5.6).
 *
 * Tela dedicada do Estágio 4. Mostra, lado a lado:
 *
 * - A **topologia do laboratório contido**: diagrama Mermaid, redes internas
 *   (nome + subnet), hosts (nome / container / papel / IPs) e o arquivo de
 *   ambiente (docker-compose) quando presente.
 * - O **estado de isolamento** em três situações:
 *   - `isolado === true` → indicador claro de "ambiente isolado / pronto"
 *     (team-blue). O Estágio 4 se autoconclui e o Estágio 5 fica liberado — o
 *     caso dos ambientes curados autoconcluídos.
 *   - `isolado === false && !isolamento_indisponivel` → mostra o `motivo` como a
 *     razão de o ambiente não estar isolado, com indicador de bloqueio/aviso
 *     (team-red), deixando claro que o Estágio 5 está bloqueado.
 *   - `isolamento_indisponivel === true` → mostra que o isolamento **não pôde
 *     ser verificado** (fail-closed) com o `motivo`, indicador de aviso, Estágio
 *     5 bloqueado — a topologia continua sendo exibida.
 *
 * É somente-leitura: carrega o payload do Estágio 4 na montagem e trata os
 * estados de carregamento e de erro de carga como as telas irmãs
 * (Stage1View / Stage3View).
 */
export function EnvironmentStageView({ casoId }: { casoId: string }) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<ProvisionamentoResponse | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    getProvisionamento(casoId)
      .then((data) => {
        if (active) setResult(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, t])

  if (loadError) {
    return <EmptyNote>{loadError}</EmptyNote>
  }
  if (!result) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const { hosts, redes, mermaid, compose_file, isolado, isolamento_indisponivel, motivo } =
    result

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Topologia do laboratório contido — diagrama + redes internas. */}
      <StageSection
        data-section="topologia"
        label={t("stage4.topologia.titulo")}
        kind="blue"
        description={t("stage4.topologia.descricao")}
        className="md:col-span-2"
      >
        <MermaidDiagram definition={mermaid} className="w-full overflow-auto" />

        <div className="flex flex-col gap-1">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            {t("stage4.topologia.redes")}
          </p>
          {redes.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t("stage4.topologia.semRedes")}</p>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {redes.map((rede) => (
                <li key={rede.nome} className="flex justify-between gap-2 text-xs">
                  <span className="text-muted-foreground">{rede.nome}</span>
                  <span className="font-mono">{rede.subnet}</span>
                </li>
              ))}
            </ul>
          )}
        </div>

        {compose_file ? (
          <p
            className="truncate text-[11px] text-muted-foreground"
            title={compose_file}
          >
            {t("stage4.topologia.composeFile")}: <code>{compose_file}</code>
          </p>
        ) : null}
      </StageSection>

      {/* Estado de isolamento — pronto / bloqueado / não verificável. */}
      <StageSection
        data-section="isolamento"
        label={t("stage4.isolamento.titulo")}
        kind={isolado ? "blue" : "red"}
      >
        <IsolationIndicator
          isolado={isolado}
          indisponivel={isolamento_indisponivel}
          motivo={motivo}
        />
      </StageSection>

      {/* Hosts do ambiente — nome / container / papel / IPs. */}
      <StageSection
        data-section="hosts"
        label={t("stage4.topologia.hosts")}
        kind="neutral"
        className="md:col-span-3"
      >
        {hosts.length === 0 ? (
          <EmptyNote>{t("stage4.topologia.semHosts")}</EmptyNote>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {hosts.map((host) => (
              <HostCard key={host.nome} host={host} />
            ))}
          </ul>
        )}
      </StageSection>
    </div>
  )
}

/**
 * The isolation state indicator — exactly one of the three mutually-exclusive
 * situations (Req. 5.4–5.6). When not isolated or not verifiable, it also makes
 * clear the Estágio 5 is blocked.
 */
function IsolationIndicator({
  isolado,
  indisponivel,
  motivo,
}: {
  isolado: boolean
  indisponivel: boolean
  motivo: string | null
}) {
  const { t } = useTranslation("pipeline")

  if (isolado) {
    return (
      <div className="flex flex-col gap-2" role="status">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-team-blue/10 px-2.5 py-1 text-sm font-medium text-team-blue">
          <CheckCircle2 className="size-4" aria-hidden />
          {t("stage4.isolamento.isolado")}
        </span>
        <p className="text-xs text-muted-foreground">
          {t("stage4.isolamento.isoladoDescricao")}
        </p>
      </div>
    )
  }

  // Both remaining states are blocking: non-isolated or unverifiable.
  const Icon = indisponivel ? ShieldQuestion : ShieldAlert
  const title = indisponivel
    ? t("stage4.isolamento.indisponivel")
    : t("stage4.isolamento.naoIsolado")

  return (
    <div className="flex flex-col gap-2">
      <span className="inline-flex items-center gap-1.5 rounded-full bg-team-red/10 px-2.5 py-1 text-sm font-medium text-team-red">
        <Icon className="size-4" aria-hidden />
        {title}
      </span>
      {indisponivel ? (
        <p className="text-xs text-muted-foreground">
          {t("stage4.isolamento.indisponivelDescricao")}
        </p>
      ) : null}
      {motivo ? <p className="text-sm text-team-red">{motivo}</p> : null}
      <p className="text-xs text-muted-foreground">{t("stage4.isolamento.bloqueado")}</p>
    </div>
  )
}

/** One lab host card: nome / container / papel / IPs. */
function HostCard({ host }: { host: TopologyHost }) {
  const { t } = useTranslation("pipeline")
  const papelLabel =
    host.papel === "attacker"
      ? t("stage4.topologia.papel.attacker")
      : host.papel === "target"
        ? t("stage4.topologia.papel.target")
        : host.papel

  return (
    <li className="flex flex-col gap-1 rounded-md border p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium" title={host.nome}>
          {host.nome}
        </span>
        <span
          className={cn(
            "shrink-0 rounded-full px-2 py-0.5 text-[11px]",
            host.papel === "attacker" && "bg-team-red/10 text-team-red",
            host.papel === "target" && "bg-team-blue/10 text-team-blue",
            host.papel !== "attacker" &&
              host.papel !== "target" &&
              "bg-muted text-muted-foreground",
          )}
        >
          {papelLabel}
        </span>
      </div>
      <p className="truncate text-xs text-muted-foreground" title={host.container}>
        <code>{host.container}</code>
      </p>
      {host.ips.length > 0 ? (
        <ul className="flex flex-wrap gap-1">
          {host.ips.map((ip) => (
            <li
              key={ip}
              className="rounded border bg-muted px-1.5 py-0.5 font-mono text-[11px]"
            >
              {ip}
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  )
}
