# Implementation Plan

Plano incremental para o Estágio 5 — Provisionamento ativo do ambiente. Cada tarefa é um passo de
código verificável (test-first onde cabe), reusa o que já existe e cita os requisitos que satisfaz.
Backend em `ui/backend`, frontend em `ui/frontend`. PostgreSQL exclusivamente.

- [x] 1. ComposeExecutor — subir/derrubar o laboratório em streaming
- [x] 1.1 Definir os DTOs `LinhaLog`, `ResultadoComando` e o Protocol `ComposeExecutor` em `app/services/environment/compose_executor.py`
  - Protocol `@runtime_checkable` com `subir(compose_file, *, on_log)` e `derrubar(compose_file, *, on_log)`
  - _Requisitos: 3.1, 3.2, 8.2_
- [x] 1.2 Implementar `DockerComposeExecutor` (subprocess, forma de lista de argumentos, sem shell)
  - `up -d --wait` nomeando **apenas** os serviços do laboratório de ataque (caldera-attacker, kali-attacker, nginx-target, mariadb-target); nunca o perfil `containers` nem `postgres`
  - Lê stdout/stderr linha a linha e invoca `on_log` em tempo quase real; `codigo` = exit code
  - Caminho do compose e nomes de serviço são constantes do backend (sem interpolação de entrada do usuário)
  - _Requisitos: 2.2, 3.1, 3.3, 3.4, 8.2_
- [x] 1.3 Testes do executor com um fake de subprocess (linhas roteadas + exit code configurável): sucesso, falha ao subir, ordem do streaming, asserção de que a lista de argumentos não inclui serviços da camada de aplicação
  - _Requisitos: 2.3, 3.4, 3.5_

- [x] 2. Verificação de saúde dos contêineres (read-only)
- [x] 2.1 Criar `app/services/environment/saude.py` com `VereditoSaude`/`ResumoSaude` e uma função que inspeciona (read-only) `running` + `healthy` por contêiner-alvo, reusando o inspector Docker read-only
  - _Requisitos: 5.1, 9.2_
- [x] 2.2 Testes com inspector fake: todos saudáveis → aprovado; um não `running` ou `unhealthy` → reprovado com razão; serviço sem healthcheck → `saudavel=None` não reprova
  - _Requisitos: 5.1_

- [x] 3. Evoluir `CuradoriaProvisionadorDeAmbiente.provisionar` para subir + verificar
- [x] 3.1 Injetar `ComposeExecutor` (default lazy = `DockerComposeExecutor`) e o inspector de saúde no construtor
  - _Requisitos: 1.4, 3.1, 10.1_
- [x] 3.2 Reescrever `provisionar(params, *, on_log=no-op)` como pipeline de etapas emitindo `LinhaLog`: subir_conteineres → aguardar_saude → carregar_abilities → verificar_isolamento → montar Saída
  - Cada etapa que falha retorna cedo com `etapa_falha` e causa; não conclui
  - Reusa `_carregar_abilities` (Caldera, ordem do DAG) e `inspect_isolation` (read-only) já existentes
  - Acrescenta `ResumoSaude` à Saída `AmbienteProvisionado`
  - _Requisitos: 1.1, 3.5, 4.1, 4.4, 5.1, 5.2, 5.3, 5.5, 6.1_
- [x] 3.3 Atualizar `desprovisionar` para chamar `ComposeExecutor.derrubar` no laboratório curado e mapear falha → `recursos_remanescentes`
  - _Requisitos: 8.1, 8.2, 8.3, 8.4_
- [x] 3.4 Testes de `provisionar`/`desprovisionar` com todos os colaboradores fakes: caminho feliz conclui com os três pré-requisitos; cada etapa falhando isoladamente produz o `etapa_falha` correto e bloqueia a conclusão; nenhum acesso à rede pública
  - _Requisitos: 3.5, 4.4, 5.3, 5.5, 7.1, 9.1, 9.2_

- [x] 4. Frame `StageProgressEvent` estendido + buffer de logs
- [x] 4.1 Adicionar `LinhaLogView` e o campo `logs: list[LinhaLogView]` (default vazio) em `StageProgressEvent` (`stage_service.py`); garantir compatibilidade dos Estágios 1–4 e 6 (campo vazio)
  - _Requisitos: 2.6_
- [x] 4.2 Implementar o buffer de logs por caso no serviço (lista limitada, `seq` monotônico, naive-UTC); API para anexar e para ler o buffer completo
  - _Requisitos: 2.5, 2.7_
- [x] 4.3 Estender `ws/channels.py` para enviar as linhas novas desde o último frame e o buffer completo no connect
  - _Requisitos: 2.2, 2.5, 2.7_

- [x] 5. Orquestração do Estágio 5 em `EnvironmentStageService`
- [x] 5.1 Disparar o provisionamento automaticamente ao iniciar o Estágio 5 (sem ação manual), encaminhando `on_log` ao buffer e ao canal; atualizar `StageRun` (progresso por etapa; `etapa_falha` + `mensagem_erro` em falha)
  - _Requisitos: 1.1, 2.2, 7.1_
- [x] 5.2 Idempotência: reusar `AmbienteProvisionado` pronto existente; persistir a Saída como JSON portável associado ao caso
  - _Requisitos: 1.6, 6.3_
- [x] 5.3 Concluir o Estágio 5 somente quando saúde + Abilities + isolamento passam; manter bloqueio do Estágio 6 enquanto não `concluido`
  - _Requisitos: 5.5, 6.2, 6.4_
- [x] 5.4 Derivar `mensagem_erro` do `StageRun` da última `LinhaLog` de erro da etapa que falhou (consistência tela ↔ console)
  - _Requisitos: 7.5_
- [x] 5.5 Testes de serviço: provisionamento automático; idempotência; buffer e recuperação; conclusão só com os três pré-requisitos; falha bloqueia o Estágio 6
  - _Requisitos: 1.1, 1.6, 5.5, 6.4, 7.1, 7.3_

- [x] 6. Endpoints REST do Estágio 5
- [x] 6.1 `GET /api/casos/{caso}/estagio/5` devolvendo estado + topologia + isolamento + saúde + buffer de logs
  - _Requisitos: 2.5, 5.6, 6.1_
- [x] 6.2 `POST /api/casos/{caso}/estagio/5/provisionar` idempotente (garante/inicia) e `GET /api/casos/{caso}/estagio/5/logs` (buffer corrente)
  - _Requisitos: 1.1, 1.6, 2.5_
- [x] 6.3 Testes de API (contratos JSON pt-BR): shape do payload, idempotência, logs no retorno
  - _Requisitos: 1.6, 2.5, 6.1_

- [x] 7. Terminal de logs ao vivo no frontend — `ProvisioningConsole`
- [x] 7.1 Wrapper tipado em `src/lib/estagios` para o payload do Estágio 5 (topologia + isolamento + saúde + logs) e para `GET .../estagio/5/logs`
  - _Requisitos: 2.5, 6.1_
- [x] 7.2 Componente `ProvisioningConsole`: assina o `useEstagioChannel`, acumula `event.logs` por `seq`, renderiza em ordem com rótulo de etapa e cor por `fluxo`, auto-scroll; recupera o buffer ao montar
  - _Requisitos: 2.1, 2.2, 2.3, 2.4, 2.5, 2.7_
- [x] 7.3 Testes (vitest/testing-library): ordem por `seq`, cores por fluxo, auto-scroll, recuperação do buffer, reconexão
  - _Requisitos: 2.3, 2.5, 2.7_

- [x] 8. Confirmação de pré-requisitos no frontend — `PrereqChecklist`
- [x] 8.1 Componente `PrereqChecklist`: três itens (contêineres saudáveis, Abilities carregadas, isolamento aprovado) com estado pendente/ok/falha; em falha mostra a causa (`mensagem_erro`/`etapa_falha`) e indica o Estágio 6 bloqueado
  - _Requisitos: 5.6, 7.2, 7.3, 7.4_
- [x] 8.2 Integrar os três blocos na tela do Estágio 5 (reuso de `EnvironmentStageView` + `ProvisioningConsole` + `PrereqChecklist`); "Próximo" bloqueado enquanto não `concluido`, liberado na conclusão
  - _Requisitos: 5.6, 6.4, 7.3_
- [x] 8.3 Testes do checklist: três estados por item; mensagem de falha; bloqueio/liberação do "Próximo"
  - _Requisitos: 5.6, 7.2, 7.3_

- [x] 9. i18n (namespace `pipeline`)
- [x] 9.1 Adicionar as chaves `stage5.provisionamento.console.*`, `stage5.provisionamento.prerequisitos.*` e `stage5.provisionamento.falha.*` em pt-BR e en
  - _Requisitos: 2.1, 2.4, 5.6, 7.2_
- [x] 9.2 Garantir o property test de paridade de chaves pt-BR/en passando
  - _Requisitos: 2.1_

- [x] 10. Desprovisionamento integrado ao término do Estágio 6
- [x] 10.1 Garantir que a conclusão do Estágio 6 chame `desprovisionar` (compose down) via a mesma costura, reportando `recursos_remanescentes` em falha
  - _Requisitos: 8.1, 8.3, 8.4, 8.5_
- [x] 10.2 Teste end-to-end (fakes): provisionar (ShadowRay) → três pré-requisitos ok → executar (mock do runner) → desprovisionar com sucesso; e o caminho de falha de teardown reportando recursos remanescentes
  - _Requisitos: 8.1, 8.2, 8.4_

- [x] 11. Verificação final (ShadowRay, caminho curado)
- [x] 11.1 Rodar a suíte do backend (`pytest`) e do frontend (`vitest --run`), além do lint, garantindo verde
  - _Requisitos: todos_
- [x] 11.2 Conferir que, para ShadowRay, o Estágio 5 sobe o laboratório via `docker/docker-compose.yml`, exibe logs ao vivo, confirma os três pré-requisitos e libera o Estágio 6; e que a falha de qualquer etapa bloqueia o avanço com mensagem na tela
  - _Requisitos: 1.3, 2.1, 5.5, 5.6, 7.2, 7.3_

- [ ] 12. Reuso de imagem e build condicional (sem rebuild quando a imagem já existe)
- [x] 12.1 Fixar o `image:` estável por serviço do laboratório no `docker/docker-compose.yml` (`sticks-kali-attacker`, `sticks-caldera-attacker`, `sticks-nginx-target`, `sticks-mariadb-target`) ao lado do `build:`
  - _Requisitos: 11.4_
- [x] 12.2 Adicionar ao `ComposeExecutor` uma verificação read-only de existência de imagem por serviço (ex.: `imagem_existe(nome)` via `docker image inspect`), injetável/fakeável nos testes
  - _Requisitos: 11.1, 11.4_
- [x] 12.3 Tornar `DockerComposeExecutor.subir` **build-condicional**: todas as imagens presentes → `up --no-build` (reusa); faltando alguma → `build` dos serviços faltantes (streamando o log) e então `up`
  - _Requisitos: 11.1, 11.2, 11.3_
- [x] 12.4 Garantir que `DockerComposeExecutor.derrubar` executa `compose down` **sem** `--rmi` (preserva a imagem para reuso entre campanhas); não remover volumes de dados que devam persistir
  - _Requisitos: 8.2, 11.6_
- [x] 12.5 Testes do executor (fake de subprocess + fake de inspeção de imagem): todas as imagens existem → `up` com `--no-build` e **sem** chamada de build; falta imagem → build primeiro e depois `up`; `derrubar` nunca inclui `--rmi`
  - _Requisitos: 11.1, 11.2, 11.3, 11.6_
- [x] 12.6 (infra, fora do fluxo) Documentar/retaguear as imagens já construídas para o nome estável (`docker tag docker-kali:latest sticks-kali-attacker:latest`, etc.) para reaproveitamento imediato; a preparação/build inicial com internet é pré-condição quando a imagem não existe
  - _Requisitos: 11.2, 11.4_
- [x] 12.7 Validar ponta a ponta pela UI (ShadowRay): com as imagens já presentes, o Estágio 5 sobe o laboratório **sem rebuild**, homologa os três pré-requisitos e libera o Estágio 6; após a execução, o teardown preserva as imagens
  - _Requisitos: 11.2, 11.5, 11.6, 8.2_
