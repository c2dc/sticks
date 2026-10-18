# Task 2.4 — Backend symbol/field rename map (authoritative, in-progress)

This is the frozen, authoritative rename map applied by task 2.4 across the
entire `ui/backend` tree (`app/` AND `tests/`). It resolves every Portuguese
identifier through design.md §2 and the morpheme rules in
`terminology-baseline.md`. Apply these EXACTLY so the backend and the lockstep
frontend (task 3.5) never drift.

Scope boundaries (unchanged by 2.4):
- `__tablename__` strings and physical column names in `alembic/**` are task 4.
- Enum string VALUES (e.g. `"curadoria_humana"`, `"conteiner"`, `"nao_iniciado"`)
  are DB/wire values — NOT renamed here (task 4 renames physical schema; the
  wire enum values stay as the serialized contract until a later decision).
- Runtime user-facing Portuguese message strings (error `detail`/`mensagem`
  prose) are left as-is to avoid breaking assertions; `motivo` machine codes
  (e.g. `"bundle_invalido"`, `"falha_download"`) stay.
- Containment gate LOGIC, emulation/DAG/provisioning behavior, Stage 6 fix:
  names/comments only.
- D1: STIX route path segment stays `/actors`; payload DTO/field + domain types
  use `adversaries` / `Adversary*`.
- `hosts`, `ability/abilities`, `adversary/adversaries` stay as-is.

## Class renames (PascalCase)
| Old | New |
|-----|-----|
| ResultadoEstagio1 | Stage1ResultStix |
| Estagio1Service | Stage1Service |
| Estagio2Service | Stage2Service |
| CampanhaListada | ListedCampaign |
| ResultadoSelecaoCampanha | CampaignSelectionResult |
| CampanhaStix | CampaignStix |
| CampanhaStixDetalhada | DetailedCampaignStix |
| CampanhaStixEntrada | CampaignStixInput |
| AtorStix | AdversaryStix |
| ContagemTipo | TypeCount |
| VersaoStix | StixVersion |
| ParametrosAmbiente (ORM row) | EnvironmentParametersRow |
| ParametrosDeAmbiente (DTO) | EnvironmentParameters |
| AmbienteProvisionadoRow | ProvisionedEnvironmentRow |
| AmbienteProvisionado (DTO) | ProvisionedEnvironment |
| RelatorioExecucao (ORM + report DTO) | ExecutionReport |
| OrigemDag | DagOrigin |
| PlataformaAmbiente | EnvironmentPlatform |
| PlataformaAmbientePersistida | PersistedEnvironmentPlatform |
| Estagio2Response | Stage2Response |
| SelecionarCampanhaRequest | SelectCampaignRequest |
| Estagio4Response | Stage4Response |
| Stage4ProvisionamentoResponse | Stage4ProvisioningResponse |
| ProvisionarEstagio5Response | ProvisionStage5Response |
| Estagio5LogsResponse | Stage5LogsResponse |
| ExecucaoRequest | ExecutionRequest |
| EscolherDagRequest | ChooseDagRequest |
| CampanhasResponse | CampaignsResponse |
| AtoresResponse | AdversariesResponse |
| DagNoView | DagNodeView |
| DagArestaView | DagEdgeView |
| DagResumo | DagSummary |
| DagDetalhe | DagDetail |
| EscolhaDagFalha | DagChoiceFailure |
| EscolhaDagResult | DagChoiceResult |
| GeracaoDagAutoResult | DagAutoGenerationResult |
| ModelagemEstrutural | StructuralModeling |
| DagProduzido | ProducedDag |
| GeradorDeDag | DagGenerator |
| CuradoriaGeradorDeDag | CurationDagGenerator |
| ConclusaoEstagio6Result | CompletionStage6Result |
| ConclusaoEstagio6Service | CompletionStage6Service |
| LinhaLog | LogLine |
| ResultadoComando | CommandResult |
| ExecucaoAbilityResult | ExecutionAbilityResult |
| ExecucaoResult | ExecutionResult |
| ExecucaoStageService | ExecutionStageService |
| ResultadoAbility (measurement) | AbilityMeasurementResult |
| MedicaoService | MeasurementService |
| MetricaTecnica | TechniqueMetric |
| MetricasSuficiencia | SufficiencyMetrics |
| VereditoSaude | HealthVerdict |
| ResumoSaude | HealthSummary |
| HostAlvo | TargetHost |
| ServicoRequerido | RequiredService |
| OperacionalizacaoProduzida | ProducedOperationalization |
| OperacionalizadorPorOrigem | OperationalizerByOrigin |
| CuradoriaOperacionalizador | CurationOperationalizer |
| VereditoContainer | ContainerVerdict |
| IsolationResumo | IsolationSummary |
| DesprovisionamentoResult | DeprovisioningResult |
| ResultadoProvisionamento | ProvisioningResult |
| CalderaLoader (keep) | CalderaLoader |
| ProvisionadorDeAmbiente | EnvironmentProvisioner |
| CuradoriaProvisionadorDeAmbiente | CurationEnvironmentProvisioner |
| ProvisionamentoAutoResult | AutoProvisioningResult |
| ProvisionamentoEstagio5Result | ProvisioningStage5Result |
| PreferenciasView | PreferencesView |
| LinhaLogView | LogLineView |

Keep (already English/STIX): Stage1Result, Stage1Input, Stage1Output,
Stage2Input, Stage2Output, Stage2Result, Stage2AbilityView, Stage2AdversaryView,
Stage2ExecutorView, Stage3*, CaseStageStates, AggregateProgress,
StageStateView, StageProgressEvent, OperationRunResult, OperationRunnerProtocol,
StageService, DagView, DagService, CaseService, CaseData, DagData, DagNode, etc.

## Function/method renames (snake_case)
obter_auditoria→get_audit, obter_topologia→get_topology, obter_relatorio→get_report,
obter_operacao→get_operation, obter_dag→get_dag, obter_dag_persistido→get_persisted_dag,
obter_campanha_por_slug→get_campaign_by_slug, obter_estado_sessao→get_session_state,
obter_dashboard→get_dashboard, listar_casos(route)→list_campaigns_endpoint,
listar_campanhas→list_campaigns, listar_atores→list_adversaries, listar_bases→list_bases,
listar_dags→list_dags, detalhar_caso→detail_campaign, selecionar_campanha→select_campaign,
reset_caso→reset_campaign, escolher_dag→choose_dag, preview_emulacao→preview_emulation,
executar_emulacao→execute_emulation, atualizar_preferencias→update_preferences,
baixar_base→download_base, _baixar_conteudo→_download_content, _url_versionada→_versioned_url,
ler_bundle→read_bundle, _chave_versao_semantica→_semantic_version_key,
versao_eh_curada→version_is_curated, _mapear_campanha→_map_campaign,
_resolver_slug_curado→_resolve_curated_slug, _extrair_external_id→_extract_external_id,
get_provisionador→get_provisioner, ws_estagios→ws_stages, ws_operacao→ws_operation,
_auto_concluir_curados→_auto_complete_curated, auto_concluir_estagio2→auto_complete_stage2,
auto_concluir_estagio4→auto_complete_stage4, _operacionalizar→_operationalize,
operacionalizar→operationalize, _ambos_artefatos_produzidos→_both_artifacts_produced,
_relatorio_sink_memoria→_report_sink_memory, executar_e_concluir→execute_and_complete,
_desprovisionar→_deprovision, desprovisionar→deprovision, verificar_saude→check_health,
inspect_saude→inspect_health, _verificar_saude→_check_health, _causa_da_saude→_health_cause,
_causa_do_isolamento→_isolation_cause, _verificar_isolamento→_check_isolation,
de_execucao→from_execution, medir_do_caso→measure_from_campaign,
relatorio_do_caso→report_from_campaign, _derivar_parametros→_derive_parameters,
_obter_modelagem→_get_modeling, construir_redes→build_networks,
_build_campanha_stix→_build_campaign_stix, iniciar_provisionamento→start_provisioning,
_params_do_caso→_params_from_campaign, _ambiente_row→_environment_row,
_persistir_ambiente→_persist_environment, ambiente_persistido→persisted_environment,
_nome_do_caso→_campaign_name, provisionar_estagio5→provision_stage5,
sincronizar_dags_curados→sync_curated_dags, _load_preferencias→_load_preferences,
concluir→complete, selecionar→select, provisionar→provision, ler→read (view methods),
listar_campanhas(Stage2)→list_campaigns.

## Enum member renames
CURADORIA_HUMANA→HUMAN_CURATION, GERADO_IA→AI_GENERATED (values unchanged).

## Field / attribute / local-var renames (snake_case)
caso_id→campaign_id, caso→campaign, casos→campaigns, versao→version,
estagio→stage, estagios→stages, concluido→completed, campanha→campaign,
campanhas→campaigns, atores→adversaries, mensagem_erro→error_message,
etapa_falha→failed_step, saude→health, isolamento→isolation, isolado→isolated,
redes→networks, hosts→hosts (keep), operacao→operation, operacoes→operations,
auditoria→audit, preferencias→preferences, topologia→topology,
provisionamento→provisioning, provisionador→provisioner,
desprovisionamento→deprovisioning, execucao→execution, medicao→measurement,
relatorio→report, emulacao→emulation, parametros→parameters, parametro→parameter,
suficiencia→sufficiency, origem→origin, origem_traducao→translation_source,
nome→name, descricao→description, motivo→reason, curada/curado(s)→curated,
curadoria→curation, slug_curado→curated_slug, selecionavel→selectable,
identificador→identifier, rotulo→label, conteudo→content, criado_em→created_at,
atualizado_em→updated_at, entrada→input, saida→output, saida_comando→command_output,
erro→error, estado→state, estados→states, progresso→progress, metricas→metrics,
metrica→metric, tecnica(s)→technique(s), veredito→verdict, linha→line, aresta→edge,
detalhe→detail, resumo→summary, falha→failure, produzido/produzida→produced,
gerador→generator, modelagem→modeling, resultado(s)→result(s), selecao→selection,
escolha→choice, plataforma→platform, identidade→identity, encontrada→found,
versao_base→base_version, motivo_indisponivel→unavailable_reason,
total_sucesso→total_success, total_falha→total_failure, iniciada_em→started_at,
finalizada_em→finished_at, container_destino→target_container, comando→command,
registrado_em→recorded_at, caso_atual→current_campaign, tema→theme, idioma→language,
veredito_sucesso→success_verdict, ambiente_desprovisionado→environment_deprovisioned,
gerado_em→generated_at, pronto→ready, curada(StixVersion)→curated,
artefatos_curados_path→curated_artifacts_path, dag_escolhido_id→chosen_dag_id,
arquivo_ability→ability_file, arquivo_adversary→adversary_file, arquivo_dag→dag_file,
ambos_produzidos→both_produced, isolamento_indisponivel→isolation_unavailable,
etapa→step (log line), fluxo→stream, texto→text, em→at (log line timestamp),
estado_anterior→previous_state, concluido_total→fully_completed,
casos_concluidos→completed_campaigns, total_casos→total_campaigns,
percentual→percentage, casos_concluidos_ids→completed_campaign_ids,
bloqueado→blocked, iniciavel→startable, abilities_carregadas→loaded_abilities,
desconhecido→unknown.

Module-level stage-role constants in stage_service.py (keep names — historical
aliases referenced across modules; translate only their comments):
ESTAGIO_SELECAO_BASE, ESTAGIO_SELECAO_CAMPANHA, ESTAGIO_GERACAO_DAG,
ESTAGIO_OPERACIONALIZACAO, ESTAGIO_AMBIENTE, ESTAGIO_EXECUCAO, ESTAGIO_MODELAGEM,
ESTAGIO_PROVISIONAMENTO, VALID_STAGES, FIRST_STAGE, LAST_STAGE, VERSOES_CONHECIDAS,
DESCRICOES_TIPO, ROTULO_TIPO, SLUG_PARA_STIX, VERSAO_CURADA — left as identifiers
(renaming these module constants is optional churn; their Portuguese spelling is
an internal constant, not a wire field). Comments/docstrings around them ARE
translated.

## Progress (files completed)
- app/models/enums.py ✓
- app/models/domain.py ✓ (ORM attrs → English; physical names default English)
- app/models/__init__.py ✓
- app/services/stix/stix_service.py ✓ (symbols; field names English)
- app/services/stix/estagio1_service.py ✓ (symbols/fields English; file name still PT)
- app/services/stix/estagio2_service.py ✓ (symbols/fields English; file name still PT)
- app/services/stix/__init__.py ✓
- app/services/environment/** ✓ ALL — completion, execution, measurement, health,
  operationalization, operationalizer, provisioner, compose_executor,
  stage_service, log_buffer, report, topology_builder, __init__
  (docstrings/comments → English; symbols already English; kept: _MSG_*/cause/
  LogLine text= runtime message prose, step machine-codes "subir_conteineres"
  etc., enum VALUES).  NOTE: fixed a cross-module rename mismatch —
  compose_executor exposes up/down (+ ctor image_exists) but provisioner.py
  still called .subir(...)/.derrubar(...); call-sites corrected to .up/.down.
- app/api/campaigns.py ✓ (docstrings/comments/summaries → English; wire fields
  renamed: casos->campaigns, erro->error, motivo->reason, entrada/saida->
  input/output; fixed call-sites ambiente_persistido->persisted_environment,
  relatorio_do_caso->report_from_campaign; kept error-detail dict keys
  motivo/mensagem + message prose).
- app/api/emulation.py ✓ (docstrings/comments/summaries; wire field mensagem->
  message; kept error-detail motivo/mensagem dict keys + message prose).
- app/api/audit.py ✓
- app/api/preferences.py ✓
- app/api/session_state.py ✓
- app/api/topology.py ✓ (kept Mermaid label prose + Caso desconhecido detail)
- app/api/router.py ✓ (already English)
- app/api/stix.py ✓ (already English; wire fields baixada/selecionavel/curada
  kept — not in §2 rename set)
- app/ws/{channels,router,__init__}.py ✓ (already English; enum VALUES in
  docstrings kept)
- app/services/stage/stage_service.py — PARTIAL (header docstrings + read_stage1/
  assemble_stage2/run_stage1 regions done; some docstring prose remains; the
  ESTAGIO_* constant comments kept; symbols/fields already English).

VERIFICATION: `python -m compileall ui/backend/app` -> exit 0 (no syntax errors).
All top-level Pydantic wire fields confirmed English (grep clean). Did NOT run
pytest (that is task 2.5).

## Remaining (app/ — docstring/comment prose ONLY; symbols/fields already English)
- services/dag/dag_service.py (large — docstrings/comments/_MSG_* prose),
  dag/gerador.py (fully English already; file name still PT),
- services/stage/stage_service.py (finish remaining docstring prose),
- services/session/session_state_service.py,
- services/stix/stix_service.py (docstring prose),
- services/operation/runner.py,
- services/containment/{isolation,subnet_validator,preview}.py (comments only — gates unchanged),
- services/caldera/client.py, services/campaign_service.py,
- services/{__init__, stage/__init__, dag/__init__, session/__init__}.py,
- db/session.py (docstring example).
INTERNAL (non-wire) names still PT, deferred to avoid breaking the 5 dag tests
that pass them: DagService(gerador=...) kwarg + .gerador property (map says
->generator). These are NOT serialized wire fields, so they do not affect the
task 3.5 lockstep.

## Remaining (tests/) — 82 files
All test files referencing renamed symbols/fields/attrs must be updated to the
new contract (do NOT weaken tests). Migration tests (test_migration_*.py) keep
their physical-name `Table(...)` literals (`casos`, `execucoes_estagio`,
`caso_id`, `estagio`) since those mirror the still-Portuguese physical schema
driven by the unchanged alembic migrations (task 4 territory) — translate only
their prose, keep the SQL/Table string literals.
