/**
 * Typed API wrapper for the explicit DAG Generation/Visualization phase
 * (DagView screen — Req. 5, 6).
 *
 * Wraps the single DAG endpoint:
 * - `GET /api/casos/{caso}/dag` -> {@link DagView}
 *
 * The backend contract is the source of truth (pt-BR field names, see
 * `ui/backend/app/services/dag/dag_service.py`). The view covers the
 * three states of the contract:
 * - Curated valid case: `curado=true`, `pode_avancar=true`, with `nos`/`arestas`
 *   filled and `mermaid` assembled (handed straight to the MermaidDiagram).
 * - Non-curated case ("passa-direto"): `curado=false`, `pode_avancar=false`,
 *   `indisponivel=true` and a `mensagem` — no AI is ever invoked (Req. 5.4, 5.5).
 * - Curated but missing/malformed DAG: typed error (`curado=true`,
 *   `pode_avancar=false`, `erro`) (Req. 5.3, 6.4).
 *
 * This module lives beside `src/lib/estagios.ts` and mirrors its style.
 *
 * No esquema de cinco Estágios, a Geração do DAG é o **Estágio 2** (derivada da
 * Modelagem Estrutural do Estágio 1) e os DAGs são entidades persistidas e
 * multi-origem (Req. 3). Este módulo adiciona, além do `getDag` legado (DagView
 * somente-leitura), os tipos/wrappers dos endpoints de DAG persistido (Req. 3.7, 8.7):
 * - `GET  /api/casos/{caso}/dags`                 -> {@link DagResumo}[] (lista)
 * - `GET  /api/casos/{caso}/dags/{dag_id}`        -> {@link DagDetalhe} (grafo)
 * - `POST /api/casos/{caso}/estagio/3/escolher-dag` -> {@link CaseStageStates}
 */
import { api } from "@/lib/api"
import type { CaseStageStates } from "@/lib/casos"

// ---------------------------------------------------------------------------
// DAG view — `GET /api/casos/{caso}/dag` (Req. 5, 6).
// ---------------------------------------------------------------------------

/**
 * One node of the DAG as shown in the payload (the `nos` entries): the
 * structural node's `node_id` plus its ATT&CK technique identity (Req. 5.2).
 */
export interface DagNoView {
  node_id: string
  technique_id: string | null
  technique_name: string | null
  tactic: string | null
}

/**
 * One directed edge of the DAG (an `arestas` entry): `de` is the source
 * node_id, `para` the target node_id (Req. 5.2, 6.1).
 */
export interface DagArestaView {
  de: string
  para: string
}

/**
 * The DAG view returned by `GET /api/casos/{caso}/dag` (Req. 5, 6). `nos`,
 * `arestas` and `mermaid` are filled only for a curated valid case; the stub /
 * error fields (`indisponivel`, `mensagem`, `erro`) are populated only in the
 * non-curated or error states.
 */
export interface DagView {
  caso_id: string
  nome: string
  curado: boolean
  pode_avancar: boolean
  nos: DagNoView[]
  arestas: DagArestaView[]
  mermaid: string
  indisponivel: boolean
  mensagem: string | null
  erro: string | null
}

/**
 * Fetch the DAG view (nós / arestas / Mermaid, or stub / error) for a case.
 *
 * Endpoint legado `GET /api/casos/{caso}/dag`: mantido para compatibilidade.
 * No esquema persistido (Req. 4), ele passa a resolver o DAG escolhido (ou, na
 * ausência de escolha, o único DAG curado) via `obter_dag_persistido`; a forma
 * de resposta {@link DagView} permanece.
 */
export function getDag(caso: string): Promise<DagView> {
  return api.get<DagView>(`/casos/${encodeURIComponent(caso)}/dag`)
}

// ---------------------------------------------------------------------------
// DAG persistido e multi-origem — Estágio 2 (Req. 3). Backend: pt-BR field
// names (ver `ui/backend/app/services/dag/dag_service.py` e `app/api/casos.py`).
// ---------------------------------------------------------------------------

/**
 * Origem de um DAG — atributo explícito e extensível (Req. 3.7, 10.4).
 *
 * Segue exatamente o precedente de `OrigemTraducao`/`TranslationSource`:
 * `"curadoria_humana"` é o valor dos DAGs curados; `"gerado_ia"` já é aceito
 * pelo esquema (CHECK) para a implementação de IA encaixável depois (Req. 9). A
 * união permanece aberta (`string & {}`) para novos valores futuros sem
 * alteração de contrato no frontend.
 */
export type OrigemDag = "curadoria_humana" | "gerado_ia" | (string & {})

/**
 * Resumo de um DAG persistido, para a listagem de uma Campanha
 * (`GET /api/casos/{caso}/dags`, backend `DagResumo` — Req. 3.7). Carrega as
 * colunas de identidade — sem o `conteudo` gráfico, obtido sob demanda por
 * {@link getDagPorId}.
 */
export interface DagResumo {
  id: number
  caso_id: string
  origem: OrigemDag
  identificador: string
  rotulo?: string | null
  /** ISO-8601 timestamp (`criado_em` do backend). */
  criado_em: string
}

/**
 * `GET /api/casos/{caso}/dags` payload — DAGs persistidos da Campanha (backend
 * `DagListResponse`, Req. 3.7). A listagem é precedida de um backfill
 * idempotente dos DAGs curados, de modo que um Caso curado sempre exibe ao
 * menos o seu DAG de curadoria.
 */
export interface DagListResponse {
  caso_id: string
  dags: DagResumo[]
}

/**
 * O grafo + Mermaid de um DAG persistido (`GET /api/casos/{caso}/dags/{dag_id}`,
 * Req. 4.4). Reusa a forma gráfica de {@link DagView} (`nos`/`arestas`/`mermaid`,
 * entregues direto ao `MermaidDiagram`) e acrescenta a identidade persistida do
 * DAG (`dag_id`, `origem`, `identificador`).
 */
export interface DagDetalhe {
  caso_id: string
  dag_id: number
  origem: OrigemDag
  identificador: string
  rotulo?: string | null
  nos: DagNoView[]
  arestas: DagArestaView[]
  mermaid: string
}

/**
 * Lista os DAGs persistidos de uma Campanha (Estágio 2 — Req. 3.7, 8.7). Um
 * Caso curado sempre devolve ao menos o seu DAG de curadoria (backfill
 * idempotente no backend).
 */
export function getDags(caso: string): Promise<DagListResponse> {
  return api.get<DagListResponse>(
    `/casos/${encodeURIComponent(caso)}/dags`,
  )
}

/**
 * Obtém o grafo + Mermaid de um DAG persistido pelo seu `dagId` (Req. 4.4).
 * Responde 404 quando o `dagId` é inexistente ou não pertence ao Caso
 * (propagado como {@link ApiError} pelo cliente HTTP).
 */
export function getDagPorId(caso: string, dagId: number): Promise<DagDetalhe> {
  return api.get<DagDetalhe>(
    `/casos/${encodeURIComponent(caso)}/dags/${encodeURIComponent(dagId)}`,
  )
}

/**
 * Escolhe um DAG para a execução, concluindo o Estágio 3 (Geração do DAG) e
 * liberando o Estágio 4 (`POST /api/casos/{caso}/estagio/3/escolher-dag`,
 * Req. 8.7). Devolve o estado por Estágio ({@link CaseStageStates}) já com o
 * Estágio 3 `concluido` e o 4 liberado.
 *
 * Falhas tipadas do backend (propagadas como {@link ApiError} com o `detail`
 * pt-BR `{motivo, mensagem}`):
 * - **409 Conflict** — a Campanha não possui nenhum DAG persistido;
 * - **404 Not Found** — `dagId` inexistente ou alheio ao Caso;
 * - **500** — falha ao persistir (o estado anterior é preservado).
 */
export function escolherDag(
  caso: string,
  dagId: number,
): Promise<CaseStageStates> {
  return api.post<CaseStageStates>(
    `/casos/${encodeURIComponent(caso)}/estagio/3/escolher-dag`,
    { dag_id: dagId },
  )
}
