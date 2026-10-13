import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Loader2, Sparkles } from "lucide-react"

import { MermaidDiagram } from "@/components/MermaidDiagram"
import { EmptyNote } from "@/components/StageSection"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import type { CaseStageStates } from "@/lib/casos"
import {
  escolherDag,
  getDagPorId,
  getDags,
  type DagDetalhe,
  type DagResumo,
} from "@/lib/dag"

/**
 * DagStageView — corpo do **Estágio 2 (Geração do DAG)** dentro do
 * `ExecutionWizard` (Req. 3). Sob o esquema de cinco Estágios, a Geração do DAG
 * é o Estágio 2 (derivado da Modelagem Estrutural do Estágio 1). Não é uma fase
 * roteada autônoma: a navegação (Avançar/Voltar) e o gate sequencial vivem no
 * wizard e no backend. Aqui apenas listamos os DAGs persistidos da Campanha,
 * exibimos o grafo do DAG selecionado e deixamos o Pesquisador escolher o DAG
 * que conclui o Estágio 2.
 *
 * Props (contrato enxuto para a costura do wizard):
 * - `casoId`: slug da Campanha; usado em `getDags`/`getDagPorId`/`escolherDag`.
 * - `onDagEscolhido`: chamado após uma escolha bem-sucedida com os
 *   {@link CaseStageStates} devolvidos pelo backend (Estágio 2 já `concluido` e
 *   Estágio 3 liberado). O wizard relê/deriva seu estado a partir daí.
 *
 * Estados renderizados (Req. 3.4, 3.5):
 * - **carregando** — nota `role="status"` enquanto `getDags` resolve.
 * - **erro** — mensagem pt-BR `stix.dag.erro` em caso de falha de carga/escolha.
 * - **sem DAG** (lista vazia) — nota `stix.dag.semDag` e o botão "Gerar com IA"
 *   **visível porém desabilitado** com a dica "em breve"; o avanço fica
 *   bloqueado (nenhum DAG a escolher). Clicar no botão é no-op (sem IA).
 * - **lista** — cada {@link DagResumo} com seu identificador/rótulo e a origem
 *   (`stix.dag.origem.<origem>`), a seleção do DAG, o grafo do selecionado via
 *   `MermaidDiagram` e a ação "Selecionar DAG" que chama `escolherDag`.
 *   O botão "Gerar com IA" segue visível e desabilitado (Req. 6.1, 6.5).
 */
export function DagStageView({
  casoId,
  onDagEscolhido,
}: {
  casoId: string
  onDagEscolhido?: (estados: CaseStageStates) => void
}) {
  const { t } = useTranslation("stix")

  const [dags, setDags] = useState<DagResumo[] | null>(null)
  const [erro, setErro] = useState<string | null>(null)
  const [carregando, setCarregando] = useState(true)

  // Carrega os DAGs persistidos da Campanha uma vez por slug (padrão de
  // active-flag das demais telas, para evitar set-state após desmontagem).
  useEffect(() => {
    let active = true
    setDags(null)
    setErro(null)
    setCarregando(true)
    getDags(casoId)
      .then((resp) => {
        if (!active) return
        setDags(resp.dags)
        setCarregando(false)
      })
      .catch(() => {
        if (!active) return
        setErro(t("stix.dag.erro"))
        setCarregando(false)
      })
    return () => {
      active = false
    }
  }, [casoId, t])

  return (
    <Card className="flex flex-col">
      <CardHeader className="gap-1 pb-3">
        <CardTitle className="text-base">{t("stix.dag.titulo")}</CardTitle>
        <CardDescription>{casoId}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 pt-0">
        {carregando ? (
          <p className="text-sm text-muted-foreground" role="status">
            {t("stix.dag.carregando")}
          </p>
        ) : erro ? (
          <EmptyNote>{erro}</EmptyNote>
        ) : dags && dags.length > 0 ? (
          <DagList
            casoId={casoId}
            dags={dags}
            onErro={() => setErro(t("stix.dag.erro"))}
            onDagEscolhido={onDagEscolhido}
          />
        ) : (
          <SemDag />
        )}

        {/* "Gerar DAG" é sempre visível porém desabilitado ("em breve"):
            no-op nesta iteração, nenhum cliente de IA é acionado (Req. 3.4,
            3.5). Mantido fora dos ramos para aparecer também na lista. */}
        {!carregando && !erro ? <GerarComIaButton /> : null}
      </CardContent>
    </Card>
  )
}

/**
 * Estado "sem DAG": a Campanha não possui nenhum DAG persistido, então o avanço
 * para além do Estágio 2 fica bloqueado (não há o que escolher) e exibimos a
 * nota `stix.dag.semDag` (Req. 3.4). O botão "Gerar DAG" desabilitado é
 * renderizado pelo pai.
 */
function SemDag() {
  const { t } = useTranslation("stix")
  return <EmptyNote>{t("stix.dag.semDag")}</EmptyNote>
}

/**
 * Botão "Gerar DAG" — visível e **desabilitado** com a dica "em breve"
 * (Req. 3.4). É um no-op: nenhum cliente de IA é invocado nesta iteração
 * (Req. 3.5).
 */
function GerarComIaButton() {
  const { t } = useTranslation("stix")
  return (
    <div className="flex items-center gap-2">
      <Button variant="outline" disabled aria-disabled title={t("stix.dag.emBreve")}>
        <Sparkles aria-hidden />
        {t("stix.dag.gerarComIa")}
      </Button>
      <span className="text-xs text-muted-foreground">({t("stix.dag.emBreve")})</span>
    </div>
  )
}

/**
 * Lista de DAGs persistidos (≥1): seleção, prévia do grafo do selecionado e a
 * ação "Selecionar DAG" que conclui o Estágio 2 (Req. 3.3, 3.7). O primeiro DAG
 * da lista é pré-selecionado para já exibir um grafo.
 */
function DagList({
  casoId,
  dags,
  onErro,
  onDagEscolhido,
}: {
  casoId: string
  dags: DagResumo[]
  onErro: () => void
  onDagEscolhido?: (estados: CaseStageStates) => void
}) {
  const { t } = useTranslation("stix")
  const [selecionadoId, setSelecionadoId] = useState<number>(dags[0].id)
  const [escolhendo, setEscolhendo] = useState(false)

  const handleEscolher = async () => {
    setEscolhendo(true)
    try {
      const estados = await escolherDag(casoId, selecionadoId)
      onDagEscolhido?.(estados)
    } catch {
      onErro()
    } finally {
      setEscolhendo(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-2" aria-label={t("stix.dag.selecionar")}>
        {dags.map((dag) => {
          const selecionado = dag.id === selecionadoId
          return (
            <li key={dag.id}>
              <button
                type="button"
                aria-pressed={selecionado}
                onClick={() => setSelecionadoId(dag.id)}
                className={
                  "flex w-full flex-col items-start gap-0.5 rounded-md border p-3 text-left text-sm transition-colors " +
                  (selecionado
                    ? "border-team-blue ring-1 ring-team-blue"
                    : "hover:bg-muted/50")
                }
              >
                <span className="font-medium">
                  {dag.rotulo || dag.identificador}
                </span>
                <span className="text-xs text-muted-foreground">
                  {t("stix.dag.origem.label")}: {t(`stix.dag.origem.${dag.origem}`)}
                </span>
              </button>
            </li>
          )
        })}
      </ul>

      <DagGraph casoId={casoId} dagId={selecionadoId} onErro={onErro} />

      <div className="flex items-center justify-end">
        <Button onClick={handleEscolher} disabled={escolhendo}>
          {escolhendo ? (
            <Loader2 className="animate-spin" aria-hidden />
          ) : null}
          {t("stix.dag.selecionar")}
        </Button>
      </div>
    </div>
  )
}

/**
 * Prévia do grafo do DAG selecionado: busca `getDagPorId` por `dagId` e o
 * renderiza com `MermaidDiagram` (que já fixa `securityLevel: "strict"`). Recarrega
 * a cada troca de `dagId`, com active-flag para descartar respostas obsoletas.
 */
function DagGraph({
  casoId,
  dagId,
  onErro,
}: {
  casoId: string
  dagId: number
  onErro: () => void
}) {
  const { t } = useTranslation("stix")
  const [detalhe, setDetalhe] = useState<DagDetalhe | null>(null)
  const [carregando, setCarregando] = useState(true)

  useEffect(() => {
    let active = true
    setDetalhe(null)
    setCarregando(true)
    getDagPorId(casoId, dagId)
      .then((d) => {
        if (!active) return
        setDetalhe(d)
        setCarregando(false)
      })
      .catch(() => {
        if (!active) return
        setCarregando(false)
        onErro()
      })
    return () => {
      active = false
    }
    // onErro é um callback estável do pai; recarrega apenas por caso/dag.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [casoId, dagId])

  if (carregando) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        {t("stix.dag.carregando")}
      </p>
    )
  }
  if (!detalhe || detalhe.mermaid.trim().length === 0) return null

  return (
    <div className="max-h-[60vh] overflow-auto rounded-md border bg-background p-3">
      <MermaidDiagram definition={detalhe.mermaid} />
    </div>
  )
}
