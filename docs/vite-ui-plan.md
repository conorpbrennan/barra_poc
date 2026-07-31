# Plan — New Vite UI (`flexagg2++`) replacing the Streamlit dashboard

Status: SPEC. Replace the Streamlit app `python_src/risk_pivot_app.py` with a **Vite + React SPA**, served
at base path `flexagg2++`, running **alongside** the current `flexagg++` Streamlit app through the
transition. It **replicates all existing functionality against the same `risk_api.py` endpoints** — with
one unavoidable API addition (the saved-views Repository has no HTTP endpoint today; §6). All layout,
charts and tables follow **Tufte & Few** (the hard requirement in CLAUDE.md → "New UI (Vite)"). **Free
stack only** — every dependency MIT/ISC/BSD; the one licensed component stays the backend Atoti.

## Goals / non-goals

- **Goal:** feature parity with the Streamlit UI (§3 is the checklist), a strict single-screen Overview
  monitor, a left-rail of lenses, and an Excel-style pivot with a server-driven parent→child drill.
- **Goal:** the cube stays the single source of truth — the browser never groups, aggregates or pivots
  (§5). Every number comes from a `/pivot` (or other endpoint) call.
- **Non-goal:** no change to the cube, the six/seven frames, or the *existing* risk endpoints. The only
  backend work is the views CRUD API (§6) and the serving wiring (§8).
- **Non-goal:** not a redesign of the analytics — same measures, same lenses. The IA change (single
  global context bar instead of per-panel selectors, §9) is the only deliberate behaviour change, and
  it's signed off (2026-06-30).

## Design & layout

Settled in discussion (recorded in CLAUDE.md): **strict single-screen Overview**, **persistent left
rail**, **global context bar**, Tufte/Few throughout (data-ink, grey + one accent, RAG for status only,
direct labelling, sparklines, bullet graphs, small multiples, prose in a ~46rem reading column).

```
┌────────────────────────────────────────────────────────────────────────┐
│ Soros factor risk     Book▾  As-of 2024-12-31▾  Scenario HistFull▾   📖 │  context bar
├──────────────┬─────────────────────────────────────────────────────────┤
│ Overview     │   RISK                 LIMITS (bullet)      TRADE/QUALITY │
│ Pivot        │   VaR99 3.6% ▁▂▃▅▆      VaR ▕███▏░ ⚠        Gross 1.00    │
│ Trends       │   ES   4.8% ▁▂▄▅▇      Name ▕██▏░░ ●        Net   1.00    │
│ Stress       │   DD  −39% ▁▁▃▇▂       HHI  ▕██▏░░ ●        Liq 87%≤5d    │
│ What-if      │                                            DQ  ● pass     │
│ Liquidity    │   TOP FACTOR EXPOSURES            WHAT CHANGED (QoQ)      │
│ Universe     │   Market ▏██████ 1.00             +4 entered −3 exited    │
│ Drift        │   ResidVol ▏███ 1.13              ResidVol↑ Value↑        │
│ Attribution  │   …                               → leans intentional     │
│ Ask · Read   │                                                          │
└──────────────┴─────────────────────────────────────────────────────────┘
```

The Overview is Few's "monitor": everything that answers *are we OK now* on one screen, no scroll —
limits as **bullet graphs**, each risk number with an inline **sparkline**, top exposures as
direct-labelled bars. Each group links into its lens. Everything else is a lens route reached from the
rail.

## §3 — Feature-parity checklist (every Streamlit panel → new UI)

This is the contract for "replicate all functionality". Source functions are in `risk_pivot_app.py`.

| Streamlit panel (`render_*`) | Endpoint(s) | New-UI home |
|---|---|---|
| `render_overview` (scorecard + sparklines) | `/trends`, `/drawdown`, `/whatif` (net/gross) | **Overview** monitor (hero) |
| `render_status_line` (3 RAG dots) | `/limits`, `/dq`, `/backtest` | Overview status strip |
| `render_limits_banner` | `/limits` | Overview → bullet graphs; Checks lens (detail) |
| `render_dq_badge` | `/dq` | Overview status; Checks lens |
| `render_backtest_badge` | `/backtest` | Overview status; Checks lens |
| `render_drawdown` (equity + underwater) | `/drawdown` | **Trends/Drawdown** lens |
| `render_trends` (VaR/ES/HHI + factors) | `/trends` (×2) | **Trends** lens (small multiples) |
| `render_universe` (membership/funnel/span) | `/universe`, `/funnel`, `/span` | **Universe** lens |
| `render_drift` | `/drift` | **Drift** lens |
| `render_liquidity` | `/liquidity` (participation/horizon sliders) | **Liquidity** lens |
| `render_stress` (custom + reverse, 2 tabs) | `POST /stress`, `/reverse_stress` | **Stress** lens |
| `render_whatif` (editable holdings) | `POST /whatif` (bootstrap + run) | **What-if** lens |
| `render_whatchanged` (QoQ diff + LLM) | `/whatchanged`, `POST /whatchanged/analysis` ⧉ | **Changes** lens |
| `render_ask` (scoped Q&A) | `POST /ask` ⧉ | **Ask** lens |
| Pivot grid `show_grid` | `GET /pivot` | **Pivot** workspace (§5) |
| Pivot commentary | `POST /analysis` ⧉ | Pivot workspace, on-demand |
| Chart mode `render_spec` / builder | `GET /pivot` (per query) + Vega-Lite specs | Pivot workspace → chart mode (§5) |
| Repository `render_panel` / `render_tree` | **none today** → new views API (§6) | Pivot workspace → Repository |
| `_doc_links`, `_excel_links` | static `app/static/*.html`, `*.xlsx` | Context-bar 📖 menu |

⧉ = streaming (raw `text/markdown`, consume via `ReadableStream`; §7).

Other endpoints the current UI doesn't surface but exist and are worth wiring (`/meta`, `/risk`,
`/scenarios`, `/exposures`, `/attribution` [risk-by-level], `/timeseries`, `/position`, `/validation`,
`/dims`, `/scenario_pnl`): `/dims` + `/meta` bootstrap the context bar and field list; `/position` backs
a name drill-down; the rest are optional extras, not parity requirements.

> **Naming clash — resolved (2026-06-30):** a `GET /attribution` already exists for *risk* attribution by
> level (`risk_api.py:203`); the Step-15 PnL feature is renamed to **`/pnl_attribution`**, leaving the
> existing risk endpoint untouched, so both ship side by side. The new UI's "Attribution" lens surfaces
> both (risk-by-level and PnL).

## §4 — Stack (all free)

- **Vite + React + TypeScript** (MIT) — SPA shell, routing via React Router.
- **TanStack Query** (MIT) — data fetching/caching, mirrors the Streamlit `@st.cache_data(ttl=300)`
  behaviour (cache GETs ~5 min, dedupe, background refetch on context change).
- **react-vega / vega-embed** (BSD) — renders the Overview sparklines and **the saved chart views**,
  which are already Vega-Lite v5 specs in the view JSON. We reuse the specs, not reimplement charts.
- **Grid: AG Grid Community (`ag-grid-react`)** (MIT). *Correction from our earlier exchange:* it's AG
  Grid **Enterprise** that's paid; **Community is free and already used in this repo** (`st_aggrid`) as a
  flat cube-renderer with custom JsCode value-formatters and heatmap cell-styles — those port directly.
  Community covers everything we need (virtualized flat rows, cell renderers, value formatters, pinned
  bottom totals, sort/resize/reorder, CSV export). Grouping/SSRM are Enterprise and **not needed** — the
  cube aggregates and the drill is hand-rolled (§5). *Alternative:* TanStack Table (headless) for tighter
  Tufte control; **decided (2026-06-30): AG Grid Community** — continuity + the existing JsCode formatters/
heatmap port directly.
- **dnd-kit** (MIT) — drag-drop for the Excel field list (§5).
- **Sparklines / bullet graphs** — hand-rolled SVG (a few lines each; no library).
- **Styling** — plain CSS, the static-docs palette (`--bg:#fffff8`, `--ink:#111`, `--faint:#6b6b63`,
  accent `#3b5e8c`, RAG greens/ambers/reds), et-book serif for prose.

No Node tooling exists in the repo today; this adds a `frontend/` (or `web/`) workspace with its own
`package.json`, isolated from the Python venv.

## §5 — The pivot workspace (server-side, Excel-style)

**Invariant: the grid is a pure renderer.** AG Grid holds only the rows on screen; it never groups or
sums. Every reshape and every drill is a `/pivot` call to Atoti behind the `_validate_pivot` allowlist
(`DIM_NAMES` — 10 dims; `MEASURE_NAMES` — 33 measures; `SCEN_DEP` — the 27 scenario-dependent ones).
Totals come from the cube's `per_row` / `per_col` / `grand` margins (VaR is non-additive — never summed
client-side), rendered as pinned bottom rows exactly as today.

**The Excel field list** — a custom panel, dock-right and dismissable (controls on demand; the grid is
full-width when closed):

```
┌─ Fields ──────────┐   zones map straight to the /pivot request:
│ Dimensions        │     ROWS    → rows=…
│  Factor Sector …  │     COLUMNS → cols=…   (transpose of returned cells, layout only)
│ Measures          │     VALUES  → measures=…
│  Net exp VaR99 …  │     FILTERS → filters={dim:[members]}
├───────────────────┤
│ ROWS  Factor ▸ Pos│   the source lists are populated from /dims (allowlist),
│ VALUES Net, VaR99 │   so only valid fields can be dragged; the server still
│ FILTER Sector=…   │   validates (defense in depth, same as today).
└───────────────────┘
```

**The drill (parent→child), hand-rolled because the cube does the work:**

```
expand "Value"  →  GET /pivot rows=[Factor,Position] filters={Factor:["Value"]} …
                →  splice the returned child rows under "Value", indent (level+1)
collapse        →  drop those child rows
sort            →  /pivot sorted (server) or sort the loaded page (presentation only)
```

A cell renderer in the first column draws the expand caret + indentation; we manage the row array and
expanded-key set in React. This is the same flat-render pattern the Streamlit app already proves, plus
expand carets. It's where the Step-15 attribution drill lands (Factor→Name and Name→Factor).

**The `ScenarioSet` constraint** (CLAUDE.md: scenario measures must be sliced to a *single* set or
ragged vectors get compared): enforced by the context-bar scenario selector + the field list refusing to
render a `SCEN_DEP` measure without a set — surfacing the same `warning` the `/pivot` response returns.

**Chart mode** — parity with `render_spec`: a view holds named pivot queries (each its own `/pivot`) and
a Vega-Lite spec whose `source` names a query; react-vega renders it. The **chart builder** reconstructs
the spec from form controls (mark, x/xtype, measure, y-format, height, legend) and a raw-JSON editor,
writing the same view JSON schema (`STATE_FIELDS`). Heavier piece — flagged in effort.

## §6 — The one API addition: views CRUD (`/views`)

The saved-views **Repository** is the only feature with no HTTP endpoint — `risk_pivot_app.py` calls the
`views_repo.py` module in-process (file-backed JSON under `views/`, sections `Public`/`Private`, nested
folders, schema v1, `STATE_FIELDS`). A separate SPA can't reach that, so add a thin REST wrapper over the
existing `views_repo` functions (no new storage, no logic change):

| Method | Path | Maps to `views_repo` |
|---|---|---|
| GET | `/views?section=` | list tree (folders + views) |
| GET | `/views/{section}/{path}` | `load_view` |
| PUT | `/views/{section}/{path}` | `save_view` (body = state JSON) |
| POST | `/views/{section}/{path}/move` | `move_view` |
| DELETE | `/views/{section}/{path}` | `delete_view` |
| POST | `/views/folder` …, DELETE `/views/folder/…` | `make_folder` / `rename_folder` / `delete_folder` |

Same FastAPI app, same wildcard CORS, no auth (gated by nginx basic-auth like everything else). Tests in
`test_views_api.py` (the pure `views_repo` logic is already covered by `test_views_repo.py`).

## §7 — Streaming (LLM panels)

`/analysis`, `/ask`, `/whatchanged/analysis` return **raw `text/markdown`**, not SSE (no `data:` frames).
Consume with `fetch` + `response.body.getReader()`, decode UTF-8 deltas, append to a markdown buffer,
render with a markdown component in the ~46rem reading column. The `/ask` stream interleaves
`> 🔎 query_cube …` progress lines (keep them visible, as today). Client-side cache the finished answer
keyed by view+slice / question (mirrors `pv_analysis_cache` / `pv_ask_cache`), so a re-view is static
until "Regenerate". Respect the shared **20 calls / 60s** rate limit (`429`) with a clear inline message.
nginx must set `proxy_buffering off` on the API route or the stream won't flush (§8).

## §8 — Serving `flexagg2++`

The SPA is static files; unlike Streamlit it has no server process — but it must reach the API directly,
and the API (`:8010`) is currently loopback-only with **no public route**. So two nginx locations, both
under the existing basic-auth gate, added to `/etc/nginx/conf.d/flexagg-funnel.conf` (production config
lives in `/etc`, outside the repo — same as `flexagg++`):

```nginx
# built SPA — static files
location /flexagg2++/ {
    auth_basic "flexagg"; auth_basic_user_file /etc/nginx/.htpasswd_flexagg;
    alias /home/abrennan/dev/barra_poc/frontend/dist/;
    try_files $uri $uri/ /flexagg2++/index.html;     # SPA fallback
}
# API, same-origin under the prefix (no CORS needed; auth inherited)
location /flexagg2++/api/ {
    auth_basic "flexagg"; auth_basic_user_file /etc/nginx/.htpasswd_flexagg;
    proxy_pass http://127.0.0.1:8010/;               # strip the /flexagg2++/api/ prefix
    proxy_buffering off;                             # so LLM token streams flush
    proxy_http_version 1.1; proxy_read_timeout 86400;
}
```

- **Vite `base: '/flexagg2++/'`** so asset URLs resolve under the un-stripped prefix (same reason
  Streamlit needs `--server.baseUrlPath`; nginx does not strip it).
- **API base in the app = `/flexagg2++/api`** (relative, same-origin) → no CORS reliance, gated by the
  same basic-auth, and the Cloudflare→cloudflared→nginx:8090 chain is unchanged.
- The static docs (`app/static/*.html`, the Excel files) keep working — either keep linking the
  `flexagg++` copies or copy them under the SPA and serve from `dist/`.
- **Deploy** = `vite build` → `frontend/dist/`; nginx serves it directly, no systemd unit. A small
  `make web` / deploy script builds and reloads nginx. (Dev: `vite dev` proxying `/api` → `:8010`.)

**`flexagg++` is left exactly as it is** — the existing Python/Streamlit app, unchanged. `flexagg2++` is
a brand-new route serving the Vite SPA. The two run side by side **permanently** (decision: keep both —
no forced cutover); deleting the two `flexagg2++` nginx locations reverts cleanly to Streamlit-only.

## §9 — Global context bar (the one IA change)

Today there is **no** global book/date/scenario control — each panel carries its own scenario selectbox
and the as-of date is read from the latest `/trends` record. The new UI hoists **book / as-of date /
scenario-set** into one context bar shared by every lens (Few: shared context once, not repeated chrome).
Panels inherit it; the few that are intrinsically set-specific (drawdown/backtest default `HistFull`)
take the global set unless overridden. This is the only deliberate behaviour change — **decided
(2026-06-30): global bar, panels inherit.**

## §10 — Migration / rollout

1. Stand up `frontend/`, the shell, context bar, and `/dims`-driven field list.
2. Build the Overview monitor (highest-value, read-only).
3. Port lenses in parity order: Pivot workspace → Trends/Drawdown → Stress/What-if → Liquidity →
   Universe/Drift → Changes/Ask → Checks.
4. Add the views API (§6) + Repository + chart builder (the heaviest).
5. Wire `flexagg2++` serving; run **alongside** `flexagg++`.
6. Parity sign-off against the §3 checklist. **Both UIs stay linked** (decision: keep both) — `flexagg++`
   Streamlit and `flexagg2++` Vite run in parallel indefinitely. Fully reversible — deleting the two
   `flexagg2++` nginx locations reverts to Streamlit-only.

## §11 — Testing

- **Component** (Vitest + Testing Library, free): the field list emits correct `/pivot` requests; the
  drill splices/indents; bullet graph + sparkline render from fixtures; streaming reader appends deltas.
- **Parity checklist** (§3) as a literal test doc — each panel ticked when its lens matches the Streamlit
  output on the same inputs.
- **E2E** (Playwright, free): load Overview, open Pivot, drag a field, expand a row, run an LLM panel
  (mocked stream), save/load a view.
- **Backend:** `test_views_api.py` for §6.

## Decisions (settled 2026-06-30)

- **Keep both UIs indefinitely.** `flexagg++` stays the existing Python/Streamlit app, unchanged;
  `flexagg2++` is a **new route** serving the Vite SPA. They run side by side permanently — no forced
  cutover.
- Vite + React + TS SPA, **in-repo** under `frontend/` (one repo, one deploy); free stack only.
- **Global context bar, panels inherit** — book/date/scenario hoisted to one shared bar; set-specific
  panels default `HistFull` but follow the global set (§9). The only deliberate behaviour change vs today.
- Server-side pivot only — grid is a pure renderer; **AG Grid Community** (free, already proven here),
  custom dnd-kit field list, hand-rolled drill.
- Charts via react-vega (preserve saved Vega-Lite specs); sparklines/bullet graphs hand-rolled SVG.
- API reached **same-origin** under `/flexagg2++/api` via a new nginx proxy route (basic-auth, buffering
  off); no reliance on the wildcard CORS.
- One backend addition only: the `/views` CRUD wrapper over `views_repo`.
- **`/attribution` clash resolved** — the new Step-15 PnL feature is renamed **`/pnl_attribution`**; the
  existing risk-attribution `/attribution` is left as-is.

## Effort / risk

~3–4 weeks for full parity. Rough split: shell + context bar + Overview ~3–4 days; the pivot workspace
(field list + drill + chart builder + Repository + views API) is the bulk, ~2 weeks; the remaining lenses
are mostly endpoint-wiring + a chart each, ~1 week. Main risks: (1) the chart builder + saved-view schema
fidelity (mitigated by reusing the Vega-Lite specs verbatim); (2) the pivot drill UX on large
cross-sections (virtualization + lazy expand handle it); (3) the serving change is an ops edit in `/etc`
(outside the repo) needing a careful nginx reload. No cube/frame/risk-endpoint change, so the analytics
blast radius is zero — the risk is entirely in the new frontend + the thin views API.
