import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"

import { EmptyNote, StageSection } from "@/components/StageSection"
import { StageStateBadge } from "@/components/StageStateBadge"
import {
  getEstagio1,
  type CampanhaStixEntrada,
  type Stage1Result,
  type Stage1Technique,
} from "@/lib/estagios"

/**
 * Stage1View — Structural Modeling (Task 12.2, Req. 2).
 *
 * Renders the three labelled sections of Stage 1:
 * - Entrada: the source STIX intelligence of the selected case (Req. 2.1).
 *   The DAG is NOT an input here — it is produced in Stage 2 (DAG Generation).
 * - Processamento: the modeling state (nao_iniciado / em_andamento / concluido /
 *   erro), surfacing the failure cause and step when it errors (Req. 2.6).
 * - Saída: the extracted techniques, relationships, indicators, infrastructure,
 *   malware and metadata (Req. 2.3). When the extraction produced no structural
 *   elements (`saida.is_empty`), it shows the "no structural elements extracted"
 *   indication instead (Req. 2.5).
 */
export function Stage1View({
  casoId,
  versao,
}: {
  casoId: string
  /**
   * Versão da base STIX selecionada no fluxo (Req. 3.1, 3.2). Propagada ao
   * read `getEstagio1(casoId, versao)` para que a Entrada recorte o objeto
   * `campaign` da base correta. Opcional/retrocompatível: quando ausente, o
   * backend aplica o fallback (`versao_ausente`) sem erro.
   */
  versao?: string
}) {
  const { t } = useTranslation("pipeline")
  const [result, setResult] = useState<Stage1Result | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    setResult(null)
    setLoadError(null)
    getEstagio1(casoId, versao)
      .then((data) => {
        if (active) setResult(data)
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, versao, t])

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

  const { entrada, saida, estado } = result

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Entrada — source STIX intelligence (Req. 2.1). The DAG is produced
          in Stage 2, so it is not shown as an input here. */}
      <StageSection
        data-section="entrada"
        label={t("sections.entrada")}
        kind="blue"
      >
        {/* Entrada da Modelagem Estrutural é a identidade da campanha STIX da
            base selecionada (recorte do objeto `campaign`). O DAG e qualquer
            caminho/arquivo da Caldera NÃO são exibidos aqui (Property 3). */}
        <CampanhaStixBlock campanha={entrada.campanha_stix} />
      </StageSection>

      {/* Processamento — modeling state + failure cause/step (Req. 2.6). */}
      <StageSection
        data-section="processamento"
        label={t("sections.processamento")}
        kind="purple"
      >
        <div className="flex items-center gap-2">
          <StageStateBadge estado={estado} />
        </div>
        {estado === "erro" ? (
          <div className="flex flex-col gap-1 text-team-red">
            <p className="text-sm">{result.mensagem_erro || t("error.unknown")}</p>
            {result.etapa_falha ? (
              <p className="text-xs text-muted-foreground">
                {t("stage1.processamento.failedStep", { step: result.etapa_falha })}
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            {t("stage1.processamento.description")}
          </p>
        )}
      </StageSection>

      {/* Saída — extracted structural elements, or absence indication (2.3/2.5).
          `saida` is null until the stage actually runs (read-only GET): show a
          "not run yet" note instead of fabricating a completed output. */}
      <StageSection
        data-section="saida"
        label={t("sections.saida")}
        kind="neutral"
      >
        {!saida ? (
          <EmptyNote>{t("stage1.saida.notRun")}</EmptyNote>
        ) : saida.is_empty ? (
          <EmptyNote>{saida.note || t("stage1.saida.empty")}</EmptyNote>
        ) : (
          <div className="flex flex-col gap-3">
            <TechniquesList techniques={saida.techniques} />
            <CountRow
              label={t("stage1.saida.relationships")}
              count={saida.relationships.length}
            />
            <CountRow
              label={t("stage1.saida.indicators")}
              count={saida.indicators.length}
            />
            <CountRow
              label={t("stage1.saida.infrastructure")}
              count={saida.infrastructure.length}
            />
            <CountRow label={t("stage1.saida.malware")} count={saida.malware.length} />
            <MetadataBlock metadata={saida.metadata} />
          </div>
        )}
      </StageSection>
    </div>
  )
}

/**
 * Entrada do Estágio 1: identidade da campanha STIX da base selecionada
 * (recorte do objeto `campaign` — Req. 1.1, 2.1).
 *
 * - Encontrada (`encontrada === true`): renderiza nome, ID STIX, external_id,
 *   descrição legível (scroll/limite de altura), aliases (chips), datas e versão
 *   da base. Campos ausentes (null/empty) são OMITIDOS — nunca renderiza linhas
 *   vazias/confusas (Req. 2.3, 2.4).
 * - Não encontrada (falsy ou `encontrada === false`): mostra um aviso claro
 *   (`role="alert"`) e o identificador curado disponível (nome + external_id),
 *   mantendo a tela funcional (Req. 2.1, 4.2).
 *
 * Nunca exibe caminhos/arquivos da Caldera (Property 3).
 */
export function CampanhaStixBlock({
  campanha,
}: {
  campanha?: CampanhaStixEntrada | null
}) {
  const { t } = useTranslation("pipeline")

  if (!campanha || campanha.encontrada !== true) {
    const curado = [campanha?.nome, campanha?.external_id]
      .filter((v): v is string => Boolean(v))
      .join(" · ")
    return (
      <div
        className="flex flex-col gap-1 rounded-md border border-team-red/30 bg-team-red/5 p-2"
        role="alert"
        data-testid="campanha-stix-nao-encontrada"
      >
        <p className="text-sm text-team-red">
          {t("stage1.entrada.naoEncontrada")}
        </p>
        {curado ? (
          <p className="break-all text-xs text-muted-foreground">{curado}</p>
        ) : null}
      </div>
    )
  }

  return (
    <div
      className="flex flex-col gap-2"
      data-testid="campanha-stix"
      data-section="campanha-stix"
    >
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage1.entrada.campanhaStix")}
      </p>
      <dl className="flex flex-col gap-2">
        <Field label={t("stage1.entrada.nome")} value={campanha.nome} />
        <Field label={t("stage1.entrada.stixId")} value={campanha.stix_id} />
        <Field
          label={t("stage1.entrada.externalId")}
          value={campanha.external_id}
        />
        {campanha.descricao ? (
          <div className="flex flex-col">
            <dt className="text-xs font-medium text-muted-foreground">
              {t("stage1.entrada.descricao")}
            </dt>
            <dd className="max-h-40 overflow-auto whitespace-pre-wrap text-sm">
              {campanha.descricao}
            </dd>
          </div>
        ) : null}
        {campanha.aliases.length > 0 ? (
          <div className="flex flex-col gap-1">
            <dt className="text-xs font-medium text-muted-foreground">
              {t("stage1.entrada.aliases")}
            </dt>
            <dd>
              <ul className="flex flex-wrap gap-1.5">
                {campanha.aliases.map((alias, index) => (
                  <li
                    key={`${alias}-${index}`}
                    className="rounded-md border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
                  >
                    {alias}
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
        <Field
          label={t("stage1.entrada.firstSeen")}
          value={campanha.first_seen}
        />
        <Field label={t("stage1.entrada.lastSeen")} value={campanha.last_seen} />
        <Field
          label={t("stage1.entrada.versaoBase")}
          value={campanha.versao_base}
        />
      </dl>
    </div>
  )
}

/**
 * Linha label/valor da Entrada. Omite-se por completo quando `value` é ausente
 * (null/undefined/vazio) para não renderizar linhas vazias/confusas (Req. 2.4).
 */
function Field({
  label,
  value,
}: {
  label: string
  value?: string | null
}) {
  if (!value) return null
  return (
    <div className="flex flex-col">
      <dt className="text-xs font-medium text-muted-foreground">{label}</dt>
      <dd className="break-all text-sm">{value}</dd>
    </div>
  )
}

function TechniquesList({ techniques }: { techniques: Stage1Technique[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage1.saida.techniques", { count: techniques.length })}
      </p>
      {techniques.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("stage1.saida.noTechniques")}</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {techniques.map((tech, index) => (
            <li
              key={tech.technique_id ?? index}
              className="rounded-md border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
              title={tech.description ?? undefined}
            >
              <span className="font-medium">{tech.technique_id ?? "—"}</span>
              {tech.name ? <span className="text-muted-foreground"> · {tech.name}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CountRow({ label, count }: { label: string; count: number }) {
  return (
    <div className="flex items-center justify-between text-sm">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">{count}</span>
    </div>
  )
}

function MetadataBlock({ metadata }: { metadata: Record<string, unknown> }) {
  const { t } = useTranslation("pipeline")
  const entries = Object.entries(metadata ?? {})
  if (entries.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("stage1.saida.metadata")}
      </p>
      <dl className="flex flex-col gap-0.5">
        {entries.map(([key, value]) => (
          <div key={key} className="flex justify-between gap-3 text-xs">
            <dt className="text-muted-foreground">{key}</dt>
            <dd className="break-all text-right">{String(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
