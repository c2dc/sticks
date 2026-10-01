# LAB.md — Integração ponta a ponta com ambiente REAL (Tarefa 15.3)

> ⚠️ **Este procedimento roda EXCLUSIVAMENTE na máquina de laboratório do
> usuário.** Ele executa emulação de adversário **real** contra a Caldera real e
> os containers reais do Ambiente_Docker. **Nunca** rode no ambiente de
> desenvolvimento nem em CI.

O teste `tests/test_e2e_real_lab.py` conduz 1–2 casos curados de ponta a ponta
com **Caldera real** em `http://localhost:8888` e o **Ambiente_Docker real**,
com a contenção efetivamente satisfeita, validando a emulação e a **trilha de
auditoria persistida** (Requisitos **4.2, 4.4, 6.3, 6.8**).

Ele usa o **CalderaClient real** (sem mock) e o **inspetor de isolamento real**
(somente leitura, via Docker Engine API). Fora da máquina de laboratório ele fica
**pulado** por dois guardas independentes:

- o marcador `lab`, desmarcado por padrão via `addopts = -m "not lab"` no
  `pyproject.toml`; e
- `@pytest.mark.skipif(os.getenv("PIPELINE_UI_LAB") != "1", ...)`.

Assim, a suíte completa continua verde em dev/CI sem nenhum ambiente de
laboratório.

## Pré-requisitos de contenção

Por decisão de design (*"Decisão registrada — endurecimento via compose separado
(Opção A)"*), a contenção só é satisfeita na máquina de laboratório com o compose
**endurecido** aplicado. O arquivo `docker/docker-compose.hardened.yml` é uma
**referência exclusiva de laboratório**: ele não altera o `docker-compose.yml`
usado no desenvolvimento pelos demais integrantes. O compose endurecido deve:

- remover `local-network` (bridge) de `kali`, `nginx` e `db`, deixando-os
  **somente** nas redes `internal: true` — `192.168.10.0/24`, `192.168.20.0/24`,
  `192.168.30.0/24`;
- não configurar DNS externo nesses containers;
- pré-satisfazer offline dependências de egress dos comandos curados
  (`apt-get`, `pip install`, `git clone`, `wget`/`curl` externos), já que a
  Operação é bloqueada se qualquer destino cair fora das subnets internas.

O pre-flight de contenção (destinos + isolamento) recusa a execução enquanto essa
contenção não estiver satisfeita — é isso que torna a execução real segura.

## Como rodar na máquina de laboratório

1. Suba a stack endurecida (arquivo de referência lab-only):

   ```powershell
   docker compose -f docker/docker-compose.yml -f docker/docker-compose.hardened.yml up -d
   ```

2. Confirme que a Caldera responde em `http://localhost:8888` (header `KEY`).

3. A partir de `pipeline_ui/backend`, com o venv ativo, defina a variável de
   opt-in e rode apenas o teste de laboratório:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   $env:PIPELINE_UI_LAB = "1"
   pytest -m lab
   ```

   (Alternativa POSIX: `PIPELINE_UI_LAB=1 pytest -m lab`.)

### Opções

- **Casos executados** — por padrão roda 1 caso (`shadowray`). Para escolher
  1–2 casos, defina `PIPELINE_UI_LAB_CASES` com slugs separados por vírgula, por
  exemplo `$env:PIPELINE_UI_LAB_CASES = "shadowray,c0010"`.
- **Banco** — por padrão a trilha de auditoria é persistida em um SQLite
  temporário. Para usar o PostgreSQL real do laboratório, aponte
  `PIPELINE_UI_DATABASE_URL` para ele antes de rodar.

Se a Caldera não estiver no ar quando o teste rodar, ele **pula** com uma
mensagem pedindo para subir a stack endurecida — em vez de falhar.

## O que o teste valida

- **Req. 4.2** — carrega Abilities + Adversary do caso e inicia uma Operação real
  na Caldera contra o Ambiente_Docker.
- **Req. 4.4/4.5** — persiste o resultado por Ability e o agregado
  (`total_sucesso`/`total_falha`), consistentes entre si.
- **Req. 6.3** — o pre-flight de isolamento **real** (somente leitura) confirma
  que os containers-alvo estão isolados antes de qualquer execução.
- **Req. 6.8** — a trilha de auditoria é persistida **1:1**: exatamente um
  registro por comando executado, com comando + container de destino + resultado,
  sem duplicatas.
