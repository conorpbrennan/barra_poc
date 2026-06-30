# flexagg2++ — Vite SPA risk dashboard

A Vite + React + TypeScript single-page app that **replaces** the Streamlit `risk_pivot_app.py`,
served at base path `/flexagg2++/` alongside the existing `flexagg++` Streamlit app. It replicates
the existing functionality against the **same `risk_api.py` endpoints** (one backend addition: the
`/views` CRUD wrapper, `views_api.py`). All layout/charts/tables follow **Tufte & Few** — see
`docs/vite-ui-plan.md` and `../CLAUDE.md` → "New UI (Vite)".

## Quick start

```bash
npm install
npm run dev        # http://localhost:5173/flexagg2++/  (proxies /api -> :8010)
npm run build      # -> dist/   (static; nginx serves it)
npm test           # vitest
```

The local FastAPI cube must be running on `:8010`:
`cd ../python_src && BARRA_CUBE_PORT=9091 ../barra/bin/uvicorn risk_api:app --port 8010`.

## Layout

```
src/
  api/        client.ts (fetch + base url) · hooks.ts (TanStack Query) · stream.ts (LLM streaming)
              views.ts (saved-views CRUD) · types.ts (endpoint response shapes)
  context/    AppContext.tsx — global book / as-of date / scenario (the one IA change, §9)
  components/ svg.tsx (sparkline · bullet graph · label bar · line path) · LineChart.tsx
              Markdown.tsx (reading column) · StreamPanel.tsx (on-demand streamed LLM) · ui.tsx
  shell/      ContextBar.tsx · LeftRail.tsx
  routes/     Overview · Pivot · Trends · Stress · WhatIf · Liquidity · Universe · Drift
              Attribution · Changes · Ask · Checks
  pivot/      usePivot.ts (state + server drill) · FieldList.tsx (dnd-kit) · PivotGrid.tsx (AG Grid)
              ChartMode.tsx (react-vega) · Repository.tsx (/views)
  lib/        format.ts (pct / RAG mapping / tabular numbers)
```

## Design tokens

`src/index.css`: `--bg:#fffff8 --ink:#111 --faint:#6b6b63 --accent:#3b5e8c`, RAG `--ok/--warn/--bad`
for status only, et-book serif for prose. Numbers are tabular-nums; tables are hairline-only (Tufte);
the grid drill shows depth by indentation.

## Invariants

- **The grid is a pure renderer.** Every number comes from a `/pivot` (or other endpoint) call; the
  browser never groups, aggregates, or pivots. The only total is the cube's `grand` corner (VaR is
  non-additive — never summed client-side).
- **Scenario measures need a single `ScenarioSet`.** The field list warns and the API returns a
  `warning` when a `SCEN_DEP` measure is requested without one.
- **Same-origin API** under `/flexagg2++/api` (no CORS reliance); LLM endpoints stream raw markdown
  (consumed via `ReadableStream`), so nginx needs `proxy_buffering off`.

Serving / deploy: `docs/vite-ui-serving.md` (or `./deploy.sh`).

## Parity vs the Streamlit app (§3)

| Streamlit `render_*` | Lens |
|---|---|
| overview scorecard + status line | **Overview** (hero + sparklines + RAG strip + top exposures + QoQ) |
| limits / dq / backtest | **Checks** (+ Overview strip) |
| trends + drawdown | **Trends** (small multiples + equity/underwater) |
| universe / funnel / span | **Universe** (3 tabs + live scatter) |
| drift | **Drift** |
| liquidity | **Liquidity** (participation/horizon sliders) |
| stress + reverse | **Stress** |
| whatif | **What-if** |
| whatchanged + LLM | **Changes** |
| ask | **Ask** |
| pivot grid + chart + analysis + repository | **Pivot** workspace |

PnL attribution (`/pnl_attribution`, Step 15) is stubbed in the Attribution lens until that endpoint
ships; the existing risk-attribution `/attribution` is wired.
