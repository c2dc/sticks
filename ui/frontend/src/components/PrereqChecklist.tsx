import type { ComponentType } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, Circle, ShieldAlert, XCircle } from "lucide-react"

import { StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import type { EstadoEstagio } from "@/lib/casos"
import type {
  IsolationResumo,
  ResumoSaude,
  VereditoContainer,
  VereditoSaude,
} from "@/lib/estagios"

/**
 * PrereqChecklist — confirmação de pré-requisitos do Estágio 5 (Req. 5.6, 7.2–7.4).
 *
 * Lista os **três** pré-requisitos de execução que precisam estar preenchidos e
 * verificados antes de liberar o Estágio 6 (Req. 5.6):
 *
 * 1. **Contêineres saudáveis** — derivado de `saude: ResumoSaude`; em falha, os
 *    `VereditoSaude` por contêiner trazem a `razao` do contêiner reprovado.
 * 2. **Abilities carregadas no Caldera** — derivado de `abilitiesCarregadas`.
 * 3. **Isolamento aprovado** — derivado de `isolamento: IsolationResumo`; em
 *    falha, os `VereditoContainer` nomeiam o contêiner não isolado e as `razoes`
 *    da reprovação (Req. 7.4).
 *
 * Cada item tem exatamente um de três estados — `pendente` / `ok` / `falha`
 * (Req. 5.6). A derivação por item (ver {@link derivarEstados}) combina a etapa
 * que falhou (`etapaFalha`) com os dados de saúde/abilities/isolamento e o
 * estado agregado do Estágio:
 *
 * - Quando `estado === "concluido"`, os três itens são `ok`.
 * - Quando `etapaFalha` identifica a etapa de um item, esse item é `falha` (com
 *   a causa), os itens **anteriores** que passaram ficam `ok` e os **posteriores**
 *   ficam `pendente`.
 * - Caso contrário, usa os dados: `saude.aprovado`, `abilitiesCarregadas`,
 *   `isolamento.aprovado` → `ok`/`falha`/`pendente`.
 * - Quando nada rodou ainda (`estado === "nao_iniciado"` sem dados), os três são
 *   `pendente`.
 *
 * Em qualquer `falha` (ou `estado === "erro"`), mostra um indicador claro de
 * **Estágio 6 bloqueado** (Req. 7.2, 7.3). Quando os três são `ok`
 * (`estado === "concluido"`), mostra a confirmação de que o ambiente está pronto
 * (Req. 5.6).
 */

/** O estado de um pré-requisito individual (Req. 5.6). */
export type EstadoPrereq = "pendente" | "ok" | "falha"

/**
 * As etapas da pipeline de provisionamento (`etapa_falha` do StageRun /
 * StageProgressEvent) mapeadas a cada pré-requisito. A união fica aberta
 * (`string & {}`) para novas etapas sem quebrar o contrato.
 */
export type EtapaProvisionamento =
  | "subir_conteineres"
  | "aguardar_saude"
  | "carregar_abilities"
  | "verificar_isolamento"
  | "concluir"
  | (string & {})

/**
 * As três etapas que mapeiam 1:1 para os pré-requisitos exibidos (saúde,
 * abilities, isolamento). É o domínio de chaves que {@link derivarEstados}
 * sempre devolve — um subconjunto fechado de {@link EtapaProvisionamento}.
 */
export type EtapaPrereq =
  | "aguardar_saude"
  | "carregar_abilities"
  | "verificar_isolamento"

/** Props do {@link PrereqChecklist}. */
export interface PrereqChecklistProps {
  /** Estado agregado do Estágio 5 (StageRun / StageProgressEvent). */
  estado: EstadoEstagio
  /** Resumo de saúde dos contêineres-alvo (Req. 5.1, 5.6). */
  saude: ResumoSaude
  /** Resumo de isolamento dos contêineres-alvo (Req. 5.3, 7.4). */
  isolamento: IsolationResumo
  /** `true` sse as Abilities ordenadas foram carregadas no Caldera (Req. 5.2). */
  abilitiesCarregadas: boolean
  /** Etapa que falhou (`null`/omitida quando não há falha) (Req. 7.1, 7.2). */
  etapaFalha?: string | null
  /** Causa legível da falha (`null`/omitida quando não há falha) (Req. 7.2). */
  mensagemErro?: string | null
  /**
   * `true` sse o Estágio 6 está bloqueado. Quando omitido, é derivado de
   * `estado !== "concluido"` (bloqueio sequencial — Req. 7.3).
   */
  estagio6Bloqueado?: boolean
  className?: string
}

/** A identidade visual/semântica de cada um dos três pré-requisitos. */
interface ItemChecklist {
  /** Chave estável (também usada como `key` da lista). */
  id: "conteineresSaudaveis" | "abilitiesCarregadas" | "isolamentoAprovado"
  /** Etapa da pipeline que decide este item (para casar com `etapaFalha`). */
  etapa: EtapaProvisionamento
  estado: EstadoPrereq
  rotulo: string
  /** Causa legível quando `falha`; `null` caso contrário (Req. 7.2). */
  causa: string | null
  /** Veredictos de saúde por contêiner (apenas no item de saúde). */
  saude?: readonly VereditoSaude[]
  /** Veredictos de isolamento por contêiner (apenas no item de isolamento). */
  isolamento?: readonly VereditoContainer[]
}

/** Ícone + classe de cor por estado do pré-requisito (ver EnvironmentStageView). */
const VISUAL_ESTADO: Record<
  EstadoPrereq,
  { Icon: ComponentType<{ className?: string }>; cor: string; statusKey: string }
> = {
  ok: { Icon: CheckCircle2, cor: "text-team-blue", statusKey: "ok" },
  falha: { Icon: XCircle, cor: "text-team-red", statusKey: "falha" },
  pendente: { Icon: Circle, cor: "text-muted-foreground", statusKey: "pendente" },
}

/** Ordem canônica dos três pré-requisitos (reflete a ordem da pipeline). */
const ORDEM_ETAPAS: readonly EtapaPrereq[] = [
  "aguardar_saude",
  "carregar_abilities",
  "verificar_isolamento",
]

/**
 * Deriva o estado (`pendente`/`ok`/`falha`) dos três pré-requisitos a partir do
 * estado agregado, da etapa que falhou e dos dados de saúde/abilities/isolamento
 * (ver a doc do {@link PrereqChecklist}). Exportada para teste (task 8.3).
 */
export function derivarEstados({
  estado,
  saude,
  isolamento,
  abilitiesCarregadas,
  etapaFalha,
}: {
  estado: EstadoEstagio
  saude: ResumoSaude
  isolamento: IsolationResumo
  abilitiesCarregadas: boolean
  etapaFalha?: string | null
}): Record<EtapaPrereq, EstadoPrereq> {
  // Concluído: os três pré-requisitos passaram (Req. 5.5, 5.6).
  if (estado === "concluido") {
    return {
      aguardar_saude: "ok",
      carregar_abilities: "ok",
      verificar_isolamento: "ok",
    }
  }

  // A etapa que falhou mapeia para o pré-requisito; `subir_conteineres` falha
  // antes da saúde, então reprova o item de contêineres saudáveis.
  const etapaDoItem = etapaFalha === "subir_conteineres" ? "aguardar_saude" : etapaFalha

  if (etapaDoItem && ORDEM_ETAPAS.includes(etapaDoItem as EtapaPrereq)) {
    const indiceFalha = ORDEM_ETAPAS.indexOf(etapaDoItem as EtapaPrereq)
    const resultado = {} as Record<EtapaPrereq, EstadoPrereq>
    ORDEM_ETAPAS.forEach((etapa, indice) => {
      // Itens anteriores à falha passaram; o item da falha é `falha`;
      // os posteriores ainda não rodaram (`pendente`).
      resultado[etapa] =
        indice < indiceFalha ? "ok" : indice === indiceFalha ? "falha" : "pendente"
    })
    return resultado
  }

  // Sem etapa de falha identificada: usa os dados verificados por item. Um
  // resumo "não aprovado" sem dados (ainda não rodou) permanece `pendente`.
  const saudeEstado: EstadoPrereq = saude.aprovado
    ? "ok"
    : saude.veredictos.length > 0
      ? "falha"
      : "pendente"
  const abilitiesEstado: EstadoPrereq = abilitiesCarregadas ? "ok" : "pendente"
  const isolamentoEstado: EstadoPrereq = isolamento.aprovado
    ? "ok"
    : isolamento.veredictos.length > 0
      ? "falha"
      : "pendente"

  return {
    aguardar_saude: saudeEstado,
    carregar_abilities: abilitiesEstado,
    verificar_isolamento: isolamentoEstado,
  }
}

/** Monta a razão legível por contêiner de saúde reprovado (Req. 7.2). */
function razaoSaude(veredictos: readonly VereditoSaude[]): string | null {
  const reprovados = veredictos.filter((v) => !v.rodando || v.saudavel === false)
  if (reprovados.length === 0) return null
  return reprovados
    .map((v) => `${v.container}: ${v.razao ?? ""}`.trim().replace(/:\s*$/, ""))
    .join(" · ")
}

/**
 * Monta a razão legível por contêiner não isolado, nomeando o contêiner e a
 * razão da reprovação (Req. 7.4).
 */
function razaoIsolamento(veredictos: readonly VereditoContainer[]): string | null {
  const reprovados = veredictos.filter((v) => !v.isolado)
  if (reprovados.length === 0) return null
  return reprovados
    .map((v) => `${v.container}: ${v.razoes.join(", ")}`.replace(/:\s*$/, ""))
    .join(" · ")
}

export function PrereqChecklist({
  estado,
  saude,
  isolamento,
  abilitiesCarregadas,
  etapaFalha,
  mensagemErro,
  estagio6Bloqueado,
  className,
}: PrereqChecklistProps) {
  const { t } = useTranslation("pipeline")

  const estados = derivarEstados({
    estado,
    saude,
    isolamento,
    abilitiesCarregadas,
    etapaFalha,
  })

  // Causa por item: prefere a `mensagemErro` da etapa que falhou; completa com
  // as razões por contêiner (saúde/isolamento) para nomear o recurso ofensor.
  const causaSaude =
    estados.aguardar_saude === "falha"
      ? razaoSaude(saude.veredictos) ?? mensagemErro ?? null
      : null
  const causaAbilities =
    estados.carregar_abilities === "falha" ? mensagemErro ?? null : null
  const causaIsolamento =
    estados.verificar_isolamento === "falha"
      ? razaoIsolamento(isolamento.veredictos) ?? mensagemErro ?? null
      : null

  const itens: ItemChecklist[] = [
    {
      id: "conteineresSaudaveis",
      etapa: "aguardar_saude",
      estado: estados.aguardar_saude,
      rotulo: t("stage5.provisionamento.prerequisitos.conteineresSaudaveis", {
        defaultValue: "Contêineres saudáveis",
      }),
      causa: causaSaude,
      saude: saude.veredictos,
    },
    {
      id: "abilitiesCarregadas",
      etapa: "carregar_abilities",
      estado: estados.carregar_abilities,
      rotulo: t("stage5.provisionamento.prerequisitos.abilitiesCarregadas", {
        defaultValue: "Abilities carregadas no Caldera",
      }),
      causa: causaAbilities,
    },
    {
      id: "isolamentoAprovado",
      etapa: "verificar_isolamento",
      estado: estados.verificar_isolamento,
      rotulo: t("stage5.provisionamento.prerequisitos.isolamentoAprovado", {
        defaultValue: "Isolamento aprovado",
      }),
      causa: causaIsolamento,
      isolamento: isolamento.veredictos,
    },
  ]

  const temFalha = estado === "erro" || itens.some((item) => item.estado === "falha")
  const prontos = estado === "concluido" && itens.every((item) => item.estado === "ok")
  // Estágio 6 bloqueado: explícito pela prop ou derivado do estado (Req. 7.3).
  const bloqueado = estagio6Bloqueado ?? estado !== "concluido"

  return (
    <StageSection
      data-section="prerequisitos"
      label={t("stage5.provisionamento.prerequisitos.titulo", {
        defaultValue: "Pré-requisitos de execução",
      })}
      kind={temFalha ? "red" : prontos ? "blue" : "neutral"}
      className={className}
    >
      <ul className="flex flex-col gap-2">
        {itens.map((item) => (
          <PrereqItem key={item.id} item={item} />
        ))}
      </ul>

      {/* Falha bloqueante: indica claramente o Estágio 6 bloqueado (Req. 7.2, 7.3). */}
      {temFalha ? (
        <div
          className="flex flex-col gap-1 rounded-md border border-team-red/40 bg-team-red/10 p-2.5"
          role="alert"
        >
          <span className="inline-flex items-center gap-1.5 text-sm font-medium text-team-red">
            <ShieldAlert className="size-4" aria-hidden />
            {t("stage5.provisionamento.falha.titulo", {
              defaultValue: "Provisionamento falhou",
            })}
          </span>
          {mensagemErro ? (
            <p className="text-xs text-team-red">{mensagemErro}</p>
          ) : null}
          <p className="text-xs text-muted-foreground">
            {t("stage5.provisionamento.falha.estagio6Bloqueado", {
              defaultValue:
                "O Estágio 6 (Execução da Campanha) permanece bloqueado até que o provisionamento conclua.",
            })}
          </p>
        </div>
      ) : prontos ? (
        /* Confirmação de ambiente pronto quando os três passam (Req. 5.6). */
        <div
          className="flex items-start gap-1.5 rounded-md border border-team-blue/40 bg-team-blue/10 p-2.5"
          role="status"
        >
          <CheckCircle2 className="mt-px size-4 shrink-0 text-team-blue" aria-hidden />
          <p className="text-sm font-medium text-team-blue">
            {t("stage5.provisionamento.prerequisitos.pronto", {
              defaultValue:
                "Ambiente pronto — todos os pré-requisitos foram verificados.",
            })}
          </p>
        </div>
      ) : bloqueado ? (
        /* Enquanto pendente/em andamento: o Estágio 6 segue bloqueado (Req. 7.3). */
        <p className="text-xs text-muted-foreground">
          {t("stage5.provisionamento.falha.estagio6Bloqueado", {
            defaultValue:
              "O Estágio 6 (Execução da Campanha) permanece bloqueado até que o provisionamento conclua.",
          })}
        </p>
      ) : null}
    </StageSection>
  )
}

/**
 * Um item do checklist: ícone por estado (Req. 5.6), rótulo, badge de status e —
 * em falha — a causa (e as razões por contêiner, nomeando o recurso ofensor,
 * Req. 7.2, 7.4).
 */
function PrereqItem({ item }: { item: ItemChecklist }) {
  const { t } = useTranslation("pipeline")
  const { Icon, cor, statusKey } = VISUAL_ESTADO[item.estado]

  const statusDefault =
    statusKey === "ok" ? "OK" : statusKey === "falha" ? "Falha" : "Pendente"
  const statusLabel = t(`stage5.provisionamento.prerequisitos.${statusKey}`, {
    defaultValue: statusDefault,
  })

  return (
    <li className="flex flex-col gap-1 rounded-md border p-2">
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-sm">
          <Icon className={cn("size-4 shrink-0", cor)} aria-hidden />
          {item.rotulo}
        </span>
        <span
          className={cn(
            "shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium",
            item.estado === "ok" && "bg-team-blue/10 text-team-blue",
            item.estado === "falha" && "bg-team-red/10 text-team-red",
            item.estado === "pendente" && "bg-muted text-muted-foreground",
          )}
        >
          {statusLabel}
        </span>
      </div>

      {/* Causa legível da falha deste item, consistente com o console (Req. 7.2, 7.5). */}
      {item.estado === "falha" && item.causa ? (
        <p className="text-xs text-team-red">{item.causa}</p>
      ) : null}
    </li>
  )
}
