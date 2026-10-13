/**
 * Typed API wrappers and real-time helpers for the per-stage screens
 * (Stage1View / Stage2View / Stage3View — Task 12.2, Req. 2, 3, 4, 11).
 *
 * These wrap the stage-detail endpoints and the operation WebSocket. No esquema
 * de cinco Estágios (Req. 1.1, 1.3, 1.7), o pipeline é: Estágio 1 = Modelagem
 * Estrutural, Estágio 2 = Geração do DAG (ver `src/lib/dag.ts`), Estágio 3 =
 * Operacionalização, Estágio 4 = Provisionamento do Ambiente, Estágio 5 =
 * Execução da Campanha. Os nomes dos wrappers e das interfaces (`getEstagio1`
 * /`Stage1Result` = Modelagem, `getEstagio2`/`Stage2Result` = Operacionalização,
 * `getEstagio3`/`Stage3Result` = Execução) descrevem o *papel/forma dos dados*,
 * não o número da URL; as URLs apontam para as rotas corretas 1..5 do backend.
 * - `GET  /api/casos/{caso}/estagio/1`         -> {@link getEstagio1} -> {@link Stage1Result} (Modelagem Estrutural)
 * - `POST /api/casos/{caso}/estagio/1/executar`-> {@link executarEstagio1} -> {@link Stage1Result}
 * - `GET  /api/casos/{caso}/estagio/3`         -> {@link getEstagio2} -> {@link Stage2Result} (Operacionalização)
 * - `POST /api/casos/{caso}/estagio/3/concluir`-> {@link concluirEstagio3} -> {@link CaseStageStates}
 * - `GET  /api/casos/{caso}/estagio/5`         -> {@link getEstagio3} -> {@link Stage3Result} (Execução da Campanha)
 * - `GET  /api/casos/{caso}/estagio/4`         -> {@link getTopologia} -> {@link TopologyResponse} (Provisionamento)
 * - `WS   /ws/operacao/{operacao}`             -> {@link OperacaoFrame}
 *
 * `src/lib/casos.ts` (task 12.1) owns the `GET /api/casos` + stage-progress
 * channel wrappers and the shared `EstadoEstagio` / `NumeroEstagio` types.
 * This module lives beside it and extends the surface with the stage-detail
 * contracts without editing the existing exports. Backend contracts are the
 * source of truth (pt-BR field names, see the pipeline-ui design).
 */
import { api } from "@/lib/api"
import type { CaseStageStates, EstadoEstagio } from "@/lib/casos"
import { connectWs, type WsConnection } from "@/lib/ws"

// ---------------------------------------------------------------------------
// Translation source — explicit, extensible attribute (Req. 11.1, 11.2).
// ---------------------------------------------------------------------------

/**
 * Origin of a Stage-2 translation. `"curadoria_humana"` is the value for the 8
 * curated cases; the union stays open (`string & {}`) so a future AI-assisted
 * origin can be shown without changing the persisted model (Req. 11.1, 11.2).
 */
export type OrigemTraducao = "curadoria_humana" | (string & {})

// ---------------------------------------------------------------------------
// Modelagem Estrutural — Estágio 1 no esquema 1..5 (Req. 2).
// ---------------------------------------------------------------------------

/**
 * Bloco de identidade da campanha STIX exibido na Entrada do Estágio 1
 * (recorte do objeto `campaign` do pacote STIX da base selecionada — Req. 1.1,
 * 2.1). Espelha o DTO `CampanhaStixEntrada` do backend (campos pt-BR). Todos os
 * campos ricos são opcionais/anuláveis; só `encontrada` é garantido.
 *
 * Quando `encontrada` é `false`, `motivo_indisponivel` traz a razão pt-BR do
 * fallback — `"versao_ausente"` (versão da base não informada),
 * `"bundle_indisponivel"` (base ilegível/ inválida) ou
 * `"campanha_nao_encontrada"` (objeto `campaign` ausente na base). A união fica
 * aberta (`string & {}`) para novos motivos sem quebrar o contrato (Req. 4.2, 4.3).
 */
export interface CampanhaStixEntrada {
  /** `true` sse o objeto `campaign` foi localizado na base selecionada. */
  encontrada: boolean
  /** Versão da base STIX usada para a resolução; `null` quando não informada. */
  versao_base: string | null
  /** ID STIX do objeto (`campaign--…`); `null` quando não encontrada. */
  stix_id: string | null
  /** Nome canônico da campanha; preservado no fallback como identificador. */
  nome: string | null
  /** External id ATT&CK (ex. `C0045`); preservado no fallback. */
  external_id: string | null
  /** Descrição legível da campanha; `null` quando ausente. */
  descricao: string | null
  /** Apelidos conhecidos da campanha (chips na UI); vazio quando ausentes. */
  aliases: string[]
  /** Primeira observação (ISO); `null` quando ausente. */
  first_seen: string | null
  /** Última observação (ISO); `null` quando ausente. */
  last_seen: string | null
  /** Razão pt-BR do fallback quando `!encontrada`; `null` quando encontrada. */
  motivo_indisponivel:
    | "versao_ausente"
    | "bundle_indisponivel"
    | "campanha_nao_encontrada"
    | (string & {})
    | null
}

/**
 * Stage-1 Entrada: a identidade da campanha STIX da base selecionada (Req. 2.1).
 *
 * A UI consome `campanha_stix` (o recorte do objeto `campaign`); os campos
 * legados `dag_file`/`stix_reference`/`campaign_name` permanecem no DTO do
 * backend por compatibilidade de contrato, mas deixam de ser exibidos/consumidos
 * pela UI — por isso são opcionais aqui (o `dag_file` já não é exibido e o
 * `stix_reference` deixa de ser lido como entrada — Req. 1.2, 1.3).
 */
export interface Stage1Entrada {
  caso_id: string
  nome: string
  /** @deprecated Legado da Caldera, não mais exibido pela UI. */
  dag_file?: string
  /** @deprecated Legado, não mais consumido pela UI (substituído por `campanha_stix`). */
  stix_reference?: string
  /** @deprecated Legado, não mais consumido pela UI. */
  campaign_name?: string
  /** Recorte do objeto `campaign` do pacote STIX da base selecionada (Req. 2.1). */
  campanha_stix?: CampanhaStixEntrada | null
}

/** A single extracted ATT&CK technique (Req. 2.3). */
export interface Stage1Technique {
  technique_id?: string
  name?: string
  tactic?: string
  description?: string
  [key: string]: unknown
}

/** A relationship between extracted actions (Req. 2.3). */
export interface Stage1Relationship {
  source?: string
  target?: string
  tipo?: string
  [key: string]: unknown
}

/**
 * Stage-1 Saída: the structural elements extracted from the STIX dataset
 * (Req. 2.3). When `is_empty` is true, no structural elements were extracted
 * and the UI shows the corresponding indication (Req. 2.5).
 */
export interface Stage1Saida {
  techniques: Stage1Technique[]
  relationships: Stage1Relationship[]
  indicators: unknown[]
  infrastructure: unknown[]
  malware: unknown[]
  metadata: Record<string, unknown>
  is_empty: boolean
  note?: string | null
}

/** Response of `GET /api/casos/{caso}/estagio/1` (Modelagem Estrutural). */
export interface Stage1Result {
  caso_id: string
  estado: EstadoEstagio
  entrada: Stage1Entrada
  /** Null until the stage actually runs — the read-only GET never fabricates it. */
  saida: Stage1Saida | null
  /** Failure cause when `estado === "erro"` (Req. 2.6). */
  mensagem_erro?: string | null
  /** Modeling step at which the failure occurred (Req. 2.6). */
  etapa_falha?: string | null
}

/**
 * Fetch the Modelagem Estrutural Entrada/Saída for a case — READ-ONLY.
 *
 * No esquema de cinco Estágios (Req. 1.1, 1.3, 1.7), a Modelagem Estrutural é o
 * **Estágio 1** (ponto de partida do pipeline), então este wrapper aponta para
 * `GET /api/casos/{caso}/estagio/1`. A leitura é somente-leitura: abrir a tela
 * não executa nem persiste a modelagem (Req. 2.5).
 *
 * Quando `versao` (a base STIX selecionada no fluxo) é informada, acrescenta
 * `?versao=<encoded>` para que o backend recorte o objeto `campaign` da base
 * correta na Entrada. Retrocompatível: sem `versao`, o backend aplica o fallback
 * (`versao_ausente`) sem erro (Req. 5.2, 5.3).
 */
export function getEstagio1(caso: string, versao?: string): Promise<Stage1Result> {
  const base = `/casos/${encodeURIComponent(caso)}/estagio/1`
  const url = versao ? `${base}?versao=${encodeURIComponent(versao)}` : base
  return api.get<Stage1Result>(url)
}

/**
 * Trigger the Modelagem Estrutural (Estágio 1) structural modeling for a case
 * (`POST /api/casos/{caso}/estagio/1/executar`). Persists the extracted
 * elements and marks the stage `concluido` on success (or `erro` with the
 * failing step — reported in the typed {@link Stage1Result}, not as HTTP error).
 *
 * Aceita a mesma `versao` opcional de {@link getEstagio1}: quando informada,
 * acrescenta `?versao=<encoded>` para enriquecer a Entrada do resultado com o
 * recorte da campanha STIX da base selecionada (sem afetar a extração
 * estrutural). Retrocompatível quando omitida (Req. 5.2).
 */
export function executarEstagio1(caso: string, versao?: string): Promise<Stage1Result> {
  const base = `/casos/${encodeURIComponent(caso)}/estagio/1/executar`
  const url = versao ? `${base}?versao=${encodeURIComponent(versao)}` : base
  return api.post<Stage1Result>(url)
}

// ---------------------------------------------------------------------------
// Operacionalização — Estágio 3 no esquema 1..5 (Req. 4, 11).
// ---------------------------------------------------------------------------

/** Stage-2 Entrada: the abstract ATT&CK techniques from Stage 1 (Req. 3.1). */
export interface Stage2Entrada {
  techniques: Stage1Technique[]
  /** False when Stage 1 produced no techniques -> no Stage-2 input (Req. 3.6). */
  has_input: boolean
  note?: string | null
}

/** One concrete executor of an Ability (name / platform / command) (Req. 3.2). */
export interface AbilityExecutor {
  name: string
  platform: string
  command: string
}

/** A curated Ability derived from a technique (Req. 3.2). */
export interface Ability {
  ability_id: string
  name: string
  technique_id: string
  tactic: string
  description: string
  executors: AbilityExecutor[]
  /** Translation origin at the Ability level (Req. 11.1). */
  origem_traducao?: OrigemTraducao
}

/** Adversary grouping of Abilities with its atomic ordering (Req. 3.3, 3.5). */
export interface Adversary {
  id: string
  name: string
  description: string
  /** Sequential ordering / dependencies of the grouped Abilities (Req. 3.3). */
  atomic_ordering: string[]
  /** Translation origin at the Adversary level (Req. 11.1). */
  origem_traducao?: OrigemTraducao
}

/**
 * Stage-2 Saída: the curated Abilities, their ordering, the Adversary grouping
 * and the explicit translation-source indicator (Req. 3.2–3.5, 11.1). Absence
 * states are conveyed by `has_abilities` / `has_adversary` (Req. 3.7, 3.8).
 */
export interface Stage2Saida {
  abilities: Ability[]
  /** Sequential order / dependencies across the Abilities (Req. 3.3). */
  ordering: string[]
  adversary: Adversary | null
  /** Explicit, extensible translation-source attribute (Req. 3.4, 11.1). */
  origem_traducao: OrigemTraducao
  /** Human-curation flag associated with the Saída (Req. 3.4). */
  human_curation: boolean
  /** False when the case has no curated Abilities (Req. 3.7). */
  has_abilities: boolean
  /** False when the case has no associated Adversary (Req. 3.8). */
  has_adversary: boolean
  note?: string | null
  adversary_note?: string | null
}

/** Response of `GET /api/casos/{caso}/estagio/3` (Operacionalização). */
export interface Stage2Result {
  caso_id: string
  entrada: Stage2Entrada
  saida: Stage2Saida
  /** True when the Modelagem produced no techniques -> Saída suppressed (Req. 3.6). */
  suppressed: boolean
}

/**
 * Fetch the Operacionalização Entrada/Saída for a case.
 *
 * No esquema de cinco Estágios (Req. 1.1, 1.3, 1.7), a Operacionalização é o
 * **Estágio 3** (técnicas abstratas → Abilities executáveis), então este
 * wrapper aponta para `GET /api/casos/{caso}/estagio/3`. O tipo `Stage2Result`
 * descreve a *forma dos dados* da Operacionalização; preserva-se o vocabulário
 * `origem_traducao`/`TranslationSource` (Req. 4.3).
 */
export function getEstagio2(caso: string): Promise<Stage2Result> {
  return api.get<Stage2Result>(`/casos/${encodeURIComponent(caso)}/estagio/3`)
}

/**
 * Conclui a Operacionalização (Estágio 3) e destrava o Estágio 4
 * (`POST /api/casos/{caso}/estagio/3/concluir`). Respeita o bloqueio sequencial
 * (exige o Estágio 2 concluído; senão o backend responde 409). Devolve o estado
 * por Estágio ({@link CaseStageStates}) já com o Estágio 3 `concluido`.
 */
export function concluirEstagio3(caso: string): Promise<CaseStageStates> {
  return api.post<CaseStageStates>(
    `/casos/${encodeURIComponent(caso)}/estagio/3/concluir`,
  )
}

// ---------------------------------------------------------------------------
// Execução da Campanha — Estágio 5 no esquema 1..5 (Req. 6).
// ---------------------------------------------------------------------------

/** Per-Ability execution status during / after an Operation (Req. 4.3). */
export type AbilityResultStatus = "pendente" | "em_execucao" | "sucesso" | "falha"

/** Stage-3 Entrada: the Adversary + Abilities to emulate (Req. 6.1). */
export interface Stage3Entrada {
  caso_id: string
  nome: string
  adversary: Adversary | null
  abilities: Ability[]
  /** False when the case cannot be emulated (e.g. missing Adversary/Abilities). */
  can_emulate: boolean
  note?: string | null
}

/** Result of a single Ability within an Operation (Req. 4.4). */
export interface AbilityResult {
  ability_id: string
  status: AbilityResultStatus
  saida_comando: string
}

/** Overall Operation state (from `GET /api/casos/{caso}/estagio/5`). */
export type OperationState =
  | "nao_iniciada"
  | "em_execucao"
  | "concluida"
  | "falha"
  | (string & {})

/**
 * A view of one Operation for the case: its state, per-Ability results and the
 * aggregate success/failure totals (Req. 4.4, 4.5).
 */
export interface OperationResultView {
  operacao_id: string
  estado: OperationState
  total_sucesso: number
  total_falha: number
  resultados: AbilityResult[]
}

/** Response of `GET /api/casos/{caso}/estagio/5` (Execução da Campanha). */
export interface Stage3Result {
  entrada: Stage3Entrada
  saida: OperationResultView[]
}

/**
 * Fetch the Execução Entrada (Adversary + Abilities) + accumulated Operation
 * results for a case.
 *
 * No esquema de cinco Estágios (Req. 1.1, 1.3, 1.7), a Execução da Campanha é o
 * **Estágio 5** (emulação real contra o ambiente provisionado), então este
 * wrapper aponta para `GET /api/casos/{caso}/estagio/5`. O tipo `Stage3Result`
 * descreve a *forma dos dados* da Execução.
 */
export function getEstagio3(caso: string): Promise<Stage3Result> {
  return api.get<Stage3Result>(`/casos/${encodeURIComponent(caso)}/estagio/5`)
}

// ---------------------------------------------------------------------------
// Operation real-time channel — `WS /ws/operacao/{operacao}` (Req. 4.3–4.5).
// ---------------------------------------------------------------------------

/**
 * A per-Ability status frame pushed by `WS /ws/operacao/{operacao}` while an
 * Operation runs (Req. 4.3, 4.4). Carries the Ability id, its new status and
 * the command output produced so far.
 */
export interface OperacaoAbilityFrame {
  tipo: "ability"
  ability_id: string
  status: AbilityResultStatus
  saida_comando: string
}

/**
 * The final aggregate frame pushed when the Operation reaches its terminal
 * state (Req. 4.5): success/failure totals and the Operation's final state.
 */
export interface OperacaoAgregadoFrame {
  tipo: "agregado"
  operacao_id: string
  estado: OperationState
  total_sucesso: number
  total_falha: number
}

/** Any frame carried by the Operation channel. */
export type OperacaoFrame = OperacaoAbilityFrame | OperacaoAgregadoFrame

/**
 * Subscribe to the per-Operation channel. `onFrame` receives per-Ability status
 * updates and the final aggregate (Req. 4.3, 4.4, 4.5). Reconnects on drop so
 * the UI keeps reflecting real-time status.
 */
export function connectOperacao(
  operacao: string,
  onFrame: (frame: OperacaoFrame) => void,
): WsConnection {
  return connectWs<OperacaoFrame>(
    `/ws/operacao/${encodeURIComponent(operacao)}`,
    { onMessage: onFrame },
  )
}

// ---------------------------------------------------------------------------
// Provisionamento do Ambiente — Estágio 4 (read-only): topologia do laboratório
// contido + estado de isolamento (Req. 5.4, 5.5, 5.6).
// ---------------------------------------------------------------------------

/** One lab host in the contained topology. */
export interface TopologyHost {
  nome: string
  container: string
  papel: "attacker" | "target" | (string & {})
  ips: string[]
}

/** One internal-only lab network and the hosts attached to it. */
export interface TopologyNetwork {
  nome: string
  subnet: string
  internal: boolean
  hosts: string[]
}

/**
 * The contained lab topology for a case plus a ready-to-render Mermaid diagram.
 * `isolado` is true when every attack-plane network is `internal: true` (no
 * route to the host or the internet) — the attack never leaves the sandbox.
 */
export interface TopologyResponse {
  caso_id: string
  nome: string
  isolado: boolean
  hosts: TopologyHost[]
  redes: TopologyNetwork[]
  mermaid: string
  compose_file?: string | null
}

/**
 * Fetch the Provisionamento do Ambiente view (Estágio 4): the contained lab
 * topology (+ Mermaid) and its isolation state (Req. 5.4, 5.5, 5.6).
 *
 * No esquema de cinco Estágios, a topologia + isolamento passam a ser o
 * **Estágio 4**, servidos por `GET /api/casos/{caso}/estagio/4` (não mais pelo
 * endpoint `/topologia`). O payload do backend `Stage4ProvisionamentoResponse`
 * é mais rico que {@link TopologyResponse} (acrescenta `isolamento_indisponivel`,
 * `motivo` e `estados`); este wrapper expõe o subconjunto já tipado aqui — a
 * tela dedicada `EnvironmentStageView` (tarefa 10.2/10.3) consome os campos
 * extras.
 */
export function getTopologia(caso: string): Promise<TopologyResponse> {
  return api.get<TopologyResponse>(
    `/casos/${encodeURIComponent(caso)}/estagio/4`,
  )
}

/**
 * Resposta completa do Provisionamento do Ambiente (Estágio 4), servida por
 * `GET /api/casos/{caso}/estagio/4` (backend `Stage4ProvisionamentoResponse`).
 *
 * É um superconjunto estrutural de {@link TopologyResponse}: além da topologia
 * contida (hosts, redes internas, Mermaid, compose), carrega o estado de
 * isolamento rico que a tela dedicada `EnvironmentStageView` consome:
 *
 * - `isolado` — `true` sse **todos** os containers-alvo estão isolados → o
 *   Estágio 4 se autoconclui e o Estágio 5 é liberado.
 * - `isolamento_indisponivel` — `true` quando o isolamento **não pôde ser
 *   verificado** (daemon Docker indisponível). *Fail-closed*: o estágio **não**
 *   se conclui e o Estágio 5 permanece bloqueado, mas a topologia ainda é
 *   exibida.
 * - `motivo` — razão em pt-BR quando não isolado / não verificável; `null`
 *   quando `isolado`.
 * - `estados` — estado dos cinco Estágios ({@link CaseStageStates}), já
 *   refletindo a autoconclusão (ou não) do Estágio 4.
 */
export interface ProvisionamentoResponse {
  caso_id: string
  nome: string
  /** `true` sse todos os containers-alvo estão isolados → pronto p/ Estágio 5. */
  isolado: boolean
  /** `true` quando o isolamento não pôde ser verificado (fail-closed). */
  isolamento_indisponivel: boolean
  /** Razão pt-BR quando não isolado / não verificável; `null` quando isolado. */
  motivo: string | null
  hosts: TopologyHost[]
  redes: TopologyNetwork[]
  mermaid: string
  compose_file?: string | null
  /** Estado dos cinco Estágios, refletindo a autoconclusão do Estágio 4. */
  estados: CaseStageStates
}

/**
 * Busca a resposta completa do Provisionamento do Ambiente (Estágio 4) — a
 * topologia contida **mais** o estado de isolamento rico (Req. 5.4, 5.5, 5.6).
 *
 * Aponta para `GET /api/casos/{caso}/estagio/4` (mesmo endpoint de
 * {@link getTopologia}), mas tipado para o payload completo
 * {@link ProvisionamentoResponse}. Preferir este wrapper na tela dedicada
 * `EnvironmentStageView`; {@link getTopologia}/{@link TopologyResponse}
 * permanecem para o subconjunto usado pelo Stage3View.
 */
export function getProvisionamento(caso: string): Promise<ProvisionamentoResponse> {
  return api.get<ProvisionamentoResponse>(
    `/casos/${encodeURIComponent(caso)}/estagio/4`,
  )
}
