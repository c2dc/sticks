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
 * - `GET  /api/casos/{caso}/estagio/6`         -> {@link getEstagio3} -> {@link Stage3Result} (Execução da Campanha)
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
import type { DagView } from "@/lib/dag"
import type { EmulacaoPreview, EmulacaoResultadoAbility } from "@/lib/emulacao"
import type { BaseStixInfo, CampanhaStix, DashboardStix } from "@/lib/stix"
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

/** Overall Operation state (from `GET /api/casos/{caso}/estagio/6`). */
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

/** Response of `GET /api/casos/{caso}/estagio/6` (Execução da Campanha). */
export interface Stage3Result {
  entrada: Stage3Entrada
  saida: OperationResultView[]
}

/**
 * Fetch the Execução Entrada (Adversary + Abilities) + accumulated Operation
 * results for a case.
 *
 * No esquema de seis Estágios, a Execução, medição e relatório é o
 * **Estágio 6** (emulação real contra o ambiente provisionado no Estágio 5),
 * então este wrapper aponta para `GET /api/casos/{caso}/estagio/6`. O tipo
 * `Stage3Result` descreve a *forma dos dados* da Execução.
 */
export function getEstagio3(caso: string): Promise<Stage3Result> {
  return api.get<Stage3Result>(`/casos/${encodeURIComponent(caso)}/estagio/6`)
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
 * - `saude` — resumo de saúde dos contêineres-alvo ({@link ResumoSaude}), para o
 *   checklist de pré-requisitos da tela do Estágio 5. Read-only: vem da Saída
 *   {@link AmbienteProvisionado} persistida, ou um resumo vazio (não aprovado)
 *   quando ainda não há Saída — abrir a tela nunca sobe contêineres (Req. 6.1).
 * - `logs` — buffer corrente das linhas de log do provisionamento
 *   ({@link LinhaLogView}, com `seq` monotônico) para recuperar o terminal ao
 *   vivo ao montar a tela (Req. 2.5). Vazio nos Estágios sem provisionamento.
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
  /** Resumo de saúde dos contêineres-alvo p/ o checklist (Req. 6.1). */
  saude: ResumoSaude
  /** Buffer corrente das linhas de log do provisionamento (Req. 2.5). */
  logs: LinhaLogView[]
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

// ===========================================================================
// Pipeline de SEIS Estágios — wrappers tipados dos endpoints novos (task 13.1).
//
// No esquema de **seis** Estágios (Req. 1.1, 1.2, 1.7): 1 = Seleção da base
// STIX, 2 = Seleção da campanha, 3 = Geração do DAG, 4 = Operacionalização,
// 5 = Criação do ambiente de simulação, 6 = Execução, medição e relatório.
//
// Estes wrappers e tipos espelham os contratos pt-BR do backend (fonte de
// verdade): `app/api/casos.py` + os DTOs tipados em `app/services/stix/*` e
// `app/services/environment/*`. Convivem com os wrappers históricos acima
// (`getEstagio1`/`getEstagio2`/`getEstagio3`, cujos números de URL seguem o
// esquema de cinco Estágios) sem editá-los — as classes DTO `Stage1*`/`Stage2*`/
// `Stage3*` não são renomeadas (dívida técnica fora de escopo, Req. 18.5).
// ===========================================================================

// ---------------------------------------------------------------------------
// Estágio 1 — Seleção da base STIX (Req. 5.1, 5.2, 5.7, 5.8, 2.2).
// ---------------------------------------------------------------------------

/**
 * Uma base STIX na listagem do Estágio 1, com a flag de versão curada
 * (backend `BaseStixListada`, Req. 5.5, 5.6). Estende {@link BaseStixInfo} com
 * `curada`: `true` apenas para a versão que carrega os 8 casos curados.
 */
export interface BaseStixListada extends BaseStixInfo {
  /** `true` sse a versão é a versão curada (carrega os 8 casos). */
  curada: boolean
}

/**
 * Resultado tipado do Estágio 1 "Seleção da base STIX"
 * (backend `ResultadoEstagio1`, Req. 5.8, 5.10, 2.2).
 *
 * `concluido` é `true` **somente quando** o bundle da versão está em disco e o
 * dashboard foi montado; `false` em qualquer falha (mantendo o Estágio não
 * concluído, com `mensagem_erro`). A Saída (bundle em disco + dashboard +
 * campanhas) é a Entrada do Estágio 2 (Req. 2.2).
 */
export interface ResultadoEstagio1 {
  versao: string
  concluido: boolean
  bundle_em_disco: boolean
  dashboard: DashboardStix | null
  campanhas: CampanhaStix[]
  curada: boolean
  mensagem_erro?: string | null
}

/**
 * Baixa o pacote STIX da `versao` quando ausente — único egresso de rede do
 * Estágio 1 (`POST /api/stix/bases/{versao}/baixar`, Req. 5.2, 5.3). Devolve a
 * info atualizada da base (`baixada=true`); falhas surgem como `ApiError`
 * (422 versão desconhecida, 502 falha de rede/conteúdo/escrita — Req. 5.9).
 */
export function baixarBaseStix(versao: string): Promise<BaseStixInfo> {
  return api.post<BaseStixInfo>(
    `/stix/bases/${encodeURIComponent(versao)}/baixar`,
  )
}

/**
 * Resultado do Estágio 1 (Seleção da base STIX): dashboard do bundle + veredito
 * de conclusão (`GET /api/casos/{caso}/estagio/1`, Req. 5.1, 5.7, 5.8).
 *
 * Quando `versao` é informada, acrescenta `?versao=<encoded>`; omitida, o
 * backend aplica a versão curada padrão (Req. 5.5). Read-only.
 */
export function getEstagioBaseStix(
  caso: string,
  versao?: string,
): Promise<ResultadoEstagio1> {
  const base = `/casos/${encodeURIComponent(caso)}/estagio/1`
  const url = versao ? `${base}?versao=${encodeURIComponent(versao)}` : base
  return api.get<ResultadoEstagio1>(url)
}

// ---------------------------------------------------------------------------
// Estágio 2 — Seleção da campanha (Req. 6.1–6.7, 2.3).
// ---------------------------------------------------------------------------

/**
 * Uma campanha do bundle na listagem do Estágio 2, com a flag de seleção
 * (backend `CampanhaListada`, Req. 6.2, 6.5). Estende {@link CampanhaStix} com
 * `selecionavel`: `true` só para campanhas curadas (dependem dos artefatos
 * curados em disco).
 */
export interface CampanhaListada extends CampanhaStix {
  /** `true` sse a campanha é selecionável (curada, com artefatos em disco). */
  selecionavel: boolean
}

/**
 * Uma técnica ATT&CK extraída da campanha selecionada
 * (backend `TechniqueElement`, Req. 6.4, 2.3).
 */
export interface TechniqueElement {
  technique_id: string
  technique_name?: string | null
  tactic?: string | null
  description?: string | null
  node_id?: string | null
}

/**
 * Resultado tipado da seleção de uma campanha no Estágio 2
 * (backend `ResultadoSelecaoCampanha`, Req. 6.3–6.7).
 *
 * `concluido` é `true` **somente quando** a extração dos atributos teve sucesso
 * (identidade + técnicas); `false` em qualquer falha (mantendo o Estágio não
 * concluído, com `mensagem_erro`/`etapa_falha`). A Saída (identidade + técnicas)
 * é a Entrada do Estágio 3 (Req. 2.3). `identidade` reusa o bloco histórico
 * {@link CampanhaStixEntrada}.
 */
export interface ResultadoSelecaoCampanha {
  caso_id: string
  versao?: string | null
  concluido: boolean
  identidade: CampanhaStixEntrada | null
  tecnicas: TechniqueElement[]
  mensagem_erro?: string | null
  etapa_falha?: string | null
}

/**
 * Visão do Estágio 2 "Seleção da campanha": campanhas do bundle + a seleção
 * read-only corrente (`GET /api/casos/{caso}/estagio/2`, backend
 * `Estagio2Response`, Req. 6.2, 6.4). Read-only: abrir a tela nunca executa.
 */
export interface Estagio2Response {
  caso_id: string
  versao?: string | null
  campanhas: CampanhaListada[]
  selecao: ResultadoSelecaoCampanha
}

/**
 * Lista as campanhas do bundle + a seleção corrente do Estágio 2
 * (`GET /api/casos/{caso}/estagio/2`, Req. 6.2, 6.3, 6.4). Com `versao` monta
 * `?versao=<encoded>`; omitida, o backend aplica a versão curada padrão.
 */
export function getEstagioCampanha(
  caso: string,
  versao?: string,
): Promise<Estagio2Response> {
  const base = `/casos/${encodeURIComponent(caso)}/estagio/2`
  const url = versao ? `${base}?versao=${encodeURIComponent(versao)}` : base
  return api.get<Estagio2Response>(url)
}

/** Corpo do `POST /api/casos/{caso}/estagio/2/selecionar` (Req. 6.3). */
export interface SelecionarCampanhaRequest {
  slug: string
  versao?: string | null
}

/**
 * Seleciona uma campanha e extrai os seus atributos — conclui o Estágio 2
 * quando a extração tem sucesso (`POST /api/casos/{caso}/estagio/2/selecionar`,
 * Req. 6.3, 6.4, 6.6). Uma falha de extração surge no resultado tipado
 * (`concluido=false`), não como erro HTTP (Req. 6.7).
 */
export function selecionarCampanha(
  caso: string,
  slug: string,
  versao?: string,
): Promise<ResultadoSelecaoCampanha> {
  const corpo: SelecionarCampanhaRequest = { slug }
  if (versao !== undefined) corpo.versao = versao
  return api.post<ResultadoSelecaoCampanha>(
    `/casos/${encodeURIComponent(caso)}/estagio/2/selecionar`,
    corpo,
  )
}

// ---------------------------------------------------------------------------
// Estágio 3 — Geração do DAG (Req. 7.2, 7.3).
// ---------------------------------------------------------------------------

/**
 * Visão do Estágio 3 "Geração do DAG": grafo estrutural + Mermaid do DAG curado
 * (`GET /api/casos/{caso}/estagio/3`, backend `DagView`, Req. 7.2, 7.3).
 * Read-only — nenhuma rede/IA em qualquer caminho (Req. 7.4, 11.1).
 */
export function getEstagioDag(caso: string): Promise<DagView> {
  return api.get<DagView>(`/casos/${encodeURIComponent(caso)}/estagio/3`)
}

// ---------------------------------------------------------------------------
// Estágio 4 — Operacionalização: Abilities + ParametrosDeAmbiente (Req. 8.3,
// 8.4, 8.7, 17.1, 17.2).
// ---------------------------------------------------------------------------

/**
 * Plataforma do ambiente — contêiner hoje; VM admitida sem redesenho
 * (backend `PlataformaAmbiente`, Req. 17.1, 17.2). A união fica aberta
 * (`string & {}`) para novos valores sem quebrar o contrato (Req. 17.2).
 */
export type PlataformaAmbiente = "conteiner" | "maquina_virtual" | (string & {})

/**
 * Um host-alvo derivado dos comandos/nós do DAG curado
 * (backend `HostAlvo`, Req. 8.3). `endereco`/`container` são resolvidos a partir
 * dos destinos dos `attacker_commands`; `vulnerabilidades` vêm dos
 * `target_vuln_host_*` do nó.
 */
export interface HostAlvo {
  rotulo: string
  endereco?: string | null
  container?: string | null
  host_type?: string | null
  vulnerabilidades: string[]
}

/**
 * Serviço/vulnerabilidade a provisionar, inferido dos comandos
 * (backend `ServicoRequerido`, Req. 8.3).
 */
export interface ServicoRequerido {
  descricao: string
  tecnica_origem?: string | null
}

/**
 * Parâmetros de ambiente — 2ª Saída do Estágio 4 (Operacionalização), destinada
 * ao Estágio 5 (backend `ParametrosDeAmbiente`, Req. 8.3, 8.4, 8.7). Derivados
 * dos nós do DAG curado (hosts/serviços/`network_context`); a `origem` é
 * explícita e extensível ({@link OrigemTraducao}, Req. 16.2, 16.4).
 */
export interface ParametrosDeAmbiente {
  caso_id: string
  plataforma: PlataformaAmbiente
  hosts: HostAlvo[]
  servicos: ServicoRequerido[]
  network_context: string[]
  origem: OrigemTraducao
}

/**
 * Visão do Estágio 4 "Operacionalização": os dois artefatos tipados separados —
 * Abilities ordenadas ({@link Stage2Saida}) + {@link ParametrosDeAmbiente}
 * (`GET /api/casos/{caso}/estagio/4`, backend `Estagio4Response`, Req. 8.4,
 * 8.7). `abilities` é `null` quando a operacionalização está suprimida (sem
 * técnica a montante — Req. 8.1). `ambos_produzidos` é o critério de conclusão
 * (Req. 8.8); a conclusão em si é {@link concluirOperacionalizacao}. Read-only.
 */
export interface Estagio4Response {
  caso_id: string
  abilities: Stage2Saida | null
  parametros: ParametrosDeAmbiente
  origem: OrigemTraducao
  ambos_produzidos: boolean
  estados: CaseStageStates
}

/**
 * Visão do Estágio 4 (Operacionalização): Abilities ordenadas +
 * `ParametrosDeAmbiente` (`GET /api/casos/{caso}/estagio/4`, Req. 8.4, 8.7).
 * Read-only — abrir a tela nunca conclui o Estágio.
 */
export function getEstagioOperacionalizacao(
  caso: string,
): Promise<Estagio4Response> {
  return api.get<Estagio4Response>(
    `/casos/${encodeURIComponent(caso)}/estagio/4`,
  )
}

/**
 * Conclui a Operacionalização (Estágio 4) quando ambos os artefatos — Abilities
 * ordenadas + `ParametrosDeAmbiente` — foram produzidos
 * (`POST /api/casos/{caso}/estagio/4/concluir`, Req. 8.7, 8.8). Respeita o
 * bloqueio sequencial (exige o Estágio 3 concluído; senão 409). Devolve o estado
 * por Estágio ({@link CaseStageStates}) já com o Estágio 4 `concluido`.
 */
export function concluirOperacionalizacao(
  caso: string,
): Promise<CaseStageStates> {
  return api.post<CaseStageStates>(
    `/casos/${encodeURIComponent(caso)}/estagio/4/concluir`,
  )
}

// ---------------------------------------------------------------------------
// Estágio 5 — Criação do ambiente: topologia + isolamento + saúde + logs
// (Req. 2.5, 6.1, 9.6–9.9, 14.1–14.4, 16.3).
// ---------------------------------------------------------------------------

/**
 * Veredito de saúde de **um** contêiner-alvo
 * (backend `VereditoSaude`, Req. 5.1, 6.1). `rodando` é `true` sse o contêiner
 * está no estado `running`; `saudavel` é `true` quando o `healthcheck` reporta
 * `healthy`, `false` quando `unhealthy`/`starting`, e `null` quando o serviço
 * **não** define `healthcheck` (não há saúde a exigir — não reprova por si só).
 * `razao` traz a causa legível quando o contêiner não está saudável; `null`
 * quando aprovado.
 */
export interface VereditoSaude {
  container: string
  /** `true` sse o contêiner está no estado `running` (Req. 5.1). */
  rodando: boolean
  /** `true` = `healthy`; `false` = `unhealthy`/`starting`; `null` = sem healthcheck. */
  saudavel: boolean | null
  /** Causa quando não saudável (não `running`/`unhealthy`); `null` quando aprovado. */
  razao: string | null
}

/**
 * Resumo tipado da saúde — veredito por contêiner + geral
 * (backend `ResumoSaude`, Req. 5.1, 6.1). `aprovado` é `true` sse **todos** os
 * contêineres-alvo estão saudáveis (fail-closed: lista vazia reprova). Compõe o
 * checklist de pré-requisitos da tela do Estágio 5.
 */
export interface ResumoSaude {
  aprovado: boolean
  veredictos: VereditoSaude[]
}

/**
 * Uma linha de log do provisionamento do Estágio 5
 * (backend `LinhaLogView`, Req. 2.5, 2.6). Cada linha carrega um `seq` monotônico
 * por caso (para dedupe/reordenar na reconexão), a `etapa` da pipeline que a
 * emitiu, o `fluxo` a que pertence, o `texto` e `em` — um timestamp ISO.
 *
 * `fluxo` fica aberto (`string & {}`) para novos valores sem quebrar o contrato;
 * os conhecidos são `comando` | `stdout` | `stderr` | `info`.
 */
export interface LinhaLogView {
  /** Sequência monotônica por caso (para dedupe/reordenar na reconexão). */
  seq: number
  /** Etapa da pipeline que emitiu a linha (ex. `"subir_conteineres"`). */
  etapa: string
  /** Fluxo da linha; união aberta p/ novos valores (Req. 2.6). */
  fluxo: "comando" | "stdout" | "stderr" | "info" | (string & {})
  /** Texto da linha de log. */
  texto: string
  /** Timestamp ISO (naive-UTC no backend) da linha. */
  em: string
}

/**
 * Veredito de isolamento de **um** contêiner-alvo
 * (backend `VereditoContainer`, Req. 9.8, 14.3, 14.4). `isolado` é `true` sse o
 * contêiner passou em todas as verificações; `razoes` explica a falha.
 */
export interface VereditoContainer {
  container: string
  isolado: boolean
  razoes: string[]
  redes_externas: string[]
  dns_externo: string[]
}

/**
 * Resumo tipado do isolamento — veredito por contêiner + geral
 * (backend `IsolationResumo`, Req. 9.8, 14.3). `aprovado` é `true` sse **todos**
 * os contêineres-alvo estão isolados (fail-closed: lista vazia reprova).
 */
export interface IsolationResumo {
  aprovado: boolean
  veredictos: VereditoContainer[]
}

/**
 * Saída do Estágio 5 — ambiente pronto e isolado
 * (backend `AmbienteProvisionado`, Req. 6.1, 9.8). O Estágio 5 só conclui quando
 * `saude.aprovado`, `isolamento.aprovado` **e** `abilities_carregadas` são
 * `true` (Req. 9.6, 9.9); caso contrário o Estágio 6 fica bloqueado. A `origem`
 * é extensível (Req. 16.3, 16.4).
 *
 * Quando a pipeline falha, `etapa_falha` nomeia a etapa que falhou
 * (`"subir_conteineres"` | `"aguardar_saude"` | …) e `mensagem_erro` traz a
 * causa legível (consistente com o console/checklist da tela — Req. 7.5).
 */
export interface AmbienteProvisionado {
  caso_id: string
  plataforma: PlataformaAmbiente
  containers_alvo: string[]
  compose_file?: string | null
  /** Resumo de saúde dos contêineres-alvo (`running` + `healthy`) (Req. 6.1). */
  saude: ResumoSaude
  isolamento: IsolationResumo
  abilities_carregadas: boolean
  origem: OrigemTraducao
  /** Etapa da pipeline que falhou (`null` quando concluído sem falha). */
  etapa_falha?: string | null
  /** Causa legível da falha (`null` quando concluído sem falha). */
  mensagem_erro?: string | null
}

/**
 * Visão do Estágio 5 "Criação do ambiente": topologia contida + estado de
 * isolamento (`GET /api/casos/{caso}/estagio/5`, backend
 * `Stage4ProvisionamentoResponse`, Req. 9.6, 9.7). Reusa a forma de
 * {@link ProvisionamentoResponse} (hoje servida pelo Estágio 4 no esquema
 * antigo), agora sob o número 5 do esquema de seis Estágios.
 */
export function getEstagioAmbiente(
  caso: string,
): Promise<ProvisionamentoResponse> {
  return api.get<ProvisionamentoResponse>(
    `/casos/${encodeURIComponent(caso)}/estagio/5`,
  )
}

/**
 * Resposta do buffer corrente de logs do provisionamento do Estágio 5
 * (`GET /api/casos/{caso}/estagio/5/logs`, backend `Estagio5LogsResponse`,
 * Req. 2.5). Devolve o buffer **corrente** de {@link LinhaLogView} em ordem de
 * `seq` — efêmero por design (acompanhamento ao vivo + reconexão), não um
 * histórico persistente.
 */
export interface Estagio5LogsResponse {
  caso_id: string
  /** Linhas de log correntes (`seq`, `etapa`, `fluxo`, `texto`, `em`) por `seq`. */
  logs: LinhaLogView[]
}

/**
 * Busca o buffer corrente de linhas de log do provisionamento do Estágio 5 —
 * READ-ONLY (`GET /api/casos/{caso}/estagio/5/logs`, Req. 2.5, 2.7).
 *
 * O `ProvisioningConsole` chama este wrapper ao montar para preencher o
 * histórico do terminal antes de o canal `WS /ws/estagios/{caso}` continuar ao
 * vivo. Alinhado com {@link getEstagioAmbiente}, cujo payload já embute o mesmo
 * buffer em `logs` para a leitura inicial da tela.
 */
export function getLogsEstagio5(caso: string): Promise<Estagio5LogsResponse> {
  return api.get<Estagio5LogsResponse>(
    `/casos/${encodeURIComponent(caso)}/estagio/5/logs`,
  )
}

/**
 * Provisiona/verifica o ambiente curado do Estágio 5: resolve os
 * contêineres-alvo, carrega as Abilities ordenadas no Caldera e **verifica** o
 * isolamento (read-only) antes de concluir
 * (`POST /api/casos/{caso}/estagio/5/provisionar`, Req. 9.6, 9.8, 9.9). Não
 * conclui sem isolamento aprovado + Abilities carregadas.
 */
export function provisionarAmbiente(
  caso: string,
): Promise<AmbienteProvisionado> {
  return api.post<AmbienteProvisionado>(
    `/casos/${encodeURIComponent(caso)}/estagio/5/provisionar`,
  )
}

// ---------------------------------------------------------------------------
// Estágio 6 — Execução, medição e relatório (Req. 10.x, 12, 13, 15.x).
// ---------------------------------------------------------------------------

/**
 * Resultado tipado do desprovisionamento do ambiente ao final do Estágio 6
 * (backend `DesprovisionamentoResult`, Req. 10.8, 12.1, 12.3). O teardown é
 * **sempre** invocado; em falha `recursos_remanescentes` identifica o que
 * permaneceu.
 */
export interface DesprovisionamentoResult {
  desprovisionado: boolean
  recursos_remanescentes: string[]
  origem: OrigemTraducao
  mensagem: string
}

/**
 * Medição por técnica/nó do DAG (backend `MetricaTecnica`, Req. 15.2, 15.4,
 * 15.5). `status` reusa {@link AbilityResultStatus}; `etapa_falha` traz a etapa
 * do DAG onde falhou; `telemetria` são os sinais observados.
 */
export interface MetricaTecnica {
  technique_id?: string | null
  node_id?: string | null
  status: AbilityResultStatus
  etapa_falha?: string | null
  tempo_execucao_s?: number | null
  telemetria: string[]
}

/**
 * Métricas de suficiência procedural — proposta inicial
 * (backend `MetricasSuficiencia`, Req. 15.1–15.6). `taxa_sucesso` e
 * `cobertura_procedural` ∈ [0, 1]; `nos_falhos` são os nós/técnicas que
 * falharam; `veredito_sucesso` é o veredito geral.
 */
export interface MetricasSuficiencia {
  total_tecnicas_dag: number
  tecnicas_sucesso: number
  taxa_sucesso: number
  cobertura_procedural: number
  tempo_total_s?: number | null
  por_tecnica: MetricaTecnica[]
  nos_falhos: string[]
  veredito_sucesso: boolean
}

/** Resultado por Ability reusado no relatório (backend `Stage3AbilityResult`). */
export type RelatorioAbilityResult = AbilityResult

/**
 * Saída do Estágio 6 — relatório consolidado (backend `RelatorioExecucao`,
 * Req. 10.6, 10.7, 15.7): as {@link MetricasSuficiencia}, os resultados por
 * Ability, o flag de desprovisionamento (Req. 12) e o instante de geração.
 */
export interface RelatorioExecucao {
  caso_id: string
  metricas: MetricasSuficiencia
  resultados: RelatorioAbilityResult[]
  ambiente_desprovisionado: boolean
  gerado_em: string
}

/**
 * Resultado por Ability de uma execução do Estágio 6
 * (backend `ExecucaoAbilityResult`, Req. 10.4). `container_destino` é o rótulo
 * do alvo auditado; `saida` é a saída coletada.
 */
export interface ExecucaoAbilityResult {
  ability_id?: string | null
  status: AbilityResultStatus
  comando?: string | null
  container_destino: string
  saida?: string | null
}

/**
 * Por que a execução do runner terminou como terminou
 * (backend `RunOutcome`, Req. 13). Mapeia os caminhos de confirmação/contenção/
 * isolamento/disponibilidade/execução.
 */
export type RunOutcome =
  | "nao_confirmada"
  | "contencao_recusada"
  | "isolamento_falhou"
  | "caldera_indisponivel"
  | "concluida"
  | "abortada"
  | (string & {})

/**
 * Resultado tipado da execução do Estágio 6 (backend `ExecucaoResult`,
 * Req. 10.x, 13). `iniciada` indica se a Operação chegou a executar; `resultado`
 * é o {@link RunOutcome}; `violacoes`/`containers_nao_isolados` identificam os
 * ofensores quando recusada por contenção/isolamento.
 */
export interface ExecucaoResult {
  caso_id: string
  iniciada: boolean
  resultado: RunOutcome
  estado: OperationState
  operacao_id?: number | null
  caldera_operacao_id?: string | null
  total_sucesso: number
  total_falha: number
  resultados: ExecucaoAbilityResult[]
  concluido: boolean
  conclusao_persistida: boolean
  violacoes: string[]
  containers_nao_isolados: string[]
  mensagem: string
}

/**
 * Resultado tipado da conclusão do Estágio 6 — execução + relatório + teardown
 * (backend `ConclusaoEstagio6Result`, Req. 10.6–10.9, 12). `concluido` é `true`
 * sse a execução concluiu, o ambiente foi desprovisionado e o relatório foi
 * persistido.
 */
export interface ConclusaoEstagio6Result {
  caso_id: string
  execucao: ExecucaoResult
  relatorio: RelatorioExecucao
  desprovisionamento: DesprovisionamentoResult
  relatorio_persistido: boolean
  concluido: boolean
}

/** Corpo do `POST /api/casos/{caso}/estagio/6/execucao` (Req. 13.2). */
export interface ExecucaoRequest {
  confirmado: boolean
}

/**
 * Preview do Estágio 6 — lista completa de comandos concretos + container-alvo
 * de cada + sinalização de destino externo, **sem executar** nem criar Operação
 * (`POST /api/casos/{caso}/estagio/6/execucao/preview`, Req. 13.6, 13.7). É a
 * pré-condição visível antes de qualquer confirmação. A forma reusa
 * {@link EmulacaoPreview} (mesmo contrato `EmulationPreviewResponse`).
 */
export function previewExecucao(caso: string): Promise<EmulacaoPreview> {
  return api.post<EmulacaoPreview>(
    `/casos/${encodeURIComponent(caso)}/estagio/6/execucao/preview`,
  )
}

/**
 * Executa o Estágio 6 com os quatro gates (confirmação → contenção → isolamento
 * → disponibilidade), mede, gera o relatório e **sempre** desprovisiona
 * (`POST /api/casos/{caso}/estagio/6/execucao`, Req. 10.1–10.9, 12, 13).
 *
 * Só é chamado numa confirmação **explícita** (`confirmado=true`). Os caminhos
 * de recusa/indisponibilidade surgem como `ApiError` (409 contenção/isolamento,
 * 503 Caldera indisponível, 502 erro de API); `confirmado=false` e
 * `COMPLETED`/`ABORTED` retornam 200 com o {@link ConclusaoEstagio6Result}.
 */
export function executarEstagio6(
  caso: string,
  confirmado: boolean,
): Promise<ConclusaoEstagio6Result> {
  const corpo: ExecucaoRequest = { confirmado }
  return api.post<ConclusaoEstagio6Result>(
    `/casos/${encodeURIComponent(caso)}/estagio/6/execucao`,
    corpo,
  )
}

// ---------------------------------------------------------------------------
// Relatório consolidado (Req. 10.7, 15.7).
// ---------------------------------------------------------------------------

/**
 * Relatório consolidado + métricas de suficiência procedural do Estágio 6
 * (`GET /api/casos/{caso}/relatorio`, backend `RelatorioExecucao`, Req. 10.7,
 * 15.7). As métricas são recomputadas a cada leitura a partir dos resultados
 * persistidos + o DAG; um Caso sem execução devolve métricas zeradas.
 */
export function getRelatorio(caso: string): Promise<RelatorioExecucao> {
  return api.get<RelatorioExecucao>(
    `/casos/${encodeURIComponent(caso)}/relatorio`,
  )
}

// Reexport para quem consome o resultado por Ability da execução via este módulo
// (mantém a dependência de `emulacao.ts` explícita e tree-shakeable).
export type { EmulacaoPreview, EmulacaoResultadoAbility }
