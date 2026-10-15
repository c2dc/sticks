import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2, Loader2 } from "lucide-react"

import { CampanhaStixBlock } from "@/components/Stage1View"
import { EmptyNote, StageSection } from "@/components/StageSection"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"
import {
  getEstagioCampanha,
  selecionarCampanha,
  type CampanhaListada,
  type Estagio2Response,
  type ResultadoSelecaoCampanha,
  type TechniqueElement,
} from "@/lib/estagios"

/**
 * CampanhaView — corpo do **Estágio 2 (Seleção da campanha)** no esquema de seis
 * Estágios (Req. 4.1, 6.2, 6.3, 6.5).
 *
 * O Estágio 2 recebe o bundle STIX da base selecionada no Estágio 1 e deixa o
 * Pesquisador escolher UMA campanha do pacote. Ao selecionar, o backend extrai
 * os atributos da campanha (identidade STIX + técnicas ATT&CK) e conclui o
 * Estágio quando a extração tem sucesso (Req. 6.3, 6.5, 6.6). As 8 campanhas
 * curadas são `selecionavel` na versão 18; demais campanhas aparecem na lista
 * porém desabilitadas (sem artefatos curados em disco — Req. 6.2, 6.5).
 *
 * Fluxo:
 * - Na montagem, lê `getEstagioCampanha(casoId, versao)` → lista de campanhas +
 *   a seleção corrente (read-only; abrir a tela nunca executa).
 * - Selecionar uma campanha chama `selecionarCampanha(casoId, slug, versao)` →
 *   {@link ResultadoSelecaoCampanha}. Uma falha de extração surge no resultado
 *   tipado (`concluido === false` com `mensagem_erro`/`etapa_falha`), não como
 *   erro HTTP (Req. 6.7).
 * - Os atributos extraídos são exibidos **reusando** o bloco de identidade de
 *   campanha do `Stage1View` ({@link CampanhaStixBlock}) mais a lista de
 *   técnicas; quando `concluido`, um indicador de conclusão é mostrado.
 *
 * Props (contrato enxuto para a costura do wizard na tarefa 14.4):
 * - `casoId`: slug da Campanha; usado nos wrappers do Estágio 2.
 * - `versao`: versão da base STIX selecionada no Estágio 1 (opcional; omitida,
 *   o backend aplica a versão curada padrão).
 * - `onConcluido`: chamado após uma extração bem-sucedida com o
 *   {@link ResultadoSelecaoCampanha} (`concluido === true`). O wizard relê/deriva
 *   seu estado a partir daí.
 */
export function CampanhaView({
  casoId,
  versao,
  onConcluido,
}: {
  casoId: string
  versao?: string
  onConcluido?: (
    slugCurado: string,
    resultado: ResultadoSelecaoCampanha,
  ) => void
}) {
  const { t } = useTranslation("pipeline")

  // Report an already-concluded selection to the wizard at most once per
  // load. When reopening Stage 2 for a campaign whose selection the backend
  // already persisted (read-only `selecao.concluido`), the wizard still needs
  // to learn the chosen slug so it adopts the campaignId and marks Stage 2
  // complete — without requiring the researcher to click "select" again.
  const conclusaoReportada = useRef<string | null>(null)
  // Keep the parent callback in a ref so `reportarConclusao` has a stable
  // identity: the parent (wizard) re-creates onConcluido on every render, and
  // depending on it directly would re-run the load effect in a loop.
  const onConcluidoRef = useRef(onConcluido)
  useEffect(() => {
    onConcluidoRef.current = onConcluido
  }, [onConcluido])
  const reportarConclusao = useCallback(
    (slug: string, resultado: ResultadoSelecaoCampanha) => {
      if (conclusaoReportada.current === slug) return
      conclusaoReportada.current = slug
      onConcluidoRef.current?.(slug, resultado)
    },
    [],
  )

  const [dados, setDados] = useState<Estagio2Response | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  // Seleção corrente (resultado da extração). Inicia com a seleção read-only
  // devolvida pela leitura do Estágio 2; atualiza a cada `selecionarCampanha`.
  const [selecao, setSelecao] = useState<ResultadoSelecaoCampanha | null>(null)
  const [slugSelecionado, setSlugSelecionado] = useState<string | null>(null)
  const [selecionando, setSelecionando] = useState(false)
  // Distingue "nenhuma seleção ainda" de "seleção tentada": a leitura read-only
  // inicial e uma extração falha podem ter o mesmo conteúdo vazio, então o
  // conteúdo sozinho não basta para decidir entre a nota de "não extraído" e o
  // bloco de falha. `selecaoAplicada` passa a `true` quando a leitura inicial já
  // traz uma seleção concluída OU quando o Pesquisador aciona `selecionar`.
  const [selecaoAplicada, setSelecaoAplicada] = useState(false)

  useEffect(() => {
    let active = true
    setDados(null)
    setLoadError(null)
    setSelecao(null)
    setSlugSelecionado(null)
    setSelecaoAplicada(false)
    conclusaoReportada.current = null
    getEstagioCampanha(casoId, versao)
      .then((resp) => {
        if (!active) return
        setDados(resp)
        setSelecao(resp.selecao)
        // A seleção read-only só "conta" como aplicada quando já veio concluída
        // (ex.: ao reabrir a tela de um caso que já selecionou uma campanha).
        setSelecaoAplicada(resp.selecao.concluido)
        // Stage 2 requires an explicit campaign choice: picking the campaign
        // to reproduce is the core action of this stage. We do NOT auto-report
        // a read-only selection nor auto-pick the first campaign, so the
        // researcher actively selects (and the wizard never adopts the probe's
        // campaign on its own). The read-only selection is still shown for
        // context when reopening the stage.
      })
      .catch(() => {
        if (active) setLoadError(t("stageView.loadError"))
      })
    return () => {
      active = false
    }
  }, [casoId, versao, t])

  const handleSelecionar = async (slug: string) => {
    setSelecionando(true)
    setLoadError(null)
    setSelecaoAplicada(true)
    try {
      const resultado = await selecionarCampanha(casoId, slug, versao)
      setSelecao(resultado)
      // Report the chosen curated slug so the wizard can adopt it as the
      // campaignId that drives Stages 3..6.
      if (resultado.concluido) reportarConclusao(slug, resultado)
    } catch {
      setLoadError(t("stageView.loadError"))
    } finally {
      setSelecionando(false)
    }
  }

  if (loadError) {
    return <EmptyNote>{loadError}</EmptyNote>
  }
  if (!dados) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stageView.loading")}
      </p>
    )
  }

  return (
    <div className="grid gap-4 md:grid-cols-3">
      {/* Lista de campanhas do bundle (Entrada) — seleção da campanha. */}
      <StageSection
        data-section="campanhas"
        label={t("campanha.titulo")}
        kind="blue"
        description={t("campanha.descricao")}
      >
        <ListaCampanhas
          campanhas={dados.campanhas}
          slugSelecionado={slugSelecionado}
          selecionando={selecionando}
          onSelecionarSlug={setSlugSelecionado}
          onSelecionar={handleSelecionar}
        />
      </StageSection>

      {/* Atributos extraídos (Saída) — identidade reusando o bloco do Stage1View
          + a lista de técnicas, e o indicador de conclusão. */}
      <StageSection
        data-section="atributos"
        label={t("campanha.atributos")}
        kind="neutral"
        className="md:col-span-2"
      >
        <AtributosExtraidos selecao={selecao} aplicada={selecaoAplicada} />
      </StageSection>
    </div>
  )
}

/**
 * Lista das campanhas do bundle. Campanhas `selecionavel` são escolhíveis (8
 * curadas na versão 18); as demais aparecem desabilitadas com a nota de
 * indisponibilidade (Req. 6.2, 6.5). O botão conclui a seleção chamando
 * `selecionarCampanha` com o slug curado da campanha escolhida.
 */
function ListaCampanhas({
  campanhas,
  slugSelecionado,
  selecionando,
  onSelecionarSlug,
  onSelecionar,
}: {
  campanhas: CampanhaListada[]
  slugSelecionado: string | null
  selecionando: boolean
  onSelecionarSlug: (slug: string) => void
  onSelecionar: (slug: string) => void
}) {
  const { t } = useTranslation("pipeline")

  if (campanhas.length === 0) {
    return <EmptyNote>{t("campanha.semCampanhas")}</EmptyNote>
  }

  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col gap-2" aria-label={t("campanha.listaLabel")}>
        {campanhas.map((campanha) => {
          const slug = campanha.slug_curado
          const selecionavel = campanha.selecionavel && Boolean(slug)
          const selecionado = selecionavel && slug === slugSelecionado
          return (
            <li key={campanha.stix_id}>
              <button
                type="button"
                aria-pressed={selecionado}
                disabled={!selecionavel || selecionando}
                onClick={() => slug && onSelecionarSlug(slug)}
                className={cn(
                  "flex w-full flex-col items-start gap-0.5 rounded-md border p-3 text-left text-sm transition-colors",
                  selecionado
                    ? "border-team-blue ring-1 ring-team-blue"
                    : "hover:bg-muted/50",
                  !selecionavel && "cursor-not-allowed opacity-60",
                )}
                title={!selecionavel ? t("campanha.naoSelecionavel") : undefined}
              >
                <span className="font-medium">{campanha.nome}</span>
                {campanha.descricao ? (
                  <span className="line-clamp-2 text-xs text-muted-foreground">
                    {campanha.descricao}
                  </span>
                ) : null}
                {!selecionavel ? (
                  <span className="text-xs text-muted-foreground">
                    {t("campanha.naoSelecionavel")}
                  </span>
                ) : null}
              </button>
            </li>
          )
        })}
      </ul>

      <div className="flex items-center justify-end">
        <Button
          size="sm"
          onClick={() => slugSelecionado && onSelecionar(slugSelecionado)}
          disabled={!slugSelecionado || selecionando}
        >
          {selecionando ? (
            <Loader2 className="mr-1 size-4 animate-spin" aria-hidden />
          ) : null}
          {selecionando
            ? t("campanha.selecionando")
            : t("campanha.selecionar")}
        </Button>
      </div>
    </div>
  )
}

/**
 * Painel dos atributos extraídos da campanha selecionada. Antes de qualquer
 * seleção/extração, mostra a nota `campanha.naoExtraido`. Após a extração:
 * - identidade STIX reusando {@link CampanhaStixBlock} (o mesmo bloco do
 *   Stage1View, Req. 6.3);
 * - a lista de técnicas ATT&CK extraídas (Req. 6.4);
 * - um indicador de conclusão quando `concluido`, ou a causa da falha
 *   (`mensagem_erro`/`etapa_falha`) quando a extração falhou (Req. 6.5, 6.7).
 */
function AtributosExtraidos({
  selecao,
  aplicada,
}: {
  selecao: ResultadoSelecaoCampanha | null
  /** `true` quando uma seleção já foi tentada/aplicada (vs. tela recém-aberta). */
  aplicada: boolean
}) {
  const { t } = useTranslation("pipeline")

  // Tela recém-aberta sem nenhuma seleção aplicada → nada a exibir ainda. Uma
  // extração falha (`aplicada` com `!concluido`) NÃO cai aqui: ela mostra o
  // bloco de falha abaixo com a causa reportada (Req. 6.7).
  if (!selecao || !aplicada) {
    return <EmptyNote>{t("campanha.naoExtraido")}</EmptyNote>
  }

  return (
    <div className="flex flex-col gap-3" data-section="atributos-extraidos">
      {/* Identidade STIX — reusa o bloco de campanha do Stage1View. */}
      <CampanhaStixBlock campanha={selecao.identidade} />

      {/* Técnicas ATT&CK extraídas. */}
      <TecnicasList tecnicas={selecao.tecnicas} />

      {/* Conclusão ou falha da extração. */}
      {selecao.concluido ? (
        <p
          className="flex items-center gap-1.5 rounded-md border border-team-blue/40 bg-team-blue/10 px-3 py-2 text-xs text-team-blue"
          data-testid="campanha-concluida"
          role="status"
        >
          <CheckCircle2 className="size-4" aria-hidden />
          {t("campanha.concluido")}
        </p>
      ) : (
        <div
          className="flex flex-col gap-1 rounded-md border border-team-red/30 bg-team-red/5 px-3 py-2 text-team-red"
          role="alert"
          data-testid="campanha-falha"
        >
          <p className="text-xs">{selecao.mensagem_erro || t("campanha.falha")}</p>
          {selecao.etapa_falha ? (
            <p className="text-xs text-muted-foreground">
              {t("campanha.falhaEtapa", { step: selecao.etapa_falha })}
            </p>
          ) : null}
        </div>
      )}
    </div>
  )
}

/** Lista das técnicas ATT&CK extraídas da campanha (Req. 6.4). */
function TecnicasList({ tecnicas }: { tecnicas: TechniqueElement[] }) {
  const { t } = useTranslation("pipeline")
  return (
    <div className="flex flex-col gap-1">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {t("campanha.tecnicas", { count: tecnicas.length })}
      </p>
      {tecnicas.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("campanha.semTecnicas")}</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {tecnicas.map((tecnica, index) => (
            <li
              key={tecnica.technique_id ?? index}
              className="rounded-md border border-team-blue/30 bg-team-blue/5 px-2 py-0.5 text-xs"
              title={tecnica.description ?? undefined}
            >
              <span className="font-medium">{tecnica.technique_id}</span>
              {tecnica.technique_name ? (
                <span className="text-muted-foreground">
                  {" "}
                  · {tecnica.technique_name}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
