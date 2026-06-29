# Plan — PnL attribution & factor-model validation (projected vs realized, residual diagnostics)

Status: SPEC — endorsed by Chris (2026-06-29: "that looks reasonable"), with one addition: link the
attribution to the **risk decomposition at the start of the period** (§4). New risk-tooling step for
Chris's request (Soros risk review): a report showing the factor
model's projected PnL over a period vs the book's actual realized PnL, broken down by factor and by
residual, then a check on whether the residual is large and whether it's correlated. Uncorrelated
residual means the PM's specific bets are genuine and diversified, not a hidden systematic bet.

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

1. Projected PnL from the factor model vs actual realized PnL, over a period → the core attribution:
   realized book PnL split into a factor-explained part and a residual, reconciling exactly. §1.
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
stock will go. So "projected PnL" isn't a return forecast — it's the factor-explained part of the
realized return:

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

The residual `u_p` is the part the model can't explain — the PM's stock-specific bets. Chris
wants to know if it's large and if it's correlated.

### Is it large?

- Size — specific return as % of total realized; cumulative specific PnL.
- Realized vs predicted specific vol — realized `std(u_p)` against the model's predicted specific vol
  from `SpecificVar`. Ratio near 1 means the diagonal risk block is calibrated; well above 1 means the
  model under-forecasts idiosyncratic risk.
- Explained share — `1 − var(u_p)/var(R_p)`. Low isn't automatically bad: it's either diversified
  stock-picking or a missing factor, which the correlation tests sort out.
- Information ratio — annualized `mean(u_p)/std(u_p)`, the stock-picking number. Positive and high means
  the residual is a source of return, not just noise.

### Is it correlated? — three tests

This is what "are the residuals correlated" actually means, and "uncorrelated = good PM" reads onto all
three:

1. Serial autocorrelation of `u_p` over time — AR(1) coefficient plus Ljung-Box (`_ljung_box`, pure).
   Significant positive autocorrelation means the alpha is a persistent trend, i.e. a slow unhedged
   systematic bet dressed up as skill, not independent stock-picking. This is the most direct read of
   Chris's question.
2. Residual vs factors — correlate / regress `u_p` on the factor returns `f_k`, and on the book's own
   factor bets `x_k`. Should be near zero. A persistent loading means exposures are stale or
   mis-estimated (the 13F lag), or a factor is leaking into "alpha" — the PM is being paid for beta and
   calling it skill.
3. Cross-sectional — PCA on the name-level residual panel `ε_i(t)` (`_resid_pca_share`, pure). If the
   top principal component explains a large share, the names' specific returns move together, which
   means a common factor the model misses (a theme, sector, or crowded bet). Small PC1 share means the
   bets are genuinely independent.

The read for Chris: low autocorrelation, low residual PC1 share, positive IR → genuine
diversified stock-picking, the PM is doing well. High autocorrelation or a dominant residual PC → the
"alpha" is really an unmodelled common bet → tell the PM, add the missing factor, or hedge it.

### Bias statistics

The standard Barra/MSCI check. Standardize each realized return by its predicted vol, `z_t =
realized/predicted-vol`; the bias statistic is `B = std(z)` over a rolling 12m window (`_bias_stat`,
pure). `B ≈ 1` calibrated, `B > 1` risk under-forecast, `B < 1` over-forecast. Run it for the total
book, each factor, and the specific block. The specific-block bias stat is the formal version of "are
the residuals bigger than the model said", next to the eyeball size check above.

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
   the Carino-linked period return, and the residual diagnostics (IR, AR1 + Ljung-Box, PCA share, bias
   stats, concentration HHI, hit rate). Writes `data/pnl_attribution.parquet`. Pure stats split into
   importable functions (`_carino_link`, `_bias_stat`, `_ljung_box`, `_resid_pca_share`,
   `_concentration_hhi`, `_info_ratio`) for unit tests, same as `_kupiec_lr` / `_max_drawdown`.
4. **API.** `GET /attribution?from=&to=&book=&by=factor|group|sector|name` — the period headline +
   reconciliation + the cumulative series for the chart (Carino-linked, from the precompute). `GET
   /attribution/residual?from=&to=&book=` — the §2 diagnostics with plain verdicts. `GET
   /attribution/linkage?T=&horizon=&book=&set=` — the §4 pairing: the risk decomposition at T (marginal
   Total VaR by factor/position, from `_book_inputs` / `_risk_from_weights` — the same what-if math the
   cube reports) next to the realized PnL over T→T+1, the per-factor/position surprise z-scores, the
   surprise ranking, and the book-level within-band check (reusing the `/backtest` exception logic).
5. **UI — the pivot grid is the drill-through.** The attribution measures slot into the existing pivot
   panel, so click-to-expand parent→child is native, no bespoke widget. The §4 linkage is the same grid
   with two columns (risk at T, PnL over T→T+1). `render_attribution` adds the hero stacked-area chart,
   the period selector, the residual-diagnostics sub-panel, and the surprise ranking + within-band book
   check. Tufte/Few throughout.
6. **LLM fold-in** — `/analysis` (and `/ask`'s grounding) lead with the headline: "X% factor, Y%
   specific; specific IR Z; residuals show [no] significant autocorrelation; residual PC1 explains W%."
7. **Tests** — `test_attribution.py` (pure stats + the period reconciliation + the §4 surprise z-score
   and ranking on known inputs), and extend the cube/pivot suites (`test_risk_measures.py`,
   `test_pivot_app.py`): the new measures foot (Σ children = parent), `Realized PnL = Σ factor
   contributions + Specific PnL` ties out, and the allowlist accepts them.

**Granularity, stated so it isn't a hidden inconsistency.** The cube attribution is **monthly, on the
as-of weights** the `positions` frame carries — additive, drillable, foots at every level. The **daily
drifting-weight realized NAV + Carino linking** is the API headline (the realistic period *return*). Two
consistent views doing different jobs: the cube is the reconciling P&L-contribution drill; the API is
the period-return headline plus the skill statistics.

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
- Linkage to the ex-ante risk decomposition is in scope (Chris's 2026-06-29 addition): pair the risk
  decomposition at T with the PnL attribution over T→T+1, and flag the surprises (§4). Reuses the
  existing marginal-VaR / `/backtest` / `/limits` machinery, not a new engine.
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

He endorsed the approach (2026-06-29) and added the §4 linkage. These four are still open — he didn't
address them:

1. Default reporting period — trailing-12m + since-inception proposed. Want a fixed quarterly grid too?
2. Factor grouping in the hero chart — Market / Style(grouped) / Specific, or break out the top style
   factors individually?
3. Residual large/correlated thresholds — the green/amber/red bars for the specific-vol ratio, the
   Ljung-Box p-value, and the residual PC1 share, so the verdict is a clean RAG like `/limits`. Suggest
   starting loose and tightening once we see the book's distribution.
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
