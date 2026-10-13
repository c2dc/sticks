# Pipeline_UI — Backend (FastAPI)

Backend da **Pipeline_UI** para o projeto `sticks`. Orquestra o pipeline de três
estágios existente, expõe a API REST/WebSocket para o frontend, aplica as
verificações de contenção e persiste o estado.

Este projeto é **aditivo**: ele reutiliza (sem duplicar) o código e a
configuração do pacote `sticks/` existente na raiz do repositório.

## Estrutura

```
ui/backend/
  app/
    core/       # configuração + integração com o pipeline sticks existente
    models/     # modelos SQLAlchemy do domínio
    services/   # serviços de aplicação (CaseService, StageService, ...)
    api/        # rotas REST
    ws/         # canais WebSocket/SSE
    main.py     # app factory + roteador raiz
  tests/
  pyproject.toml
```

## Desenvolvimento (hot-reload nativo)

O projeto usa **somente PostgreSQL** — não há SQLite em lugar nenhum. A infra
(incluindo o Postgres) sobe via Docker; o backend e o frontend rodam
nativamente no host com hot-reload, apontando para o Postgres containerizado.

```bash
# 1. subir a infra (inclui o Postgres publicado em 127.0.0.1:5432)
cd docker && docker compose up -d

# 2. preparar o ambiente nativo uma única vez (venv do backend + node_modules)
#    a partir da raiz do repositório
npm run setup

# 3. rodar frontend + backend nativamente com hot-reload, contra o Postgres
#    containerizado (aplica as migrações do Alembic antes de servir)
npm run dev
```

- UI: http://localhost:8080
- API: http://localhost:8010 — health-check: `GET /health` → `{"status": "ok"}`

O banco padrão (`PIPELINE_UI_DATABASE_URL`) já aponta para o Postgres
containerizado em `localhost:5432`. O serviço `backend` do compose
(perfil `containers`, opt-in) sobrescreve o host para `postgres:5432`.

Nenhuma Caldera real ou rede Docker de ataque é usada em desenvolvimento;
tudo usa mocks/fixtures.

## Testes

```powershell
# a partir de ui/backend, com o venv ativo
pytest
```

A suíte usa apenas mocks/fixtures e não toca em Caldera nem em rede Docker de
ataque. O teste de integração ponta a ponta com o **ambiente REAL**
(`tests/test_e2e_real_lab.py`, Tarefa 15.3) é **pulado por padrão** — ele fica
sob o marcador `lab` (desmarcado via `-m "not lab"` no `pyproject.toml`) e sob
`skipif(PIPELINE_UI_LAB != "1")`, então nunca roda em dev/CI. Para rodá-lo na
**máquina de laboratório** (Caldera real + compose endurecido, contenção
satisfeita), veja [`LAB.md`](./LAB.md).
