# Plan — PnL attribution & factor-model validation (realized PnL by factor and residual, diagnostics)

Status: SPEC — endorsed by Chris (2026-06-29: "that looks reasonable"), with one addition: link the
attribution to the **risk decomposition at the start of the period** (§4); refined per his 2026-06-30
feedback (drop "projected", the known-vs-unknown-risk framing, keep the stats cheap). New risk-tooling
step for Chris's request (Soros risk review): a report that splits the book's realized PnL over a period
into a factor-explained part and a residual, broken down by factor, then a check on whether the residual
is large and whether it's correlated. Uncorrelated residual means the PM's specific bets are genuine and
diversified, not a hidden systematic bet.

Built cube-native from the outset (Chris's call): the attribution is a set of additive cube measures
that drill parent→child in the existing pivot grid — click a factor, see the names under it — and the
residual is persisted as a 7th data frame. So this changes the six-frame contract (→ seven) and needs a
rebuild. That's the deliberate cost of a live, reconciling, re-pivotable drill instead of a static
table. The heavier statistics (period linking, the residual diagnostics) stay in a Python layer the cube
can't express: a precompute artifact + two guarded API endpoints + a UI panel, headline folded into
`/analysis`, same shape as `/backtest`, `/drawdown`, `/trends`.

As a by-product it gives `/backtest` and `/drawdown` a true realized track record, instead of the
constant-portfolio factor-PnL proxy they use now.

## What this answers

Chris's asks, mapped to the deliverable:

1. How the factor model explains the realized PnL over a period (the factor + residual split) → the core
   attribution: realized book PnL split into a factor-explained part and a residual, reconciling exactly. §1.
2. Break it down by factor and by residual → the by-factor contribution table plus the specific line,
   pivotable by factor group / sector / name. §1.
3. Are the residuals large, and if so are they correlated (uncorrelated = the PM is doing a good job) →
   the residual diagnostics: size, realized vs predicted specific vol, information ratio, and three
   correlation tests. §2.
4. Other ways to look at it → §3: concentration of the residual PnL, hit rate,
   factor-timing vs static tilt, sign persistence, the tie to the VaR backtest, the absolute-vs-active
   caveat.

## Get this straight first

A factor risk model explains return after the fact. It doesn't forecast it. It has no view on where a
stock will go. So the factor component below isn't a forecast of anything — it's the part of the realized
return the factors explain (we avoid the word "projected" per Chris's 2026-06-30 note, since there's no
forward PnL forecast):

> realized book return `R_p(t) = Σ_k x_k(t)·f_k(t)  +  u_p(t)`

`x_k(t) = Σ_i w_i(t)·L_{i,k}(t)` is the book's net exposure to factor `k`, `f_k(t)` is the realized
factor return that day, `u_p(t) = Σ_i w_i(t)·ε_i(t)` is the residual (specific) return. The factor PnL
is `Σ_k x_k·f_k`; the residual is `u_p`. They sum to realized by construction, because `ε_i` is defined
as `R_i − Σ_k L_{i,k}·f_k`, which is the same residual the WLS fit already throws away
(`barra_build_frames.py:631`).

Two different things, and mixing them up is the usual mistake:

- Return attribution (§1) — where the money came from. Always reconciles. The content is the mix and
  the size of the residual, not the tie-out itself.
- Risk validation (§2's bias stats, and the existing `/backtest`) — was the model's risk forecast
  right. This is where the model is actually tested.

Worth saying this to Chris at the top. It's the difference between a performance-attribution
report and a return forecast he'd be right not to trust.

## Data inventory — what we have, and the one gap

It's all on disk. The realized side is built bottom-up from the name price series, not from the cube —
the cube has no realized-PnL measure, only the hypothetical `Scenario PnL vector`.

| Need | Source | Granularity |
|---|---|---|
| Per-name realized return `R_i(t)` | cached daily Stooq/Yahoo close, `stooq_daily` → `px["Close"].pct_change()` (`barra_build_frames.py:591`) | daily, 2016–2024 (`:49`) |
| Factor returns `f_k(t)` | `factor_returns` frame, the daily WLS coefficients (`:619`, `:628`) | daily |
| Loadings `L_{i,k}(t)` incl. Market | `exposures` frame (`:568`); Market = 1.0 leaf (`:703`) | monthly (`:653`), held flat within month |
| Held weights `w_i` / MV | `positions` frame, as-of join of 13F filings (`:714`–`:718`, `:737`) | monthly as-of, quarterly source |
| Factor groups (Market/Style) | `factor_meta` (`:749`) | static |

The one gap: the residual return isn't kept. `regress_factors` computes `resid_all` per day (`:631`)
but keeps only its square, EWMA'd into `SpecificVar` (`:640`–`:648`). The residual value `u` is thrown
away. We persist it as the 7th frame `specific_returns` — a one-line change (keep `u` next to `u²` at
`:640`) — because the cube needs it as a leaf for the `Specific PnL` measure (§5). It's the same value
the model already computes, so it's exact, not an approximation; equivalently `ε_i(t) = R_i(t) − Σ_k
L_{i,k}(t)·f_k(t)` reconstructs it from the published frames (the factor returns are the betas, the
loadings are the regression's X), which is the cross-check the tests assert.

## §1 — The core attribution

### Realized engine — unit NAVs, drifting weights, price-only

The realized number is built from the ~1k name series, not the cube:

1. Unit NAV per name — daily close → daily return → cumulative product, an index base 1.0. Separates
   the price path from position size.
2. Drifting (buy-and-hold) weights. Anchor weights at each 13F filing, then let each name's unit NAV
   compound so the book weights drift with the market until the next filing, where they reset to the
   new 13F weights. That's what actually happened to the book between filings — Soros didn't rebalance
   daily — and it's more honest than the cube's constant-portfolio assumption. It's also why
   `/backtest` and `/drawdown`, both constant-portfolio today, are weaker as realized measures than
   this.
3. Scale and sum — `w_i(t)·R_i(t)` over the drifting weights gives realized book return `R_p(t)`; scale
   by MV for dollars.

Price-only. Stooq close is price-only and the factor model is price-only (`:591`), so the realized
return fed into attribution is the same return the regression saw. That consistency is what keeps the
residual honest. Use a dividend-adjusted series on the realized side while the model is price-only and
the residual quietly absorbs every dividend as fake alpha. So dividends are out on both sides —
disclosed, not mixed in. If a total-return headline is ever wanted it's an explicit extra line, not a
swap.

Coverage. Some held names have no usable series (delisted, foreign). They can't be unitised, so the
report states coverage = % of book weight priced and lists the unpriced names. Same as `/liquidity`'s
no-ADV names and the funnel's "data unavailable". Never silently dropped from realized PnL.

### The decomposition and the tie-out

Daily, on the same drifting weights:

- factor contribution `c_k(t) = x_k(t)·f_k(t)`, `x_k(t) = Σ_i w_i(t)·L_{i,k}(t)` (Market included).
- specific `u_p(t) = R_p(t) − Σ_k c_k(t) = Σ_i w_i(t)·ε_i(t)`.
- three-way tie-out: realized (bottom-up) = Σ factor contribution + specific, to machine precision
  daily. Same idea as `/whatchanged`'s four-source reconciliation, and it's a unit test.

Period linking. Daily contributions are arithmetic; over a month/quarter/year they have to compound to
the geometric total return. Use Carino linking (`_carino_link`, pure, unit-tested) so the linked
by-factor contributions sum exactly to the compounded book return with no plug. Any linking residual is
reported, not hidden.

### The views

- Hero chart (Tufte/Few): cumulative stacked area showing where the money came from — Market, Style
  (grouped, or top style factors), Specific — summing to the realized equity curve. Direct labels, grey
  plus one accent, no gridlines or chartjunk.
- By-factor table: per factor, average net exposure `x̄_k`, cumulative factor return, contribution to
  PnL, % of total, and a t-stat (mean contribution / its standard error) so a bet that paid by luck is
  separated from one that paid reliably. Ranked by |contribution|.
- Period selector: trailing-12m and since-inception by default; YTD / custom.
- Pivot / drill: the by-factor table is the parent row per factor; expand it to the names underneath
  (next subsection), or re-pivot by factor group / sector / name natively in the grid.

### Parent/child drill (cube-native, the primary view)

Clicking a factor expands to the names under it. It foots at every level because the parent value *is* a
sum over names:

- Factor parent → name child: name *i*'s share of factor *k* = `w_i · L_{i,k} · f_k`; `Σ_i = x_k·f_k`,
  the parent. Exact.
- Specific parent → name child: `w_i · ε_i`; `Σ_i = u_p` (folds the residual-concentration view into the
  same grid).
- Market parent → name child: `w_i · f_market`.

The same leaf `(Date, Position, Factor) → contribution` drills **both** directions — Factor→Name (which
names drove this factor) and Name→Factor (why did position X make money: its factor mix + specific). One
object, two reports, delivered through the existing pivot grid so the drill and re-pivot are native — see
§5.

## §2 — Residual diagnostics

The residual is the part the model can't explain. For name `i` in month `t`, `R_i(t) = Σ_k
L_{i,k}(t)·f_k(t) + ε_i(t)` — `ε_i` is the **specific (idiosyncratic) return**, the stock-specific move
the factors miss; at book level `u_p(t) = Σ_i w_i·ε_i`. The build leaves two objects: a monthly time
series `u_p(t)` (~108 months) and a names×months panel `ε_i(t)`. The model also *predicted* the specific
risk (`SpecificVar` → a specific vol), so realized can be checked against predicted.

The premise: if the residual is genuine, diversified stock-picking, the specific returns behave like
independent coin-flips — no pattern over time, no link to the factors, no common thread across names,
sized about as predicted. Each test checks one of those.

### Is it large?

- **Specific share** — cumulative specific PnL ÷ cumulative total. How much of the book's return came
  from stock-picking vs factor bets.
- **Information ratio** — `IR = mean(u_p)/std(u_p) · √12` (annualized monthly). A Sharpe ratio for the
  idiosyncratic slice — the stock-picking skill number. `> 0.5` = reliable alpha; `~0` = noise, no edge;
  `< 0` = stock-picking destroys value. *(e.g. +0.2%/m at 1.0% std → IR ≈ 0.69.)*
- **Realized vs predicted specific vol** — predicted `σ_pred = √(Σ_i w_i²·σ_spec,i²)` (diagonal, no
  cross-terms) vs realized `std(u_p)`. **Ratio** ≈ 1 = sized right; `> 1` = model under-states specific
  risk; `< 1` = over-states. Mis-sized specific risk → wrong total VaR.
- **Explained share** — `R² = 1 − var(u_p)/var(R_p)`, the fraction of book-return variance the factors
  explain. Low isn't automatically bad (diversified stock-picking *or* a missing factor) — the
  correlation tests tell those apart.

### Is it correlated? — three tests

Three places a hidden pattern can hide: across **time**, against the **factors**, across **names**.
"Uncorrelated = good PM" reads onto all three. **Scope (Chris, 2026-06-30):** keep these cheap — the desk
quants already know the formal tests, so build the simple correlation / regression versions and treat
Ljung-Box and PCA as optional.

1. **Serial autocorrelation (across time)** — does this month's `u_p` predict next month's? The cheap read
   Chris wants: `corr(u_p(t), u_p(t−1))` and `corr(u_p(t), u_p(t−2))` — the lag-1 and lag-2
   autocorrelations. `≈ 0` = memoryless independent draws (good — what re-underwritten stock bets look
   like); clearly `> 0` = the specific return *trends*, i.e. a slow, persistent, unhedged systematic bet
   dressed up as alpha. *(Optional formal version: the **Ljung-Box** white-noise test across many lags,
   `_ljung_box`, pure — skip unless quick.)*
2. **Residual vs factors** — regress `u_p(t)` on the factor returns `f_k(t)` (and correlate with the
   book's own factor bets `x_k`). **Chris flagged this regression as a worthwhile one to build.**
   Orthogonal within one month's regression by construction, but over time — and because the 13F exposures
   are stale/lagged up to 45 days — `u_p` can still co-move with a factor. A significant loading means
   part of the "alpha" is that factor's beta mis-measured: the PM is paid for systematic risk and calling
   it skill. Near zero = clean, orthogonal alpha.
3. **Cross-sectional (across names)** — the model **assumes specific returns are uncorrelated across
   names** (the diagonal specific block). If, in practice, many names' residuals move together, that is
   **most likely a missing factor** (Chris's correction) — a common driver the model has no factor for; a
   small linked group (share classes, ADRs) is only the narrow special case. A shared residual factor
   means the diagonal block **under-states risk** — what looks like many independent bets is one bet
   repeated, VaR too low. *(Optional formal version: PCA on the residual panel, `_resid_pca_share`, pure —
   the first principal component's explained-variance share measures the common direction; deferred per
   Chris unless quick.)*

The read for Chris: positive IR, no lag-1/2 autocorrelation, `~0` factor correlation, no common residual
component across names → genuine, diversified, orthogonal stock-picking, the PM is doing well.
Autocorrelation (persistent unhedged bet), factor correlation (hidden beta), or a common residual factor
(a missing factor) each name a specific problem → tell the PM, add the factor, or hedge it.

### Bias statistics — did the model size the risk right?

The tests above ask *what* the residual is; bias stats ask whether the **risk forecast** was correct —
the canonical Barra/MSCI validation. Standardize each realized return by its *predicted* vol, `z_t =
realized_t / predicted-vol_t`; if the forecast is right, `z` has std ≈ 1. The bias statistic is `B =
std(z)` over a rolling 12–24m window (`_bias_stat`, pure): **B ≈ 1** calibrated, **B > 1** risk
under-forecast (the dangerous direction, VaR too low), **B < 1** over-forecast. A calibrated `B` sits
within `1 ± √(2/N)` for `N` observations; outside is significant bias. Run it three ways — the **whole
book**, **each factor**, and the **specific block** (`realized specific / predicted specific vol`, the
rigorous version of the calibration ratio above).

## §3 — Other ways to look at it

- By sector / by name — which positions generated the specific return; ranked winners and losers. Same
  decomposition, `by=sector|name`.
- Concentration of the residual — HHI / top-5 share of the specific PnL (`_concentration_hhi`, pure).
  Residual from one lucky name isn't skill; broad-based is. Reuses the Risk-HHI idiom.
- Hit rate — % of names (and % of periods) with positive specific return.
- Factor timing vs static tilt — split each `c_k` into `x̄_k·f̄_k` (static exposure) plus `cov(x_k,f_k)`
  (timing). Was the bet held or timed?
- Sign persistence — does a name's specific return this month predict next month? Persistent means a
  real edge or a stale 13F; mean-reverting means noise.
- Tie to the VaR backtest — `/backtest` exceptions are the days realized beat predicted risk;
  cross-reference them against the attribution to see what drove each one. The realized NAV this report
  builds also upgrades `/backtest`'s and `/drawdown`'s constant-portfolio inputs.
- Absolute vs active — there's no benchmark in the repo (Step 13, blocked). So this is absolute
  attribution: Market stands in for beta, and the specific return is the closest thing to isolatable PM
  stock-picking. Active attribution against a benchmark is gated on Step 13.

## §4 — Linkage: risk decomposition ↔ PnL attribution (Chris's key ask)

Chris's sign-off (2026-06-29) came with one addition, and it's the important one: PnL attribution on its
own is informative, but the value is in **connecting it to the risk decomposition at the start of the
period**. His words: *"it's really the connection between the PnL attribution and the risk decomposition
at the start of the period that tells you how good the risk management of the portfolio is."*

The loop he runs:

1. Risk decomposition at date T — which factors / which positions contribute how much to **total risk**.
2. Check the exposures: within limits, the intended bets, in line with mandate.
3. Exit / hedge anything that isn't.
4. PnL attribution over T→T+1 — check the PnL story matches what the risk decomposition led you to expect.

The question that ties the two together: **was the realized return in the range of outcomes the risk
decomposition implied, and did the contributions line up with the risk actually taken?** A bet that paid
in proportion to its risk is understood; a loss on something the risk decomposition called low-risk is a
**surprise** — Chris's flag that the risk wasn't fully understood at step 1.

Two checks close the loop. **Within band** — does each factor's realized contribution sit inside the band
its start-of-period risk implied? This is *not* "big-risk factors should drive the PnL" (Chris's
correction): if the big-risk factor doesn't move and a small-risk one falls off a cliff, the small one
rightly dominates — what matters is each contribution against its *own* risk-implied range, which already
folds in the move. **The surprise** — a realized contribution outside that band, an outcome the risk
decomposition didn't see coming. The interpretation grid — note the axis is **known vs unknown risk, not
made vs lost**:

| | PnL: made money | PnL: lost money |
|---|---|---|
| **Risk you knew you had** | bet paid — fine, you chose it | bet lost — fine, you chose it |
| **Risk the decomposition missed** | **investigate** — a win you can't explain | **investigate** — a loss you can't explain |

Chris's correction: an unexpected *gain* is as much a risk-process failure as an unexpected loss — "in
one case the investors are happy, the other angry, but both show the same failure." Both bottom cells are
the work. The principle: **the primary function of the risk team is not to avoid losses but to understand
all the risks the fund is taking.** Some bets pay, some don't — that's the business, not on the risk
manager. Making *or* losing money on a bet you didn't know you took is fully on the risk manager: the
direction was luck, the failure was not seeing the risk. So good risk management is **how well the PnL is
explained by the risk you knew you had**, not the sign. (Secondary function: keep those bets from
threatening the fund — limits/mandates, capital & liquidity reserves, hedging.)

Chris frames it as a universal pattern — the same loop in two other languages. **Sensitivities:** the
Greeks report at the start vs the PnL-explain over the period; if they don't line up, there are
second-order / non-linear effects the sensitivities missed. **VaR:** the realized PnL distribution vs the
VaR; too many exceptions means the VaR model is missing something (our `/backtest` is exactly this). Form
an ex-ante expectation, measure ex-post reality, treat the unexplained part as the signal.

### What we already have

The ex-ante side is mostly built. Contribution to total risk by factor and position is the cube's
**marginal / incremental contribution to Total VaR** (Risk HHI is already built from those shares);
`/limits` is step 2; `/backtest` is exactly his VaR analogy (too many exceptions ⇒ the VaR model misses
something); `/stress` is his "shock the vols and correlations" point. So the linkage reuses this
machinery — it isn't a new engine.

### What we add

1. **Side-by-side decomposition** — the same Factor × Position hierarchy with two columns: **contribution
   to risk at T** (marginal Total VaR share) next to **realized PnL over T→T+1** (the new attribution).
   Both are additive cube measures on the same hierarchy, so this is two measures in the **same drill
   grid** — the cube-native (§5) decision is what makes it fall out for free. This is the centre of the
   report.
2. **Within-expected-range check** — per factor / position, a standardized surprise `z = realized
   contribution / ex-ante risk-implied sd` (the sd from the risk decomposition at T, scaled to the
   horizon). At book level, realized period PnL vs the ex-ante VaR/ES band at T — the per-period form of
   the backtest exception.
3. **Surprise ranking** — rank factors / positions by |z|, leading with the biggest realized loss
   relative to its ex-ante risk (lost money where the risk decomposition said low risk). Step 4 made into
   a report: the names that didn't behave as the risk said they would.

The verdict the pack delivers: did the book's PnL come from the risks it meant to take (good risk
management), or from places the risk decomposition didn't flag (a gap to dig into)?

### The reconcile chart + the stressed band

The headline visual is a **band/dot plot**: one row per factor (plus specific and the book total), each
row showing the **expected distribution as a band centred at zero** with the **realized contribution as
a dot**. The band half-width is the row's start-of-period σ (`|x_k|·σ_k`), centred at zero because the
model forecasts dispersion, not direction — so the dot's position *is* the surprise z-score, and a dot
outside the band is a breach you can see. This one figure subsumes three checks: the **book-total** band
breach = the per-period `/backtest` exception; the **specific** row = the specific-block bias stat; the
**per-factor** rows = the surprise z-scores.

Draw **two bands per row** — a **base** band (today's vols and correlations) and a **stressed** band
(vols shocked up, correlations pushed toward 1). The reading sharpens: realized inside base = benign;
outside base, inside stressed = a stress regime (the calm estimate was too benign, the shocked one
covers it); outside even stressed = a genuine gap to investigate. The band width is nothing but the
covariance applied to the exposures (`σ² = xᵀFx`), so **the band is the vols-and-correlations made
visible, and the stressed band is exactly Chris's "shock the vols and correlations."** Correlations only
enter the *aggregate* rows, so the **book band widens under a correlation shock even when no single
factor's does** — that gap is the diversification the book is leaning on, and it's where correlation risk
shows up.

**One gap to close.** `/stress` today shocks factor **sigmas** (`dPnL = Σ x_k·σ_k·vol_k`), not
**correlations**. Full fidelity to Chris's ask needs a **correlation-stress mode** — blend `F` toward an
all-ρ matrix, or toward a crisis-window empirical correlation — so the stressed band reflects
correlations → 1, not just wider vols. Small addition to the existing stress engine; the reconcile chart
is where it shows. A mock of the chart is embedded in §9 of the factor-model roadmap page (the
client-facing review doc), built to share with Chris for sign-off.

## §5 — Architecture (cube-native)

Split where each half belongs: the additive decomposition + drill goes in the cube (that's what it's
for); the statistics the cube can't express (linking, autocorrelation, PCA, bias) stay in Python.

1. **Builder — the 7th frame.** Persist the residual as `specific_returns (Date, Position,
   SpecificReturn)`: un-square the value `regress_factors` already computes at
   `barra_build_frames.py:640` (keep `u` alongside `u²`). This is the contract change (six frames →
   seven) and needs a rebuild. Nothing else in the builder moves — the existing frames and their numbers
   are unchanged.
2. **Cube — three measures on two regular joins.**
   - Join `factor_returns` as a plain fact on its natural `(Date, Factor)` key (aggregated to the
     monthly calendar) and `specific_returns` on `(Date, Position)`. These are *regular* joins, not the
     scenario partial-Factor-only join, so date-alignment is automatic (Date is already a shared
     dimension) and the scenario engine is untouched.
   - `Factor contribution = Σ Weight × Loading × FactorReturn`, `Specific PnL = Σ Weight ×
     SpecificReturn`, `Realized PnL = Factor contribution(all factors) + Specific PnL`. All **additive
     scalars** — same class as `Net exposure`, which `/trends by=Factor` already queries across every
     date in one plan. No ragged vectors, so no per-date OOM (the OOM is a property of the scenario
     *vector* measures, not of an additive contribution). They foot at every level of the Factor ×
     Sector × Position hierarchy — that footing is what makes the drill reconcile.
   - Add the three to the `/pivot` measure allowlist (`MEASURE_NAMES`) so the pivot grid, `/ask` and
     `/analysis` inherit attribution behind the same `_validate_pivot` guard.
3. **`barra_pnl_attribution.py` — the parts the cube can't do.** The daily drifting-weight realized NAV,
   the Carino-linked period return, and the residual diagnostics (IR + var-ratio measures,
   residual-vs-factor regression, lag-1/2 autocorrelation, bias stats, concentration HHI, hit rate;
   Ljung-Box / PCA optional). Writes `data/pnl_attribution.parquet`. Pure stats split into
   importable functions (`_carino_link`, `_bias_stat`, `_ljung_box`, `_resid_pca_share`,
   `_concentration_hhi`, `_info_ratio`) for unit tests, same as `_kupiec_lr` / `_max_drawdown`.
4. **API.** (named `/pnl_attribution`, not `/attribution`, to avoid the existing risk-attribution endpoint
   at `risk_api.py:203` — decided 2026-06-30.) `GET /pnl_attribution?from=&to=&book=&by=factor|group|sector|name` — the period headline +
   reconciliation + the cumulative series for the chart (Carino-linked, from the precompute). `GET
   /pnl_attribution/residual?from=&to=&book=` — the §2 diagnostics with plain verdicts. `GET
   /pnl_attribution/linkage?T=&horizon=&book=&set=` — the §4 pairing: the risk decomposition at T (marginal
   Total VaR by factor/position, from `_book_inputs` / `_risk_from_weights` — the same what-if math the
   cube reports) next to the realized PnL over T→T+1, the per-factor/position surprise z-scores, the
   surprise ranking, and the book-level within-band check (reusing the `/backtest` exception logic).
5. **UI — the pivot grid is the drill-through.** The attribution measures slot into the existing pivot
   panel, so click-to-expand parent→child is native, no bespoke widget. The §4 linkage is the same grid
   with two columns (risk at T, PnL over T→T+1). `render_attribution` adds the hero stacked-area chart,
   the period selector, the residual-diagnostics sub-panel, the **reconcile band chart** (realized dot vs
   the base/stressed band per factor), the surprise ranking, and the within-band book check. Tufte/Few
   throughout.
6. **LLM fold-in** — `/analysis` (and `/ask`'s grounding) lead with the headline: "X% factor, Y%
   specific; specific IR Z; residuals show [no] significant autocorrelation; residual PC1 explains W%."
7. **Tests** — `test_attribution.py` (pure stats + the period reconciliation + the §4 surprise z-score
   and ranking on known inputs), and extend the cube/pivot suites (`test_risk_measures.py`,
   `test_pivot_app.py`): the new measures foot (Σ children = parent), `Realized PnL = Σ factor
   contributions + Specific PnL` ties out, and the allowlist accepts them.

**Measures live in the cube — slice, drill, and what-if for free.** This is the general principle, not
just for attribution: additional measures, including the more complex ones the quants define, are
implemented as cube measures by default rather than one-off scripts, whenever it makes sense to apply them
to *sliced, drilled, or modified* data. A cube measure composes with everything the cube already gives —
slice by any dimension (factor, sector, name, date, scenario set), drill parent→child, and recompute under
**what-if** (modified weights, added/dropped names) and **stress** (shocked vols/correlations) — so a
measure defined once is immediately available across every view and every hypothetical, not re-coded per
context. The exception is the genuinely non-cube statistics (period linking, autocorrelation, regression,
bias) — time-series reductions the cube can't express — which stay in the Python layer. Everything else
that benefits from slice / what-if belongs in the cube.

**Granularity, stated so it isn't a hidden inconsistency.** The cube attribution is **monthly, on the
as-of weights** the `positions` frame carries — additive, drillable, foots at every level. The **daily
drifting-weight realized NAV + Carino linking** is the API headline (the realistic period *return*). Two
consistent views doing different jobs: the cube is the reconciling P&L-contribution drill; the API is
the period-return headline plus the skill statistics.

## Have vs build — the whole request

Most of the ex-ante half already exists in the model; the new work is the attribution measures and the
connection.

| Part of the request | Already in place | To build |
|---|---|---|
| **PnL attribution** by factor + residual (ex-post, T→T+1) | factor returns, exposures, positions, prices; the identity `R_p = Σ x_k·f_k + u_p` | realized engine (unit NAVs, drifting weights); cube measures `Factor contribution` / `Specific PnL` / `Realized PnL`; the `specific_returns` frame; `/pnl_attribution` + panel |
| **"Are residuals large / correlated?"** | predicted specific vol (`SpecificVar`); the residual itself (the model's own number) | IR + var-ratio stats, residual-vs-factor regression, lag-1/2 autocorrelation, bias stats (Ljung-Box / PCA optional); `/pnl_attribution/residual` |
| **Risk decomposition at T** (factor & position contribution to total risk) | the cube already computes it — marginal / incremental contribution to Total VaR; Risk HHI is built from these shares | surface it as a tidy "contribution to risk at T" table aligned to the attribution buckets |
| **Expected forward distribution** (vols + correlations; stress / replay) | Scenario VaR/ES over full history (empirical), event-replay sets, hypothetical shocks, `/stress` (vol shocks) | the ±σ base band per row; a **correlation-stress mode** for the stressed band — `/stress` shocks vols only today |
| **The linkage / reconcile** (risk at T vs PnL T→T+1; within-range; surprise) | `/backtest` (realized-vs-VaR exceptions, Kupiec / Basel — his VaR analogy); the what-if risk math | the temporal pairing; per-factor / position surprise z-score; surprise ranking; the reconcile band chart; `/pnl_attribution/linkage` |
| **The daily loop** (decompose → check limits/intended → hedge → attribute) | `/limits`; style-drift & what-changed (intended vs not); what-if & stress (the hedge step) | no new engine — the linkage report closes the loop end to end |

## Caveats

- 13F lag + quarterly. Weights are as-of and stale between filings; intra-quarter trades are invisible.
  Drifting weights capture market drift but not trades, so some "residual" is timing error from holding
  stale weights. Bounded by turnover, disclosed.
- Price-only. Dividends out on both sides; understates total return; disclosed.
- In-sample reconciliation. `ε_i` is the model's own residual, so realized = factor + specific is an
  identity, not an out-of-sample test. The tie-out proves the arithmetic; the diagnostics in §2 test
  the model.
- No benchmark, so absolute not active attribution (Step 13).
- Contract change. The residual becomes a 7th frame, so this needs a rebuild and updates the six-frame
  contract everywhere it's documented (CLAUDE.md, the frame table, the cube docstring). Deliberate — the
  price of the cube-native drill. The existing six frames and their numbers don't change.
- Additive vs compounded. The cube drill sums daily/monthly P&L contributions (arithmetic, foots at
  every level); the API headline compounds to the geometric period return (Carino-linked). Both are
  correct for their job; the doc says which number is which so they're never confused.

## Decisions

- Cube-native from the outset (Chris's call): additive attribution measures + parent/child drill in the
  pivot grid, not a static table.
- Measures live in the cube by default — additional / quant-defined measures are implemented as cube
  measures whenever they should apply to sliced, drilled, or modified (what-if / stress) data, so they
  compose with every view and hypothetical; only non-cube statistics (linking, autocorrelation,
  regression, bias) stay in Python.
- Linkage to the ex-ante risk decomposition is in scope (Chris's 2026-06-29 addition): pair the risk
  decomposition at T with the PnL attribution over T→T+1, and flag the surprises (§4). Reuses the
  existing marginal-VaR / `/backtest` / `/limits` machinery, not a new engine.
- The linkage's headline visual is a band/dot reconcile chart (realized contribution vs its
  start-of-period ±σ band, base + stressed). Full fidelity needs a **correlation-stress mode** added to
  `/stress` (today it shocks vols only) so the stressed band reflects correlations → 1. A mock is
  embedded in §9 of the roadmap page for Chris's sign-off.
- Residual persisted as a 7th frame `specific_returns` (the un-squared `:640` value). Rebuild required;
  six-frame contract → seven.
- Realized-return headline: bottom-up unit NAVs from the name series, computed in the API (the cube has
  no live track record, and linking/diagnostics aren't cube-expressible). The cube carries the additive
  P&L-contribution attribution + drill.
- Drifting (buy-and-hold) weights, re-anchored each filing, for the API realized headline; the cube
  attribution is on the as-of monthly weights.
- Price-only, dividends out on both sides.
- Period linking: Carino, in the API headline.

## Questions to Chris

He endorsed the approach (2026-06-29) and refined it (2026-06-30 — folded in: drop "projected", the
known-vs-unknown framing, keep the stats cheap). These four are still open:

1. Default reporting period — trailing-12m + since-inception proposed. Want a fixed quarterly grid too?
2. Factor grouping in the hero chart — Market / Style(grouped) / Specific, or break out the top style
   factors individually?
3. Residual large/correlated thresholds — the green/amber/red bars for the specific-vol ratio, the
   lag-1/2 autocorrelation, and the residual-vs-factor R², so the verdict is a clean RAG like `/limits`.
   Suggest starting loose and tightening once we see the book's distribution.
4. Dividends — is price-only fine for the POC, or do you need a total-return headline? That needs a
   dividend source, which we don't have on free data today.

## Effort / risk

~1.5–2 weeks. More than the precompute-only version because B touches the builder (7th frame + rebuild),
the cube (two joins, three measures, the allowlist) and its tests, on top of the diagnostics, the panel
and the precompute. The cube work is the main risk: the Atoti measure/join signatures are
version-sensitive (CLAUDE.md flags this), so the new measures need verifying against the installed SDK,
and the foot-to-parent reconciliation must be tested at every hierarchy level. Second risk is
price-series coverage on the older book's delisted names, which the coverage report surfaces. The
contract change (six frames → seven) is small but real — it ripples into CLAUDE.md and the frame docs.
The §4 linkage adds little to the estimate — it reuses the marginal-VaR / `/backtest` machinery; the new
work is the side-by-side view, the z-score, and the surprise ranking.
