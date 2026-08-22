# Price VaR — historical simulation on stock prices, and the bridge to the model

Plan agreed 2026-08-22 (questions and answers in the session log). **Built 2026-08-22** — steps
1-5 of the Order of work below (step 6, the deck slide, is separate and not part of this build).
See CLAUDE.md's "Price VaR — historical simulation on raw stock returns, and the Model-vs-Price
bridge" section for the full writeup; the measured highlights:

- Builder: `data/stock_returns.parquet` (9th, optional frame), 96.7% of 5,197 coverage names
  priced, 10.77M sparse rows, 2016-01-04 → 2026-08-13. Two new DQ checks (coverage, return
  sanity — flags 6,799/10.77M rows with |r| > 50%, disclosed, not chased).
- Cube: `build_cube` 19.66s → 27.41s (+7.75s) in-process on the 124-book frames; JVM RSS ~7.2G
  post-build. Soros/HistFull/2026-06-30: `Price VaR 99` 2.99% vs `Total VaR 99` 3.58%.
- `/var_bridge` (Soros/2026-06-30/HistFull): T0 3.524% → T1 3.576% (+0.053%) → T2 3.486%
  (−0.090%) → T3 2.986% (−0.500%) → T4 2.986% (+0.000%) — **exposure drift is the largest term**
  on this book/date; coverage is ~0 (every held name priced and covered). Numpy verification
  diff ~7e-18.
- Scoped down, disclosed: no cube-native `PriceDays` day-fact table (would cost ~15M rows keyed
  by ~5,200 positions instead of 23 factors) — `Price PnL at day` was not built; the day-by-day
  coverage series needed by `/var_bridge` and the UI sparkline is served by reading the array
  measures directly in the API (the `/backtest`/`/drawdown` vector-unpack idiom), not the
  generic `/pivot` Day/DaySet machinery. The per-name `disagreements` table's `likely_driver`
  is a magnitude HEURISTIC (coverage is exact; specific-risk-dropped vs
  exposure-drift-or-distribution is not a true per-name T2/T3 split) — disclosed in the payload.
- Tests: `test_price_var.py`, 11/11 passing against the live backend (incl. a live LLM call,
  `RUN_LLM=1`), plus no regressions in `test_risk_measures.py`/`test_whatif.py`/`test_stress.py`/
  `test_model_vol.py`/`test_analysis.py`/`test_dq.py`/`test_build_frames.py`/
  `test_contributions.py`/`test_limits.py`/`test_backtest.py`/`test_pivot_app.py`/`test_ask.py`.
  Frontend: `npx tsc --noEmit`, `npx vitest run` (59 passed), `npm run build` all clean; the
  Model vs Price lens verified end-to-end with a headless Playwright check against the served
  SPA.

## The idea

Every VaR in the cube today comes from the factor model: `Scenario VaR 99` simulates the book on
historical **factor** returns with today's exposures; `Total VaR 99` adds a Gaussian, diagonal
specific block in quadrature. The **Price** family simulates the same book on historical **stock**
returns — no model at all — and the bridge explains the difference term by term.

The bridge works because the two are tied by an identity. For every covered name on a regression
date, `r_i = L_i·f + u_i` exactly (u is the model's own residual). So the model and the price
simulation differ only through choices the model makes, and each choice is a computable term.

## Naming

New family, prefix **Price**. Existing names untouched. In prose the two families are *Model VaR*
and *Price VaR*.

| Price family | Mirrors |
|---|---|
| `Price PnL vector` | `Scenario PnL vector` |
| `Price VaR 95` / `97.5` / `99`, `Price ES 97.5` / `99` | the Scenario ladder |
| `Price worst loss`, `Price mean PnL`, `Price PnL vol` | same |
| `Marginal Price VaR 99` (tail-day Euler, sums exactly) | `Marginal Total VaR 99` |
| `% of Price VaR 99` | `% of Total VaR 99` |
| `Incremental Price VaR 99` (book minus book-without-member) | `Incremental Total VaR 99` |
| `Marginal Price ES 97.5` | `Marginal Scenario ES 97.5` |
| `Price PnL at day` (+ markers) | `PnL at day` |
| `Price coverage` | new — weight share with a real return, as a vector by day |
| `Price coverage at tail` | new — coverage on the book's tail day |
| `… $` twins via `DOLLAR_MEASURES` | — |

The marginal and incremental conventions are copied from the model family exactly (same tail-day
Euler, same drop-the-member incremental) so the two families compare like for like.

## Data

A ninth, optional frame from the builder: **`stock_returns`** (Date, Position) → daily simple
return, every coverage name, from the same cached Stooq/Yahoo prices the builder already holds.
Absent on v1 data; the cube and API degrade like they do for `specific_returns`. ~5,200 names ×
2,618 days ≈ 13.6M values.

Missing days (young listings, delistings): **zero-fill and disclose**. A missing return means the
name contributes nothing to the book's P&L that day; `Price coverage` reports the weight share that
was actually priced, day by day, and the lens flags tail days where coverage is low. Never backfill
from the model — that would make Price VaR depend on the model it is meant to check.

## Cube

The same vector engine as the Scenario family, second copy:

- `StockReturns` table keyed (PriceSet, Position) → `ReturnVec`, aligned to the factor-return
  calendar of each set. Sets: **HistFull + every Evt:\* window** (same dates as `Scenarios`);
  Hypo:\* are sigma-shocks on factors and do not apply. A `PriceSet` hierarchy is the switch,
  like `ScenarioSet`/`DaySet` — and carries the same "slice to one set" rule.
- `Price PnL vector = Σ_i w_i · ReturnVec_i` by `OriginScope({Position})` over the joined Positions
  weight, so it drills by sector / issuer / name, takes a what-if branch, gets `$` twins and the
  Day path for free.
- Memory: one vector per name per set; HistFull ≈ 109 MB of doubles, the windows are small.
- Numpy twin in the API (`_price_var_from_frames`) recomputed on every `/var_bridge` call and
  reported as `verification`, like `/contributions`.

## The bridge

Five numbers in a fixed order; each step changes one thing, so the differences sum to the whole gap
by construction (VaR is not additive, but sequential differences are).

| Step | Number | What changes | The term |
|---|---|---|---|
| T0 | `Scenario VaR 99` | — | factor-only, today's exposures |
| T1 | `Total VaR 99` | add Gaussian diagonal specific | **specific risk dropped** (T1 − T0) |
| T2 | full-sim VaR: HS of `Σ w_i (L_i(d)·f_t + u_i,t)` | realised residual vectors replace the Gaussian block | **specific distribution**: fat tails + residual correlation (T2 − T1). Residual correlation is the missing-factor signal. |
| T3 | Price VaR on covered names | each day's own loadings replace today's: `r_t − (L(d)f_t + u_t) = (L(t) − L(d))·f_t` | **exposure drift** (T3 − T2) |
| T4 | `Price VaR 99` | add names with prices but no loadings | **coverage** (T4 − T3) |

T2 and T3 are API computations from the frames (`specific_returns` already holds `u` daily); T0,
T1 and T4 come from the cube. Per-name version of the same table for the top disagreements:
`Marginal Total VaR 99` vs `Marginal Price VaR 99`, with the bridge term that explains each gap.

## API

- `GET /var_bridge?date=&book=&set=&alpha=` — the five numbers, four terms, coverage on the tail
  day, the per-name disagreement table, `verification`.
- Price measures on the `/pivot` allowlist, `SCEN_DEP`-style dependency on `PriceSet`
  (`PRICE_DEP`), `$` twins, Day-path entries; `/dims` publishes `price_dependent`.
- `POST /var_bridge/analysis` — a CHRIS_VOICE read (`BRIDGE_SYSTEM = CHRIS_VOICE + …`): lead with
  the largest term; residual correlation → "a factor is missing"; exposure drift → rotation vs
  re-pricing (hand off to `/drift`); coverage → the unpriced list.
- `/analysis` and `/ask` grounding: both families citeable, the bridge terms named.

## UI

- Vite lens **Model vs Price**: the headline pair, the bridge waterfall (five bars, four labelled
  steps), the per-name scatter (Marginal Total VaR vs Marginal Price VaR, dot size = weight,
  coloured by the dominant bridge term), the coverage-by-day sparkline, the risk-manager read.
- Pivot: Price measures in the picker; saved views `Model vs Price — book`, `— by sector`.
- Notebooks: headline pair, bridge table, top disagreements, in both demo notebooks.

## Tests

- Identity: on covered names at a regression date, T3 rebuilt from `L·f + u` equals the price
  simulation to float precision → the bridge closes exactly.
- Euler: `Σ Marginal Price VaR 99 == Price VaR 99`; incremental sub-additive; `%` sums to 1.
- Coverage: zero-fill accounted — `Price coverage` × book = priced weight, day by day.
- Set semantics: Evt window vectors are slices of HistFull.
- `$` twins: `Price VaR 99 $ == Price VaR 99 × Book MV`.
- API: `/var_bridge` terms sum to T4 − T0; `verification` diff ≤ 1e-12.

## Order of work

1. Builder: `stock_returns` frame (+ DQ check: coverage share, return sanity).
2. Cube: `StockReturns` table, `PriceSet`, the vector measures, marginals, `$` twins, Day path.
   Measure start-up and memory before/after (the start-up floor is JVM ingest; a 109 MB table
   should add ~1 s).
3. API: allowlist, `/var_bridge`, verification twin, tests.
4. Vite lens + saved views; notebook cells.
5. `BRIDGE_SYSTEM` read + grounding.
6. Deck: one slide — "Two VaRs, one identity" — the waterfall.

## Open points

- Simple vs log returns for `stock_returns`: simple (P&L is linear in weights; matches the
  attribution precompute's `r_i`).
- Corporate actions: the cached prices are Stooq/Yahoo adjusted closes; splits are handled,
  dividends are in the adjusted series. Disclose, don't re-derive.
- The what-if branch moves Weight only, so a hypothetical's Price VaR reprices on the same return
  vectors — correct, and the same caveat as the model family.
