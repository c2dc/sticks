# Pipeline_UI — Backend (FastAPI)

Backend da **Pipeline_UI** para o projeto `sticks`. Orquestra o pipeline de três
estágios existente, expõe a API REST/WebSocket para o frontend, aplica as
verificações de contenção e persiste o estado.

Este projeto é **aditivo**: ele reutiliza (sem duplicar) o código e a
configuração do pacote `sticks/` existente na raiz do repositório.

## Estrutura

```
pipeline_ui/backend/
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

## Desenvolvimento (Windows)

```powershell
# a partir de pipeline_ui/backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# rodar o servidor de desenvolvimento
uvicorn app.main:app --reload --port 8010

# health-check
# GET http://localhost:8010/health  ->  {"status": "ok"}
```

O banco padrão em desenvolvimento é **SQLite** (arquivo local). A configuração
completa do banco chega na tarefa 1.3; o módulo de configuração já é
forward-compatible com PostgreSQL via variável de ambiente.

Nenhuma Caldera real ou rede Docker de ataque é usada em desenvolvimento;
tudo usa mocks/fixtures.

## Testes

```powershell
# a partir de pipeline_ui/backend, com o venv ativo
pytest
```

A suíte usa apenas mocks/fixtures e não toca em Caldera nem em rede Docker de
ataque. O teste de integração ponta a ponta com o **ambiente REAL**
(`tests/test_e2e_real_lab.py`, Tarefa 15.3) é **pulado por padrão** — ele fica
sob o marcador `lab` (desmarcado via `-m "not lab"` no `pyproject.toml`) e sob
`skipif(PIPELINE_UI_LAB != "1")`, então nunca roda em dev/CI. Para rodá-lo na
**máquina de laboratório** (Caldera real + compose endurecido, contenção
satisfeita), veja [`LAB.md`](./LAB.md).
