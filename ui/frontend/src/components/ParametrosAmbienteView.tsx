import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Boxes, ServerCog, ShieldAlert } from "lucide-react"

import { EmptyNote, StageSection } from "@/components/StageSection"
import {
  getEstagioOperacionalizacao,
  type HostAlvo,
  type OrigemTraducao,
  type ParametrosDeAmbiente,
  type PlataformaAmbiente,
  type ServicoRequerido,
} from "@/lib/estagios"

/**
 * ParametrosAmbienteView — painel dos parâmetros de ambiente do Estágio 4
 * (Operacionalização), exibido ao lado do `Stage2View` (Abilities) — Task 14.3,
 * Req. 4.1, 8.3, 8.4.
 *
 * O Estágio 4 produz dois artefatos tipados separados: as Abilities ordenadas
 * (renderizadas pelo `Stage2View`) e os `ParametrosDeAmbiente` destinados ao
 * Estágio 5. Esta tela cuida do segundo artefato, mostrando:
 *
 * - A **plataforma** do ambiente (contêiner hoje; VM admitida sem redesenho —
 *   Req. 17.1, 17.2) e a **origem** da tradução (explícita e extensível).
 * - Os **hosts-alvo** derivados dos nós/comandos do DAG curado: rótulo,
 *   endereço, container, tipo de host e vulnerabilidades (Req. 8.3).
 * - Os **serviços/vulnerabilidades** a provisionar, inferidos dos comandos, com
 *   a técnica de origem (Req. 8.3).
 * - O **`network_context`** derivado dos nós (notas de contexto de rede).
 *
 * É somente-leitura: carrega o payload do Estágio 4 na montagem e trata os
 * estados de carregamento e de erro de carga como as telas irmãs
 * (Stage2View / EnvironmentStageView). Reusa `getEstagioOperacionalizacao` e
 * extrai apenas o campo `parametros` do `Estagio4Response`.
 */
export function ParametrosAmbienteView({ casoId }: { casoId: string }) {
  const { t } = useTranslation("pipeline")
  const [parametros, setParametros] = useState<ParametrosDeAmbiente | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setParametros(null)
    setLoadError(null)
    getEstagioOperacionalizacao(casoId)
      .then((data) => {
        if (active) setParametros(data.parametros)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, t])

  if (loadError) return <EmptyNote>{loadError}</EmptyNote>
  if (!parametros) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  const { plataforma, origem, hosts, servicos, network_context } = parametros

  return (
    <div className="flex flex-col gap-4" data-section="parametros-ambiente">
      {/* Plataforma + origem da tradução. */}
      <StageSection
        data-section="plataforma"
        label={t("parametrosAmbiente.titulo")}
        kind="purple"
        description={t("parametrosAmbiente.descricao")}
      >
        <div className="flex flex-wrap items-center gap-2">
          <PlataformaBadge plataforma={plataforma} />
          <OrigemBadge origem={origem} />
        </div>
      </StageSection>

      {/* Hosts-alvo derivados dos nós do DAG curado (Req. 8.3). */}
      <StageSection
        data-section="hosts"
        label={t("parametrosAmbiente.hosts.titulo")}
        kind="blue"
      >
        {hosts.length === 0 ? (
          <EmptyNote>{t("parametrosAmbiente.hosts.vazio")}</EmptyNote>
        ) : (
          <ul className="grid gap-2 sm:grid-cols-2">
            {hosts.map((host, index) => (
              <HostAlvoCard key={`${host.rotulo ?? "host"}-${index}`} host={host} />
            ))}
          </ul>
        )}
      </StageSection>

      {/* Serviços/vulnerabilidades inferidos dos comandos (Req. 8.3). */}
      <StageSection
        data-section="servicos"
        label={t("parametrosAmbiente.servicos.titulo")}
        kind="red"
      >
        {servicos.length === 0 ? (
          <EmptyNote>{t("parametrosAmbiente.servicos.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-col gap-2">
            {servicos.map((servico, index) => (
              <ServicoCard key={index} servico={servico} />
            ))}
          </ul>
        )}
      </StageSection>

      {/* network_context derivado dos nós (notas de contexto de rede). */}
      <StageSection
        data-section="network-context"
        label={t("parametrosAmbiente.networkContext.titulo")}
        kind="neutral"
      >
        {network_context.length === 0 ? (
          <EmptyNote>{t("parametrosAmbiente.networkContext.vazio")}</EmptyNote>
        ) : (
          <ul className="flex flex-col gap-1">
            {network_context.map((nota, index) => (
              <li
                key={index}
                className="rounded-md border bg-muted/40 px-2 py-1 text-xs"
              >
                {nota}
              </li>
            ))}
          </ul>
        )}
      </StageSection>
    </div>
  )
}

/**
 * The platform indicator — contêiner today, VM admitted without a redesign
 * (Req. 17.1, 17.2). Falls back to the raw value for a future, not-yet-
 * translated platform so a new value shows without a model change.
 */
function PlataformaBadge({ plataforma }: { plataforma: PlataformaAmbiente }) {
  const { t } = useTranslation("pipeline")
  const key = `parametrosAmbiente.plataforma.${plataforma}`
  const translated = t(key)
  const label = translated === key ? plataforma : translated

  return (
    <span
      className="inline-flex w-fit items-center gap-1.5 rounded-full border border-team-purple/30 bg-team-purple/5 px-2.5 py-0.5 text-xs font-medium text-team-purple"
      data-plataforma={plataforma}
    >
      <Boxes className="size-3.5" aria-hidden />
      {label}
    </span>
  )
}

/**
 * The translation-source indicator (Req. 16.2, 16.4). Explicit, extensible —
 * falls back to the raw value for future origins so a new AI-assisted source
 * shows without a model change.
 */
function OrigemBadge({ origem }: { origem: OrigemTraducao }) {
  const { t } = useTranslation("pipeline")
  const key = `stage2.translationSource.${origem}`
  const translated = t(key)
  const label = translated === key ? origem : translated

  return (
    <span className="inline-flex w-fit items-center gap-1.5 rounded-full border bg-muted px-2.5 py-0.5 text-xs font-medium text-foreground" data-origem={origem}>
      <span className="text-muted-foreground">
        {t("stage2.translationSource.label")}:
      </span>
      {label}
    </span>
  )
}

/** One target-host card: rótulo / endereço / container / tipo / vulns. */
function HostAlvoCard({ host }: { host: HostAlvo }) {
  const { t } = useTranslation("pipeline")
  return (
    <li className="flex flex-col gap-1 rounded-md border p-2" data-host={host.rotulo}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-sm font-medium" title={host.rotulo}>
          {host.rotulo}
        </span>
        {host.host_type ? (
          <span className="shrink-0 rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground">
            {host.host_type}
          </span>
        ) : null}
      </div>
      {host.endereco ? (
        <p className="truncate text-xs text-muted-foreground" title={host.endereco}>
          <span className="text-muted-foreground">
            {t("parametrosAmbiente.hosts.endereco")}:{" "}
          </span>
          <code className="font-mono">{host.endereco}</code>
        </p>
      ) : null}
      {host.container ? (
        <p className="truncate text-xs text-muted-foreground" title={host.container}>
          <span className="text-muted-foreground">
            {t("parametrosAmbiente.hosts.container")}:{" "}
          </span>
          <code className="font-mono">{host.container}</code>
        </p>
      ) : null}
      {host.vulnerabilidades.length > 0 ? (
        <div className="flex flex-col gap-0.5">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            {t("parametrosAmbiente.hosts.vulnerabilidades")}
          </span>
          <ul className="flex flex-wrap gap-1">
            {host.vulnerabilidades.map((vuln, index) => (
              <li
                key={index}
                className="inline-flex items-center gap-1 rounded border border-team-red/30 bg-team-red/5 px-1.5 py-0.5 text-[11px] text-team-red"
              >
                <ShieldAlert className="size-3" aria-hidden />
                {vuln}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  )
}

/** One required service/vulnerability with its originating technique. */
function ServicoCard({ servico }: { servico: ServicoRequerido }) {
  const { t } = useTranslation("pipeline")
  return (
    <li className="flex items-start gap-2 rounded-md border p-2">
      <ServerCog className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
      <div className="flex min-w-0 flex-col">
        <span className="text-sm">{servico.descricao}</span>
        {servico.tecnica_origem ? (
          <span className="text-xs text-muted-foreground">
            {t("parametrosAmbiente.servicos.tecnicaOrigem")}:{" "}
            <code className="font-mono">{servico.tecnica_origem}</code>
          </span>
        ) : null}
      </div>
    </li>
  )
}
