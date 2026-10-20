# Pipeline UI — Frontend

React + TypeScript SPA (Vite) for the project's **Pipeline UI** (`sticks`).
Stack: React 19, TypeScript, Vite, Tailwind CSS (dark mode via the `dark` class),
shadcn/ui (New York), and i18next (pt-BR default, pt-BR fallback).

## Scripts

```powershell
npm install      # install dependencies
npm run dev      # dev server at http://localhost:8080 (proxy /api -> :8010)
npm run build    # type-check + production build in dist/
npm run preview  # serve the production build
npm run lint     # type-check without emitting
```

## Structure

- `src/i18n/` — i18next configuration with namespaces (`common`, `pipeline`,
  `preferences`) and empty resources for `pt-BR` and `en`.
- `src/lib/api.ts` — typed HTTP client for the FastAPI backend.
- `src/lib/ws.ts` — WebSocket utility (real-time channels).
- `src/components/theme-provider.tsx` — light/dark theme (`dark` class).
- `src/components/AppLayout.tsx` — base layout.
- `src/components/PipelineOverview.tsx` — real-time view of the three stages.
- `src/components/PreferencesPanel.tsx` — persisted theme and language.
- `src/components/ui/` — shadcn/ui components.

The stage screens, the campaign selector, and the emulation confirmation are
already implemented. Detailed progress is in `../../docs/project-status.md`.
