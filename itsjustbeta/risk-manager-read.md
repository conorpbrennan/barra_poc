# Risk-manager read of the ten alignment views + scope audit against the primer

Written 2026-07-02 on live numbers (as-of 2024-12-31, Soros book). Part 1 is the day-to-day
read of each view, grounded in the It's Just Beta chapter it implements. Part 2 is the scope
proposal: what to remove or demote so the tool reflects what Chris actually uses — evidence =
explicitly in the primer, or explicitly asked for in his correspondence; neither → cut candidate.

## Part 1 — the ten reads

**1. Euler contributions (ch 09).** Book model vol 1.44%/d, 94% factor — this is a market book,
not a style book: Market alone is 77% of variance, and the biggest style line (ResidVol, 7%) is
a rounding error next to it. The ch-09 patterns show up verbatim: NonLinSize and Momentum carry
*negative* CTV (hedging exposures — they co-vary against the rest of the book), and CTR
concentrates — JD + BABA are 17% of book vol on ~12% of weight. "Diversified holdings share one
factor profile": the top-5 CTR names are all the China/high-vol cluster.

**2. Conditional stress (ch 09).** Value −2σ naive reads −0.29%; conditional reads −1.00% —
3.4× worse, the chapter's exact warning ("stress tests must use correlated shocks"). The
propagation table shows why: the covariance drags Market −0.4σ and ResidVol −0.6σ along, and
those exposures dwarf the Value tilt. The naive number materially understates every style shock
on this book because Market co-moves. Day-to-day: only quote the conditional number.

**3. Regression health (ch 06/03).** Daily weighted R² ≈ 0.19 — fine for daily single-stock
data (the 0.2–0.4 rule is monthly; trend flat, no deterioration). The admission table is the
action item: Market 85%, Beta 66%, Momentum 57% of days |t|>2 comfortably clear the ⅓ bar;
**Growth at 9% fails it outright** and EarnYield (21%) / NonLinSize (18%) sit below it. Ch-03
admission criteria say Growth is a drop candidate — its factor returns are noise, and noise
factors leak real risk into wrong buckets.

**4. Calibration (ch 08).** Book bias b = 0.55 — *over*-forecast, the safe direction, expected
for a long-only book whose model vol is dominated by full-history Market vol. The problem line
is **specific: b = 1.26 (outside the 1±0.29 band) with 16.7% 2σ exceedances vs 4.6% expected,
and a window peak of 4.8**. The specific block under-forecasts by a lot in stress months —
consistent with the residual diagnostics and with 13F staleness (intra-quarter trades land in
"specific"). Ch-08's bias-statistic discipline says: don't trust the specific vol number in a
risk decomposition without this caveat.

**5. Factor covariance (ch 08).** Average |ρ| 0.15 full-window vs 0.17 recent — no broad
correlation regime shift. NonLinSize vol ratio 1.21 is the one vol-clustering flag (matches its
Q4 reconcile breach). Ch-08's "underforecasting after calm periods" failure mode currently has
one name on it: NonLinSize. Any band built from full-window vols (backtest, reconcile) is ~20%
too narrow for that factor right now.

**6. Exposure profile (ch 03/05).** ResidVol: median +0.45, 14% of the coverage cross-section
beyond ±3, and held names INSM/PACB/RILY sitting at +8 to +9 — the uncapped coverage tail doing
exactly what the estimation/coverage split intends (ch 05: coverage names "may legitimately have
out-of-range z-scores"). The read: those book names are far outside the estimation span, so
their model risk is extrapolated — cross-check them against the Universe span view before
trusting their marginals.

**7. Hedge (D6 / ch 12 scope).** The only hedge that matters on this book is Market: h* = −1.13
takes vol 1.44% → 0.53%, and it slightly beats full neutralization (0.55%) because it also nets
the correlated style covariance. No style hedge moves the needle (best non-market row saves
~8bp). Specific floor 0.35% is untouchable by any factor hedge — after the market hedge, the
book is roughly half specific, which is where the calibration warning (#4) starts to bite.

**8. Factor portfolio (ch 07).** Momentum's pure portfolio is what a momentum bet physically
was in Q4-2024: long NVDA/WMT/PLTR/CEG, short TFC/MSFT/INTC/ADBE, dollar-neutral, gross 0.94×.
PX = I verified to 1e-16. The desk read: when the Momentum factor prints +2%, that's this
long-short book's return — useful for sanity-checking whether a factor move is believable.

**9. Residual explorer (ch 13 scope / ch 10 pitfalls).** Trailing-12m specific PnL is small and
two-sided (winners AUR/IBKR/BABA +0.9/+0.9/+0.8%, losers UBER/NKE/SNOW −0.8/−0.5/−0.5%) with
persistence 0.38–0.67 — near the memoryless 0.5, no name flags the ">0.7 = stale weight or
missing factor" alarm. Consistent with the book-level read: no reliable stock-picking alpha,
but no hidden systematic bet dressed as alpha either.

**10. Quality gates (ch 16).** 30 pass / 3 warn / 0 fail. The theory gates hold: estimation
medians ≈ 0 (worst −0.32), F is PSD, regression breadth ≥ 407 names. The standing warns are the
disclosed stubs (Country, sectors) — data gaps, not model breaks, matching ch-16's "a failed
invariant indicts the inputs."

**The one-paragraph desk summary.** A long, market-dominated book (94% factor, 77% Market)
whose real risks are: a market hedge nobody has put on (h* = −1.13 halves vol), a specific-risk
block the model under-forecasts by ~26% in stress months, one factor (NonLinSize) running hot
vs its full-history vol, and a Growth factor that shouldn't be in the model. Stock-picking is
statistically invisible either way. Everything above cites the chapter it comes from.

## Part 2 — scope proposal: cut to what Chris actually uses

Evidence classes: **P** = explicit in the published primer; **C** = explicit in Chris's
correspondence (the §4 linkage ask, the residual-diagnostics scope, the "within limits /
intended bets" loop, his VaR-exceptions analogy, the universe/estimation questions);
**neither** → propose removal. Chapters 11–15 are unpublished — scope inferred there is treated
as weak evidence.

### Propose REMOVE

| Item | Where | Why |
|---|---|---|
| **"Risk by level" tab** (standalone Scenario VaR per Sector/Issuer/Position) | Attribution | Ch 09 explicitly warns standalone per-bucket risk doesn't sum and calls CTR "the standard position-level report". The Euler tab supersedes it; keeping both invites the exact CTR-vs-standalone confusion the chapter flags. |
| **Drawdown panel** (equity-curve peak-to-trough) | Trends lens | In no chapter; not in any Chris email. It's a constant-portfolio hypothetical, and the backtest already covers "was the risk forecast right" (his VaR analogy). |
| **Liquidity lens** (days-to-liquidate) | rail | Not a primer report (ch 05/07 use liquidity only as universe/friction inputs) and no Chris request on record. Real desks do watch DTL — but the brief is his process, not a generic desk's. Park the endpoint, drop the rail entry. |
| **Basel traffic-light zone** | Backtest badge | Ch 08 validates with bias stats + exceedance counts; Kupiec + exceedance share cover Chris's analogy. The Basel zone is regulatory dressing — cut the metric, keep the badge. |
| **Risk HHI** (marginal-VaR-share Herfindahl) | Overview, limits, what-if | Concentration IS a ch-09 theme, but its idiom is "top names' share of CTR" (the AXIOM 61.7% example), not a Herfindahl. Replace HHI everywhere with **top-5 CTR share** — same signal, the chapter's own units. |
| **Total VaR 99** (scenario VaR ⊕ 2.326σ composite) | Overview, limits, what-if | A house construct in no chapter. The primer decomposes σ (factor + specific) and validates with exceedances. Keep Scenario VaR/ES (C — his analogy needs them); replace "Total VaR 99" as the headline/limit with model vol σ + its factor/specific split (P) or plain Scenario VaR 99. Biggest blast radius — limits.json, overview hero, what-if deltas — so this is a direction, staged. |

### KEEP — with the evidence

- **Pivot / exposures drill** — P (ch 02/09: x = X'w is *the* object) · **Stress incl. event
  replay + conditional** — P (ch 09 verbatim) · **Attribution PnL tab** (Cariño, residual
  diagnostics, linkage band) — P (ch 10) + C (the linkage is his addition) · **Euler tab** — P ·
  **Model lens** (all five panels) — P (ch 03/06/07/08) · **Checks/DQ** — P (ch 16 gates) ·
  **Universe lens** — P (ch 05, near clause-by-clause) + C · **Drift** — C (his
  intentional-vs-not question) · **Changes (QoQ)** — C (the Soros risk-review ask) · **What-if +
  hedge** — C (loop step 3: exit/hedge) + D6 · **Limits RAG** — C ("within limits, the intended
  bets, in line with mandate") · **Backtest (Kupiec + exceedances)** — C (his VaR analogy) + P
  (ch-08 exceedance counts) · **Trends** — keep but slim to what P grounds: factor-exposure
  paths and the R²/bias trends ("trends matter more than levels", ch 06); the VaR-path chart is
  optional beyond that · **Ask/analysis LLM panels** — tooling, not metrics; keep (they ground
  in the same guarded views).

### Sequencing, if endorsed

1. Zero-risk: drop "Risk by level" tab, Basel zone, Drawdown panel, Liquidity rail entry
   (endpoints stay, nothing else consumes them).
2. Swap Risk HHI → top-5 CTR share (Overview, /limits key, what-if delta; one limits.json edit).
3. Staged: retire "Total VaR 99" as the headline in favour of model vol + split (touches
   limits.json, overview, reverse-stress default, /analysis grounding).
4. Re-audit when ch 11–15 publish — hedging/portfolio-construction chapters may re-justify or
   re-shape the What-if surface.

**Recommendation:** do 1–2 now; hold 3 for Chris's sign-off since "Total VaR 99" is the number
his limit is written against; nothing else in the tool depends on the removed items.

**Status: steps 1–2 APPLIED 2026-07-03** (Vite UI + API + limits.json; endpoints parked, not
deleted; Streamlit untouched except the two panels that read the renamed `top5_ctr_share` key).
Live: Top-5 risk share 29.5% vs warn 0.40 / limit 0.50 — green, headroom visible.

**Step 3 APPLIED 2026-07-03** (decision: vol is the reference): `model_vol_1d` (σ = √(x'Fx +
w'Δw)) leads `_risk_from_weights`, the Overview heroes ("Model vol (1d) — the reference",
1.44%/d live) and the What-if table; the desk limits moved to **Scenario VaR 99** (same
warn/limit as the old composite) + ES 97.5 + Top-5 risk share; `/reverse_stress` defaults to
the Scenario VaR 99 limit; Total VaR 99 stays as a measure, labelled "(legacy)" and quoted
last; all CHRIS_VOICE prompts state the hierarchy (vol = reference, VaR/ES = limits, Total =
house composite). Step 4 open (re-audit on ch 11–15 publish).

**Stock-level breach analysis, APPLIED 2026-07-03:** the linkage positions now carry the
factor/specific split + drivers (`weight_migration` / `specific_move` / `factor_move` / `mixed`,
`_position_driver` pure), and the two joint reads Chris runs across breaches: **breach
co-movement** (`_pairwise_mean_corr` — mean pairwise daily-residual ρ among idiosyncratic
breaches; ≥ 0.25 = common thread = missing-factor signal; Q4-24 live: PACS/MCHP/SOFI/LOAR
ρ +0.17 → independent stock events) and **hidden beta** (a `factor_move` breach whose driving
factor's own row sits within band = mis-measured loading; Q4-24 live: GFL and SNY both flag on
Liquidity). PCA stays deferred per Chris's keep-the-stats-cheap note.

**Overview re-alignment to Chris's loop, APPLIED 2026-07-03:** the landing page now leads
step 1 with **top risk contributions (CTV bars, % of variance, negatives = hedges)** — net
exposures demoted to the secondary read (ch 09: exposure ≠ risk contribution); adds the
**factor/specific variance split** hero (the σ² = x'Fx + w'Δw first-order read, 94%/6% on this
book); and closes step 4 with a **reconcile line** in the RAG strip — worst-case verdict from
`/pnl_attribution/linkage`, counting only genuine breaches (exposure_migration = band artifact,
excluded). Remaining misalignment: the "Total VaR 99" headline (step 3, held for Chris).
