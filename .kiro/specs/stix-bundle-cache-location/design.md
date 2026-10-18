# Design Document

## Overview

Esta mudança desacopla o **local de armazenamento do bundle STIX** da árvore do
repositório. Hoje o `StixService` resolve seu diretório de dados para
`<repo>/packages/cli/data/stix` (via `sticks_config._repo_root()`), o que acopla um
artefato volátil de runtime (~50 MB) ao repositório. O objetivo é mover o **default**
para um **cache persistente do usuário**, fora do repositório, preservando integralmente
o comportamento funcional do Estágio 1 — apenas **onde** o arquivo é materializado muda
(Req. 1, Req. 3).

A arquitetura atual já suporta a troca sem redesenho: `StixService.__init__` aceita um
`data_dir` injetável e o único acoplamento é o valor **default**. O design introduz:

1. Um **resolvedor de caminho puro** (`resolve_stix_data_dir`) que decide o diretório de
   dados STIX a partir de uma precedência clara: `data_dir` injetado > override de
   configuração (`PIPELINE_UI_STIX_DIR`) > default do SO no cache do usuário
   (Req. 1.1, 2.1, 2.2, 2.3).
2. Um novo atributo `stix_dir` em `Settings` (`app/core/config.py`) expondo o override por
   ambiente/`.env` com o prefixo `PIPELINE_UI_` existente (Req. 2.1, 2.4).
3. A troca do default dentro de `StixService.__init__`, chamando o resolvedor em vez de
   compor o caminho a partir de `_repo_root()` (Req. 1.1, 5.4).
4. O mapeamento de **volume de cache** no profile `containers` do compose, com
   `PIPELINE_UI_STIX_DIR` apontando para o ponto de montagem dentro do container (Req. 4.4).
5. Documentação (README raiz e do backend) e manutenção das regras de `.gitignore` como
   defesa em profundidade (Req. 4.2, 4.3).

### Decisões de design e seus porquês

- **Resolução pura e injetável.** Toda a lógica de "onde fica o diretório" vive em uma função
  pura que recebe `env` (mapa de variáveis), `platform`/`os.name` e `home` como *parâmetros
  injetáveis*, de modo que os testes cobrem Windows e POSIX via mock, sem tocar no SO real nem
  criar diretórios fora de um tmp (Req. 5.2, 5.3). O `StixService` só repassa esses valores.
- **Sem rede e sem criação de diretório na importação/instanciação.** A resolução é apenas
  cálculo de `Path`; nenhum `mkdir` nem egress ocorre em `__init__`. A criação do diretório
  continua acontecendo **somente no download**, de forma atômica e idempotente, exatamente como
  hoje (Req. 3.3, 5.4).
- **`pathlib` exclusivamente.** Nenhum separador de caminho hard-coded (`\` ou `/`); os caminhos
  são construídos por composição `Path(...) / "sticks" / "stix"`, o que torna a resolução portável
  (Req. 5.3), no mesmo espírito de `sticks_config._repo_root()`.
- **Fallback determinístico.** Se nenhuma variável de base (`LOCALAPPDATA`/`XDG_CACHE_HOME`/`HOME`)
  estiver disponível, a função recorre a `Path.home() / ".sticks" / "stix"` — e, se nem o home
  resolver, a um caminho determinístico sob o diretório de trabalho/temporário controlado — sem
  lançar erro (Req. 1.5).
- **Comportamento observável inalterado.** `base_path`, `list_bases`, `download_base`, `read_bundle`
  e `dashboard` não mudam de contrato; só passam a operar sobre o `data_dir` resolvido. Os DTOs,
  verdicts e erros tipados permanecem equivalentes (Req. 3.4).

## Architecture

```mermaid
flowchart TD
    subgraph Config["app/core/config.py"]
        S["Settings.stix_dir<br/>(env PIPELINE_UI_STIX_DIR)"]
    end

    subgraph Resolver["app/services/stix/stix_paths.py (puro)"]
        R["resolve_stix_data_dir(<br/>injected, override, env, platform, home)"]
    end

    subgraph Service["StixService.__init__"]
        I["data_dir injetado?"]
    end

    OSdefault["Default do SO<br/>%LOCALAPPDATA%\\sticks\\stix (Win)<br/>$XDG_CACHE_HOME/sticks/stix ou ~/.cache/sticks/stix (POSIX)<br/>fallback ~/.sticks/stix"]

    S -->|override opcional| R
    I -->|sim: precedência máxima| R
    I -->|não| R
    R -->|resolve, sem mkdir/rede| OSdefault
    R --> DD["_data_dir resolvido"]
    DD --> BP["base_path(version) = _data_dir / enterprise-attack-{version}.json"]
    BP --> OPS["list_bases / download_base / read_bundle / dashboard<br/>(comportamento inalterado)"]
```

### Precedência de resolução (Req. 2.1, 2.2, 2.3)

A resolução é uma cadeia de precedência estrita, do mais específico ao mais geral:

| Ordem | Fonte | Observação |
|-------|-------|------------|
| 1 (maior) | `data_dir` injetado em `StixService(data_dir=...)` | Preserva a testabilidade atual (tmp isolado). |
| 2 | `PIPELINE_UI_STIX_DIR` (`Settings.stix_dir`) | Override por ambiente/`.env`. Vazio/ausente é ignorado. |
| 3 (menor) | Default do SO no cache do usuário | Calculado por `resolve_stix_data_dir` a partir das env vars de base. |

### Resolução do default por SO (Req. 1.2, 1.3, 1.5)

```mermaid
flowchart TD
    A["resolve default"] --> B{"Windows?<br/>(os.name == 'nt' ou platform começa com 'win')"}
    B -->|sim| C{"LOCALAPPDATA definido?"}
    C -->|sim| C1["Path(LOCALAPPDATA) / 'sticks' / 'stix'"]
    C -->|não| H["home fallback"]
    B -->|não POSIX| D{"XDG_CACHE_HOME definido?"}
    D -->|sim| D1["Path(XDG_CACHE_HOME) / 'sticks' / 'stix'"]
    D -->|não| E{"HOME disponível?"}
    E -->|sim| E1["home / '.cache' / 'sticks' / 'stix'"]
    E -->|não| H
    H["home / '.sticks' / 'stix'<br/>(determinístico, sem erro)"]
```

O caminho resolvido do default **nunca** cai dentro de `_repo_root()` (Req. 1.4): todos os ramos
derivam de `LOCALAPPDATA`, `XDG_CACHE_HOME` ou do home do usuário — nenhum deles ancorado no
repositório. Essa propriedade é validada por teste (ver Correctness Properties).

### Dependências e invariantes de instanciação (Req. 5.4)

- `StixService.__init__` chama `resolve_stix_data_dir(...)` apenas quando `data_dir is None`,
  passando `Settings().stix_dir` (ou o valor já disponível) como override. A chamada é cálculo
  puro de `Path`: **nenhum** `mkdir`, **nenhum** egress de rede.
- A criação do diretório continua ocorrendo somente dentro de `download_base`
  (`self._data_dir.mkdir(parents=True, exist_ok=True)` imediatamente antes da escrita atômica),
  preservando o fluxo atual (Req. 3.3).

## Components and Interfaces

### 1. Resolvedor de caminho puro — `app/services/stix/stix_paths.py` (novo)

Módulo pequeno e sem efeitos colaterais, isolado para testabilidade. Toda a decisão de ambiente
é feita por parâmetros injetáveis (com defaults que leem o SO real em produção), de forma que os
testes cobrem Windows/POSIX sem tocar no SO (Req. 5.2, 5.3).

```python
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional


def default_stix_data_dir(
    *,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    os_name: Optional[str] = None,
    home: Optional[Path] = None,
) -> Path:
    """Resolve o diretório STIX padrão no cache persistente do usuário.

    Puro: só calcula um ``Path`` — não cria diretórios nem acessa a rede
    (Req. 5.4). Usa exclusivamente ``pathlib`` (sem separadores hard-coded,
    Req. 5.3). Parâmetros injetáveis para teste determinístico de Windows/POSIX
    (Req. 5.2); quando ``None`` leem o SO real (``os.environ``, ``sys.platform``,
    ``os.name``, ``Path.home()``).

    Precedência do default (Req. 1.2, 1.3, 1.5):
      * Windows:  %LOCALAPPDATA%\\sticks\\stix   (se LOCALAPPDATA definido)
      * POSIX:    $XDG_CACHE_HOME/sticks/stix    (se XDG_CACHE_HOME definido)
                  ~/.cache/sticks/stix           (se HOME disponível)
      * Fallback: ~/.sticks/stix                 (determinístico, sem erro)
    """


def resolve_stix_data_dir(
    *,
    injected: Optional[Path] = None,
    override: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
    os_name: Optional[str] = None,
    home: Optional[Path] = None,
) -> Path:
    """Resolve o diretório de dados STIX aplicando a precedência estrita
    (Req. 2.1, 2.2, 2.3):

        injected  >  override (PIPELINE_UI_STIX_DIR)  >  default do SO

    ``injected`` tem precedência máxima (preserva os testes). ``override`` é
    usado quando não-vazio após ``strip()`` (um valor ausente/vazio é ignorado,
    Req. 2.3). Caso contrário cai no :func:`default_stix_data_dir`. Puro: não
    cria diretórios nem acessa a rede.
    """
```

Regras de implementação:
- `injected is not None` → `return Path(injected)` (precedência máxima).
- `override` não-vazio após `strip()` → `return Path(override)`.
- caso contrário → `return default_stix_data_dir(env=..., platform=..., os_name=..., home=...)`.
- Detecção de SO: `os_name == "nt"` **ou** `platform.startswith("win")` ⇒ Windows; caso contrário
  POSIX. Isso cobre tanto `os.name` quanto `sys.platform` injetados nos testes.
- `home` default: `Path.home()`; se `Path.home()` lançar (ambiente sem home), captura e usa um
  diretório determinístico (ver Error Handling), nunca propaga erro (Req. 1.5).

### 2. Settings — `app/core/config.py` (alteração)

Adicionar um atributo opcional seguindo o padrão existente (prefixo `PIPELINE_UI_`, leitura de
`.env`, `extra="ignore"`), Req. 2.4:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PIPELINE_UI_",
        env_file=".env",
        extra="ignore",
    )

    # ... settings existentes ...

    # Override explícito do diretório de dados STIX (Req. 2.1, 2.4). Quando
    # definido (env PIPELINE_UI_STIX_DIR ou .env), tem precedência sobre o
    # default do SO; ausente/vazio → aplica o default (Req. 2.3). O StixService
    # repassa este valor como `override` para resolve_stix_data_dir.
    stix_dir: Optional[str] = None
```

- O atributo é `Optional[str]` (não `Path`) para refletir fielmente a variável de ambiente e
  deixar a normalização para `pathlib`/o resolvedor; `None`/`""` ⇒ default (Req. 2.3).
- Nenhum efeito colateral: continua sendo leitura declarativa de settings; `get_settings()`
  permanece `lru_cached` e usável como dependência FastAPI.

### 3. `StixService.__init__` — `app/services/stix/stix_service.py` (alteração)

Trocar a composição do default baseada em `_repo_root()` por uma chamada ao resolvedor:

```python
from app.services.stix.stix_paths import resolve_stix_data_dir

class StixService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        http_client: Any = None,
        stix_dir_override: Optional[str] = None,  # normalmente Settings().stix_dir
    ) -> None:
        # Precedência: data_dir injetado > PIPELINE_UI_STIX_DIR > default do SO.
        # Puro: só resolve um Path — sem mkdir, sem rede (Req. 5.4).
        self._data_dir: Path = resolve_stix_data_dir(
            injected=data_dir,
            override=stix_dir_override,
        )
        self._http_client = http_client
        # ... carregamento defensivo de STIX_URL/STIX_DIR permanece igual ...
```

- `data_dir` injetado continua tendo precedência máxima; testes atuais (tmp + http mockado) não
  mudam (Req. 2.2, 5.1).
- O override é passado explicitamente (`stix_dir_override`), por padrão vindo de
  `get_settings().stix_dir`, mantendo o `__init__` sem leitura implícita de ambiente e preservando
  a injeção. Alternativamente, quando `stix_dir_override is None`, o resolvedor pode ler
  `os.environ["PIPELINE_UI_STIX_DIR"]`; a composição via `Settings` é a rota preferida por respeitar
  a precedência/nomenclatura existente (Req. 2.4).
- `base_path`, `data_dir` (property), `list_bases`, `download_base`, `read_bundle`, `dashboard`
  permanecem inalterados — apenas operam sobre o novo `_data_dir`.

### 4. Container (profile `containers`) — `infra/docker/docker-compose.yml` (alteração)

O serviço `backend` monta hoje `../../packages/cli/data/stix:/repo/packages/cli/data/stix` para
manter o diretório gravável sob o `/repo:ro`. O design substitui essa montagem por um **volume de
cache nomeado** desacoplado do repositório e aponta o override para o ponto de montagem (Req. 4.4):

```yaml
  backend:
    profiles: ["containers"]
    environment:
      PIPELINE_UI_DATABASE_URL: postgresql+psycopg://pipeline_ui:pipeline_ui@postgres:5432/pipeline_ui
      PIPELINE_UI_ENVIRONMENT: development
      # Cache STIX dentro do container, persistido em volume nomeado (Req. 4.4).
      PIPELINE_UI_STIX_DIR: /var/cache/sticks/stix
    volumes:
      - ../..:/repo:ro
      # Volume de cache STIX (persiste entre recriações do container e é
      # compartilhado/reaproveitado; substitui o bind em packages/cli/data/stix).
      - sticks-stix-cache:/var/cache/sticks/stix
      - /var/run/docker.sock:/var/run/docker.sock:ro

volumes:
  sticks-stix-cache:
    name: sticks-stix-cache
```

- `/repo` volta a poder ser inteiramente `:ro` (não é mais necessário furar um subdiretório
  gravável dentro do repo). A escrita do download vai para o volume de cache.
- Com o override definido, o container resolve `data_dir = /var/cache/sticks/stix` pela regra de
  precedência (override > default), sem depender da detecção de SO (Req. 2.1).
- O volume nomeado persiste a cópia de ~50 MB entre recriações e permite reaproveitamento,
  evitando re-download (decisão de contexto da spec).

> Observação sobre compartilhamento host↔container: um volume **nomeado** persiste e é reusado pelo
> container entre recriações. Se for desejável que o host (dev nativo) e o container usem exatamente
> o mesmo arquivo físico, a alternativa é um **bind mount** de um diretório de cache do host (ex.: o
> próprio `%LOCALAPPDATA%\sticks\stix` / `~/.cache/sticks/stix`) para `/var/cache/sticks/stix`. O
> volume nomeado é o default recomendado (portável e sem suposições de layout do host); o bind é uma
> opção documentada quando o compartilhamento físico com o host for requisito operacional.

### 5. `.gitignore` e documentação (Req. 4.2, 4.3)

- As regras `packages/cli/data/stix/*.json` e `sticks/data/stix/*.json` **permanecem** como defesa
  em profundidade, mesmo que o default não grave mais ali (Req. 4.2).
- README raiz e `apps/backend/README` passam a descrever: (a) que o bundle é materializado no cache
  do usuário (`%LOCALAPPDATA%\sticks\stix` / `$XDG_CACHE_HOME/sticks/stix` / `~/.cache/sticks/stix`),
  (b) o fallback `~/.sticks/stix`, e (c) como sobrescrever via `PIPELINE_UI_STIX_DIR` (Req. 4.3).

## Data Models

Não há novos modelos de dados persistidos nem alterações nos DTOs públicos. Os modelos tipados do
`StixService` (`BaseStixInfo`, `TypeCount`, `DashboardStix`, `CampaignStix`, `AdversaryStix`) e os
erros tipados (`StixFileError`/`StixLoadErrorType`, `StixDownloadError`) permanecem **inalterados**
em forma e semântica (Req. 3.4). O único "modelo" afetado é o **caminho resolvido** (`Path`), um
valor computado, não persistido:

- `data_dir: Path` — diretório de dados STIX resolvido pela precedência.
- `base_path(version): Path = data_dir / f"enterprise-attack-{version}.json"` — contrato inalterado.
- `Settings.stix_dir: Optional[str]` — override declarativo (env/`.env`), normalizado para `Path`
  apenas dentro do resolvedor.

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

A resolução de caminho é uma função **pura** e a escrita atômica tem invariantes universais bem
definidas, então o PBT é adequado para o núcleo desta mudança (portabilidade da resolução,
precedência, criação idempotente, não-deixar-parcial, equivalência observável). A fiação de
`Settings`, o `.gitignore` e a documentação/compose são verificados por testes de exemplo/snapshot
(ver Testing Strategy), não por propriedades.

As propriedades abaixo já refletem uma **reflexão de redundância**: a não-contenção no repo
(originalmente 1.1, 1.4 e 4.1) foi consolidada em uma única propriedade; as regras por SO (1.2 e 1.3)
foram combinadas em uma propriedade de mapeamento do default; e as três regras de precedência
(2.1, 2.2, 2.3) foram unificadas em uma única propriedade de precedência.

### Property 1: Precedência estrita da resolução

*Para qualquer* `injected` (opcional), `override` (opcional) e mapa de ambiente `env`,
`resolve_stix_data_dir` SHALL retornar:
- `Path(injected)` quando `injected` não é `None` (precedência máxima);
- caso contrário `Path(override)` quando `override` é não-vazio após `strip()`;
- caso contrário o default do SO calculado a partir de `env` (igual a `default_stix_data_dir(env=...)`).
Em particular, um `override` ausente/vazio nunca altera o resultado do default.

**Validates: Requirements 2.1, 2.2, 2.3**

### Property 2: Mapeamento do default por SO

*Para qualquer* diretório-base `b`:
- com plataforma Windows e `env["LOCALAPPDATA"] == b`, `default_stix_data_dir` SHALL retornar
  `Path(b) / "sticks" / "stix"`;
- com plataforma POSIX e `env["XDG_CACHE_HOME"] == b`, SHALL retornar `Path(b) / "sticks" / "stix"`;
- com plataforma POSIX, sem `XDG_CACHE_HOME` e `home == b`, SHALL retornar
  `Path(b) / ".cache" / "sticks" / "stix"`.

**Validates: Requirements 1.2, 1.3**

### Property 3: Default resolvido fora da raiz do repositório

*Para qualquer* `env`, plataforma e `home` que não esteja contido na raiz do repositório
(`_repo_root()`), o caminho retornado por `default_stix_data_dir` (e, consequentemente, por
`download_base` ao materializar o bundle) SHALL NOT ser relativo a `_repo_root()` — ou seja,
`resolved.is_relative_to(repo_root)` é `False`.

**Validates: Requirements 1.1, 1.4, 4.1**

### Property 4: Fallback determinístico sem erro

*Para qualquer* `env` sem as chaves de base (`LOCALAPPDATA`, `XDG_CACHE_HOME`), a resolução do
default SHALL NOT lançar exceção e SHALL ser determinística (duas chamadas com o mesmo input
retornam o mesmo `Path`); quando apenas o home está disponível, o resultado SHALL terminar em
`.sticks / stix`.

**Validates: Requirements 1.5**

### Property 5: Portabilidade por pathlib

*Para qualquer* diretório-base e plataforma (Windows/POSIX) injetados, o caminho resolvido SHALL ser
exatamente a composição `pathlib` da base com o sufixo esperado — isto é, suas duas últimas
componentes (`resolved.parts[-2:]`) SHALL ser `("sticks", "stix")` e
`resolved == Path(base).joinpath(*suffix_parts)` — propriedade que só se sustenta se a construção
usar exclusivamente `pathlib`, sem separadores hard-coded.

**Validates: Requirements 5.3**

### Property 6: Instanciação sem efeitos colaterais

*Para qualquer* `data_dir` apontando para um diretório inexistente, instanciar `StixService` com um
`http_client` espião SHALL NOT criar o diretório nem realizar qualquer chamada de rede — após o
`__init__`, o diretório ainda não existe e nenhum método do cliente HTTP foi invocado.

**Validates: Requirements 5.4**

### Property 7: Criação de diretório idempotente sob demanda

*Para qualquer* `data_dir` resolvido inicialmente inexistente, executar `download_base` (com fetch
mockado para um bundle válido) SHALL criar o diretório e seus pais sem lançar, e uma segunda
execução sobre o diretório já existente SHALL também concluir sem erro (idempotência de
`mkdir(parents=True, exist_ok=True)`).

**Validates: Requirements 3.3**

### Property 8: Nenhum arquivo parcial em caso de falha

*Para qualquer* falha injetada no caminho de download (erro de fetch, conteúdo não-STIX, ou falha de
escrita/rename), após `download_base` lançar `StixDownloadError`, o `base_path(version)` SHALL NOT
existir e nenhum arquivo temporário SHALL permanecer no `data_dir` — a escrita atômica nunca deixa um
bundle meio escrito.

**Validates: Requirements 3.3**

### Property 9: Equivalência do comportamento observável independente do local

*Para qualquer* bundle STIX válido escrito em dois diretórios de dados distintos, o resultado
observável de `list_bases`, `read_bundle` e `dashboard` SHALL ser equivalente entre os dois locais a
menos dos campos que embutem o caminho (`file`): os `counts` do dashboard, o dict retornado por
`read_bundle` e os flags `downloaded`/`selectable` por versão SHALL ser idênticos.

**Validates: Requirements 3.1, 3.4**

## Error Handling

A mudança **não altera** o tratamento de erro do download, da leitura ou da listagem (Req. não-objetivo).
Os erros tipados existentes permanecem o contrato observável:

- **Resolução de caminho (`stix_paths`)** — a resolução é "total": nunca lança para calcular um
  caminho. Em particular, o fallback (Req. 1.5) é robusto: se `Path.home()` não puder ser resolvido
  (ambiente sem `HOME`/perfil), a função captura a exceção e usa um diretório determinístico
  (`Path(".sticks") / "stix"` relativo ao cwd, ou um raiz portável equivalente) em vez de propagar —
  garantindo que a importação/instanciação nunca falhe por ambiente (Req. 1.5, 5.4). Nenhuma
  normalização agressiva (ex.: `resolve()` que toca o FS) é feita durante a resolução, para manter a
  pureza e a ausência de efeitos colaterais.
- **`StixDownloadError`** — inalterado. Cobre versão desconhecida (validada antes de disco/rede),
  erro de fetch/HTTP, conteúdo não-STIX e falha de escrita/rename. Em qualquer falha o arquivo
  temporário é removido (`finally`) e o `base_path` continua ausente — garantido pela Property 8.
- **`StixFileError` / `StixLoadErrorType`** — inalterado. Distingue `MISSING_FILE`,
  `MALFORMED_JSON`, `INVALID_STRUCTURE`; `read_bundle` nunca retorna objeto parcial. Com o novo
  local, um bundle não baixado continua produzindo `MISSING_FILE` normalmente.
- **Criação do diretório** — `mkdir(parents=True, exist_ok=True)` é idempotente; um diretório
  pré-existente não é erro (Property 7). Uma falha de `OSError` na criação/escrita é mapeada para
  `StixDownloadError` sem deixar parcial.
- **Override inválido** — `PIPELINE_UI_STIX_DIR` é tratado como caminho literal; não há validação de
  existência na resolução (coerente com a semântica atual, onde a criação é no download). Um valor
  vazio/branco é ignorado e cai no default (Req. 2.3), coberto pela Property 1.

## Testing Strategy

### Biblioteca de PBT

- **Hypothesis** (já presente no backend — ver `apps/backend/.hypothesis/`). Não implementar PBT do
  zero. Cada teste de propriedade roda **no mínimo 100 iterações** (`@settings(max_examples=100)` ou
  o default ≥100) e é anotado com um comentário referenciando a propriedade do design no formato:
  `# Feature: stix-bundle-cache-location, Property {n}: {texto}`.
- Cada propriedade do design é implementada por **um único** teste de propriedade.

### Isolamento (Req. 5.1, 5.2, 5.3)

- Os testes do `StixService` continuam injetando um `data_dir` em `tmp_path` e um `http_client`
  mockado — nunca tocam o cache real do usuário nem a rede.
- Os testes do resolvedor injetam `env`, `platform`/`os_name` e `home` (parâmetros da função pura),
  cobrindo Windows e POSIX **sem** criar diretórios reais fora de um tmp e **sem** depender do SO.
- Nenhuma propriedade de resolução cria diretórios (a função é pura); apenas as propriedades de
  download (7, 8) escrevem, sempre dentro de `tmp_path`.

### Testes de propriedade (Hypothesis) → propriedades do design

| Teste | Propriedade | Geradores |
|-------|-------------|-----------|
| precedência injected/override/default | Property 1 | `injected`/`override`/`env` opcionais e strings de caminho |
| mapeamento do default por SO | Property 2 | base de diretório + platform ∈ {win, posix} |
| default fora do repo | Property 3 | `env`/`home` fora de um repo-root controlado |
| fallback determinístico | Property 4 | `env` sem chaves de base |
| portabilidade pathlib | Property 5 | base + platform |
| sem efeitos colaterais na instanciação | Property 6 | `data_dir` inexistente + `http_client` espião |
| mkdir idempotente | Property 7 | `data_dir` inexistente + bundle válido mockado |
| sem arquivo parcial em falha | Property 8 | ponto de falha ∈ {fetch, validação, replace} |
| equivalência observável entre locais | Property 9 | bundle STIX válido gerado + dois `data_dir` |

### Testes de exemplo / edge cases (unit)

- **Fiação de `Settings` (Req. 2.4)** — `setenv PIPELINE_UI_STIX_DIR=/x` ⇒ `Settings().stix_dir == "/x"`;
  ausente ⇒ `None`. (A ausência de cache do `get_settings` entre casos é garantida limpando o
  `lru_cache` ou instanciando `Settings` diretamente.)
- **Reuso de cópia em disco (Req. 3.2)** — pré-criar `enterprise-attack-{v}.json` no `tmp` ⇒
  `list_bases` reporta `downloaded=true` e `read_bundle` lê sem chamar o `http_client`.
- **Estado inicial limpo (Req. 3.1, 3.5)** — `data_dir=tmp` vazio ⇒ todas as versões
  `downloaded=false`.
- **`.gitignore` (Req. 4.2)** — asserção estática de que as linhas `packages/cli/data/stix/*.json`
  (e `sticks/data/stix/*.json`) permanecem presentes.
- **Override com precedência sobre default** — exemplo concreto confirmando que
  `PIPELINE_UI_STIX_DIR` vence o default resolvido para o SO corrente.

### Verificações de integração / snapshot (não-PBT) (Req. 4.4, 4.3)

- **Compose (Req. 4.4)** — verificação de snapshot/parse do `docker-compose.yml`: o serviço
  `backend` define `PIPELINE_UI_STIX_DIR=/var/cache/sticks/stix` e monta o volume nomeado
  `sticks-stix-cache` em `/var/cache/sticks/stix`. Isto é configuração de infra, não função com
  input/output — por isso é snapshot, não propriedade.
- **Documentação (Req. 4.3)** — checagem (grep/manual) de que o README raiz e o
  `apps/backend/README` descrevem o local do cache e o override `PIPELINE_UI_STIX_DIR`. Validação
  completa é editorial/manual.

### Balanceamento

Os testes de propriedade cobrem a correção universal (precedência, portabilidade, invariantes de
escrita e equivalência). Os testes de exemplo cobrem pontos concretos de integração (fiação de
Settings, reuso de disco, estado inicial) e edge cases, evitando duplicar o que o PBT já cobre.
