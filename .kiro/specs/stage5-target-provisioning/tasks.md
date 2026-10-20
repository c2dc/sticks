# Implementation Plan: stage5-target-provisioning

## Overview

A implementação segue o design: estende a costura **já existente** do Estágio 5
(`EnvironmentProvisioner`/`CurationEnvironmentProvisioner` em
`apps/backend/app/services/environment/provisioner.py`) com duas sub-etapas inseridas
**dentro** do pipeline atual — **5.1 Build (online)** e **5.2 Runtime (isolado)** — sem
reinventar o que a spec irmã entregou (`ComposeExecutor`, `LogLine`/`on_log`, `health`,
`inspect_isolation`). A ordem de construção é incremental e de baixo para cima: primeiro as
peças puras e injetáveis (classificação de comandos; os `Protocol`s novos `EgressWindow`,
`EgressProber`, `TargetSetup`; a extensão do `ComposeExecutor`), depois a orquestração no corpo
de `provision`/`deprovision`, e por fim a fiação de observabilidade (marcação de sub-etapa no
`LogLine`) e o frontend. Cada passo termina integrado ao anterior.

Todas as dependências externas (operações de rede do Compose, `exec` de setup, sonda de egresso)
são `Protocol`s `@runtime_checkable` injetáveis, com defaults de produção resolvidos
**preguiçosamente** — nenhum teste toca a rede pública nem o daemon Docker. Os testes de
propriedade (Hypothesis, ≥100 iterações) verificam as 7 propriedades de correção sobre fakes que
registram a ordem de `attach`/`detach`/`exec`; os testes de exemplo cobrem o split real do
`shadowray_suta.sh`, cada `failed_step` e a paridade i18n. A linguagem é **Python** (backend) e
**TypeScript/React** (frontend), com testes em `apps/backend/tests/` e `apps/frontend/`.

## Tasks

- [x] 1. Classificação determinística online/offline dos comandos de `vulnerabilities`
  - Criar `apps/backend/app/services/environment/target_setup.py` com `SetupPhase` (`ONLINE`/`OFFLINE`), `ClassifiedCommand` (dataclass) e `classify_commands(vulnerabilities: list[str]) -> list[ClassifiedCommand]`
  - Padrões ONLINE (regex estáveis): `\bapt(-get)?\s+(install|update)`, `\bpip3?\s+install`, `\bwget\b`, `\bcurl\b.*\b(-O|-o|--output)\b`, `\bgit\s+clone\b`, download explícito de binário (`https?://` + gravação em arquivo)
  - Padrões OFFLINE: `useradd`/`usermod`/`chpasswd`/`smbpasswd`, escrita de payload (`cat > ... << EOF`, `base64 >`), start de serviço (`sshd &`, `php-fpm`, `nginx`, `su ... -c "... &"`), `chmod`/`chown`/`mkdir`
  - Regra de borda: comando que não casa nenhum padrão ONLINE ⇒ classificado OFFLINE (default seguro); função **pura e determinística**, sem rede
  - _Requirements: 2.2, 5.1_

  - [x] 1.1 Teste de propriedade — classificação estável e segura por padrão
    - **Property 7: Classificação determinística online/offline é estável e segura por padrão**
    - **Validates: Requirements 2.2, 5.1**
    - Em `apps/backend/tests/test_target_setup_property.py`; gerar listas aleatórias de comandos (mix de online/offline/atípicos); assere determinismo (mesma entrada → mesma saída) e que todo comando sem padrão online vira OFFLINE; comentário `# Feature: stage5-target-provisioning, Property 7: ...`; `@settings(max_examples=100)`

  - [x] 1.2 Teste de exemplo — split do `shadowray_suta.sh` real
    - Em `apps/backend/tests/test_target_setup_example.py`: `apt-get install`/`pip3 install` ⇒ ONLINE; `useradd`/`cat > ...backdoor.py`/`sshd &` ⇒ OFFLINE
    - _Requirements: 2.2_

- [x] 2. Estender o `ComposeExecutor` com operações de rede e `exec`
  - Em `apps/backend/app/services/environment/compose_executor.py`: adicionar ao `Protocol` `attach_network(container, network, *, on_log)`, `detach_network(container, network, *, on_log)` e `exec_in(container, argv: list[str], *, on_log)` retornando `CommandResult`
  - Implementar no `DockerComposeExecutor` (produção) via `docker network connect|disconnect` e `docker exec` em **forma de lista de argumentos** (sem shell); nomes/redes são constantes do backend, nunca interpolados de entrada do usuário final
  - Comandos de setup curados são passados a `exec_in` como `["bash","-lc",<comando>]`; manter streaming de `LogLine`; nenhum subprocess aberto na importação
  - _Requirements: 2.3, 3.1, 3.3_

  - [x] 2.1 Teste de exemplo — contrato das novas operações (fake)
    - Em `apps/backend/tests/test_compose_executor_netexec.py`: um fake registra `attach`/`detach`/`exec_in` com argumentos; assere forma de lista (sem shell) e emissão de `LogLine`
    - _Requirements: 2.3, 3.1, 3.3_

- [x] 3. `EgressWindow` — abertura/encerramento da janela de egresso (uma via por vez)
  - Criar `apps/backend/app/services/environment/egress.py` com `EGRESS_NETWORK = "sticks-egress-ephemeral"` (bridge, NÃO internal), `EgressWindowResult` e o `Protocol` `EgressWindow` com `open(container, attack_networks, *, on_log)` e `close(container, *, on_log)`
  - `open`: ordem estrita `detach(redes de ataque)` **antes** de `attach(EGRESS_NETWORK)` — nunca deixa as duas juntas; `close`: `detach(EGRESS_NETWORK)`
  - Implementação de produção usa `ComposeExecutor.detach_network`/`attach_network` e as redes de ataque de `topology_builder`/`subnets`; injetável (fake nos testes)
  - _Requirements: 5.1, 5.2, 5.3_

  - [x] 3.1 Teste de propriedade — invariante de contenção (nunca egresso + ataque juntos)
    - **Property 1: Invariante de contenção — nunca egresso e redes de ataque juntos**
    - **Validates: Requirements 5.1, 5.2, 5.3**
    - Em `apps/backend/tests/test_egress_window_property.py`; fake registra a ordem de attach/detach; para qualquer sequência de hosts/redes, em nenhum instante o conjunto anexado contém egresso **e** rede de ataque; transições sempre `detach` antes de `attach`; comentário de Property; `@settings(max_examples=100)`

- [x] 4. `EgressProber` — sonda positiva read-only de perda de egresso
  - Em `apps/backend/app/services/environment/egress.py`: `EgressVerdict` (`egress_lost`, `conclusive`, `reason`), o `Protocol` `EgressProber.probe(container, *, on_log)` e a função pura `egress_lost(verdicts) -> bool` (aprova só quando **todos** perderam egresso de forma **conclusiva**)
  - Sonda read-only: tenta alcançar endpoint externo fixo (ex.: `1.1.1.1`, TCP/ICMP, timeout curto) e **deve falhar**; sucesso ⇒ egresso presente (reprova); inconclusivo ⇒ tratado como egresso presente (fail-closed)
  - Adaptador de produção lazy via `ComposeExecutor.exec_in` (não instala nada, não escreve payload — só lê o resultado); injetável (fake nos testes)
  - _Requirements: 3.2, 5.4, 5.5_

  - [x] 4.1 Teste de propriedade — verificação positiva antes de qualquer setup offline
    - **Property 2: Perda de egresso positivamente verificada antes de qualquer setup offline**
    - **Validates: Requirements 3.2, 3.5, 5.4, 5.5**
    - Em `apps/backend/tests/test_egress_prober_property.py`; gerar vereditos (perdido/presente/inconclusivo); assere que nenhum setup offline ocorre sem veredito **conclusivo** de egresso perdido para todos; comentário de Property; `@settings(max_examples=100)`

  - [x] 4.2 Teste de exemplo — fail-closed da sonda
    - Em `apps/backend/tests/test_egress_prober_example.py`: egresso perdido ⇒ aprova; egresso presente ⇒ reprova; inconclusivo ⇒ reprova (fail-closed)
    - _Requirements: 5.4, 5.5_

- [x] 5. `TargetSetup` — aplicação do setup por fase (online/offline)
  - Em `apps/backend/app/services/environment/target_setup.py`: `Protocol` `TargetSetup.apply(container, commands: list[ClassifiedCommand], phase: SetupPhase, *, on_log) -> CommandResult`
  - Roda, em ordem, apenas os comandos da fase indicada via `ComposeExecutor.exec_in`; retorna `code != 0` na primeira falha; injetável (fake nos testes)
  - _Requirements: 2.3, 3.3_

  - [x] 5.1 Teste de exemplo — aplica apenas os comandos da fase
    - Em `apps/backend/tests/test_target_setup_apply_example.py`: dada uma lista classificada, `apply(ONLINE)` roda só os ONLINE e `apply(OFFLINE)` só os OFFLINE, na ordem; falha propaga `code != 0`
    - _Requirements: 3.3_

- [x] 6. Estender os modelos de saída: `LogLine.substage`, `SubStageSummary`, `ProvisionedEnvironment`
  - Em `apps/backend/app/services/environment/provisioner.py` (ou módulo dos DTOs): adicionar `LogLine.substage: Optional[str] = None` (`"5.1"`/`"5.2"`/`None`), propagado no `LogLineView` do WS; preservar `seq` monotônico
  - Adicionar `SubStageSummary` (`build_online_ok`, `egress_closed`, `egress_lost_verified`, `offline_applied`, `egress_verdicts`) e `ProvisionedEnvironment.substages`
  - Estender `failed_step` com `build_online`, `encerrar_egresso`, `reisolar`, `verificar_egresso`, `setup_offline`; `ready` passa a exigir também `substages.build_online_ok`, `egress_lost_verified` e `offline_applied` (além de health/loaded_abilities/isolation)
  - _Requirements: 4.6, 6.3, 8.1_

  - [x] 6.1 Teste de exemplo — compatibilidade e `ready` estendido
    - Em `apps/backend/tests/test_provisioned_env_substages_example.py`: `LogLine.substage` default `None` (etapas herdadas); `provision(params)` sem `on_log` continua válido; `ready` só é `True` com os novos campos satisfeitos
    - _Requirements: 4.6, 6.3_

- [x] 7. Orquestrar as sub-etapas 5.1/5.2 no corpo de `CurationEnvironmentProvisioner.provision`
  - Manter a assinatura `provision(params, *, on_log)` (Req. 9.2); reusar `subir_conteineres` + `aguardar_saude` inalterados
  - **5.1**: para cada host — `classify_commands(host.vulnerabilities)`; `EgressWindow.open` (detach ataque → attach egresso); `TargetSetup.apply(ONLINE)`; `EgressWindow.close`; em falha, `_fail_safe` encerra a janela antes de retornar com `failed_step` ∈ {`build_online`, `encerrar_egresso`}
  - **5.2**: reatar redes de ataque (`reisolar`); `EgressProber.probe` para todos + `egress_lost(...)` (`verificar_egresso`); `TargetSetup.apply(OFFLINE)` (`setup_offline`); `carregar_abilities` (reuso, ordem do DAG + Adversary); portão `inspect_isolation` (reuso, read-only, fail-closed) → `verificar_isolamento`
  - Emitir `LogLine` marcada por `substage`/`step` em cada transição; só concluir (ready) quando todas as etapas passarem e desbloquear o Estágio 6
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 2.1, 2.4, 2.5, 2.6, 3.1, 3.3, 3.4, 3.6, 4.1, 4.2, 4.4, 4.5, 6.1, 6.2, 6.3_

  - [x] 7.1 Teste de propriedade — ordem estrita 5.1 antes de 5.2
    - **Property 3: Ordem estrita 5.1 antes de 5.2**
    - **Validates: Requirements 1.2, 1.3, 2.6**
    - Em `apps/backend/tests/test_provision_order_property.py`; fakes roteirizam sucesso/falha; a 5.2 só inicia quando a 5.1 concluiu para **todos**; se a 5.1 falha, a 5.2 não executa (assere via histórico de chamadas); comentário de Property; `@settings(max_examples=100)`

  - [x] 7.2 Teste de propriedade — portão de isolamento fail-closed como pré-condição
    - **Property 5: Portão de isolamento fail-closed como pré-condição de conclusão**
    - **Validates: Requirements 4.1, 4.3, 4.5**
    - Em `apps/backend/tests/test_provision_isolation_gate_property.py`; conclui só quando `inspect_isolation` aprova todos; lista vazia ou daemon indisponível reprova; `@settings(max_examples=100)`

  - [x] 7.3 Teste de exemplo — cada `failed_step` novo e estado seguro
    - Em `apps/backend/tests/test_provision_failed_steps_example.py`: fakes que falham em `build_online`/`encerrar_egresso`/`verificar_egresso`/`setup_offline`/`verificar_isolamento`; cada um produz o `failed_step` correto, mantém Estágio 5 não concluído e Estágio 6 bloqueado
    - _Requirements: 8.1, 8.2, 8.5_

- [x] 8. Tratamento de falhas sem estado inseguro (`_fail_safe`) e re-execução idempotente
  - Implementar `_fail_safe`/`_fail_safe_all`: em qualquer falha com janela de egresso aberta, encerrar o egresso do host (ou desprovisioná-lo) antes de retornar; nenhum caminho de erro sai com egresso aberto
  - Garantir que 5.1/5.2 reaplicam o setup de forma idempotente (reaproveitar pré-requisito já presente; `useradd` tolerante, `|| true`, `pgrep || start`, `cat >` sobrescreve); re-execução preserva o invariante de contenção
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.3, 8.4, 8.5_

  - [x] 8.1 Teste de propriedade — falha nunca deixa um alvo com egresso
    - **Property 4: Falha nunca deixa um alvo com egresso**
    - **Validates: Requirements 8.3, 8.4**
    - Em `apps/backend/tests/test_provision_failclosed_property.py`; injetar falha em cada ponto; o estado final de cada host é isolado (sem egresso) ou desprovisionado (assere via histórico do fake); comentário de Property; `@settings(max_examples=100)`

  - [x] 8.2 Teste de propriedade — idempotência da re-execução
    - **Property 6: Idempotência da re-execução**
    - **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
    - Em `apps/backend/tests/test_provision_idempotent_property.py`; re-executar sobre ambiente já provisionado leva a estado final equivalente, sem duplicar recursos nem violar o invariante; `@settings(max_examples=100)`

- [x] 9. Checkpoint — resolvedor de sub-etapas e invariantes do backend passam
  - Rodar, no `apps/backend` com o venv, todos os testes novos (target_setup, egress_window, egress_prober, provision_*) e a suíte de regressão do pacote `environment`/`containment`
  - Garantir 0 falhas antes de prosseguir para o teardown e o frontend
  - _Requirements: 1.1, 2.2, 3.2, 4.1, 5.1, 7.1, 8.3_

- [x] 10. Desprovisionamento cobrindo o que 5.1/5.2 criaram
  - Em `CurationEnvironmentProvisioner.deprovision`: além do `compose down` existente, remover a `EGRESS_NETWORK` efêmera (se sobreviveu a algum erro) e reportar `remaining_resources` em falha; manter a origem coesa
  - _Requirements: 10.1, 10.2, 10.3_

  - [x] 10.1 Teste de exemplo — teardown remove a rede de egresso e mapeia resíduos
    - Em `apps/backend/tests/test_deprovision_egress_example.py`: `deprovision` remove a `EGRESS_NETWORK`; falha de teardown → `remaining_resources` preenchido
    - _Requirements: 10.2, 10.3_

- [x] 11. Frontend — rótulo de sub-etapa no console e checklist de pré-requisitos
  - `ProvisioningConsole`: renderizar o rótulo de sub-etapa (5.1 Build/online, 5.2 Runtime/isolado) além do `step`, a partir do `substage` novo em `LogLineView`, preservando a ordem de emissão
  - `PrereqChecklist`: adicionar os itens "pré-requisitos online obtidos", "egresso perdido (verificado)" e manter "isolamento aprovado"; em falha, mostrar `failed_step` + causa e Estágio 6 bloqueado
  - Adicionar chaves i18n sob `stage5.provisionamento.*` (sub-etapas e novos pré-requisitos) com paridade pt-BR/en
  - _Requirements: 6.3, 6.4, 8.2_

  - [x] 11.1 Teste — console e checklist refletem as sub-etapas
    - Em `apps/frontend` (Vitest + Testing Library): `ProvisioningConsole` mostra o rótulo de sub-etapa na ordem; `PrereqChecklist` mostra os novos itens e, em falha, o `failed_step` + causa com Estágio 6 bloqueado
    - _Requirements: 6.4, 8.2_

  - [x] 11.2 Teste de propriedade — paridade de chaves i18n pt-BR/en
    - Em `apps/frontend`: as novas chaves `stage5.provisionamento.*` existem em pt-BR e en com o mesmo conjunto de chaves
    - _Requirements: 6.3_

- [x] 12. Checkpoint final — suíte completa (backend + frontend) passa
  - Rodar a suíte do backend (`pytest -q` em `apps/backend`) e do frontend (type-check + Vitest em `apps/frontend`); garantir 0 falhas e que as 7 propriedades estão tagueadas e executando ≥100 iterações
  - _Requirements: 1.1, 3.2, 4.1, 5.1, 6.3, 7.1, 8.3, 10.2_

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "2", "6"] },
    { "id": 1, "tasks": ["1.1", "1.2", "3", "4", "5", "6.1"] },
    { "id": 2, "tasks": ["2.1", "3.1", "4.1", "4.2", "5.1"] },
    { "id": 3, "tasks": ["7"] },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3", "8"] },
    { "id": 5, "tasks": ["8.1", "8.2", "9"] },
    { "id": 6, "tasks": ["10", "11"] },
    { "id": 7, "tasks": ["10.1", "11.1", "11.2"] },
    { "id": 8, "tasks": ["12"] }
  ]
}
```

## Notes

- **Reuso, não reinvenção.** `ComposeExecutor`, `LogLine`/`on_log`, `health.inspect_health`,
  `inspect_isolation`/`IsolationResult` e `containment/subnets` são da spec irmã
  `estagio5-provisionamento-ambiente` e são **reusados**; esta spec só os estende/orquestra. A
  assinatura `EnvironmentProvisioner.provision(params, *, on_log)` **não muda** (Req. 9.2).
- **Invariante de contenção é o esqueleto.** Toda troca de rede é `detach` antes de `attach`, uma
  via por vez; a perda de egresso é **positivamente verificada** (sonda que DEVE falhar), nunca
  presumida; nenhum caminho de erro sai com egresso aberto (Property 1, 2, 4).
- **Sem Docker/rede real nos testes.** Todos os seams novos são `Protocol`s injetáveis; os testes
  de propriedade (Hypothesis, ≥100 iterações, tagueados `# Feature: stage5-target-provisioning,
  Property N: ...`) usam fakes que registram a ordem de `attach`/`detach`/`exec`.
- **Split heurístico agora, declarativo depois.** `classify_commands` é a decisão desta iteração
  (não altera a curadoria existente); o split declarativo por nó do DAG fica como evolução futura
  sob a mesma costura, quando a origem `gerado_ia` entrar.
- **Build-time x runtime.** A instalação de dependências estáveis pode seguir no build-time da
  imagem (contorno atual); esta spec cobre a aplicação em **runtime** dos pré-requisitos derivados
  do Estágio 4. As duas camadas coexistem sem conflito.
- **Pré-condição de ambiente.** A camada 1 (binários pesados pré-presentes na imagem) é
  pré-condição; a 5.1 cobre o que precisa de egresso em runtime dentro da janela controlada.
