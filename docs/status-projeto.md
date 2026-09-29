# Status do projeto — reunião de 1º de outubro de 2026

## Resumo executivo

A Pipeline UI está implementada de ponta a ponta no modo mockado e suas três
etapas foram inspecionadas no navegador com a campanha ShadowRay. O frontend,
o backend, a persistência, a contenção, a integração Caldera e os canais em
tempo real existem e têm cobertura automatizada. O único marco ainda não
comprovado é a execução completa no laboratório Docker real após a correção do
payload enviado à Caldera.

## O que pode ser demonstrado

1. Seleção entre os oito casos curados e progresso por estágio.
2. Modelagem estrutural do ShadowRay no Estágio 1.
3. Abilities, comandos, dependências e Adversary no Estágio 2.
4. Preview seguro dos comandos e destinos no Estágio 3.
5. Confirmação explícita antes de qualquer execução.
6. Bloqueio de destinos externos e de containers não isolados.
7. Tema claro/escuro (seguindo o sistema operacional e persistente entre
   recarregamentos), pt-BR/en com bandeiras em SVG e persistência da sessão.
8. Fluxo completo mockado com resultados, agregação e auditoria.
9. Ambiente de desenvolvimento com um único comando na raiz (`npm run dev`)
   que sobe backend, frontend e prepara o banco de uma só vez.

## Situação por área

| Área | Situação | Evidência |
|---|---|---|
| Scaffold, banco e migrações | Concluído | FastAPI, React/Vite, SQLAlchemy e Alembic |
| Oito casos curados | Concluído | API lista e carrega todos os casos |
| Contenção e isolamento | Concluído | Properties 1 e 2 + suíte de segurança |
| Caldera API | Implementado | Cliente e contratos mockados; payload real corrigido |
| Três estágios da interface | Concluído | Auditoria visual do ShadowRay |
| WebSocket | Corrigido | Três assinaturas; sem loop após close 1000 |
| Tema e internacionalização | Concluído | Tema segue o SO e persiste no reload; bandeiras SVG; testes 11/12 |
| Experiência de desenvolvimento | Concluído | `npm run dev` único na raiz (backend+frontend+migração), node_modules unificado |
| E2E mockado | Concluído | Preview → confirmação → execução → auditoria |
| E2E Docker real | Pendente | Build Kali/Caldera e nova execução do ShadowRay |

## Pendências priorizadas

1. Estabilizar o build da imagem Kali diante dos erros HTTP 403 do espelho.
2. Repetir o E2E real do ShadowRay após a inclusão de `technique_name`.
3. Confirmar os 11 resultados e a trilha de auditoria no banco.
4. Implementar os property tests opcionais ainda ausentes: Properties 3, 4,
   7, 8, 9 e 10, além do teste local real do Estágio 1.
5. Encerrar o checkpoint final somente após o E2E real.

## Riscos e decisões

- O laboratório permanece contido nas redes `172.20/21/22.0.0/24`; a rede
  `172.23.0.0/24` é exclusiva do plano de gerenciamento da Caldera.
- A porta 8000 não será usada por este projeto. A aplicação usa 15173/8010.
- O teste real é opt-in e não roda automaticamente em desenvolvimento ou CI.
- O checklist detalhado e retomável está em `.kiro/specs/pipeline-ui/tasks.md`.
