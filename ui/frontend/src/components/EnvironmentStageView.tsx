import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, ShieldAlert, ShieldQuestion } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { MermaidDiagram } from "@/components/MermaidDiagram"
import { PrereqChecklist } from "@/components/PrereqChecklist"
import { ProvisioningConsole } from "@/components/ProvisioningConsole"
import { cn } from "@/lib/utils"
import type { EstadoEstagio } from "@/lib/casos"
import {
  getEstagioAmbiente,
  provisionarAmbiente,
  type AmbienteProvisionado,
  type IsolationResumo,
  type ProvisionamentoResponse,
  type ResumoSaude,
  type TopologyHost,
} from "@/lib/estagios"

/** Resumo de saúde vazio (não aprovado) usado como fallback do checklist. */
const SAUDE_VAZIA: ResumoSaude = { aprovado: false, veredictos: [] }
/** Resumo de isolamento vazio (não aprovado) usado como fallback do checklist. */
const ISOLAMENTO_VAZIO: IsolationResumo = { aprovado: false, veredictos: [] }

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
 * Além da topologia + isolamento (herdados do Estágio 4), a tela do Estágio 5
 * monta o **terminal de logs ao vivo** ({@link ProvisioningConsole}) e o
 * **checklist dos três pré-requisitos** ({@link PrereqChecklist}, com o
 * indicador de Estágio 6 bloqueado).
 *
 * Ao abrir a tela, dispara **automaticamente** o provisionamento idempotente
 * (`provisionarAmbiente` — garante/inicia; reusa um ambiente pronto, Req. 1.1,
 * 1.6) sem ação manual e, em paralelo, lê o payload read-only
 * (`getEstagioAmbiente`) para a topologia + checklist. O `ProvisioningConsole`
 * transmite os logs ao vivo pelo canal, de forma independente.
 *
 * Quando o provisionamento conclui com sucesso, chama
 * `onProvisionamentoConcluido` para que o wizard releia o estado do backend e
 * libere o "Próximo" (Req. 6.4, 7.3) — o Estágio 5 só fica `concluido` após os
 * três pré-requisitos passarem.
 */
export function EnvironmentStageView({
  casoId,
  onProvisionamentoConcluido,
}: {
  casoId: string
  /**
   * Chamado quando o provisionamento conclui com sucesso (os três
   * pré-requisitos passam). O wizard usa isso para reler o estado do backend e
   * destravar o "Próximo" (Req. 6.4, 7.3).
   */
  onProvisionamentoConcluido?: () => void
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<ProvisionamentoResponse | null>(null)
  const [provisionado, setProvisionado] = useState<AmbienteProvisionado | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setProvisionado(null)
    setLoadError(null)

    // Dispara o provisionamento idempotente (garante/inicia) ao abrir a tela —
    // sem botão manual (Req. 1.1). Reusar um ambiente pronto é seguro ao
    // remontar (Req. 1.6). A Saída alimenta o checklist (saúde/isolamento/
    // abilities) e, na conclusão, destrava o "Próximo" via o callback.
    provisionarAmbiente(casoId)
      .then((ambiente) => {
        if (!active) return
        setProvisionado(ambiente)
        const concluiu =
          !ambiente.etapa_falha &&
          ambiente.saude.aprovado &&
          ambiente.isolamento.aprovado &&
          ambiente.abilities_carregadas
        if (concluiu) onProvisionamentoConcluido?.()
        // Relê o payload read-only após o provisionamento para refletir a
        // topologia + estados mais recentes no checklist.
        return getEstagioAmbiente(casoId).then((data) => {
          if (active) setResult(data)
        })
      })
      .catch(() => {
        // Provisionamento falhou/indisponível: ainda assim exibe a topologia
        // e o estado read-only correntes (fail-closed na tela).
        if (!active) return
        getEstagioAmbiente(casoId)
          .then((data) => {
            if (active) setResult(data)
          })
          .catch(() => {
            if (active) setLoadError(t("stageView.loadError"))
          })
      })

    return () => {
      active = false
    }
  }, [casoId, t, onProvisionamentoConcluido])

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

  // Estado do Estágio 5 nos `estados` do payload read-only (fonte do checklist).
  const estagio5 = result.estados.estagios.find((e) => e.estagio === 5)
  const estadoChecklist = (estagio5?.estado ?? "nao_iniciado") as EstadoEstagio
  // Preferir o resultado fresco do `provisionar` para saúde/isolamento/abilities
  // (o payload GET não traz `abilities_carregadas`); cair no GET quando ausente.
  const saudeChecklist = provisionado?.saude ?? result.saude ?? SAUDE_VAZIA
  const isolamentoChecklist = provisionado?.isolamento ?? ISOLAMENTO_VAZIO
  const abilitiesCarregadas = provisionado?.abilities_carregadas ?? false
  const etapaFalha = provisionado?.etapa_falha ?? estagio5?.etapa_falha ?? null
  const mensagemErro = provisionado?.mensagem_erro ?? estagio5?.mensagem_erro ?? null

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

      {/* Terminal de logs ao vivo do provisionamento (Req. 2.x). */}
      <ProvisioningConsole casoId={casoId} className="md:col-span-2" />

      {/* Checklist dos três pré-requisitos + indicador do Estágio 6 bloqueado
          (Req. 5.6, 7.2, 7.3). */}
      <PrereqChecklist
        estado={estadoChecklist}
        saude={saudeChecklist}
        isolamento={isolamentoChecklist}
        abilitiesCarregadas={abilitiesCarregadas}
        etapaFalha={etapaFalha}
        mensagemErro={mensagemErro}
      />
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
