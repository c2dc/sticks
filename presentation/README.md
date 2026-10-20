# STICKS — CE-299 presentation

A [Slidev](https://sli.dev) deck presenting STICKS for **CE-299 — Artificial
Intelligence for Cybersecurity**. Light theme, STICKS brand palette
(blue → purple → red), dense-but-clean slides, with speaker notes on every slide.

## Run

```bash
cd presentation
npm install
npm run dev        # opens the deck at http://localhost:3030
```

- **Presenter mode (slides + notes):** press <kbd>p</kbd> in the browser, or open
  `http://localhost:3030/presenter` — the left pane shows the current/next slide,
  the right pane shows the speaker notes for the live talk.
- **Overview of all slides:** press <kbd>o</kbd>.

## Export

```bash
npm run build         # static site -> dist/
npm run export        # PDF of the slides (needs playwright-chromium)
npm run export-notes  # PDF of the speaker notes
```

> `npm run export` uses Playwright; if prompted, install the browser with
> `npx playwright install chromium`.

## Structure

- `slides.md` — the deck (content + per-slide speaker notes in `<!-- ... -->`).
- `style.css` — the STICKS theme (palette, cards, Input/Processing/Output blocks).
- `public/` — logos (`sticks-logomarca-modo-claro.png`, `sticks-logo.png`) and
  `public/images/` — the custom SVG diagrams (ATT&CK matrix, pipeline, lab
  topology, AI architecture) plus the CC0 photos.

## Outline (4 sections, 22 slides)

1. Cover · Motivation · Agenda
2. **Background & Building Blocks** — ATT&CK matrix · Caldera & the lab · Atomic Red Team · provisioning types
3. **The Six-Stage Pipeline** — overview + one slide per stage (Input · Processing · Output)
4. **The Gap & My Proposal** — the manual gap · the AI curation agent · architecture · components ×2
5. **Evidence & Close** — what's proven · roadmap · Q&A

## Image credits

- “Closeup binary coding screen” and “Computer coding on a screen”, rawpixel —
  **CC0 (public domain)**, via Openverse.
- Logos and all SVG diagrams are original STICKS project assets.
