import { useCallback, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { CheckCircle2 } from "lucide-react"

import { StageSection } from "@/components/StageSection"
import { StixBasesList } from "@/components/StixBasesList"
import { StixIntelligenceDashboard } from "@/components/StixIntelligenceDashboard"
import { Button } from "@/components/ui/button"
import { getEstagioBaseStix, type ResultadoEstagio1 } from "@/lib/estagios"
import { cn } from "@/lib/utils"

export interface BaseStixViewProps {
  /** Slug da Campanha; usado para resolver o resultado do Estágio 1. */
  casoId: string
  /**
   * Versão da base STIX já selecionada no fluxo (controlada pelo wizard —
   * tarefa 14.4). Quando ausente, a tela mostra a lista de bases para o
   * Pesquisador escolher. Quando presente, mostra o dashboard do bundle.
   */
  versao?: string
  /**
   * Chamado quando o Pesquisador escolhe uma base na lista (Req. 5.1). O wizard
   * guarda a seleção e a repassa de volta por `versao`, para que a Saída do
   * Estágio 1 (bundle + dashboard) seja a Entrada do Estágio 2 (Req. 2.2).
   */
  onVersaoSelecionada?: (versao: string) => void
  /**
   * Reportado sempre que o veredito de conclusão do Estágio 1 é recalculado
   * (Req. 5.8, 5.10). `true` **somente quando** o bundle da versão selecionada
   * está em disco **e** o dashboard foi exibido; o wizard usa isso para liberar
   * o controle "Próximo". Qualquer falha mantém o Estágio não concluído
   * (`false`), com a causa reportada na tela (Req. 5.9).
   */
  onConclusaoChange?: (concluido: boolean) => void
}

/**
 * BaseStixView — corpo do **Estágio 1 (Seleção da base STIX)** do pipeline de
 * seis Estágios (Req. 4.1, 5). Não é uma fase roteada autônoma: a navegação
 * (Próximo/Voltar) e o gate sequencial vivem no `ExecutionWizard` (tarefa 14.4)
 * e no backend. Aqui conduzimos a seleção da base e exibimos o dashboard do
 * bundle, reusando as telas do fluxo `fluxo-stix-dashboard`:
 *
 * - **Sem versão selecionada** → {@link StixBasesList}: lista as versões
 *   conhecidas (nenhuma baixada por padrão), baixa a versão ausente da fonte
 *   oficial do MITRE e deixa selecionar uma base já em disco (Req. 5.1–5.4).
 *   Escolher uma base dispara {@link BaseStixViewProps.onVersaoSelecionada}.
 * - **Com versão selecionada** → {@link StixIntelligenceDashboard}: exibe o
 *   dashboard do bundle (contagens por tipo) da base em disco (Req. 5.7, 5.8),
 *   com uma ação "Trocar base" que volta à lista.
 *
 * Em paralelo, lê `getEstagioBaseStix(casoId, versao)` para obter o veredito de
 * conclusão tipado ({@link ResultadoEstagio1}): o Estágio 1 só pode ser
 * concluído quando `bundle_em_disco` e o dashboard foram montados — refletido
 * por `concluido`. Esse veredito é propagado por
 * {@link BaseStixViewProps.onConclusaoChange} para o wizard habilitar "Próximo"
 * (Req. 5.8, 5.10). Uma falha de download/leitura mantém o Estágio não
 * concluído e mostra a causa (Req. 5.9).
 */
export function BaseStixView({
  casoId,
  versao,
  onVersaoSelecionada,
  onConclusaoChange,
}: BaseStixViewProps) {
  const { t } = useTranslation("pipeline")

  const [resultado, setResultado] = useState<ResultadoEstagio1 | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [verificando, setVerificando] = useState(false)

  // Guarda a última conclusão reportada para só notificar o wizard em mudanças
  // reais (evita loops de render quando o callback é recriado a cada render).
  const ultimaConclusao = useRef<boolean | null>(null)
  const notificarConclusao = useCallback(
    (concluido: boolean) => {
      if (ultimaConclusao.current === concluido) return
      ultimaConclusao.current = concluido
      onConclusaoChange?.(concluido)
    },
    [onConclusaoChange],
  )

  // Resolve o veredito de conclusão sempre que a versão selecionada muda. Sem
  // versão, não há base em disco a confirmar → Estágio não concluído.
  useEffect(() => {
    let active = true
    setLoadError(null)
    setResultado(null)

    if (!versao) {
      setVerificando(false)
      notificarConclusao(false)
      return
    }

    setVerificando(true)
    getEstagioBaseStix(casoId, versao)
      .then((data) => {
        if (!active) return
        setResultado(data)
        notificarConclusao(data.concluido)
      })
      .catch(() => {
        if (!active) return
        setLoadError(t("baseStix.erro"))
        notificarConclusao(false)
      })
      .finally(() => {
        if (active) setVerificando(false)
      })

    return () => {
      active = false
    }
  }, [casoId, versao, t, notificarConclusao])

  return (
    <div className="flex flex-col gap-4">
      <StageSection
        data-section="base-stix"
        label={t("baseStix.titulo")}
        kind="blue"
        description={t("baseStix.descricao")}
      >
        {!versao ? (
          // Sem base selecionada: lista + download (Req. 5.1–5.4).
          <StixBasesList
            onSelecionarBase={(v) => onVersaoSelecionada?.(v)}
          />
        ) : (
          // Base selecionada: dashboard do bundle (Req. 5.7, 5.8).
          <div className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-muted-foreground">
                {t("baseStix.selecionada", { versao })}
              </p>
              {onVersaoSelecionada ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onVersaoSelecionada("")}
                >
                  {t("baseStix.trocarBase")}
                </Button>
              ) : null}
            </div>

            {/* Reusa a tela de dashboard do fluxo fluxo-stix-dashboard. O CTA
                "Selecionar campanha/ator" não avança no wizard de seis Estágios
                (a navegação é do ExecutionWizard — tarefa 14.4), então é um
                no-op aqui. */}
            {/* Embedded in Stage 1: the wizard owns navigation, so the
                dashboard's own "select campaign/actor" CTA is hidden. */}
            <StixIntelligenceDashboard versao={versao} hideCta />
          </div>
        )}
      </StageSection>

      {/* Veredito de conclusão do Estágio 1 (Req. 5.8, 5.10): só pronto quando o
          bundle está em disco e o dashboard foi exibido. */}
      <ConclusaoBanner
        verificando={verificando}
        loadError={loadError}
        resultado={resultado}
      />
    </div>
  )
}

/**
 * Faixa de estado de conclusão do Estágio 1. Mostra:
 * - a causa da falha quando a leitura/validação falhou (`loadError` ou
 *   `resultado.mensagem_erro`), mantendo o Estágio não concluído (Req. 5.9);
 * - a nota de "verificando" enquanto o veredito é resolvido;
 * - o indicador de "pronto" quando `concluido` (bundle em disco + dashboard —
 *   Req. 5.8, 5.10);
 * - a nota de "aguardando" nos demais casos (ex.: nenhuma base selecionada).
 */
function ConclusaoBanner({
  verificando,
  loadError,
  resultado,
}: {
  verificando: boolean
  loadError: string | null
  resultado: ResultadoEstagio1 | null
}) {
  const { t } = useTranslation("pipeline")

  const erro = loadError ?? resultado?.mensagem_erro ?? null
  if (erro) {
    return (
      <p
        className="rounded-md border border-team-red/40 bg-team-red/10 px-3 py-2 text-sm text-team-red"
        role="alert"
        data-section="conclusao"
      >
        {erro}
      </p>
    )
  }

  if (verificando) {
    return (
      <p
        className="text-sm text-muted-foreground"
        role="status"
        data-section="conclusao"
      >
        {t("baseStix.verificando")}
      </p>
    )
  }

  const concluido = resultado?.concluido ?? false
  return (
    <p
      className={cn(
        "flex items-center gap-1.5 rounded-md border px-3 py-2 text-sm",
        concluido
          ? "border-team-blue/40 bg-team-blue/10 text-team-blue"
          : "text-muted-foreground",
      )}
      role="status"
      data-section="conclusao"
      data-concluido={concluido}
    >
      {concluido ? <CheckCircle2 className="size-4" aria-hidden /> : null}
      {concluido ? t("baseStix.pronto") : t("baseStix.aguardando")}
    </p>
  )
}
