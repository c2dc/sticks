# STICKS (Pipeline UI + Emulação de Adversário)

---

## 1. Em uma frase

Desde a última reunião, eu tirei o projeto do papel: saí de uma especificação
escrita e de 8 casos curados manualmente para um **pipeline funcionando de ponta
a ponta**, com interface web, backend, contenção de segurança e — o marco mais
importante — a **emulação real de um adversário (ShadowRay) executada com
sucesso dentro do laboratório Docker isolado**.

Para acelerar e manter qualidade, usei **IA como ferramenta de apoio** (pareamento
para codificar, depurar e documentar), sempre conduzindo as decisões de
arquitetura, segurança e validação.

---

## 2. Contexto rápido (para situar o professor)

O STICKS acompanha o artigo sobre a *lacuna de semântica procedural em
ATT&CK-in-STIX*. O pipeline tem **3 estágios**:

1. **Modelagem Estrutural** (automática): extrai técnicas, relações, indicadores,
   infraestrutura e malware a partir de CTI em STIX.
2. **Tradução Curada** (hoje humana): converte comportamento abstrato em passos
   executáveis mínimos — **é a lacuna que a pesquisa quer automatizar**.
3. **Emulação de Adversário**: executa os passos no MITRE Caldera, dentro de um
   ambiente Docker isolado.

Existem **8 campanhas já curadas manualmente**. O meu trabalho desta fase foi
construir a base — interface, orquestração e laboratório — para primeiro
**entender e reproduzir** bem esse fluxo antes de automatizar o Estágio 2.

---

## 3. O que eu entreguei da última reunião até hoje

### 3.1. Interface web do pipeline (frontend)
- SPA em React + TypeScript que torna o pipeline evidente: os 3 estágios lado a
  lado, com estado (não iniciado / em andamento / concluído / erro), seções de
  Entrada / Processamento / Saída e progresso.
- Seleção entre as 8 campanhas, condução na ordem 1 → 2 → 3 com **bloqueio
  sequencial** (não deixa iniciar um estágio bloqueado).
- **Modal de confirmação de emulação**: antes de executar qualquer coisa, mostra
  a lista completa de comandos concretos e o container-alvo de cada um. Nada roda
  sem confirmação explícita.
- Tema claro/escuro (seguindo o sistema e persistente) e internacionalização
  pt-BR / en.

### 3.2. Backend e orquestração (FastAPI)
- Serviços que leem as 8 campanhas, executam o Estágio 1 real, orquestram os
  estágios, conversam com o Caldera e persistem o estado.
- API REST + canais em tempo real (WebSocket) para o progresso.
- Persistência de estado para dar continuidade ao trabalho entre sessões.

### 3.3. Núcleo de segurança — contenção (o ponto que eu tratei como inegociável)
- **Validação de destino**: qualquer comando que aponte para fora das subnets
  internas do laboratório **bloqueia a operação inteira** e identifica o comando
  ofensor.
- **Verificação de isolamento**: antes de executar, inspeciono (somente leitura)
  os containers-alvo; se algum não estiver isolado, a operação é abortada.
- **Trilha de auditoria 1:1**: cada comando executado gera exatamente um registro
  (comando + container de destino + resultado).

### 3.4. O ambiente de laboratório (o SUT — System Under Test)
Montei e deixei funcionando a rede isolada que simula o alvo:

```
caldera (C2)  →  kali (atacante)  →  nginx (alvo web)  →  db (alvo backend)
```

- Tudo em redes `internal: true` — **sem rota para o host nem para a internet**.
- O atacante (Kali) roda o agente do Caldera e executa os comandos de cada
  campanha contra os alvos.

### 3.5. O marco: emulação real ponta a ponta
Coloquei o laboratório de pé e **executei a campanha ShadowRay de verdade**: as
**11 técnicas** rodaram no ambiente real, de forma contida, com a trilha de
auditoria persistida. Esse era o único marco que faltava para fechar a fase.

---

## 4. Métricas (para comprovar)

| Métrica | Número |
|---|---|
| Estágios do pipeline funcionando | 3 |
| Campanhas curadas integradas | 8 |
| Técnicas executadas no ShadowRay (emulação real) | 11 |
| Extração do Estágio 1 (ShadowRay) | 10 técnicas, 9 relações, 14 indicadores, 2 infra, 2 malware |
| Testes automatizados do backend | 188 (em 25 arquivos) |
| Testes do frontend | 17 (em 6 arquivos) |
| Propriedades de correção verificadas (property-based) | 12 |
| Containers no laboratório | 4 (caldera, kali, nginx, db) |
| Resultado do E2E real do ShadowRay | **PASSOU** |

Observação: as **12 propriedades** incluem as de segurança (nenhuma emulação
inicia com destino externo; só prossegue com todos os alvos isolados; execução
exige confirmação explícita; auditoria 1:1; falha de auditoria aborta a
operação). Essas foram testadas com **centenas de iterações** cada.

---

## 5. O que foi mais fácil e o que foi mais difícil

### Mais fácil / fluiu bem
- A **interface e o backend**: com a especificação clara (requisitos → design →
  tarefas), a construção foi incremental e previsível.
- A **lógica de contenção e os testes**: por serem lógica pura, deu para validar
  tudo com testes automatizados, sem depender do ambiente real.
- O **Estágio 1** (modelagem estrutural): reaproveitei o parsing já existente e
  validei a extração real com os arquivos locais.

### Mais difícil / onde gastei mais esforço
- **Subir o laboratório real foi o maior desafio** — e foi onde aprendi mais.
  Encontrei uma cadeia de problemas reais que precisei diagnosticar um a um:
  1. A **imagem do Kali** quebrava no download (metapacotes gigantes). Resolvi
     enxugando a imagem só com as ferramentas que as campanhas usam
     (de ~2,4 GB para ~400 MB).
  2. O **Caldera subia mas sem a interface web** — descobri que um
     compartilhamento de pasta escondia a UI já compilada e ainda travava o boot.
  3. Os **alvos não subiam** porque tentavam instalar pacotes pela internet em
     tempo de execução, mas a rede é isolada de propósito. Movi essas instalações
     para o momento da construção da imagem.
  4. A **primeira emulação rodava "vazia"**: o perfil do adversário não era
     carregado no Caldera e o sistema não esperava a execução terminar. Corrigi o
     carregamento e o acompanhamento da operação.
- A lição: a contenção (sem internet) é exatamente o que torna o laboratório
  seguro, mas também o que mais exige cuidado para montar — tudo tem que ser
  resolvido offline, antecipadamente.

---

## 6. Decisão de engenharia que vale destacar: endereçamento do laboratório

Percebi que o laboratório usava faixas de rede classe B grandes, que **colidiam
com outras redes já presentes na máquina**. Uma colisão dessas poderia rotear um
comando de ataque para o lugar errado — um risco de contenção.

Reprojetei o endereçamento para faixas classe C menores (192.168.x), com uma
convenção legível: **o número final do IP identifica a máquina** (o atacante é
sempre `.20`, o alvo web sempre `.30`), em qualquer rede. E adicionei uma
**verificação automática que roda antes de subir o laboratório** e aborta se
alguma faixa já estiver em uso na máquina — para garantir que nenhum pacote de
ataque escape para fora do ambiente.

---

## 7. Estado atual e próximos passos

**Estado:** a fase de base está concluída. O pipeline funciona de ponta a ponta,
a emulação real foi comprovada e tudo está coberto por testes e documentado.

**Próximos passos:**
1. Reproduzir **mais 1–2 campanhas** no laboratório real, além do ShadowRay.
2. Fazer a **demonstração visual** pela interface: escolher uma campanha e
   caminhar pelos 3 estágios até a emulação.
3. Iniciar a **Fase 2 — automatizar o Estágio 2** (a tradução hoje manual), que
   é o objetivo central da pesquisa. A base já foi preparada para acomodar isso:
   a "origem da tradução" é um atributo extensível (hoje "curadoria humana";
   futuramente "agente automatizado").

---

## 8. Roteiro sugerido para os 15–20 min

1. **(2 min)** Contexto: a lacuna do Estágio 2 e os 3 estágios.
2. **(3 min)** O que entreguei: interface, backend, contenção, laboratório.
3. **(4 min)** Demonstração: abrir a UI, mostrar os 3 estágios e o modal de
   confirmação com os comandos concretos.
4. **(4 min)** O marco: a emulação real do ShadowRay (mostrar que as 11 técnicas
   rodaram, contidas, com auditoria).
5. **(3 min)** Métricas e o que foi mais difícil (o laboratório real).
6. **(2 min)** Próximos passos: automatizar o Estágio 2.
