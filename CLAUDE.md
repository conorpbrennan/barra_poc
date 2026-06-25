# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A proof-of-concept Barra-style equity factor-risk model built entirely on free/public data,
demoed against the Soros Fund Management 13F book. It splits cleanly into two halves:

1. **Frame builders** pull raw data and emit six canonical parquet frames.
2. **The Atoti cube** consumes those six frames and exposes exposures + a unified
   scenario/stress engine (historical sim, event replay, hypothetical shocks).

The two halves communicate *only* through the six parquet files written to the repo-local
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
is read **only** from the repo `.env` (`_anthropic_key`) — the rest of `.env` is never sourced
because its `ATOTI_LICENSE` path is broken and would break the cube. Without a key, `/analysis`
returns a clean 502 and nothing else is affected. Model: `claude-opus-4-8`. Tests:
`test_analysis.py` (unit guards always run; integration needs the backend; the one live LLM
call is opt-in via `RUN_LLM=1`).

## What changed quarter-over-quarter (`/whatchanged`)

`risk_api.py` `GET /whatchanged?date=&prev=&book=` is a **deterministic** diff between two 13F
filings: positions **entered / exited / resized** (by 13F weight), the book's net factor-exposure
**drift attributed** with Phase 4's `barra_universe_drift.decompose` (each factor's Δ split into
rotation = entered/exited vs re-pricing = loading_drift, summing to Δ exactly), and the **book risk
delta** (Scenario VaR/ES, Total VaR 99, Risk HHI, specific vol, gross/net) computed at each date with
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
precision. `GET /reverse_stress?loss=` inverts it: for a target loss `L`, the single-factor move
`σ_k = −L/(x_k·vol_k)` per factor, ranked by `|σ|` (smallest = most vulnerable; default `L` = the
Total VaR 99 desk limit). UI "🧪 Stress test" panel (`render_stress`). Tests: `test_stress.py`.

## Pre-trade / what-if (`/whatif`)

`risk_api.py` `POST /whatif {trades:[{position, weight}]}` recomputes book risk under a modified weight
vector — resize/drop held names, or add a universe name (absolute target weight; 0 drops). It
reproduces the cube's risk math in numpy (`_book_inputs` + `_risk_from_weights`): factor P&L vector
`R·(Lᵀw)`, the diagonal specific block `Σ wᵢ²σᵢ²`, and Risk HHI from the marginal-Total-VaR shares —
so **"before" matches the cube's reported figures exactly** and only the BEFORE→AFTER delta is the
new information. No cube rebuild. Empty `trades` returns the current holdings (ticker+weight) so the
UI bootstraps its editor, and `universe` (every tradeable name with loadings that date) so the UI's
"add from coverage universe" control can add a non-held name. Returns before/after/delta for Scenario
VaR 99/97.5, ES 97.5/99, Total VaR 99, Specific vol, Risk HHI, gross, net. UI panel `render_whatif`.
Tests: `test_whatif.py`.

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
so files in `python_src/static/` are served at `<baseUrlPath>/app/static/...` (behind the same gate).
`static/guide.html` is hand-written (the feature guide). `static/barra_model_reference.html` is
**generated** by `barra_cro_report.py`, which now writes to both `tmp/` (the CLI artifact) and
`python_src/static/`; rerun it to refresh. Tests: `test_docs.py`.

## The two model versions (v1 vs v2)

Both produce the **identical six-frame schema** and feed the same unchanged cube. They differ
only in how `exposures`, `factor_returns`, and `specific_var` are computed:

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

## The six frames (contract between builders and cube)

| Frame | Key | Payload | Role |
|---|---|---|---|
| `exposures` | (Date, Position, Factor) | Loading | the granular leaf |
| `positions` | (Date, Book, Position) | Weight, MV | Soros 13F weight overlay, as-of joined |
| `securities` | (Position) | Ticker, CIK, CUSIP, Issuer, Sector, Country | dimension |
| `factor_meta` | (Factor) | FactorGroup | dimension |
| `factor_returns` | (Date, Factor) | Return | the shared scenario cache |
| `specific_var` | (Date, Position) | SpecificVar | diagonal idiosyncratic block |

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
