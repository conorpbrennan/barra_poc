# Additional views to align with It's Just Beta — full-primer gap analysis

**Status 2026-07-02: ALL TEN BUILT.** #1–#5 in suggested order — #2 conditional shock on
`/stress`, #1 `/contributions` + the Attribution "Contributions (Euler)" tab, #4 `/calibration`
(route renamed from "validation": that path was taken), #3 `/regression` + the builder's
`regression_stats.parquet` side artifact, #5 `/factor_cov`; #3–#5 share the new Vite **Model**
lens. Then #6–#10 — #6 `/exposure_profile` (Model lens), #7 `/hedge` (What-if lens), #8
`/factor_portfolio` (Model lens), #9 `/pnl_attribution/names` (Attribution PnL tab), #10 the
DQ model gates in `barra_dq_checks.py` (standardization on funnel survivors / F PSD /
regression-stats sanity, inherited by `/dq`).

First live reads: book bias b≈0.55 (over-forecast, conservative), specific b≈1.26 with 16.7%
2σ exceedances (under-forecast — consistent with the residual diagnostics); Growth clears
|t|>2 on only 9% of days (a drop candidate per the admission bar); the min-variance market
hedge h*=−1.13 cuts book vol 1.44%→0.53% and slightly beats full Market neutralization (the
correlated-covariance effect); style pure-factor portfolios run ~0.9–1.4× gross on this broad
cross-section (the primer's 11.9× is a 10-stock artifact) and net to 0, Market to exactly +1;
the standardization gate on the full cross-section legitimately reads off-centre (Size −1.1)
because the uncapped coverage tail is smaller by design — the gate checks funnel survivors.

Still open: the benchmark (Step 13) for everything active-space; re-fetch chapters 11–15 when
they publish.

Written 2026-07-02, from the offline index (all 20 pages). Method: same as the attribution
alignment — for each chapter, what the primer treats as *the* standard report vs what our
cube/API/UI already shows. Most engines exist; the gaps are mostly views that *name* the
chapter's read. Chapters 11–15 are unpublished stubs; their expected scope is inferred from
cross-references and the mini-example code (ch 18), which already implements hedging and
neutralization numerically.

## Already aligned (no new view needed)

- **Ch 05 Universes** → the Universe lens (membership / funnel / span) covers §5.2–5.6 almost
  clause by clause — the span view *is* the chapter's "out-of-universe flag", the funnel is the
  membership-criteria stack, PIT membership is the locked decision. Best-aligned lens we have.
- **Ch 10 Performance attribution** → done (see pnl-attribution-comparison.md): Cariño naming,
  tilt-vs-skill, t = IR√T, disclosures, and the reconcile driver read.
- **Ch 01/02 foundations** → the model-reference doc and pivot cover the objects; no view gap.

## Proposed views, ranked

### 1. Risk contributions — Euler / CTR / CTV (ch 09) — the biggest gap
The chapter's central deliverable: per-position **CTR = wᵢ·MCRᵢ** (sums exactly to portfolio
vol — "the standard position-level report") and per-factor **CTV = xₖ(Fx)ₖ** (sums to factor
variance, cross-terms 50/50, legitimately negative for hedges). Our Attribution "Risk by level"
tab shows *standalone* Scenario VaR per bucket — those don't sum, and the chapter explicitly
distinguishes the two. The cube already computes marginal-Total-VaR shares (Risk HHI is built
from them) — this view surfaces the machinery we already trust, with the sum pinned to total
risk at the bottom. Two columns (CTV for factors, CTR for positions), never compared directly
(different unit pairings — the chapter's warning, quote it in the caption).
*Build:* new `/contributions` endpoint from `_book_inputs`-style math (x, F, Δ, w all on hand);
replace/extend the "Risk by level" tab. Moderate.

### 2. Conditional (correlated) factor shock — Stress lens (ch 09)
"Stress tests must use correlated shocks, not isolated single-factor moves." Our `/stress`
shocks factors independently — exactly the naive case the chapter warns about (its example:
VALUE −8% naive = −3.1%, conditional = −6.1%, because MKT and MOM co-move). The fix is one
line of algebra: **E[f | fₖ = s] = F₍·,ₖ₎/Fₖₖ · s** — propagate a single-factor shock through
the factor covariance we already have. View: shock one factor, show naive vs conditional impact
side by side with the per-factor propagation bars.
*Build:* small — a `conditional=true` mode on `/stress` + a toggle in the Stress lens. High
correctness value per line of code.

### 3. Regression health / factor significance (ch 06 + ch 03 admission criteria)
Nothing user-facing shows whether the monthly WLS is healthy: **cross-sectional R² trend**
(0.2–0.4 monthly is healthy; trends matter more than levels), **per-factor % of months with
|t| > 2** (≥ one-third justifies inclusion — the admission bar), thin-cross-section months
(the builder already skips < 30 names — disclose them). This is the "is the model well
estimated" view the desk quants would ask for first.
*Build:* the builder computes all of it and throws it away — persist a small
`regression_stats.parquet` (Date, R², per-factor t) in `regress_factors`, add `/regression`
+ a panel (Checks lens or a new Model tab). Needs a rebuild (artifact only, frames unchanged).

### 4. Model validation — consolidated fit-for-purpose report (ch 08 §validation + ch 14's scope)
We already have the pieces the unpublished ch 14 will describe — Kupiec/Basel backtest, bias
stats (book/specific/per-factor), residual diagnostics — but scattered across three panels.
One view: **rolling bias statistic b with the 1 ± √(2/T) acceptance band** (time series, not
just the point estimate we show today), exceedance counts (2σ-band breaches — cheap new stat),
the backtest verdict, R² trend from #3. This is also where the ch 08 failure modes belong as
captions ("underforecasting after calm periods" — our NonLinSize Q4-2024 case is a live example).
*Build:* mostly composition; new rolling-window variant of `_bias_stat`. Moderate-small.

### 5. Factor covariance & correlation view (ch 08)
The F matrix is the model's engine room and completely invisible in the UI. Tufte-style shaded
correlation matrix (no rainbow heatmap), factor vol column with **full-window vs EWMA/recent-1y
vol side by side** — the chapter's vol-clustering point, and exactly the gap the reconcile-band
driver analysis exposed (full-history σ understated the Q4-2024 NonLinSize regime). Optional
crisis-window overlay (event sets) to show correlation spikes; ties visually to the stressed
band (`_stressed_cov` is this view's "what if" button).
*Build:* `/factor_cov?window=` endpoint (pivot of `factor_returns`, trivial); one lens panel.
Small-moderate. A follow-on model decision (EWMA vols in the linkage bands) falls out of it.

### 6. Factor definitions / exposure profile (ch 03)
The "model-conditional — always quote the model" caveat needs a home: per factor, the
descriptor recipe (what *our* Value means), the estimation-universe cross-section distribution
(strip/quantile plot, winsorization bounds ±3 marked), and the book's names overlaid — showing
uncapped coverage names sitting outside ±3 (the Chris point the split-z design implements).
Doubles as the standardization quality gate (cap-weighted mean ≈ 0).
*Build:* exposures frame has everything; `/exposure_profile?factor=` + panel. Small-moderate.

### 7. Hedge panel (ch 12 expected scope + appendix D6 + mini-example §7–8)
The mini example already does it numerically: **minimum-variance hedge h\* = −β_{p,h}**
(single instrument) and multi-instrument **h\* = −Cov(r_H)⁻¹Cov(r_H, r_p)**; plus "neutralize
factor k with minimal turnover" (mini-example §8 does min-TE with MOM neutralized). View: pick
an exposure to kill (e.g. the attribution lens's momentum leak → "hedge it" deep-link), show
the hedge trade and before/after risk via the existing what-if math.
*Build:* the what-if engine reprices modified books already; the hedge solve is small numpy.
Needs a hedge instrument convention (market proxy from the estimation universe, or factor-unit
notional). Moderate. Re-check when ch 12 publishes.

### 8. Factor portfolio inspector (ch 07)
Factor returns are portfolio returns (**f̂ = Pr**, PX = I). Show it: pick a factor/date → the
pure factor portfolio's top long/short names, gross leverage (the chapter's VALUE = 11.9×
gross point), and the cumulative factor return as that portfolio's equity curve. Builds desk
trust in what a "+2.15% momentum quarter" physically was.
*Build:* P = (X'WX)⁻¹X'W from the exposures frame per month. Small-moderate; pedagogical more
than operational — good for the client-facing story.

### 9. Residual explorer (ch 13 expected scope)
"What can't the model explain": per-name cumulative specific PnL small-multiples, sign
persistence, and the residual-concentration read in one place. Partially exists (pivot drill on
`Specific PnL`, hit rate, HHI in residual diagnostics) — this is a curation view, not a new
engine. Low effort, do after 1–5. Re-check when ch 13 publishes.

### 10. Pipeline quality gates (ch 16 §16.2)
Extend `/dq` from data checks to the **theory-derived gates**: standardized exposures have
cap-weighted mean ≈ 0 / sd ≈ 1, the regression constraint residual vanishes, F is PSD, frame
freshness per stage. Present as the six-stage pipeline with a gate light per stage ("when one
fails on today's data, the data is what broke"). Small; mostly new checks in
`barra_dq_checks.py`.

## The one blocker worth naming

**A benchmark (roadmap Step 13)** is the single unlock behind everything the primer does in
"active" space: tracking error and its decomposition (ch 02/08/09), active attribution +
Brinson (ch 10), min-TE construction (ch 11/18§8). Every view above is designed absolute-first
and gets an active mode for free once weights `w_a = w_p − w_b` exist.

## Suggested order

Quick wins first: **#2 conditional shock** (smallest, corrects a real methodology gap), then
**#1 Euler contributions** (the chapter-09 standard report), **#4 model validation** and
**#3 regression health** (they share a panel family), **#5 factor covariance**. Then 6–10 as
capacity allows. Re-fetch chapters 11–15 when they publish and re-run this analysis.
