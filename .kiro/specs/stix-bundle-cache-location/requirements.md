# Requirements Document

## Introduction

O download do pacote STIX (bundle `enterprise-attack-{versao}.json`) é uma decisão de
**tempo de execução** do Pesquisador no **Estágio 1 — Seleção da base STIX**: nenhuma versão
vem baixada por padrão, e o Backend busca o pacote da fonte oficial do MITRE apenas sob
solicitação explícita, quando ainda não há cópia em disco.

Hoje, porém, o `StixService` materializa esse download **dentro do repositório**
(`<repo>/packages/cli/data/stix/enterprise-attack-{versao}.json`, resolvido via
`_repo_root()`). Isso acopla um **artefato volátil de runtime** à árvore do repositório:
inchou o histórico do Git (os bundles de ~50 MB foram versionados por engano e tiveram de ser
expurgados), exige regras de `.gitignore` defensivas e torna o repositório dependente de um
dado que o usuário escolhe baixar em runtime.

Esta spec **desacopla o local de armazenamento do bundle STIX do repositório**, movendo o
destino padrão do download para um **diretório de cache persistente do usuário**, fora da
árvore do repositório, mantendo o comportamento funcional do Estágio 1 intacto. A arquitetura
atual já suporta a troca sem redesenho: `StixService.__init__` aceita um `data_dir` injetável;
apenas o **default** aponta para dentro do repositório. A mudança troca esse default e expõe um
ponto de configuração explícito, preservando a injeção para testes.

### Decisões de contexto (já alinhadas com o usuário — não são requisitos funcionais)

- O destino **não** deve ser o diretório temporário volátil do SO (`%TEMP%` / `/tmp`), que é
  limpo em reboot/limpeza de disco e forçaria re-download de ~50 MB. O destino deve ser um
  **cache persistente do usuário**: `%LOCALAPPDATA%\sticks\stix` no Windows e
  `$XDG_CACHE_HOME/sticks/stix` (com fallback `~/.cache/sticks/stix`) no Linux/macOS.
- A resolução de caminho deve ser **portável** (Windows/Linux/macOS) via `pathlib`, no mesmo
  espírito da estratégia já usada por `sticks_config._repo_root()` / `CaseService`.
- O caso em que o Backend roda **em container** (profile `containers` do compose) deve ser
  considerado: o cache precisa ser mapeado para um volume para persistir e para que o host e o
  container compartilhem a mesma cópia. O detalhamento do mapeamento é do design.

### Escopo

- Resolução do diretório de dados STIX do `StixService` (default + override por configuração).
- Integração com `app/core/config.py` (`Settings`, prefixo `PIPELINE_UI_`) para expor o
  override.
- Criação do diretório de cache sob demanda (no download), de forma atômica e idempotente.
- `.gitignore` e documentação (README / README do backend) alinhados ao novo local.
- Consideração do caso containerizado (volume de cache) no design.

### Não-objetivos

- Alterar a lógica de download em si (fonte MITRE, validação de versão, escrita atômica,
  tratamento de erro) — apenas **onde** o arquivo é materializado.
- Alterar o `CaseService` ou o local dos artefatos **curados** (`packages/cli/data/api` e
  `data/dag`), que são dados de curadoria legítimos do repositório, não artefatos de runtime.
- Migrar automaticamente bundles já baixados de instalações antigas (reuso oportunista é
  aceitável, mas não é requisito).
- Qualquer mudança no fluxo visível do Estágio 1 na UI.

## Glossary

- **Bundle STIX / pacote STIX:** arquivo `enterprise-attack-{versao}.json` de uma versão
  específica do ATT&CK-in-STIX, baixado da fonte oficial do MITRE. Artefato de runtime, não
  versionado.
- **Diretório de dados STIX (`data_dir`):** diretório onde o `StixService` lê e grava os bundles
  (`base_path(versao) = data_dir / enterprise-attack-{versao}.json`).
- **Cache persistente do usuário:** diretório fora do repositório, específico do usuário/SO,
  que sobrevive a reboots: `%LOCALAPPDATA%\sticks\stix` (Windows),
  `$XDG_CACHE_HOME/sticks/stix` ou `~/.cache/sticks/stix` (Linux/macOS).
- **Override de configuração:** variável de ambiente/`.env` (`PIPELINE_UI_STIX_DIR`) que, quando
  definida, substitui o default do diretório de dados STIX.
- **Repo-root:** raiz do repositório localizada por `sticks_config._repo_root()` (contém
  `packages/cli/`). Era a base do default antigo.

## Requirements

### Requirement 1: Default fora do repositório, no cache persistente do usuário

**User Story:** Como mantenedor do repositório, quero que o bundle STIX baixado em runtime seja
materializado num cache persistente do usuário fora da árvore do repositório, para que o
repositório não dependa de nem seja poluído por um artefato volátil de runtime.

#### Acceptance Criteria

1. WHEN o `StixService` é instanciado sem `data_dir` e sem override de configuração THEN o
   sistema SHALL resolver o diretório de dados STIX para um cache persistente do usuário fora da
   árvore do repositório.
2. WHEN o sistema roda no Windows THEN o diretório padrão SHALL ser `%LOCALAPPDATA%\sticks\stix`
   (usando `LOCALAPPDATA` quando definido).
3. WHEN o sistema roda em Linux/macOS THEN o diretório padrão SHALL ser
   `$XDG_CACHE_HOME/sticks/stix` quando `XDG_CACHE_HOME` estiver definido, caso contrário
   `~/.cache/sticks/stix`.
4. WHEN o diretório padrão é resolvido THEN o caminho resolvido SHALL NOT estar dentro da raiz do
   repositório (`_repo_root()`).
5. IF nenhuma variável de ambiente de base (`LOCALAPPDATA`/`XDG_CACHE_HOME`/`HOME`) estiver
   disponível THEN o sistema SHALL recorrer a um fallback determinístico e portável sob o diretório
   home do usuário (ex.: `~/.sticks/stix`) sem lançar erro na importação/instanciação.

### Requirement 2: Override explícito por configuração

**User Story:** Como operador em diferentes ambientes (dev nativo, container, CI), quero poder
sobrescrever o diretório do cache STIX por configuração, para adequar o local às restrições do
ambiente sem alterar código.

#### Acceptance Criteria

1. WHEN a variável `PIPELINE_UI_STIX_DIR` está definida (ambiente ou `.env`) THEN o sistema SHALL
   usar esse caminho como diretório de dados STIX, com precedência sobre o default.
2. WHEN um `data_dir` é injetado diretamente no `StixService` THEN o sistema SHALL usar o valor
   injetado com precedência sobre o override de configuração e sobre o default (preservando a
   testabilidade atual).
3. WHEN o override de configuração está ausente/vazio THEN o sistema SHALL aplicar o default do
   Requirement 1 sem erro.
4. WHERE a configuração central (`app/core/config.py`, `Settings`) expõe o diretório STIX THEN o
   atributo SHALL seguir o padrão de nomenclatura/precedência das demais settings
   (prefixo `PIPELINE_UI_`, leitura de `.env`).

### Requirement 3: Comportamento funcional do Estágio 1 preservado

**User Story:** Como Pesquisador, quero que a seleção/baixa da base STIX funcione exatamente como
antes, para que a mudança de local do cache seja transparente no fluxo.

#### Acceptance Criteria

1. WHEN uma versão não está presente no diretório de dados STIX resolvido THEN `list_bases` SHALL
   reportá-la como não baixada (`downloaded=false`), e `download_base` SHALL baixá-la para esse
   diretório.
2. WHEN uma versão já está presente no diretório resolvido THEN o sistema SHALL reusar a cópia em
   disco sem novo download (idempotência preservada).
3. WHEN o download ocorre e o diretório de cache ainda não existe THEN o sistema SHALL criá-lo sob
   demanda (incluindo diretórios-pai) de forma idempotente, mantendo a **escrita atômica** e o
   não-deixar-arquivo-parcial em caso de falha, exatamente como hoje.
4. WHEN o download, a listagem, a leitura do bundle e o dashboard são exercidos sobre o novo local
   THEN o resultado observável (DTOs, verdicts de conclusão do Estágio 1, erros tipados) SHALL ser
   equivalente ao comportamento atual.
5. WHEN os bundles STIX não estão versionados no repositório THEN o Estágio 1 SHALL continuar
   partindo de "nenhuma versão baixada por padrão" (Req. 5.4 do pipeline de seis estágios).

### Requirement 4: Independência do repositório e documentação

**User Story:** Como mantenedor, quero que o repositório permaneça livre de bundles e que o novo
comportamento esteja documentado, para evitar reincidência do inchaço e manter clareza.

#### Acceptance Criteria

1. WHEN um bundle é baixado para o cache do usuário THEN nenhum arquivo de bundle SHALL ser criado
   dentro da árvore do repositório.
2. WHERE o `.gitignore` cobre dados STIX THEN as regras existentes para `packages/cli/data/stix/*.json`
   SHALL ser mantidas como defesa em profundidade, mesmo que o default não grave mais ali.
3. WHEN a mudança está concluída THEN o README (raiz) e o README do backend SHALL descrever onde o
   bundle é armazenado (cache do usuário) e como sobrescrever via `PIPELINE_UI_STIX_DIR`.
4. IF o Backend roda em container (profile `containers` do compose) THEN o design SHALL especificar
   o mapeamento de volume do cache (host↔container) e o valor de `PIPELINE_UI_STIX_DIR` dentro do
   container, de modo que a cópia persista e seja reaproveitada.

### Requirement 5: Compatibilidade, testes e portabilidade

**User Story:** Como desenvolvedor, quero garantir que a mudança seja portável e verificável sem
tocar na rede nem no SO real, para manter a suíte determinística.

#### Acceptance Criteria

1. WHEN os testes exercitam o `StixService` THEN eles SHALL continuar injetando um `data_dir`
   isolado (tmp) e um `http_client` mockado, sem depender do cache real do usuário.
2. WHEN a resolução do default é testada THEN o teste SHALL cobrir Windows e POSIX via injeção/mock
   das variáveis de ambiente (`LOCALAPPDATA`, `XDG_CACHE_HOME`, `HOME`), sem criar diretórios reais
   fora de um tmp controlado.
3. WHEN a resolução do caminho é feita THEN ela SHALL usar exclusivamente `pathlib` e NOT conter
   separadores de caminho hard-coded específicos de SO.
4. WHEN a instanciação do `StixService` ocorre na importação do módulo/aplicação THEN ela SHALL NOT
   realizar acesso à rede nem criar o diretório de cache (criação só no download — Req. 3.3).
