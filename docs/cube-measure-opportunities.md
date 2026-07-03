# Audit: Python/pandas/numpy math that could move into the Atoti cube

Written 2026-07-03, after the Model-vol family + `/contributions` migration proved the pattern
(cube-served, numpy retained as a live cross-check — `verification` diffs ~1e-17). Governing
principle (CLAUDE.md, Step-15 decision): **measures live in the cube whenever they benefit from
slice / drill / what-if; genuinely non-cube statistics stay in Python.** This audit walks all
41 endpoints and classifies every Python-computed quantity.

The test for "cube-expressible" used here: (a) the inputs are already cube facts (exposures,
weights, shock vectors, specific var), (b) the math is per-cell algebra / array ops / additive
aggregation — no matrix inverses, no rolling windows, no cross-member order statistics, and
(c) a slice, drill, or what-if of the number is actually useful to the desk.

## Tier 1 — clear wins, small and mechanical

**1. `Factor return vol` measure** (replaces `_factor_vols()` in `/stress`, `/reverse_stress`)
`vol_k = std(f_k)` is pandas today (`wide.std()`, full history, ddof=1). In the cube it is
`tt.array.std` of the factor's ShockVec at the Factor member — the exact estimator, HistFull
convention. Payoff beyond deduplication: the grid gets "which factor is wild" directly, and
the naive stress table's `σ_k·vol_k` becomes cube-consistent by construction. Effort: ~5 lines
+ allowlist + a tie-out test.

**2. Hedge table from the cube** (replaces most of `_hedge_table` in `/hedge`)
Two pieces, both polarization-identity algebra we already ship:
- *Vol after neutralizing factor k* = `√(var(book_vec − v_k) + specvar_book)`. NB the current
  `Incremental Model vol` at a FACTOR member subtracts the fanned-out specific (correct for
  names, wrong for factors) — a factor-aware variant (keep the full specific block) IS the
  hedge table's `vol_after`, drillable and what-if-composable.
- *Min-variance hedge ratio* `h*_k = −cov(book_vec, v_k)/var(v_k)` — same `cov` via
  polarization; per-factor as a measure, the D6 hedge for ANY slice (hedge a sector's book,
  not just the whole book). The multi-instrument hedge (matrix inverse) stays in Python.
Effort: small-moderate; tie-outs against `_hedge_table` like the CTR pattern.

## Tier 2 — high value, real architecture (atoti simulation features)

**3. Custom stress as a parameter simulation** (`/stress` naive leg)
The custom shock is `dPnL = Σ x_k·σ_k·vol_k` with user-supplied σ — parameterized, so it can't
be a static measure, but atoti's `create_parameter_simulation` exists for exactly this: a
per-Factor `Shock σ` parameter table → a `Custom stress PnL` measure. What the API version
cannot do and the cube version gets free: **drill the shock P&L to names/sectors** (per-name
contribution `w·L_k·σ_k·vol_k` foots exactly), combine with any filter, and appear in the
pivot/`/ask`. The **conditional** leg (`F⁻¹` solve) stays API-side — matrix inverse.
Effort: moderate; the simulation API is the version-sensitive corner of atoti (CLAUDE.md
warning applies doubly), so it needs verifying against 0.9.15 before committing.

**4. What-if as an atoti scenario branch** (`/whatif`, and `/whatchanged`'s risk deltas)
The strategic one. `_risk_from_weights` exists because trades were simulated in numpy; atoti's
table scenarios (override `positions` rows in a branch) recompute **every** measure under the
trade — the whole Model-vol family, the VaR ladder, attribution, HHI — sliceable and drillable,
not just the fixed summary the endpoint returns. The `/contributions` pattern applies: serve
the scenario branch, keep `_risk_from_weights` as the live cross-check. `/whatchanged`'s
at-each-date risk delta inherits the same machinery. Effort: the largest here (scenario
lifecycle, cleanup, concurrency of parallel what-ifs); do it after 1–3 prove uneventful.

## Tier 3 — stays in Python, with the reason on record

| Endpoint(s) | Python math | Why it stays |
|---|---|---|
| `/backtest` | rolling equal/EWMA/FHS VaR thresholds, Kupiec, Basel | rolling windows + sequential EWMA recursion — not array algebra; the CLAUDE.md exception class verbatim |
| `/drawdown` | cumprod equity curve, running max | scan/prefix operations — no cube primitive |
| `/calibration` | rolling bias stat, `_pred_book_vols` | rolling window + **point-in-time covariance** (F built on history ≤ t; the cube's scenario vector is deliberately the full history — Date slices exposures, not the shock cache) |
| `/pnl_attribution` + `/residual` | Cariño linking, IR, autocorrelation, residual-vs-factor regression, bias stats | time-series reductions and regressions — the designed split; the additive drill already lives in the cube |
| `/pnl_attribution/linkage` | bands `|x_k|·σ_k(≤T)·√h`, `_stressed_cov`, driver reads | same point-in-time-covariance reason (band σ at T ≠ full-history σ), plus the ρ-blend matrix op; the *realized* side already ties to the cube's attribution measures |
| `/stress` conditional | `E[f|s] = F[:,S]F[S,S]⁻¹s` | matrix solve |
| `/factor_portfolio` | `P = (X'W²X)⁻¹X'W²` | matrix inverse |
| `/hedge` multi-instrument | `−Cov(r_H)⁻¹Cov(r_H, r_p)` | matrix inverse (single-instrument h* moves in Tier 1) |
| `/limits` Top-5 risk share | top-k across members | cross-member ORDER statistics — atoti's `n_lowest` ranks within a vector, not across members (Risk HHI worked because it is algebraic; top-5 is not) |
| `/exposure_profile` | histogram, quantiles of the loading cross-section | cross-member order statistics again |
| `/pnl_attribution/names` | winners/losers ranking, sign persistence | ranking + sequential sign runs |
| `/universe`, `/funnel`, `/span`, `/drift` | PIT classification, Mahalanobis, funnel stages | offline precomputes over artifacts by design (network-fetched inputs, not cube facts) |
| `/regression`, `/factor_cov` | WLS t-stats artifact; factor correlation MATRIX | builder-side artifact; pairwise cross-member correlation matrix has no member×member axis in the cube |
| `/liquidity` | days-to-liquidate = MV/(p·ADV), share within horizon | *is* expressible (row algebra + `tt.where`), but the lens is parked post-scope-audit — revisit only if it returns |
| `/whatchanged` | filing set-diffs, drift decompose | frame/set logic between two filings, not a sliceable measure |

## Cross-cutting rules learned from the migrations

- **Always keep the numpy implementation as a live cross-check** (`verification` block), not
  delete it — the `/contributions` pattern. The pure functions are also the unit-test surface.
- **Sample-std conventions matter**: atoti `sample` mode == `np.cov` ddof=1 — the reason the
  tie-outs read 1e-17. Any new measure must state its estimator and match the API twin.
- **The specific-variance fan-out is the recurring trap**: any per-FACTOR measure that touches
  `Specific variance` (marginal, incremental, hedge) must handle it explicitly and document
  the by-name-vs-by-factor semantics.
- **Point-in-time covariance is the recurring blocker**: anything needing F(≤T) (calibration,
  linkage bands) cannot use the cube's full-history shock cache without a per-date truncated
  vector design — deliberately out of scope.

## Recommendation

Do Tier 1 now (a morning, low risk, immediate consistency payoff). Prototype Tier 2 #3 behind
the existing `/stress` response (serve both, compare) before touching the UI. Treat #4 as its
own roadmap item with Chris's input — it changes the what-if architecture, and the payoff
(every measure what-if-able, per-slice) is exactly his "measures live in the cube" principle
taken to its conclusion.
