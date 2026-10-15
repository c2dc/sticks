import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { TerminalSquare } from "lucide-react"

import { StageSection } from "@/components/StageSection"
import { cn } from "@/lib/utils"
import { connectEstagios } from "@/lib/casos"
import { getLogsEstagio5, type LinhaLogView } from "@/lib/estagios"

/**
 * ProvisioningConsole — terminal de logs ao vivo do Estágio 5 (Req. 2.1–2.5, 2.7).
 *
 * Exibe, como um console monoespaçado, as linhas de log do provisionamento
 * ativo do ambiente — os comandos executados e sua saída (stdout/stderr) ao
 * vivo (Req. 2.1, 2.2). O fluxo de dados é duplo e idempotente por `seq`:
 *
 * - **Recuperação ao montar (Req. 2.5, 2.7):** chama `getLogsEstagio5(casoId)`
 *   para semear o buffer corrente — assim, reabrir a tela (ou um reload) não
 *   perde o histórico já acumulado do provisionamento em andamento.
 * - **Assinatura ao vivo (Req. 2.2):** assina o canal do Estágio 5 via
 *   `connectEstagios(casoId, 5, …)`; cada frame carrega as linhas **novas**
 *   desde o último frame em `logs` (e, no connect, o buffer completo — o que,
 *   junto da recuperação acima, torna o histórico íntegro mesmo após uma
 *   reconexão — Req. 2.7).
 *
 * As linhas são acumuladas num `Map<seq, LinhaLogView>` (dedupe/reordenação por
 * `seq`) e **sempre** renderizadas em ordem crescente de `seq` (Req. 2.3). Cada
 * linha mostra um rótulo da etapa (Req. 2.4) e é colorida por `fluxo` (Req. 2.4:
 * `stderr` → team-red, `comando` → accent/negrito, `info` → muted,
 * `stdout` → cor padrão). O console faz auto-scroll para a linha mais recente
 * sempre que as linhas mudam.
 */
export function ProvisioningConsole({
  casoId,
  className,
}: {
  casoId: string
  className?: string
}) {
  const { t } = useTranslation("pipeline")
  // Buffer acumulado por `seq` — dedupe/reordenação idempotentes (Req. 2.3).
  const [linhasPorSeq, setLinhasPorSeq] = useState<Map<number, LinhaLogView>>(
    () => new Map(),
  )
  const scrollRef = useRef<HTMLDivElement>(null)

  // Mescla novas linhas no buffer por `seq` (recuperação + frames ao vivo).
  const acumular = (novas: readonly LinhaLogView[] | undefined) => {
    if (!novas || novas.length === 0) return
    setLinhasPorSeq((anterior) => {
      const proximo = new Map(anterior)
      for (const linha of novas) proximo.set(linha.seq, linha)
      return proximo
    })
  }

  useEffect(() => {
    let active = true
    // Reinicia o buffer ao trocar de caso.
    setLinhasPorSeq(new Map())

    // Recupera o buffer corrente ao montar (Req. 2.5, 2.7).
    getLogsEstagio5(casoId)
      .then((resposta) => {
        if (active) acumular(resposta.logs)
      })
      .catch(() => {
        // A recuperação é best-effort: o canal ao vivo preenche o buffer
        // completo no connect de qualquer modo (Req. 2.7).
      })

    // Assina o canal do Estágio 5; cada frame traz as linhas novas (Req. 2.2).
    const conexao = connectEstagios(casoId, 5, (frame) => {
      if (active) acumular(frame.logs)
    })

    return () => {
      active = false
      conexao.close()
    }
  }, [casoId])

  // Linhas sempre em ordem crescente de `seq` (Req. 2.3).
  const linhas = useMemo(
    () => Array.from(linhasPorSeq.values()).sort((a, b) => a.seq - b.seq),
    [linhasPorSeq],
  )

  // Auto-scroll para a linha mais recente sempre que as linhas mudam.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [linhas])

  return (
    <StageSection
      data-section="console"
      label={t("stage5.provisionamento.console.titulo")}
      kind="neutral"
      className={className}
    >
      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-label={t("stage5.provisionamento.console.titulo")}
        className="h-72 overflow-auto rounded-md border bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-100"
      >
        {linhas.length === 0 ? (
          <p className="flex items-center gap-1.5 text-zinc-400">
            <TerminalSquare className="size-3.5" aria-hidden />
            {t("stage5.provisionamento.console.vazio")}
          </p>
        ) : (
          <ul className="flex flex-col gap-0.5">
            {linhas.map((linha) => (
              <ConsoleLine key={linha.seq} linha={linha} />
            ))}
          </ul>
        )}
      </div>
    </StageSection>
  )
}

/** Cor por `fluxo` da linha (Req. 2.4). `info` muted, `stdout` padrão. */
const COR_FLUXO: Record<string, string> = {
  comando: "text-team-blue font-bold",
  stderr: "text-team-red",
  info: "text-zinc-400",
  stdout: "text-zinc-100",
}

/**
 * Uma linha do console: badge da etapa (Req. 2.4, distingue as etapas) + texto
 * colorido por `fluxo` (Req. 2.4). Rótulos via i18n com fallback para a string
 * crua da etapa/fluxo quando a chave ainda não existe (task 9.1).
 */
function ConsoleLine({ linha }: { linha: LinhaLogView }) {
  const { t } = useTranslation("pipeline")
  const corFluxo = COR_FLUXO[linha.fluxo] ?? "text-zinc-100"
  const etapaLabel = t(`stage5.provisionamento.console.etapas.${linha.etapa}`, {
    defaultValue: linha.etapa,
  })

  return (
    <li className="flex items-start gap-2">
      <span
        className="mt-px shrink-0 rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-zinc-300"
        title={etapaLabel}
      >
        {etapaLabel}
      </span>
      <span className={cn("whitespace-pre-wrap break-all", corFluxo)}>
        {linha.texto}
      </span>
    </li>
  )
}
