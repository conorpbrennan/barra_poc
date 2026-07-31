# Chapter 10 (Performance Attribution) vs our Step-15 implementation

Compares https://www.itsjustbeta.com/chapters/10-performance-attribution/ against the shipped
`/pnl_attribution` stack (spec `docs/pnl-attribution-plan.md`, precompute
`python_src/barra_pnl_attribution.py`, cube measures, API, UI). Written 2026-07-02.

Context worth knowing: the site's author is "Chris" (ex-DB, BlueMountain, Citadel), contact
chris@itsjustbeta.com — almost certainly the same Chris whose feedback shaped Step 15. Reading
the chapter as "what the reviewer expects" makes the alignment below unsurprising.

## Where we match the chapter exactly

| Chapter 10 concept | Our implementation |
|---|---|
| Exact single-period identity `r = x⊤f + w⊤ε`, no cross-terms | Same identity: `R_p(t) = Σ_k x_k·f_k + u_p(t)`, with `ε` the WLS fit's own residual (7th frame `specific_returns`) — tie-out at machine precision is a unit test. |
| Contribution = exposure × factor return per factor, plus a specific line | `Factor contribution = WLoading × f_k` (cube leaf), `Specific PnL = w × SpecificReturn`, `Realized PnL` = their sum (an identity measure). |
| Multi-period linking so contributions sum to the geometric return (Cariño κ_t = ln-ratio rescale; Menchero, Frongello also acceptable — "consistency matters more than which one you pick") | Cariño implemented (`_carino_link`, pure, unit-tested); linked contributions sum to the compounded period return exactly, no plug. We picked one and use it consistently. |
| Skill vs tilt: factor contributions from stable exposures = tilt, specific = selection; significance via `t = IR√T` (IR 0.5 ≈ 16y to 2σ) | `_info_ratio` (annualized monthly IR on `u_p`) + per-factor t-stats in the by-factor table + RAG verdicts. The chapter's "a single +24bp quarter is noise" caveat is exactly why our thresholds start loose. |
| Pitfall: "specific means what the model's factors don't span"; correlated specific across time/factors/names signals a missing factor, not skill | This is our whole §2 residual-diagnostics endpoint: lag-1/2 autocorrelation, residual-vs-factor regression (daily), cross-sectional read, bias stats (book / per-factor / specific), residual HHI, hit rate — i.e. we *operationalize* the chapter's alarm sign. |
| Pitfall: horizon/model mismatch | Matched by construction — monthly model, monthly as-of exposures, residuals reconstructed from the model's own frames (`R_i = L_i·f + ε_i` exact). |
| Pitfall: model-conditional results — "always quote the model" | Attribution measures exist only on v2 data (allowlist pruned on v1); the measure definitions and forward-month convention are documented with the numbers. |
| Production practice: recompute frequently and link, rather than assume constant exposures | Daily reconstruction with drifting buy-and-hold weights re-anchored at each 13F filing, then Cariño-linked — closer to the chapter's production ideal than its own constant-exposure worked example. |

## Where we differ — and why

1. **Absolute, not active.** The chapter attributes *active* return (`w_a = w_p − w_b`,
   benchmark-relative). We have no benchmark in the repo (roadmap Step 13, blocked), so ours is
   absolute attribution: the Market factor stands in for beta and specific return is the
   closest isolatable stock-picking read. Known, documented caveat — the decomposition math is
   identical, only the weight vector differs. When Step 13 lands, active attribution is the
   same code on active weights.

2. **Trading residual is not a separate line.** The chapter says intra-period trading +
   exposure timing should be broken out as its own line, never lumped into specific. Our data
   is quarterly 13F — intra-quarter trades are invisible, so timing error necessarily folds
   into the residual (disclosed in the caveats, bounded by turnover). This is the one chapter
   recommendation we *cannot* meet on free data; drifting weights are the honest best
   available. If richer position data ever exists, a trading-residual line is the upgrade.

3. **No Brinson companion view.** The chapter recommends running factor-based *and* Brinson
   allocation/selection side by side and explaining the differences. We only do factor-based.
   Brinson needs sector benchmark weights/returns — same Step-13 blocker. The pivot grid's
   re-pivot by sector is a partial substitute (sector grouping, not allocation/selection
   split). Worth adding once a benchmark exists; low value before then.

## Where we go beyond the chapter

- **Risk↔PnL linkage (`/pnl_attribution/linkage`).** The chapter stops at "where did returns
  come from." Our §4 reconciles each factor's realized contribution against its
  start-of-period risk-implied ±2σ band (base + correlation-stressed via `_stressed_cov`),
  with surprise z-scores and a within / stress-regime / investigate verdict. That was Chris's
  explicit addition ("the connection between the PnL attribution and the risk decomposition at
  the start of the period is what tells you how good the risk management is") — it's the part
  of his own framework the published chapter doesn't (yet) cover.
- **Bias statistics ex-post.** The site puts the bias stat in chapter 08 as model-assembly
  validation; we run it on the live book (book / specific / per-factor) as part of the
  residual diagnostics, with RAG verdicts.
- **Cube-native drill.** The chapter presents static tables; our contributions are additive
  cube measures, so factor→name and name→factor drills, re-pivots, and `/ask`//`/analysis`
  grounding come for free.
- **Coverage disclosure.** Priced share of book weight + named unpriced (delisted/foreign)
  names — the chapter doesn't address survivorship in the realized leg.

## Vite UI alignment (applied 2026-07-02)

The engine matched the chapter but the Vite Attribution lens under-stated it — the alignment
pass closed the presentation gaps (`frontend/src/routes/Attribution.tsx`):

- **Linking is now named.** The headline and by-factor table say **Cariño-linked, parts sum
  to the geometric return exactly** (the server always linked; the UI never said so), and the
  hero chart is labelled as the unlinked arithmetic path so the two are never confused (§10.2).
- **Pure-factor-portfolio reading.** The by-factor table carries the chapter's §10.1
  interpretation: each line is x·f, the return on the pure-factor portfolio implicitly held.
- **Skill vs tilt + significance (§10.3).** The residual-diagnostics panel opens with the
  tilt-vs-skill split and shows `t = IR·√T` with the years-to-2σ read
  (`irSignificance`, exported + unit-tested against the chapter's canonical IR 0.5 → 16y,
  IR 1.0 → 4y) — "a single good quarter is noise" made visible.
- **Disclosures (§10.4–10.5).** A footer states: absolute attribution (no benchmark — Market
  stands in for beta; Brinson needs one), model-conditional factor definitions, and the
  13F/quarterly caveat (intra-period trading and exposure timing fold into Specific).

Still open (unchanged, blocked): active attribution + Brinson companion (need a benchmark,
Step 13); a separate trading-residual line (needs intra-quarter position data 13F can't give).

## Verdict

The implementation is faithful to the chapter's methodology: exact single-period decomposition,
Cariño linking with exact reconciliation, IR-based significance, and explicit treatment of all
four pitfalls. The two real gaps — active attribution and a Brinson companion — share one
blocker (no benchmark, Step 13); the trading-residual line is data-bound (13F). Everything else
we either match or exceed, and the §4 risk-linkage goes past the published chapter.
