# Implementation Plan: stix-bundle-cache-location

## Overview

A implementação segue o design: primeiro o **resolvedor de caminho puro**
(`app/services/stix/stix_paths.py`), depois a **fiação de configuração**
(`Settings.stix_dir`), em seguida a **troca do default** no `StixService.__init__`,
e por fim a **infraestrutura e documentação** (compose, `.gitignore`, READMEs).
Cada passo é incremental e termina integrado ao anterior: a função pura é construída
e testada isoladamente, o `Settings` expõe o override, o `StixService` consome ambos,
e o compose/docs refletem o novo local. Os testes de propriedade (Hypothesis, ≥100
iterações) são escritos próximos ao código que validam, e os testes de exemplo/snapshot
cobrem fiação, reuso de disco e infra. A linguagem é **Python** (o design já usa Python
concreto), com testes em `apps/backend/tests/`.

## Tasks

- [x] 1. Criar o resolvedor de caminho puro `stix_paths`
  - Criar `apps/backend/app/services/stix/stix_paths.py` com `default_stix_data_dir(*, env, platform, os_name, home)` e `resolve_stix_data_dir(*, injected, override, env, platform, os_name, home)`
  - `default_stix_data_dir`: detecção de SO por `os_name == "nt"` **ou** `platform.startswith("win")`; Windows → `Path(LOCALAPPDATA) / "sticks" / "stix"`; POSIX → `Path(XDG_CACHE_HOME) / "sticks" / "stix"` ou `home / ".cache" / "sticks" / "stix"`; fallback `home / ".sticks" / "stix"`
  - Composição exclusivamente via `pathlib` (`Path(...) / "sticks" / "stix"`), sem separadores hard-coded; sem `mkdir` e sem rede
  - Defaults de parâmetros leem o SO real quando `None` (`os.environ`, `sys.platform`, `os.name`, `Path.home()`); capturar exceção de `Path.home()` e usar caminho determinístico sem propagar erro
  - `resolve_stix_data_dir`: precedência estrita `injected` (não-`None`) > `override` (não-vazio após `strip()`) > `default_stix_data_dir(...)`
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 2.1, 2.2, 2.3, 5.3, 5.4_

  - [x] 1.1 Teste de propriedade — precedência da resolução
    - **Property 1: Precedência estrita da resolução**
    - **Validates: Requirements 2.1, 2.2, 2.3**
    - Em `apps/backend/tests/test_stix_paths_property.py`; geradores de `injected`/`override`/`env` opcionais; comentário `# Feature: stix-bundle-cache-location, Property 1: ...`; `@settings(max_examples=100)`

  - [x] 1.2 Teste de propriedade — mapeamento do default por SO
    - **Property 2: Mapeamento do default por SO**
    - **Validates: Requirements 1.2, 1.3**
    - Base de diretório + `platform ∈ {win, posix}`; verifica sufixos `("sticks","stix")` e ramo `.cache` no POSIX sem `XDG_CACHE_HOME`

  - [x] 1.3 Teste de propriedade — default fora da raiz do repositório
    - **Property 3: Default resolvido fora da raiz do repositório**
    - **Validates: Requirements 1.1, 1.4, 4.1**
    - `env`/`home` fora de um repo-root controlado; assere `not resolved.is_relative_to(repo_root)`

  - [x] 1.4 Teste de propriedade — fallback determinístico sem erro
    - **Property 4: Fallback determinístico sem erro**
    - **Validates: Requirements 1.5**
    - `env` sem chaves de base; não lança, é determinístico (duas chamadas iguais) e termina em `.sticks / stix` quando só há home

  - [x] 1.5 Teste de propriedade — portabilidade por pathlib
    - **Property 5: Portabilidade por pathlib**
    - **Validates: Requirements 5.3**
    - Base + platform; `resolved.parts[-2:] == ("sticks","stix")` e `resolved == Path(base).joinpath(*suffix_parts)`

- [x] 2. Expor o override `stix_dir` em `Settings`
  - Adicionar `stix_dir: Optional[str] = None` em `Settings` (`apps/backend/app/core/config.py`), seguindo o `model_config` existente (prefixo `PIPELINE_UI_`, `env_file=".env"`, `extra="ignore"`)
  - Sem efeitos colaterais; `get_settings()` permanece `lru_cached`; `None`/`""` ⇒ default
  - _Requirements: 2.1, 2.4_

  - [x] 2.1 Teste de exemplo — fiação de Settings
    - Em `apps/backend/tests/test_stix_paths_example.py`: `setenv PIPELINE_UI_STIX_DIR=/x` ⇒ `Settings().stix_dir == "/x"`; ausente ⇒ `None`; limpar `lru_cache` ou instanciar `Settings` diretamente entre casos
    - _Requirements: 2.4_

- [x] 3. Trocar o default do `StixService` para o resolvedor
  - Em `apps/backend/app/services/stix/stix_service.py`: importar `resolve_stix_data_dir`; adicionar parâmetro `stix_dir_override: Optional[str] = None` ao `__init__` (por padrão vindo de `get_settings().stix_dir`)
  - Resolver `self._data_dir = resolve_stix_data_dir(injected=data_dir, override=stix_dir_override)` em vez de compor a partir de `_repo_root()`
  - Garantir que `__init__` não faz `mkdir` nem rede; manter o carregamento defensivo de `STIX_URL`/`STIX_DIR` como está
  - Manter `base_path`, `data_dir` (property), `list_bases`, `download_base` (escrita atômica, mkdir sob demanda, sem parcial), `read_bundle`, `dashboard` inalterados em comportamento
  - _Requirements: 1.1, 2.2, 3.1, 3.2, 3.3, 3.4, 3.5, 5.1, 5.4_

  - [x] 3.1 Teste de propriedade — instanciação sem efeitos colaterais
    - **Property 6: Instanciação sem efeitos colaterais**
    - **Validates: Requirements 5.4**
    - Em `apps/backend/tests/test_stix_service_cache_property.py`; `data_dir` inexistente em `tmp_path` + `http_client` espião; após `__init__` o diretório não existe e nenhum método do cliente foi chamado

  - [x] 3.2 Teste de propriedade — criação de diretório idempotente sob demanda
    - **Property 7: Criação de diretório idempotente sob demanda**
    - **Validates: Requirements 3.3**
    - `data_dir` inexistente + fetch mockado para bundle válido; `download_base` cria pais sem lançar; segunda execução também conclui sem erro

  - [x] 3.3 Teste de propriedade — nenhum arquivo parcial em falha
    - **Property 8: Nenhum arquivo parcial em caso de falha**
    - **Validates: Requirements 3.3**
    - Ponto de falha ∈ {fetch, conteúdo não-STIX, replace}; após `StixDownloadError`, `base_path(version)` ausente e nenhum temporário remanescente no `data_dir`

  - [x] 3.4 Teste de propriedade — equivalência observável entre locais
    - **Property 9: Equivalência do comportamento observável independente do local**
    - **Validates: Requirements 3.1, 3.4**
    - Bundle STIX válido gerado em dois `data_dir` distintos (tmp); `list_bases` (flags `downloaded`/`selectable`), `read_bundle` (dict) e `dashboard` (`counts`) idênticos, a menos do campo `file`

  - [x] 3.5 Testes de exemplo — reuso de disco e estado inicial
    - Em `apps/backend/tests/test_stix_service_cache_example.py`: pré-criar `enterprise-attack-{v}.json` em `tmp` ⇒ `downloaded=true` e `read_bundle` lê sem chamar `http_client` (Req. 3.2); `data_dir=tmp` vazio ⇒ todas as versões `downloaded=false` (Req. 3.1, 3.5); exemplo concreto de override vencendo o default do SO corrente
    - _Requirements: 3.1, 3.2, 3.5_

- [x] 4. Checkpoint — garantir que resolvedor, Settings e StixService passam
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Mapear o volume de cache no profile `containers` do compose
  - Em `infra/docker/docker-compose.yml`, serviço `backend`: substituir o bind `../../packages/cli/data/stix:...` por volume nomeado `sticks-stix-cache:/var/cache/sticks/stix`
  - Definir `environment.PIPELINE_UI_STIX_DIR: /var/cache/sticks/stix`; voltar `/repo` para `:ro` (`../..:/repo:ro`)
  - Declarar o volume de topo `volumes: sticks-stix-cache: { name: sticks-stix-cache }`
  - _Requirements: 4.4, 2.1_

  - [x] 5.1 Teste de snapshot/parse do compose
    - Em `apps/backend/tests/test_compose_stix_cache.py`: parsear `infra/docker/docker-compose.yml` e assertir que `backend` define `PIPELINE_UI_STIX_DIR=/var/cache/sticks/stix` e monta `sticks-stix-cache` em `/var/cache/sticks/stix`
    - _Requirements: 4.4_

- [x] 6. Manter `.gitignore` e documentar o novo local
  - Confirmar/manter as regras `packages/cli/data/stix/*.json` e `sticks/data/stix/*.json` no `.gitignore` como defesa em profundidade
  - Atualizar o README raiz e `apps/backend/README` descrevendo (a) o cache do usuário (`%LOCALAPPDATA%\sticks\stix` / `$XDG_CACHE_HOME/sticks/stix` / `~/.cache/sticks/stix`), (b) o fallback `~/.sticks/stix`, e (c) o override `PIPELINE_UI_STIX_DIR`
  - _Requirements: 4.2, 4.3_

  - [x] 6.1 Teste de asserção estática do `.gitignore`
    - Em `apps/backend/tests/test_gitignore_stix.py`: assertir que as linhas `packages/cli/data/stix/*.json` e `sticks/data/stix/*.json` permanecem presentes
    - _Requirements: 4.2_

- [x] 7. Checkpoint final — garantir que toda a suíte passa
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tarefas marcadas com `*` são opcionais (testes) e podem ser puladas para um MVP mais rápido; as tarefas de implementação (sem `*`) devem ser executadas.
- Cada tarefa referencia requisitos específicos para rastreabilidade; cada teste de propriedade referencia explicitamente uma Property do design.
- Testes de propriedade usam Hypothesis (já presente no backend), ≥100 iterações, com comentário `# Feature: stix-bundle-cache-location, Property {n}: {texto}`.
- Isolamento: `StixService` sempre com `data_dir` em `tmp_path` + `http_client` mockado; resolvedor testado por injeção de `env`/`platform`/`os_name`/`home`, sem tocar o SO real nem a rede.
- Compose e documentação são verificados por snapshot/asserção estática (infra/editorial), não por propriedades.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1", "2", "5", "6"] },
    { "id": 1, "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "5.1", "6.1", "3"] },
    { "id": 2, "tasks": ["3.1", "3.2", "3.3", "3.4", "3.5"] }
  ]
}
```
