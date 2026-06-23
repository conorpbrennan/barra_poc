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

- [ ] **Step 7 · Scheduled CRO report.** [M] Wire `barra_cro_report.py` + the `/analysis` commentary
  into a daily/weekly markdown/PDF on a cron/timer. Touches: api, llm, timer unit, test.

- [ ] **Step 8 · LLM — portfolio-level synthesis.** [S] One CRO-style narrative across several views
  at once, not per-view. Builds on `/analysis`. Touches: api, llm, test.

- [ ] **Step 9 · LLM — "what changed" quarter-over-quarter.** [M] Diff this 13F filing vs the prior
  and narrate the risk delta. Touches: api, llm, test.

- [ ] **Step 10 · LLM — scoped Q&A drill-down.** [M] Give the model exactly one `query_cube` tool
  (the `/pivot` allowlist) via the Anthropic SDK tool runner, so it can pull slices itself — still
  zero filesystem/network, one tool only. Touches: api, llm, test.

## Tier B — buildable but needs pipeline work

- [ ] **Step 11 · Liquidity risk (days-to-liquidate).** [M] Volume is fetched in the builder but is
  NOT in the six frames — add an ADV column to the pipeline, carry it into a frame, then a cube
  measure (position ÷ ADV) and a UI flag. Touches: builder, cube, api, ui, rebuild, test.

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

## Universe diagnostics (from Chris's review — see docs/universe-diagnostics-plan.md)

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
- [ ] **Phase 3 · Span / high-confidence check.** [M] Does each holding sit inside the estimation
  universe's factor space (Chris's VALUE/SIZE picture): 2D factor-pair scatter + numeric in-span flag.
  (Prototyped: % of book by weight inside the PIT-S&P-500 Mahalanobis span across all filings — ~90%
  mean, ~95% pre-2021 falling to ~85% since 2021.)
- [ ] **Phase 4 · Style-drift attribution — intentional vs not** (Chris's follow-up, 2026-06-23). [M]
  The span check surfaced a post-2021 drift of the book out of the S&P 500's factor space (toward
  smaller, higher-vol names). Chris's point: at ~85% overlap it's fine for the estimation universe,
  but the drift itself raises the question of whether the shift was **intentional** (a deliberate
  style tilt / a new PM covering smaller names) or **unintentional** (risk re-pricing making those
  names more attractive) — and the action differs: **update the benchmark** if intentional, **update
  the hedging** if not. Decompose the drift by factor (Size/ResidVol/Liquidity contributions over
  time), tie it to filing-over-filing exposure changes (ties to Step 9 "what changed QoQ"), and
  surface it as a flag the risk read can act on. Awaiting Chris's write-up for framing.

## Done

- [x] **Per-view risk-analyst commentary (`/analysis`).** On-demand button; plain Messages API, no
  tools; grounded on the view's own numbers; streamed; cached per view+slice. See CLAUDE.md.
