# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A proof-of-concept Barra-style equity factor-risk model built entirely on free/public data,
demoed against the Soros Fund Management 13F book. It splits cleanly into two halves:

1. **Frame builders** pull raw data and emit seven canonical parquet frames.
2. **The Atoti cube** consumes those frames and exposes exposures + a unified
   scenario/stress engine (historical sim, event replay, hypothetical shocks).

The two halves communicate *only* through the seven parquet files written to the repo-local
`data/` dir (gitignored). Run a builder, then run the cube — they share no in-process state.

## Running

Scripts live in `python_src/`; the Python environment is the `barra/` venv at the repo root
(deps pinned in `requirements.txt`, full freeze in `requirements.lock`). There is no build
system or test suite — scripts are run directly:

```bash
cd python_src
../barra/bin/python barra_build_frames.py        # v2 (default): characteristic z-scores + own regression
../barra/bin/python barra_build_frames_v1.py     # v1 (alternative): returns-based time-series betas
../barra/bin/python barra_factor_risk_cube.py    # builds the cube; expects the six parquets to already exist
```

**Before running a builder:** `SEC_UA` in `python_src/barra_build_frames.py:43` must be a real
`"name email"` string — SEC EDGAR blocks anonymous traffic and the pipeline will fail without it.
`OPENFIGI_KEY` is read from the environment (optional; raises OpenFIGI batch/rate limits).

Both builders write the six frames to `data/` at the repo root (created on demand) and the
cube reads from there (`OUT` / `out` constants, resolved relative to the script file).

## LLM voice — CHRIS_VOICE (mandatory for every LLM feature)

**Every LLM system prompt in `risk_api.py` starts from the shared `CHRIS_VOICE` persona block**
(defined above `ANALYST_SYSTEM`): the voice and doctrine of the desk's senior quantitative risk
manager, modelled on the It's Just Beta primer's editorial discipline (plain compressed
sentences, cite the figure next to every claim, no filler) and the reviewer's documented
corrections (understand ALL risks — unexplained gains investigated like losses; it's-usually-
just-beta; exposure ≠ risk contribution; correlated residuals = missing factor; t = IR·√T
humility; artifacts before alarms). It deliberately never signs a name or claims a specific
identity. **Any new LLM endpoint must be written as `NEW_SYSTEM = CHRIS_VOICE + """..."""`** —
`test_analysis.py::t_all_llm_prompts_carry_chris_voice` fails otherwise. Current prompts:
`ANALYST_SYSTEM`, `OVERVIEW_SYSTEM`, `WHATCHANGED_SYSTEM`, `ASK_SYSTEM`.

## Overview morning summary (`/overview/analysis`)

`POST /overview/analysis {date?, book?, set?, notes?}` streams the whole-book morning read in
the CHRIS_VOICE persona: assembles the monitor's own numbers (limits RAG, `_risk_from_weights`
heroes + Euler variance split/top CTV, the trimmed reconcile verdicts + drivers + breach
co-movement via the linkage route, Kupiec backtest headline, trailing-12m attribution headline,
DQ counts) and narrates them in the daily-loop order, ending with a "Do next". Same plain
Messages-API no-tools pattern and rate limit as `/analysis`. UI: the "Risk-manager summary"
StreamPanel at the bottom of the Vite Overview (on-demand, cached per date/book/set).

## Risk-analyst commentary (`/analysis`)

`risk_api.py` has a `POST /analysis` endpoint that writes a short risk-manager read of one
pivot view. It re-runs the *same* guarded pivot the UI shows (shared `_validate_pivot` /
`_pivot_result` helpers, so it can never query an off-allowlist dim/measure), then sends only
those tidy numbers to the Anthropic Messages API and streams back markdown. It's the **plain
Messages API with no tools** — the model gets the figures as text and nothing else; it has zero
access to the cube, the filesystem, or any tool, and cannot re-query. All domain grounding
(measure meanings, the Market-loading concentration caveat, "cite the numbers, invent nothing")
lives in `ANALYST_SYSTEM`. The Streamlit UI calls it from an on-demand "🔍 Risk-analyst
commentary" button under the grid (no token spend unless clicked; cached per view+slice).

`ANTHROPIC_API_KEY` is required for `/analysis` only. The process env wins; otherwise the key
is read **only** from the repo `.env` (`_anthropic_key`) — the API still does **not** source the
whole `.env` (it extracts just the key) to keep the service env minimal. The Atoti license is
supplied out-of-band by the `flexagg-api.service` systemd unit
(`Environment=ATOTI_LICENSE=…/ActivePivot.lic.43457`), which disables telemetry; the `.env`
`ATOTI_LICENSE` path typo (`barra-_poc` → `barra_poc`) was fixed 2026-06-30, so it now resolves
too. Without a key, `/analysis` returns a clean 502 and nothing else is affected. Model:
`claude-opus-4-8`. Tests:
`test_analysis.py` (unit guards always run; integration needs the backend; the one live LLM
call is opt-in via `RUN_LLM=1`).

## What changed quarter-over-quarter (`/whatchanged`)

`risk_api.py` `GET /whatchanged?date=&prev=&book=` is a **deterministic** diff between two 13F
filings: positions **entered / exited / resized** (by 13F weight), the book's net factor-exposure
**drift attributed** with Phase 4's `barra_universe_drift.decompose` (each factor's Δ split into
rotation = entered/exited vs re-pricing = loading_drift, summing to Δ exactly), and the **book risk
delta** (Scenario VaR/ES, Total VaR 99, Top-5 risk share, specific vol, gross/net) computed at each date with
the what-if math (`_book_inputs` + `_risk_from_weights`, so it's cube-consistent, on the full
factor-return history). `prev` defaults to the previous *distinct* book (`_prior_filing_date` walks
back past the flat monthly as-of months to the prior quarterly filing).

`POST /whatchanged/analysis` streams a grounded "what changed" read of that diff — the **same plain
Messages-API, no-tools pattern as `/analysis`** (model `claude-opus-4-8`, adaptive thinking, cached
`WHATCHANGED_SYSTEM`), leading with the biggest change and flagging factor drift as intentional
(rotation → benchmark) vs not (loading drift → hedge). UI "📋 What changed (QoQ)" panel
(`render_whatchanged`): the diff tables + an on-demand commentary button. Tests: `test_whatchanged.py`
(unit `_prior_filing_date`; integ shape + the four-source reconciliation; live LLM opt-in via
`RUN_LLM=1`). Step 9 of the risk-tooling roadmap.

## Scoped Q&A drill-down (`/ask`)

`risk_api.py` `POST /ask {question, notes?}` is the **one LLM endpoint with a tool**. The model gets
**exactly one tool — `query_cube`** — which is the `/pivot` allowlist behind the *same*
`_validate_pivot` + `_pivot_result` guards the grid and `/analysis` use. So it can pull its own cube
slices to answer a free-text desk question, but it **cannot reach an off-allowlist dim/measure, the
filesystem, the network, or any other tool**. We run a **manual agentic loop** (not the SDK tool
runner) because each call must go through that guard and the cube query is a slow synchronous call:
loop is bounded to `ASK_MAX_ROUNDS` (8) tool round-trips, each result trimmed to `ASK_MAX_RECORDS`
(250) rows (with a `truncated` note, never silent). `_run_query_cube` validates **before** touching
the cube and returns an `{"error": ...}` dict on a bad name (so the model retries, the loop doesn't
die) — that error path is unit-testable with no cube. The tool description enumerates the live
`DIM_NAMES`/`MEASURE_NAMES` so the model picks valid names; `ASK_SYSTEM` carries the same grounding as
`ANALYST_SYSTEM` (Market-loading caveat, scenario-set slicing, cite-the-numbers). Streams markdown
(model `claude-opus-4-8`, adaptive thinking, cached system prompt), echoing each `query_cube` call
inline (`> 🔎 query_cube …`) so the grounding is visible. UI "💬 Ask the risk model" panel
(`render_ask`). Tests: `test_ask.py` (unit tool-schema + allowlist guard always; integ empty-question
400; live full loop opt-in via `RUN_LLM=1`, asserts a `query_cube` marker appears). Step 10 of the
risk-tooling roadmap.

## Desk limits (`/limits`)

`risk_api.py` has a `GET /limits?date=&set=&book=` endpoint that compares the book's numbers to a
desk limit set in repo-root `limits.json` (reloaded each call) and returns a red/amber/green status
per limit + a worst-of overall. Book-level VaR/ES/HHI come from the cube (scenario-dependent, so the
limit reads against one `ScenarioSet` — config default `HistFull`, overridable via `set`);
single-name and sector weight come from the positions overlay as-of the date. All limits are UPPER
bounds (`warn` = amber, `limit` = red). The Streamlit UI renders a RAG banner + detail table atop
the page (`render_limits_banner`), and `/analysis` folds the same status into its payload so the
commentary leads with any breach. Tests: `test_limits.py`.

## Data-quality / trust (`/dq`)

`barra_dq_checks.py` is both a CLI (`run()` prints a PASS/WARN/FAIL report) and a library: `run(frames)`
returns the same checks as structured `{level, name, detail}` dicts. `risk_api.py` `GET /dq` calls it
against the cube's **live in-memory frames** (`S["frames"]`, not a disk re-read) and adds the known
stubs (Country="US" on every name, "Unknown" sectors) and each frame's latest date. The UI renders a
pass/warn/fail badge + detail expander next to the limits banner (`render_dq_badge`). Tests: `test_dq.py`.

## VaR backtest (`/backtest`)

`risk_api.py` `GET /backtest?set=&date=&book=&alpha=&window=` validates the VaR methodology. The 13F
book has no live daily P&L, so this is a **constant-portfolio backtest**: the current book's exposures
applied to the daily factor-return history (the `Scenario PnL vector` + its `Scenario dates` dual),
rolling a window (default 250d) to estimate VaR each day and counting exceptions where the realized
day beat VaR. It runs the **Kupiec POF** test (`_kupiec_lr`, χ²(1)@95% = 3.841) and assigns the
**Basel traffic-light** zone from the binomial CDF (`_basel_zone` — green/amber/red, generalizing the
250/99 zones to any window). Counting-in-range isn't a cube primitive, so the rolling logic lives in
the API (pure stats split out for unit tests). UI badge + detail expander (`render_backtest_badge`).
Tests: `test_backtest.py`. Defaults to `HistFull` (the only set with a long daily history).

`method` (in `_var_thresholds`) selects the VaR estimator: `equal` (plain rolling historical sim),
`ewma` (RiskMetrics parametric-normal, decay `lam`), `fhs` (filtered historical simulation —
EWMA-vol-scaled empirical tail). **Default is `fhs`, `lam=0.94`**, chosen by a sweep: at 99% over the
Soros book, equal-HS under-covers (1.79%, Kupiec-amber) and parametric ewma over-breaches on the fat
tail (~2.1-2.4%, red), while fhs λ=0.94 lands at ~1.0% (Kupiec LR ~0.01, green). FHS keeps the
empirical fat tail but rescales it by reactive EWMA vol, so it gets reactivity without the normality
penalty.

## Drawdown (`/drawdown`)

`risk_api.py` `GET /drawdown?set=&date=&book=` is the path lens VaR/ES miss. It pulls the same book
P&L vector as `/backtest` (the `Scenario PnL vector` + its `Scenario dates` dual), compounds it into a
constant-portfolio equity curve over the set's daily path, and takes peak-to-trough: `max_drawdown`
(negative fraction), peak/trough dates, `recovered` + recovery date, and the longest underwater run.
`_max_drawdown` is pure stats (unit-tested without a cube); cumulate/running-max isn't a cube
primitive so it lives in the API like the backtest. Like the backtest it's a **constant-portfolio
what-if** on the held book over history, not a live track record. Meaningful on HistFull (long path)
and event sets; hypo sets are length-1 → `status: insufficient`. The UI panel `render_drawdown`
charts the equity curve + underwater area; `/analysis` folds the headline in (the model leads with a
deep drawdown the static tail can't show). Tests: `test_drawdown.py`. NB on the Soros book HistFull
reads ≈ −39%, peak 2020-02-19 → trough 2020-03-23 (the COVID crash), recovered mid-2020.

## Risk trends (`/trends`)

`risk_api.py` `GET /trends?set=&measures=&by=` returns a tidy time series of book measures over the
whole calendar for one `ScenarioSet`. The book-level path (no `by`) computes **date-by-date** — asking
the cube for the scenario/HHI measures across all ~108 dates in one plan materialises the full P&L
vector per date and OOMs the Java heap, so it loops one date (one vector) at a time. The `by=Factor`
path is a single query (Net exposure is additive, no vectors). The UI "📈 Risk trends" panel
(`render_trends`) charts Scenario VaR 99 / ES 97.5, Risk HHI, and the top style-factor exposures over
2016–2024. Tests: `test_trends.py`.

## Stress (`/stress`, `/reverse_stress`)

A hypothetical shock's book P&L is linear: `dPnL = Σ_k x_k·(σ_k·vol_k)` — book net factor exposure ×
(sigma shock × factor vol). `risk_api.py` computes both custom and reverse stress in the API from
exposures (cube `Net exposure` by Factor) + factor vols (`_factor_vols`, matching `build_scenarios`'s
`wide.std()`), so neither needs a cube rebuild. `POST /stress {shocks:{Factor:σ}}` returns the book
P&L + per-factor contribution breakdown — verified to match the cube's baked-in Hypo sets to float
precision. **`conditional: true`** adds the correlated read: `E[f|f_S=s] = F[:,S]·F[S,S]⁻¹·s`
(`_conditional_shock`, pure) propagates the shock through the factor covariance so co-moving factors
move too — the naive result holds them still and understates a real event. `GET /reverse_stress?loss=`
inverts it: for a target loss `L`, the single-factor move
`σ_k = −L/(x_k·vol_k)` per factor, ranked by `|σ|` (smallest = most vulnerable; default `L` = the
Total VaR 99 desk limit). UI "🧪 Stress test" panel (`render_stress`); the Vite Stress lens has the
conditional toggle (naive vs conditional side by side + the propagation table). Tests: `test_stress.py`.

## Euler risk contributions (`/contributions`) & model trust (`/calibration`, `/regression`, `/factor_cov`)

The It's Just Beta alignment set (see `itsjustbeta/additional-views-plan.md`), shipped 2026-07-02:

- **`GET /contributions?date=&book=`** — the ch-09 standard reports from `_euler_contributions`
  (pure): per-factor **CTV** `x_k(Fx)_k` (sums to factor VARIANCE, cross-terms 50/50, negative =
  hedge) and per-position **CTR** `w·MCR` (sums EXACTLY to book daily vol). Model vol `σ² = x'Fx +
  w'Δw` on the full factor-return history — the additive decomposition the standalone per-bucket
  VaR view can't give; never compare CTR (vol units) with CTV (variance units). Vite Attribution
  lens "Contributions (Euler)" tab, sums pinned. Tests: `test_contributions.py`.
- **`GET /calibration?window=&book=`** — the ROLLING bias statistic (`_rolling_bias`, pure):
  `b = std(realized/predicted vol)` over a trailing window with the `1 ± √(2/window)` acceptance
  band, book + specific, plus 2σ exceedance counts (expected ≈ 4.6%). NB the route is
  `/calibration` because `/validation` was already the scenario cross-check. Reads the attribution
  artifact + `_pred_book_vols` (cached full-calendar on `S`).
- **`GET /regression`** — the builder's WLS fit health from the **`regression_stats.parquet` side
  artifact** (`barra_build_frames.py` now persists per-day weighted cross-sectional R², per-factor
  t-stats, and N — an eighth parquet, NOT part of the seven-frame cube contract; written inside
  `build_frames`, needs a rebuild to populate, 404s cleanly when absent). Serves the monthly R²
  trend (mean ≈ 0.19 daily — the trend matters, not the monthly 0.2–0.4 rule) and the admission
  table: % of days `|t|>2` per factor (Market 85% … Growth 9%, a drop candidate).
- **`GET /factor_cov?date=`** — the F matrix made visible: correlation matrix + per-factor daily
  vols, full window vs recent-1y side by side (vol-clustering warning where the ratio ≫ 1).

UI: the Vite **Model lens** (`frontend/src/routes/Model.tsx`, rail entry "Model") — rolling-bias
small multiples with the acceptance band, R² trend + admission table, shaded correlation matrix.
Tests: `test_model_trust.py` (integ), `_rolling_bias` unit in `test_attribution.py`, `BiasChart`
render in `Model.test.tsx`.

Second wave (plan items #6–#10, same day):

- **`GET /exposure_profile?factor=&date=`** — one factor's cross-section: histogram + quantiles,
  the ±3 estimation-winsor lines, the uncapped beyond-±3 tail (off-index names' true tilts, e.g.
  a held name at Size −5.5), the held book overlaid (dot size = weight), and the descriptor
  recipe (`FACTOR_RECIPES`) — the "model-conditional: what OUR Value means" view. Model lens.
- **`GET /hedge?date=&book=`** — ch-12/D6: book vol before/after NEUTRALIZING each factor (−x_k
  units of the pure factor-k portfolio), ranked by vol saved, plus the single-instrument
  minimum-variance market hedge `h* = −(Fx)_m/F_mm` (beats full neutralization — it nets the
  correlated style covariance too). Specific vol is the floor no factor hedge touches.
  `_hedge_table` pure. What-if lens panel.
- **`GET /factor_portfolio?factor=&date=`** — ch-07's dual made visible: reconstructs
  `P = (X'W²X)⁻¹X'W²` on the funnel survivors (≈ estimation universe; W = the builder's
  exp(Size/4) sqrt-cap proxy) and serves one factor's pure portfolio: top longs/shorts, gross,
  net, and the PX = I purity check (self-exposure 1, cross ~1e-16). Empirical note: style
  portfolios are dollar-neutral and gross runs ~0.9–1.4× on this broad cross-section (the
  primer's 11.9× is a 10-stock artifact); Market nets to exactly +1. Model lens.
- **`GET /pnl_attribution/names?from=&to=`** — ch-13 scope: specific PnL name by name (winners /
  losers), each with sign persistence (share of consecutive same-sign months; ≈0.5 memoryless,
  ≫0.5 = edge / stale 13F / missing factor) and months-positive. `_name_attr` gained a
  `monthly=True` mode returning the per-month specific panel. Attribution lens, PnL tab.
- **DQ model gates** — `barra_dq_checks.py` §6: estimation-universe style medians ≈ 0
  (standardization gate, on funnel survivors — full-cross-section medians legitimately sit off 0
  because the uncapped coverage tail is smaller/less liquid BY DESIGN), factor covariance PSD,
  and regression_stats sanity (N ≥ 30, R² ∈ [0,1]). Inherited by `/dq` + the Checks lens.

## Pre-trade / what-if (`/whatif`)

`risk_api.py` `POST /whatif {trades:[{position, weight}]}` recomputes book risk under a modified weight
vector — resize/drop held names, or add a universe name (absolute target weight; 0 drops). It
reproduces the cube's risk math in numpy (`_book_inputs` + `_risk_from_weights`): factor P&L vector
`R·(Lᵀw)`, the diagonal specific block `Σ wᵢ²σᵢ²`, and the **Top-5 risk share** from the
marginal-Total-VaR contributions (the ch-09 CTR concentration idiom; replaced Risk HHI in the
what-if/limits/whatchanged payloads 2026-07-02 — the cube's `Risk HHI` measure itself is unchanged) —
so **"before" matches the cube's reported figures exactly** and only the BEFORE→AFTER delta is the
new information. No cube rebuild. Empty `trades` returns the current holdings (ticker+weight) so the
UI bootstraps its editor, and `universe` (every tradeable name with loadings that date) so the UI's
"add from coverage universe" control can add a non-held name. Returns before/after/delta for Scenario
VaR 99/97.5, ES 97.5/99, Total VaR 99, Specific vol, Top-5 risk share, gross, net. UI panel `render_whatif`.
Tests: `test_whatif.py`.

## Liquidity / days-to-liquidate (`/liquidity`)

`risk_api.py` `GET /liquidity?date=&participation=&horizon=` answers "how long to unwind this book."
The builder now carries a **trailing-63d dollar-ADV** column on the `positions` frame (mcap-style:
`close × volume` rolled 63d, as-of the calendar date, from the same cached Stooq/Yahoo Volume the
prices come from — no Step-11 frame work beyond the one column). The API computes per name
**days-to-liquidate = MV / (participation·ADV)** (`_days_to_liquidate`, pure → unit-tested), then the
book **share of weight liquidatable within `horizon`** days, the **weighted-average days**, the
least-liquid names (detail sorted desc), and any names with **no ADV** (delisted/illiquid, reported
separately — never silently counted as instant). `participation` (default 0.20) is the fraction of
each name's ADV you'll trade per day; `horizon` (default 5) is the cutoff. No cube — reads
`S["frames"]` positions + securities. UI "💧 Liquidity (days-to-liquidate)" panel `render_liquidity`
(participation/horizon sliders). Tests: `test_liquidity.py`. NB on the Soros book at 20%
participation: ~87% of weight liquidatable within 5 days, wavg ≈ 2.2d, worst ≈ 13.4d (GFL), no-ADV 0.

## PnL attribution & factor-model validation (`/pnl_attribution`)

Step 15 (spec: `docs/pnl-attribution-plan.md`, roadmap §9 — shipped 2026-07-02). Splits the book's
**realized** PnL into a factor-explained part + a residual, tests the residual, and links both back
to the ex-ante risk decomposition. Three layers:

- **Builder/cube (the drill).** The 7th frame `specific_returns` persists the un-squared WLS
  residual `u` daily. The cube derives three **additive** measures — `Factor contribution`
  (= WLoading × fwd-month factor return, a physical leaf column), `Specific PnL` (as-of weight ×
  fwd-month residual, OriginScope like `Specific variance` — fans out by Factor, read it by
  name/book), `Realized PnL` (their sum, an identity) — all on the **forward-month convention**:
  the value at Date d0 is the PnL over the month after d0 (the month d0's exposures explain).
  They're on the `/pivot` allowlist (pruned at startup on v1 data), so the grid, `/ask` and
  `/analysis` inherit the factor→name drill.
- **Precompute `barra_pnl_attribution.py`** → `data/pnl_attribution.parquet` (tidy daily
  Date/Kind/Source/Value). Reconstructs each name's daily return **from the frames alone**
  (`R_i = L_i·f + ε_i` — exact, ε is the model's own residual, so realized = factor + specific ties
  out at machine precision; that identity is unit-tested), compounds **drifting buy-and-hold
  weights** re-anchored at each 13F filing, and discloses coverage (priced share + unpriced names).
  Pure stats importable for tests: `_carino_link`, `_info_ratio`, `_autocorr`, `_bias_stat`,
  `_concentration_hhi`, `_hit_rate`, `_resid_factor_regression`, `_stressed_cov`. Rerun after a
  rebuild.
- **API + UI.** `GET /pnl_attribution?from=&to=&by=` — Carino-linked period headline (linked
  contributions sum to the geometric return exactly), the cumulative hero series, the by-factor
  table (avg exposure, cum factor return, contribution, t-stat), coverage. `GET
  /pnl_attribution/residual` — the §2 diagnostics with **RAG verdicts** (IR, realized/predicted
  specific vol, lag-1/2 autocorr, residual-vs-factor regression at **daily** resolution, Barra bias
  stats book/specific/per-factor, residual HHI, hit rate; thresholds start loose). `GET
  /pnl_attribution/linkage?T=&horizon=` — the §4 reconcile: per factor + Specific + book total, the
  start-of-period ±2σ **base band** and a **stressed band** (vols ×`vol_mult` 1.25, correlations
  blended toward 1 by `rho` 0.75 via `_stressed_cov`), the realized dot, surprise z, and a
  within / stress-regime / investigate verdict, plus per-position surprises. Rows outside the ±2σ
  base band also carry a **driver read** (`_linkage_driver`, pure): the band freezes `x` at T, so
  each breach is classified `exposure_migration` (x(T) unrepresentative — z rebuilt on the
  in-window avg exposure `exposure_window_avg` sits within band; loading churn / 13F re-anchor,
  drawn as a hollow dot in the Vite UI), `factor_move` (exposure stable, factor moved ≥1.5σ), or
  `mixed`, each with a one-sentence `text`; specific/book breaches point at the bias stats /
  backtest instead (`vol_underforecast`). **Positions get the same treatment** (`_position_driver`,
  pure): each name's row carries its factor/specific PnL split + `weight_window_avg`, and breaches
  classify as `weight_migration` (13F re-anchor/resize made w(T) unrepresentative — band artifact),
  `specific_move` (idiosyncratic event, ≥65% specific — cross-check the residual explorer),
  `factor_move` (loadings carried a factor move, with the largest factor named), or `mixed`; the
  Vite positions table shows the split + a stock-level driver-read block. Two joint reads on top:
  **`breach_comovement`** (`_pairwise_mean_corr`, pure — Chris's missing-factor test, cheap
  version): pairwise daily-residual correlation among the specific/mixed breach names; mean ρ ≥
  0.25 ⇒ `common_thread` (one driver the model has no factor for), else `independent` (separate
  stock events). And **`hidden_beta`** on `factor_move` AND `mixed` breaches (the flag reads on
  the factor component): if the driving factor's own row sits WITHIN its band, the factor moved
  normally — the name's realized comovement exceeded its T loading, so suspect the loading, not
  the factor. NB `mixed` means neither component dominates (specific share 35–65%); it is a
  composition statement, orthogonal to the hidden-beta inference. `/stress` gained the
  same correlation-stress mode (optional `vol_mult`/`rho` → `correlation_stress` block).
  `/analysis` folds in a trailing-12m headline. UI: Streamlit `render_attribution` (Risk tab —
  period presets, vega-lite stacked hero, RAG table, reconcile SVG) and the Vite Attribution lens
  "PnL attribution" tab (same views, hand-rolled SVG). Tests: `test_attribution.py` (+ a panel
  test in `test_pivot_app.py`, React tests in `Attribution.test.tsx`).

## Estimation universe — index membership (`/universe`)

`barra_universe_membership.py` is a **precompute step** (like a builder): for every Soros 13F filing it
classifies each held name by index, **bitemporally** — `report_date` is valid time, `filing_date` is
knowledge time, and S&P 500 membership is read **as-of `report_date`** from the hanshof PIT change log
(survivorship-bias-free). Current S&P 1500 (Wikipedia 500/400/600, parsed with bs4 — no lxml) is a
**current-membership proxy**, matched by ticker OR normalized company name (so foreign-domicile names
like Linde/Accenture resolve without a US ticker). CUSIP→ticker reuses the builder's warm OpenFIGI
crosswalk (call with the full held list so batches hit cache, not 429) + a SEC `company_tickers`
name map. Names with neither ticker nor name match are **"Unclassified"**, kept OUT of the headline.
Mutually-exclusive, weight-aggregated buckets: `S&P 500` (PIT) / `S&P 400/600` (current) / `Outside
S&P 1500` / `Unclassified`. Russell 3000 is **not** classifiable on free data (iShares serves HTML,
FTSE is paid). Writes `data/universe_membership.parquet` (gitignored, like the six frames). Run it
from `python_src/` after a build.

`risk_api.py` `GET /universe?date=` reads **only** that parquet (no cube, no network at request time):
a weight-by-bucket time series, the latest (or `date`) filing's split + the "outside S&P 1500"
headline, and the Outside/Unclassified names. UI "🌐 Estimation universe" panel (`render_universe`).
Coverage is strong on recent filings and thins on older ones (accumulated delisted names lose their US
ticker) — the latest filing's headline is the reliable read. Tests: `test_universe.py` (pure
classification logic always runs; integ needs the backend + the built artifact). Phase 1 of the
universe diagnostics — see `docs/universe-diagnostics-plan.md`.

## Estimation universe — filtration funnel (`/funnel`)

`barra_universe_funnel.py` is Phase 2 (another **precompute step**). The pre-filter population is the
**point-in-time S&P 500** (the hanshof change-log snapshot as-of each month-end — the only
survivorship-free PIT index on free data; this is the *locked* decision, endorsed by the desk). Each
member is run through a fixed DQ filter stack — **listing → size → history → trading frequency →
liquidity/ADV → completeness → stability buffer** — and tagged with the FIRST stage that drops it.
Metrics are point-in-time from the builder's cached prices/fundamentals (mcap = `close × shares`
as-of `filed`; ADV/trading-frequency from cached Volume — **no Step-11 frame work needed**) plus
descriptor completeness from the `exposures` frame. **Free float** and **confirmed-M&A removal** have
no free source and appear as inert, *disclosed* stages. A name we can't measure (delisted PIT member
not in the built universe, or missing a share count) is tagged **"data unavailable"** — shown, never
counted as a filter drop. The funnel is **near-flat by design**: the S&P 500 is committee-curated, so
the filters confirm a clean input (latest month: ~446 of ~486 evaluable names survive); the visible
population↔survivor gap in early years is data-availability (survivorship), not filtering. Stability
buffers use ADV-percentile hysteresis (enter/exit bands) across months to stop churn. Thresholds live
in repo-root `universe_filters.json` (documented + tunable). Writes `data/universe_funnel.parquet`
(gitignored).

`risk_api.py` `GET /funnel?date=` reads **only** that parquet: a per-month population→survivors
waterfall with the drop count per stage, the selected month's drop list (name + the stage that
dropped it + its metrics), and the thresholds. The "🌐 Estimation universe" panel (`render_universe`)
renders it under the membership view (population/survivor/data-unavailable trend + per-stage drops +
drop-list + thresholds). Tests: `test_funnel.py` (pure filter logic always runs; integ needs the
backend + artifact). Phase 2 of the universe diagnostics — see `docs/universe-diagnostics-plan.md`.

## Estimation universe — span / high-confidence (`/span`)

`barra_universe_span.py` is Phase 3 (precompute): Chris's VALUE/SIZE picture, generalized. For each
month it takes the **estimation cloud** = the funnel survivors (Phase 2; falls back to the PIT S&P 500
∩ exposures if the funnel artifact is absent), computes each holding's squared **Mahalanobis distance**
D² from the cloud's centre in the cloud's own covariance, and flags **"inside"** = D² within the
cloud's 99th-percentile edge (the region the estimation universe populated, where exposures are
well-supported; beyond it the model extrapolates). It also records which descriptors push a name out
(loading beyond the cloud's 1–99th box). Aggregated **by 13F weight**: ~90% of the book sits inside on
average, drifting from ~95% pre-2021 to ~85% since (the book moving into smaller/higher-vol names — the
Phase-4 question). Pure geometry is unit-tested. Writes `data/universe_span.parquet` (gitignored).

`risk_api.py` `GET /span?date=&fx=&fy=` reads the artifact for the per-month inside-share series + the
selected month's per-name verdict (D²/inside/extreme factors), and builds a **live 2D `fx`×`fy`
scatter** (estimation cloud vs the book, coloured inside/outside) from the in-memory `exposures` frame
— so any factor pair can be picked without rebuilding (default Size×ResidVol). The "🌐 Estimation
universe" panel (`render_universe`) renders the inside-% trend, the scatter with a factor-pair
selector, and the outside-the-span drill-down. Loadings are z-scored (estimation winsorized, coverage
uncapped), so the "space" is in standardized-exposure terms. With the estimation/coverage split on,
~82% of the book sits inside the estimation cloud on average (off-index holdings now show their true
extreme loadings rather than being clipped to ±3, so this reads lower and truer than the old ~90%),
dipping since 2021. Tests: `test_span.py`. Phase 3 of the universe diagnostics — see
`docs/universe-diagnostics-plan.md`.

## Estimation universe — style-drift attribution (`/drift`)

`barra_universe_drift.py` is Phase 4 (precompute): it makes Chris's intentional-vs-not question
empirical. It tracks the book's **net factor exposure** `x_k = Σ w·L` over time (writes
`data/universe_drift.parquet`, the per-month series) and decomposes each factor's drift `Δx_k`
(pre-`split` book `t0` → latest `t1`) into four sources that **sum to Δ exactly**: **entered / exited**
(rotation in/out), **reweighted** (held-name resizing), **loading_drift** (held names' own loadings
moving). The read: rotation-dominated drift leans **intentional** (mandate shifted → update the
**benchmark**); loading-drift-dominated leans **unintentional** (re-pricing → update the **hedge**).
The final verdict needs desk knowledge — this is the evidence, not the call.

`risk_api.py` `GET /drift?split=` reads the series artifact for the per-factor trend and computes the
attribution **live** from the in-memory `exposures`/`positions` frames (so `t0` follows the `split`
date); it returns the ranked drift, the four-source split per factor, and a per-factor "lean". The
"🧭 Style-drift attribution" panel (`render_drift`) charts the net-exposure trend + the attribution
bars + a ranked table. **Finding:** the post-2021 drift (ResidVol/NonLinSize/Value/Beta up — book into
smaller, higher-vol names) is dominated by `entered`, so it leans **intentional → benchmark**. The
uncapped-coverage split matters: the book's true off-index tilts now show. Tests: `test_drift.py`.
Phase 4 of the universe diagnostics — see `docs/universe-diagnostics-plan.md`.

## Estimation vs coverage universe — the loading-cap split

`barra_build_frames.py` splits its single universe into **estimation** (the clean S&P 500 seed,
flagged `is_estimation` on `sec`) and **coverage** (estimation ∪ every held name). Controlled by
`UNCAP_COVERAGE` (default on; `False` = legacy single-universe / cap-everything). Three coupled effects:

- **Standardization (`_split_z`)** — each descriptor is centred/scaled by the **estimation**
  cross-section's median/MAD. **Estimation loadings are winsorized at ±3**; **coverage loadings are
  left uncapped**, with only a loose `COVERAGE_CAP = ±10` backstop. So a genuinely tiny held name reads
  its true large-negative Size loading (≈ −6, Chris's point) instead of being clipped to −3 — but a
  corrupt XBRL value (negative-equity Leverage → `inf`) is still clamped, not allowed to blow up.
- **Factor returns** are regressed on the **estimation universe only**, so held-but-not-S&P-500 names
  never pull the factor-return estimates.
- **Specific risk** is still formed for **every** coverage name (residual of its own daily return
  against the estimation-fitted factor returns), so the cube can price the whole book.

The six-frame schema and the cube are unchanged — `is_estimation` lives only inside the builder.
Rebuild to take effect (it changes factor returns, specific risk, and every downstream number; book
Total VaR ≈ 3.6%, unchanged headline). See `docs/estimation-coverage-design.md`.

## In-UI docs (static serving)

Two HTML docs are linked from the top of the dashboard (📖 Dashboard guide, 📐 Model & data
reference). Streamlit static serving is enabled (`.streamlit/config.toml` → `enableStaticServing`),
served at `<baseUrlPath>/app/static/...` (behind the same gate). **The files live in repo-root
`docs/static/`; `python_src/static` is a symlink to `../docs/static`** — Streamlit requires the served
folder be named `static` beside the entrypoint, so the symlink keeps serving working while the docs sit
with the rest of `docs/`. So a filesystem path like `python_src/static/<f>` still resolves (through the
symlink) and the served URL `app/static/<f>` is unchanged. `guide.html` is hand-written (the feature
guide). `barra_model_reference.html` is **generated** by `barra_cro_report.py`, which writes to both
`tmp/` (the CLI artifact) and `python_src/static/` (→ `docs/static/`); rerun it to refresh. The
client-facing review docs also live here: `factor-model-roadmap.html` + `factor-model-roadmap-summary.html`
and their PDFs (`Factor-Model-Roadmap.pdf`, `Factor-Model-Summary.pdf`, rendered with weasyprint).
Tests: `test_docs.py`.

## New UI (Vite) — layout & design (Tufte & Few)

A Vite SPA is being built to **replace** the Streamlit `risk_pivot_app.py`, served at base path
`flexagg2++` (alongside the current `flexagg++` Streamlit app during the transition). It **replicates
all existing functionality against the same `risk_api.py` endpoints** — no API changes. **All layout,
charts and tables follow Edward Tufte and Stephen Few — a hard requirement, not a preference:**

- **Overview-first monitor screen.** A single at-a-glance Overview (Few's dashboard sense): hero numbers
  (Total VaR 99, ES, factor/specific variance split, Top-5 risk share, gross/net) + the
  limits/DQ/backtest/**reconcile** RAG strip + **top risk contributions (CTV)** with net exposures
  as the secondary read, no scrolling for the summary. The reconcile line counts only GENUINE
  linkage breaches (exposure_migration drivers are band artifacts, excluded). Everything else is
  a lens reached on demand.
- **Details on demand.** One route per lens (Pivot, Trends, Stress, What-if, Universe, Drift,
  Attribution, Model, the LLM panels). Drill-downs stay hidden until asked for.
- **Data-ink.** Grey + one accent; colour encodes status (RAG) or data only, never decoration; no
  cards/shadows/gratuitous borders — whitespace and hairline rules separate. Numbers are the hero
  (large, tabular-nums). Direct labelling on charts, not legends.
- **Few idioms:** bullet graphs for limits (actual vs warn/limit bands, not gauges); sparklines inline
  with each scorecard number; small-multiples for the trends grid (shared axes).
- **Tufte table for the pivot:** horizontal hairlines only, right-aligned tabular numerals, no zebra,
  drill shown by indentation.
- **Prose (LLM commentary) in a ~46rem reading column**, matching the static docs' et-book serif;
  on-demand/streamed, no token spend until requested.

Palette/typography match the static docs (`python_src/static/*.html`): `--bg:#fffff8`, `--ink:#111`,
grey `--faint:#6b6b63`, one accent `#3b5e8c`; RAG green/amber/red for status only. Full plan:
`docs/vite-ui-plan.md`.

**Implemented** under `frontend/` (Vite + React + TS; `npm run dev|build|test`). It is a pure client
of the existing endpoints — the **only** backend change is the saved-views CRUD wrapper `views_api.py`
(a FastAPI `APIRouter` over `views_repo`, mounted in `risk_api.py`; no cube dependency, tested by
`test_views_api.py`). Lenses: Overview (hero + sparklines + limits bullet graphs + RAG strip + top
exposures + QoQ), Pivot (dnd-kit field list → server-side `/pivot` drill in AG Grid as a pure
renderer, grand-total only since VaR is non-additive; react-vega chart mode; `/views` Repository;
on-demand `/analysis`), Trends, Stress, What-if (+ hedge panel), Universe (membership/funnel/span
+ live scatter), Drift, Attribution (Euler + PnL tabs), Changes, Model, Ask, Checks. Global context
bar (book/date/scenario, §9). **Scope cuts 2026-07-02** (itsjustbeta audit, `itsjustbeta/
risk-manager-read.md` Part 2, steps 1–2 applied): the Attribution "Risk by level" tab, the Drawdown
panel, the Liquidity rail entry, and the Basel zone display were removed from the Vite UI (the
`/attribution`, `/drawdown`, `/liquidity` endpoints and `_basel_zone` are parked, still served and
tested; the Streamlit app still shows them); the backtest badge reads Kupiec pass/reject; **Risk
HHI was replaced by Top-5 risk share** in `_risk_from_weights` (`top5_ctr_share`), `/limits`
(computed set-independently from the what-if math, not the cube), `limits.json`
(warn 0.40 / limit 0.50), the what-if/whatchanged payloads and both UIs — the cube's `Risk HHI`
measure remains on the pivot allowlist. Streamed LLM panels consume raw `text/markdown` via `ReadableStream`. Served at `/flexagg2++/`
same-origin under `/flexagg2++/api` — **alongside** the unchanged `flexagg++` Streamlit app; ops in
`docs/vite-ui-serving.md` (build → nginx alias + proxy with `proxy_buffering off`; restart the cube
once for `/views`). The Attribution lens's "PnL attribution" tab is live against the Step-15
`/pnl_attribution*` endpoints (stacked hero, by-factor table, RAG residual diagnostics, reconcile
band chart).

## Reading email (Gmail over IMAP — NOT the MCP connector)

To read the project email threads (Chris / Soros), connect to Gmail **over IMAP with the app password
in the repo `.env`**. Do **not** reach for the claude.ai Gmail MCP connector — it needs an interactive
`/mcp` browser OAuth that isn't available in a headless/CLI session, so it dead-ends.

- Account `conorpbrennan@gmail.com`; app password is `GMAIL_PWD` in `.env` (read it from the file,
  never print it; 2FA + app password are already set up).
- `imaplib.IMAP4_SSL("imap.gmail.com")` → `login(user, GMAIL_PWD)` → `select("INBOX", readonly=True)`
  → `search(None, "FROM", "chris")`. Reliable mailbox is `INBOX` (`"[Gmail]/All Mail"` select can fail
  on quoting). The latest message in a thread quotes the whole chain, so fetching it (`RFC822`, take the
  `text/plain` part) gives the conversation in one go.
- The main correspondent is **Chris Haltiner `<Chris.Haltiner@soros.com>`** (Soros Fund Management) —
  the "Chris" whose model feedback the docs cite.

## The two model versions (v1 vs v2)

Both produce the **same core six-frame schema** and feed the same unchanged cube (v2 also emits
the optional 7th `specific_returns` frame — v1 doesn't, so v1 data has no PnL attribution). They
differ only in how `exposures`, `factor_returns`, and `specific_var` are computed:

- **v2 — `barra_build_frames.py`** (the primary/canonical builder). Exposures are
  cross-sectional characteristic **z-scores** (Size, Value, Momentum, etc.). Factor returns
  and specific risk are **derived** from a monthly cross-sectional WLS regression of forward
  returns on lagged exposures — so exposures, factor returns, and specific risk are duals of
  one regression. Monthly calendar, sample 2016–2024.

- **v1 — `barra_build_frames_v1.py`** (alternative). Exposures are per-name **time-series
  betas** regressed against *published* daily factor returns (JKP clusters, else Ken French
  FF5+Mom). Factor returns are the downloaded series used **verbatim** (never regressed).
  Specific var is EWMA of regression residuals. Sample 2022–2024.

v1 **imports plumbing from v2** (`positions_from_13f`, `crosswalk_cusips`, `ticker_to_cik`,
`stooq_daily`, `_get`, and config constants). So `barra_build_frames.py` is the shared library;
editing its data-fetch functions or constants affects both builders.

## Data flow and external sources

All sources are free/public, fetched over HTTP with a polite disk cache:

- **positions** → SEC EDGAR 13F (Soros, CIK 0001029160) — the book held as a *weight overlay*.
- **crosswalk** → OpenFIGI v3 (CUSIP→FIGI/ticker) + SEC `company_tickers.json` (ticker→CIK).
- **fundamentals** → SEC EDGAR XBRL company-facts API (point-in-time, CIK-keyed).
- **prices/returns** → Stooq per-symbol daily CSV (ticker-keyed), with a **Yahoo chart-API
  fallback** (`_yahoo_daily`, same Date/Close/Volume contract) when Stooq serves its JS
  anti-bot challenge page instead of CSV — which it does for all traffic from some hosts.
- **factor returns (v1 only)** → JKP daily clusters or Ken French daily files.

`_get()` (in `barra_build_frames.py`) disk-caches every HTTP response under the repo-local
`tmp/` dir (gitignored), keyed by URL md5; `_post_json()` does the same for POSTs (OpenFIGI),
keyed on url + body. Reruns
are cheap and don't re-hit rate-limited APIs; a warm-cache full rebuild takes ~1–2 min. Delete
that cache dir to force a fresh pull.

### The canonical identity key

**`Position` == `SecId` == FIGI**, resolved up front. Every frame keys positions on FIGI so the
cube only ever joins on `Position`. CUSIP/ticker/CIK exist only inside the builders to bridge the
different source APIs; they are resolved to a single FIGI before frames are emitted.

## The seven frames (contract between builders and cube)

| Frame | Key | Payload | Role |
|---|---|---|---|
| `exposures` | (Date, Position, Factor) | Loading | the granular leaf |
| `positions` | (Date, Book, Position) | Weight, MV, ADV | Soros 13F weight overlay, as-of joined (ADV = trailing-63d $ vol, for `/liquidity`) |
| `securities` | (Position) | Ticker, CIK, CUSIP, Issuer, Sector, Country | dimension |
| `factor_meta` | (Factor) | FactorGroup | dimension |
| `factor_returns` | (Date, Factor) | Return | the shared scenario cache |
| `specific_var` | (Date, Position) | SpecificVar | diagonal idiosyncratic block |
| `specific_returns` | (Date, Position) | SpecificReturn | daily WLS residual `u` (PnL attribution); **v2-only, optional** — v1 doesn't emit it and the cube/API degrade gracefully (attribution measures/endpoints absent) |

Two risk blocks only: a linear **factor P&L** block (driven by `factor_returns`) and a
**diagonal specific-risk** block (`specific_var`). No full specific covariance matrix.

The 13F book is a quarterly weight overlay **as-of joined** onto the monthly/COB calendar
(lagged by filing date via `pd.merge_asof(..., direction="backward")`). The as-of join selects
the latest *filing* per calendar date and takes only the names in that filing — exited positions
expire on the next filing, so weights sum to 1.0 on every date.

## The cube's central design: scenarios as one operation

`barra_factor_risk_cube.py` is built around a single idea — **all three scenario modes are the
same operation** `dPnL = Σ_k x_k · df_k`, differing only in the *source* of the shock vector:

- `HistFull` → full factor-return history (historical-simulation VaR/ES)
- `Evt:*` → factor returns over a past window (event replay; see `EVENT_WINDOWS`)
- `Hypo:*` → hand-set sigma-shocks, length-1 vectors (see `HYPO_SHOCKS`)

The mechanism is a **partial Atoti join**: `t_exp.join(t_scn, on Factor only)` makes
`ScenarioSet` a *hierarchy* rather than a join key. Slicing that hierarchy selects which
`ShockVec` flows through the same `Scenario PnL vector` measure — that slice *is* "the switch."
`OriginScope({l["Factor"]})` pins shock-vector scaling to one vector per factor.

**Critical constraint:** vector lengths differ across scenario sets (full history vs a 3-month
window vs length-1). Always query the scenario risk measures (`Scenario VaR 99`,
`Scenario worst loss`, etc.) **sliced to a single `ScenarioSet`** — mixing sets in one cell
compares ragged vectors. The `Market` factor **is included** in scenarios: it carries a leaf
loading of 1.0 per name (the v2 cross-sectional regression intercept), so a fully-invested book
has unit market exposure (`x_Market = Σ weights`) and the directional market return flows through
`dPnL`. This is what makes `Scenario VaR 99` / `Total VaR 99` read as real long-equity book risk
(~3.5% daily 99% VaR) rather than style-tilt-only (~1.5%). Market loadings are added in
`build_frames` *after* `regress_factors` so the style factor returns are unaffected.

## Things that bite

- **`atoti` array helper names** (`tt.array.mean/quantile/min`, `tt.agg.sum_product` with
  `scope=`) may vary by SDK version — the source flags this. Verify against the installed
  `atoti` before assuming a call signature.
- **Universe is capped** at `UNIVERSE_CAP = 250` so the demo actually runs; factor-return
  quality scales with cross-section breadth, so widen `UNIVERSE_EXTRA` / the cap for anything
  real. v2's regression skips dates with `< 30` valid names.
- **Sector** is populated from a free SEC SIC → GICS-11 crosswalk, CIK-keyed
  (`sic_to_gics` / `sectors_for_ciks` in `barra_build_frames.py`; SIC comes from the SEC
  submissions JSON). ~5 names (BDCs/closed-end funds) carry a blank SEC SIC and fall back to
  `"Unknown"`. **Country is still stubbed** (`"US"`) in `securities`.
- Rate limits are real: SEC ≤10 req/s (handled via `sleep` in `_get`), OpenFIGI 25/min
  unauthenticated (export `OPENFIGI_KEY` to raise it; POSTs are disk-cached so the
  crosswalk is only slow on a cold cache).
- See `PROJECT_HISTORY.md` for the session log of how the pipeline was brought up
  (Stooq block, XBRL tag gaps, positions-overlay fix).
