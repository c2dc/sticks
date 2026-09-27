# Pipeline UI — Frontend

SPA em React + TypeScript (Vite) para a **Pipeline_UI** do projeto `sticks`.
Stack: React 19, TypeScript, Vite, Tailwind CSS (dark mode via classe `dark`),
shadcn/ui (New York) e i18next (pt-BR padrão, fallback pt-BR).

## Scripts

```powershell
npm install      # instala dependências
npm run dev      # dev server em http://localhost:5173 (proxy /api -> :8000)
npm run build    # type-check + build de produção em dist/
npm run preview  # serve o build de produção
npm run lint     # type-check sem emitir
```

## Estrutura

- `src/i18n/` — configuração i18next com namespaces (`common`, `pipeline`,
  `preferences`) e recursos vazios para `pt-BR` e `en`.
- `src/lib/api.ts` — cliente HTTP tipado para o backend FastAPI.
- `src/lib/ws.ts` — utilitário de WebSocket (canais de tempo real).
- `src/components/theme-provider.tsx` — tema Modo_Claro/Modo_Escuro (classe `dark`).
- `src/components/AppLayout.tsx` — layout base.
- `src/components/PipelineOverview.tsx` — placeholder da visão de pipeline.
- `src/components/PreferencesPanel.tsx` — placeholder do painel de preferências.
- `src/components/ui/` — componentes shadcn/ui.

Telas reais (estágios, seletor de casos, emulação) são implementadas em tarefas
posteriores.
