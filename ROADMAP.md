# ROADMAP.md — risk-manager enhancements

Living checklist for the risk-manager feature program. Built one step at a time; each step is
independently shippable, tested, and reversible. Check a box when its step lands.

Touch legend: **cube** = `python_src/barra_factor_risk_cube.py` · **api** = `python_src/risk_api.py`
· **ui** = `python_src/risk_pivot_app.py` · **builder** = `python_src/barra_build_frames.py` ·
**llm** = the `/analysis` Messages-API layer.

Feasibility is grounded in the current data: the six frames carry no Volume and no benchmark;
`factor_returns` spans 2016-02 to 2024-12 only.

Execution order: 1 → 2 → 3 → 4 (daily-driver core), then 5 → 6, 7, 8 → 9 → 10, then 11 → 12,
and 13 → 14 once data sources are chosen.

---

## Tier A — buildable now (no new data)

- [x] **Step 1 · Limits & breach monitoring (RAG).** [S–M] `limits.json` (VaR/ES caps, Risk-HHI
  concentration cap, single-name & sector weight caps); `/limits?date=&set=&book=` compares cube
  numbers to it; red/amber/green status panel atop the UI with a per-limit detail table; breaches
  fed into the `/analysis` payload so commentary leads with them. Tests in `test_limits.py`. Done.

- [x] **Step 2 · Data-quality / trust panel.** [S] `barra_dq_checks.run()` refactored to return
  structured results; `/dq` runs it against the cube's live frames and adds the known stubs
  (Country="US", Unknown sector) + per-frame latest date; UI badge + detail expander beside the
  limits banner. Tests in `test_dq.py`. Done.

- [x] **Step 3 · VaR backtesting (traffic-light / Kupiec).** [M] `/backtest?set=&alpha=&window=`
  runs a rolling-window constant-portfolio backtest of the book's daily factor-P&L (HistFull vector
  + date dual), counts VaR exceptions, runs the Kupiec POF test, and assigns the Basel zone from the
  binomial CDF. UI badge + detail (exception dates). Tests in `test_backtest.py`. Done.

- [x] **Step 4 · Time-series / trend views.** [S–M] `/trends?set=&measures=&by=` returns book
  measures over the whole calendar (computed date-by-date to avoid OOM on the P&L vectors) + a
  `by=Factor` exposure breakdown; UI "📈 Risk trends" panel charts VaR 99 / ES 97.5, Risk HHI, and
  the top style-factor exposures over 2016–2024. Tests in `test_trends.py`. Done.

- [x] **Step 5 · Custom & reverse stress tests.** [M] `POST /stress` (book P&L under user-defined
  per-factor sigma shocks, `dPnL = Σ x_k·σ_k·vol_k` — validated to match the cube's Hypo sets exactly)
  and `GET /reverse_stress?loss=` (per-factor sigma move that breaches a target loss, ranked by
  vulnerability). UI "🧪 Stress test" panel (custom shock editor + reverse). Tests in `test_stress.py`. Done.

- [x] **Step 6 · Pre-trade / what-if.** [M–L] `POST /whatif` recomputes book VaR/ES/Total VaR/Specific
  vol/HHI + gross/net before vs after hypothetical trades (resize/drop held names; add a universe
  name), via the cube's risk math reproduced in numpy — "before" matches the cube exactly. UI
  "🔀 Pre-trade / what-if" panel (holdings weight editor). Tests in `test_whatif.py`. Done.

- [x] **Drawdown lens (`/drawdown`).** [S–M] Constant-portfolio max peak-to-trough over the scenario
  path (equity curve + underwater area), the path lens VaR/ES miss; headline folded into `/analysis`.
  UI `render_drawdown`. Tests in `test_drawdown.py`. Done. (Built from Chris's "drawdown is a missing
  lens" note; on the Soros book HistFull reads ≈ −39%, the 2020 COVID crash.)

- [ ] **Step 7 · Scheduled CRO report.** [M] Wire `barra_cro_report.py` + the `/analysis` commentary
  into a daily/weekly markdown/PDF on a cron/timer. Touches: api, llm, timer unit, test.

- [ ] **Step 8 · LLM — portfolio-level synthesis.** [S] One CRO-style narrative across several views
  at once, not per-view. Builds on `/analysis`. Touches: api, llm, test.

- [x] **Step 9 · LLM — "what changed" quarter-over-quarter.** [M] `GET /whatchanged?date=&prev=&book=`
  diffs this 13F filing vs the prior: positions entered / exited / resized (by weight), the net
  factor-exposure drift attributed with Phase 4's machinery (rotation vs loading drift), and the book
  risk delta (VaR/ES/HHI/specific vol — cube-consistent via the what-if math). `POST
  /whatchanged/analysis` streams a grounded "what changed" read (same plain Messages-API, no-tools
  pattern as `/analysis`, model claude-opus-4-8). UI "📋 What changed (QoQ)" panel + on-demand
  commentary button. Tests in `test_whatchanged.py` (unit + integ; live LLM opt-in via RUN_LLM=1).
  Reuses Phase 4's `decompose`. Done.

- [ ] **Step 10 · LLM — scoped Q&A drill-down.** [M] Give the model exactly one `query_cube` tool
  (the `/pivot` allowlist) via the Anthropic SDK tool runner, so it can pull slices itself — still
  zero filesystem/network, one tool only. Touches: api, llm, test.

## Tier B — buildable but needs pipeline work

- [x] **Step 11 · Liquidity risk (days-to-liquidate).** [M] `GET /liquidity?date=&participation=&horizon=`
  Builder now carries a trailing-63d dollar-ADV column on the `positions` frame; the API computes
  days-to-liquidate = MV / (participation·ADV) per name, the book share liquidatable within a horizon,
  the weighted-average days, the least-liquid names, and any names with no ADV. UI "💧 Liquidity
  (days-to-liquidate)" panel with participation/horizon sliders. Tests in `test_liquidity.py` (unit
  formula always; integ needs :8010 + the ADV frame). On the Soros book at 20% participation: ~87% of
  weight liquidatable within 5 days, wavg ~2.2d, worst ~13.4d (GFL), no-ADV names 0.

- [ ] **Step 12 · Deeper-history events (2008 GFC, Euro crisis).** [M–L] `factor_returns` only goes
  back to 2016. The v1 builder already pulls published Ken French / JKP daily returns (decades deep)
  — splice those for pre-2016 windows and replay against current exposures. Touches: builder, cube,
  rebuild, test.

## Tier C — data/decision-blocked (need input)

- [ ] **Step 13 · Active risk / tracking error.** [L] The model is absolute long-only with no
  benchmark in the repo. DECISION NEEDED: which benchmark, and is there a free source for its
  weights? Blocked until then.

- [ ] **Step 14 · Geographic risk.** [M] `Country` is hardcoded "US". DECISION NEEDED: an acceptable
  country-of-domicile/listing crosswalk source. Then it lights up the already-present `Country`
  dimension.

---

## Chris's review — model improvements & universe diagnostics

22–23 Jun review (see `docs/universe-diagnostics-plan.md`, `docs/estimation-coverage-design.md`). Each
point from Chris's notes is mapped to its status below.

- [x] **Estimation / coverage split — uncapped coverage loadings.** [M] builder. The central
  data-quality point from Chris's notes: *"cap the loadings in the estimation universe, don't cap the
  coverage universe."* `barra_build_frames.py` now flags `is_estimation` (the S&P 500 seed),
  standardizes each descriptor against the estimation cross-section, **winsorizes estimation at ±3**,
  leaves **coverage uncapped** (with a `COVERAGE_CAP = ±10` backstop against corrupt XBRL values), and
  **fits factor returns on estimation names only** while still forming specific risk for every coverage
  name. `UNCAP_COVERAGE` flag (default on; `False` = legacy). Rebuilt: 503 estimation / 693
  coverage-only. **Before → after:** style-loading range −3.1…3.1 → −10.1…10.6; most-negative held
  Size loading −2.97 → **−6.78** (true, was clipped); style-factor VaR 99 ex-Market **0.51% → 1.05%**
  (reveals previously-clipped style risk); market-dominated headline Total VaR ≈ 3.6% unchanged (Market
  is structural); span inside-share ~90% → ~82%. `factor_returns`/`specific_var` counts ≈ unchanged;
  full suite green. See `docs/estimation-coverage-design.md` (IMPLEMENTED). Done.

- [x] **Phase 1 · Bitemporal index membership.** [M] `barra_universe_membership.py` classifies every
  13F holding by index, per filing, bitemporally (S&P 500 membership read as-of the report date from
  the hanshof PIT change log; current S&P 1500 from the Wikipedia 500/400/600 lists, by ticker OR
  company name). Writes `data/universe_membership.parquet`; `GET /universe` serves a weight-by-bucket
  time series + the latest book's "outside S&P 1500" headline + the Outside/Unclassified names; UI
  "🌐 Estimation universe" panel. Russell 3000 not classifiable on free data (iShares HTML, FTSE
  paid). Identity resolution caps ~current-book coverage; Unclassified is shown, not folded into the
  headline. Tests in `test_universe.py`. Done. (Latest filing: ~20% of book weight outside S&P 1500.)
- [x] **Phase 2 · Filtration funnel.** [M] Pre-filter population **LOCKED to PIT S&P 500**
  (`population(t)` = hanshof change-log snapshot as-of t — the only survivorship-free PIT index on free
  data). `barra_universe_funnel.py` runs each member through the filter stack — listing → size →
  history → trading frequency → liquidity/ADV → completeness → stability buffer (ADV-percentile
  hysteresis) — tagging the first stage that drops it. Metrics are PIT from the builder's cached
  prices/fundamentals (mcap, ADV, trading-freq — no Step-11 needed) + exposures completeness. Free
  float & confirmed-M&A removal are disclosed *inert* stages (no free source); unmeasurable names →
  "data unavailable", never a filter drop. Near-flat by design (latest: ~446/~486 survive), as Chris
  predicted. Thresholds in `universe_filters.json`. `GET /funnel` + funnel view in the "🌐 Estimation
  universe" panel. Tests in `test_funnel.py`. Done.
- [x] **Phase 3 · Span / high-confidence check.** [M] Does each holding sit inside the estimation
  universe's factor space (Chris's VALUE/SIZE picture)? `barra_universe_span.py` computes each
  holding's squared Mahalanobis distance from the funnel-survivor cloud; "inside" = within the cloud's
  99th-pct edge, plus the per-factor extremes that push a name out. `GET /span?date=&fx=&fy=` serves the
  weight-inside time series + per-name verdict + a live 2D factor-pair scatter (cloud vs book) built
  from the exposures frame. Panel: inside-% trend, scatter with factor-pair picker, outside-the-span
  drill-down. ~82% of book inside on avg post estimation/coverage split (off-index holdings now show
  their true extreme loadings, so lower/truer than the pre-split ~90%), dipping since 2021 (Phase-4 drift). Tests in
  `test_span.py`. Done.
- [x] **Phase 4 · Style-drift attribution — intentional vs not** (Chris's follow-up, 2026-06-23). [M]
  Makes Chris's question empirical. `barra_universe_drift.py` tracks the book's net factor exposure
  x_k = Σ w·L over time and decomposes each factor's drift Δx_k (pre-split t0 → latest t1) into four
  sources — **entered / exited** (rotation), **reweighted** (resizing), **loading_drift** (held names'
  own loadings moving) — that sum to Δ exactly. The read: rotation-dominated drift leans **intentional**
  (mandate shifted → **update the benchmark**); loading-drift-dominated leans **unintentional**
  (re-pricing → **update the hedge**). `GET /drift?split=` serves the per-factor trend, the ranked
  drift, the attribution, and a per-factor "lean"; "🧭 Style-drift attribution" panel (`render_drift`).
  **Finding:** the post-2021 drift (ResidVol +1.2, NonLinSize/Value/Beta up — book into smaller,
  higher-vol names) is **dominated by `entered`** (new names rotating in), so it leans **intentional →
  benchmark**. The final verdict still needs desk knowledge (Soros's intent / PM changes); this is the
  evidence. Tests in `test_drift.py`. Done. Closes the last item from Chris's 2026-06-23 email.

### Still open from Chris's 22 Jun notes

- [ ] **Linked securities (share classes / dual listings / ADRs).** [M] builder/cube. Chris: linked
  lines break the uncorrelated-idiosyncratic (diagonal-Δ) assumption. **Not handled** — each line is
  treated as an independent name today. Would need a primary-line designation + a shared specific-risk
  block. Flagged in CLAUDE.md as a separate note.
- [ ] **Factor orthogonalization (multicollinearity).** [S] builder. Chris: orthogonalize factors to
  avoid multicollinearity. **Partial** — only NonLinSize is orthogonalized to Size (`build_exposures`);
  a general sequential-orthogonalization pass over the style block is not done.
- **Mapped to existing roadmap items:** *active risk / active return vs a benchmark* → **Step 13**
  (blocked on benchmark data); *intentional vs unwanted loadings & hedging* → **Phase 4** above;
  *drawdown lens* → **done** (`/drawdown`, see Tier A); *point-in-time data* → **done** (PIT
  fundamentals + the bitemporal Phases 1–3).

## Done

- [x] **Per-view risk-analyst commentary (`/analysis`).** On-demand button; plain Messages API, no
  tools; grounded on the view's own numbers; streamed; cached per view+slice. See CLAUDE.md.
