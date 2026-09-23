# Roadmap do Projeto — Automação da Curadoria (STICKS)

Este documento registra a visão geral e a ordem das fases de trabalho para
que o planejamento acompanhe o repositório e possa ser retomado em qualquer
máquina (via `git pull`). As specs detalhadas de cada fase vivem em
`.kiro/specs/{nome-da-fase}/` (requirements.md, design.md, tasks.md).

## Contexto

O projeto **STICKS** acompanha o artigo *"The Procedural Semantics Gap in
ATT&CK-in-STIX: Measuring Procedural Sufficiency for APT Emulation"*. O
pipeline atual tem três estágios:

- **Estágio 1 — Modelagem Estrutural** (automatizado): parsing de STIX,
  extração de técnicas, relacionamentos, indicadores, infraestrutura, malware
  e metadados de campanha.
- **Estágio 2 — Tradução com Humano no Loop** (curadoria humana, hoje manual):
  tradução de comportamentos abstratos (técnicas ATT&CK) em passos executáveis
  mínimos. **É a lacuna que este projeto de pesquisa quer automatizar.**
- **Estágio 3 — Emulação de Adversário**: execução das abilities/adversaries
  no MITRE Caldera dentro de um ambiente Docker isolado.

Existem **8 casos já curados manualmente** (por Roth e Sidnei), em
`sticks/data/api/`: APT41-DUST, C0010, C0026, CostaRicto, Operation
MidnightEclipse, Operation Outer Space, Salesforce Data Exfiltration e
ShadowRay.

## Objetivo do meu trabalho

Assumir a responsabilidade de **criar agente(s) de IA** que preencham a lacuna
do Estágio 2 (curadoria hoje feita por humano). O caminho até lá é incremental
e começa pela interface e pela replicação dos casos existentes, para primeiro
entender bem o fluxo antes de automatizá-lo.

## Restrições permanentes

- **Contenção de segurança é inegociável.** Todo o trabalho de emulação deve
  ficar contido no ambiente Docker isolado (redes `internal: true`:
  172.20/21/22.0.0/24). Nenhum comando de adversário pode atingir o host nem
  alvos externos. A execução real ocorre apenas na máquina de laboratório.
- **Limitação de hardware.** Só ambientes emuláveis em Docker Desktop.
- **Spec-driven.** Toda fase é planejada como spec (requisitos -> design ->
  tarefas) antes da implementação, para permitir continuidade entre máquinas
  (Kiro e ChatGPT) sem perder o controle.

## Fases

### Fase 1 — Pipeline UI + Replicação dos casos curados  [OK] (planejada)

- **Spec:** `.kiro/specs/pipeline-ui/`
- **Branch:** `feat/ui-pipeline`
- **Status:** Spec completa (requirements + design + tasks). Implementação
  pendente (será executada na máquina de laboratório).
- **Escopo:** Interface web moderna que torna o pipeline evidente (telas por
  fase com Entrada/Processamento/Saída), conduz passo a passo a replicação dos
  8 casos curados e executa 1-2 casos de forma contida no Docker. Tema
  claro/escuro (paleta blue/red/purple team), i18n pt-BR/en, persistência de
  estado para continuidade entre máquinas.
- **Stack:** FastAPI (reusa o pipeline Python existente) + React/TypeScript/
  Vite/Tailwind/shadcn/ui/i18next + PostgreSQL (SQLite em dev).
- **Fora de escopo (intencional):** os agentes de IA do Estágio 2.

### Fase 2 — Agentes de IA para o Estágio 2  [A PLANEJAR]

- **Spec:** a criar (`.kiro/specs/ai-curation-agents/` — nome provisório)
- **Branch prevista:** `feat/ai-curation-agents`
- **Status:** Não iniciada. **Só planejar depois de conseguir rodar 1-2
  campanhas pela UI da Fase 1**, para entender bem entrada/processamento/saída
  do Estágio 2 antes de automatizá-lo.
- **Escopo previsto:** agente(s) de IA que convertem comportamentos abstratos
  (técnicas) em passos executáveis mínimos — a curadoria hoje manual. Ponto de
  partida técnico já existente: o campo `ai_prompt_template` presente nos DAGs
  em `sticks/data/dag/*.json` é exatamente o prompt pensado para essa
  automação. A UI da Fase 1 já foi projetada para acomodar uma "origem de
  tradução" extensível (hoje "curadoria humana"; futuramente "agente de IA")
  sem alterar o modelo de dados.
- **Meta de avaliação:** medir a suficiência procedural — comparar a curadoria
  gerada por IA com os 8 casos curados por humano.

## Estratégia de branches (Git)

- Repositório de grupo: `github.com/c2dc/sticks`. Trabalhar sempre a partir da
  `main` atualizada, em **feature branches**; nunca commitar direto na `main`.
- Uma branch por fase; abrir Pull Request para a `main` ao concluir cada fase.
- Antes de criar uma nova branch: `git fetch origin` e partir de `origin/main`.
- A branch `sticks-curated` é antiga e está atrás da `main` (que já contém o
  trabalho curado). Não é necessária.

## Continuidade entre máquinas

- As specs e este roadmap vivem no repositório e viajam com o `git pull`.
- Em outra máquina: `git fetch origin && git checkout feat/ui-pipeline` traz
  todo o planejamento da fase atual.
- Ao trabalhar com outra ferramenta (ex.: ChatGPT), os documentos da spec são a
  fonte de verdade para retomar de onde parou.
