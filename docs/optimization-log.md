# Cube & API optimization log (2026-08-14 → 2026-08-21)

Every optimization applied to the 124-book cube and its API across the three rounds, with the
measured before/after, what was tried and rejected, and what is still open. Sources: the
per-round docs (`cube-optimization-plan.md`, `cube-opt-round2-*.md`, `api-bench.md`) and the
harness JSONs in `docs/` (`cube_bench_*.json`, `api_bench_merged_20260815.json`,
`api_bench_round4_20260821.json`). Bench book is
Vanguard (3,635 names, the largest) unless stated; "quiet box" = loadavg < 10 with nothing else of
ours running. The box is shared (20 cores, 62 G), and the JVM's parallel scans are load-bound, so
timings without a loadavg next to them are indicative only.

## Headline

| | before | after | where |
|---|---|---|---|
| `build_cube` (start-up) | 56–64 s | **18.9 s** quiet box | round 2 + 3 |
| Per-day scenario path, Vanguard HistFull book | 25–50 s, +14 G heap; × Sector fails | **0.5 s**; × Sector 2.1 s (199k rows) | round 2 + 3 |
| `/dims` | 15–28 s every call | **10 ms** (prewarmed) | round 2 + 3 |
| `Scenario VaR 99` by Manager, no Date | 60 s idle / fails loaded | **1.1 s** (Date defaulted, disclosed) | round 1 |
| Risk HHI by all 130 sets | fails at 11 s | **0.48 s** | round 1 |
| `/calibration` (Vanguard) | 120–370 s | **7 s** cold / 0.04 s warm | round 3 |
| `/trends` book path (Vanguard) | 34–36 s every call | **13 s** cold / 0.01 s warm | round 3 |
| `/pnl_attribution/residual` | 16–30 s | **1.4 s** cold / 0.3 s warm | round 3 |
| Idle JVM after a heavy burst | held ~18 G | 8 G after ~7 min | round 1 |
| Every `/pivot` (the fixed book-count tax) | +0.35 s on every call | **gone** (memoized) | round 4 |
| `/pivot` repeat views | full re-aggregation | **0.00–0.03 s** (bounded LRU) | round 4 |
| `/pnl_attribution/linkage` | 1.1–1.9 s | **0.2–0.6 s** | round 4 |
| `/whatchanged` (Vanguard) | 1.5 s | **0.9 s** — and it was returning the WRONG per-book exposures | round 4 |

Every routine sliced query (the shapes the Vite UI issues) was already 60–500 ms cold / 20–50 ms
warm before the programme and is unchanged; nothing regressed beyond harness noise, and every
number is pinned by the accuracy gates (`test_model_vol` 5e-10 tie-outs, `test_contributions`,
`test_stress`, `test_whatif`, `test_risk_measures`).

## Round 0 — the 124-book expansion's own perf changes (2026-08-14, before the programme)

| # | Optimization | Mechanism | Measured |
|---|---|---|---|
| 0.1 | **Explicit JVM heap + query limit** (`bc1ae26`) | `BARRA_CUBE_XMX` (default 32g; the JVM ran on the 25 %-of-RAM default ≈ 15.6g), `BARRA_CUBE_XMS` (2g measured optimum — 12g worse), query time limit 30 → 120 s. | the 124-book cube fits; cross-book queries get headroom (all-123-books-in-one-cell still trips the 20 M-row guardrail — slice or loop) |
| 0.2 | **HTTP retries in the builder** (`b21639d`) | `_get`/`_post_json` retry transient timeouts / 429 / 5xx — one SEC timeout used to kill a ~60-min 124-book pull. | no more restarts of the full pull |
| 0.3 | **`UNIVERSE_CAP` 35k → 200k** (`b21639d`) | `sec.head(UNIVERSE_CAP)` truncates silently and order-dependently; raised well above the measured ~5,197 resolved securities. | correctness, not speed — listed because it gates every number above |

## Round 1 — query pathologies (2026-08-14, `cube-optimization-plan.md`)

Built the harness first: `cube_bench.py` — 44 queries over every measure family, cold + warm,
JVM RSS sampled at 200 ms, per-stage build timings (`BARRA_CUBE_TIMINGS=1`), `--diff`.

| # | Optimization | Mechanism | Measured |
|---|---|---|---|
| 1.1 | **PIT sets off `ScenarioSet`** (Step 2) | The 123 `PIT:*` truncated-history sets moved to their own `PITSet` hierarchy with the honest-vol pair mirrored onto it; `ScenarioSet` keeps the 7 real sets. API contract unchanged (PIT names still addressable by name; non-mirrored measures 400 instead of a wrong number). | every group-by-ScenarioSet 130 → 7 members: `hhi_by_set_all130` fails@11 s → 0.48 s; `model_vol_by_set` 1.05 → 0.14 s; `var99_by_set` 0.59 → 0.15 s |
| 1.2 | **Slim Positions load** (Step 4) | `read_pandas` gets only (Date, Book, Position, Weight). | positions stage 6.6 → 4.9 s, −185 MB JVM |
| 1.3 | **JVM memory hygiene** (Step 5) | `-Xms2g -XX:G1PeriodicGCInterval=300000` — idle heap returns to the OS between bursts (was: manual service bounces). | idle RSS after a heavy burst 18 G → 8 G |
| 1.4 | **Date-context default in `/pivot`** (Step 6) | Scenario measure × Manager on an axis × no Date = one P&L vector per book over the whole calendar. `_needs_date_default` injects the latest COB and says so in the payload's `warning`. Policy, not engine. | 60 s idle / 500 s loaded → 1.08 s |
| — | Rejected: pad scenario vectors to N (Step 1) | atoti 0.9.15 array helpers PROPAGATE NaN (mean/std/quantile go NaN; `array.len` reads the padding) — permanently off on this SDK. | — |
| — | Rejected: named shared sub-expressions (Step 3) | Implemented, measured, reverted — ActivePivot already dedupes them. | — |
| — | Rejected: aggregate provider (Step 7) | Later proven impossible: every hierarchy worth providing on (Book, ScenarioSet, DaySet, Day) is an analysis hierarchy from a partial join — "not supported". | — |

Honest cost of round 1: build time went UP ~8 s (the PIT table is a second vector load) — traded
knowingly for a query that used to fail.

## Round 2 — start-up, per-day path, `/dims` (2026-08-15, three parallel agents)

| # | Optimization | Mechanism | Measured |
|---|---|---|---|
| 2.1 | **Model first, data last** (`cube-opt-round2-startup.md`) | `build_cube` defines joins/hierarchies/parameter dims/measures on SEEDED tables (`head(1000)`) and bulk-loads the full frames at the very end — every publish used to re-index whatever the tables held. | the largest single start-up win: 38.8 → 22.8 s |
| 2.2 | **One measure publish** | `_DeferredMeasures` + `tt.mapping_lookup(check=False)`: ~55 per-measure republishes and ~120 `m[name]` round-trips become one `Measures.update` batch (four flushes where an array helper needs a published measure). Same DAG, same order, same numbers. | 57.4 → 48.4 s (with 2.1: 22.8) |
| 2.3 | **Cheap start-up wins** | early publish of the vector measures, dropped dead round-trips, two JVM knobs tried and removed as cargo. | 48.4 → 40.8 → 38.8 s |
| 2.4 | **Persisted attribution prep** (optional) | `barra_persist_attribution.py` writes the `FactorPnL` column + `specific_pnl` frame so the cube skips ~3.8 s of pandas per start. Regenerate/delete after any rebuild. | −3.8 s when present |
| 2.5 | **Days as facts, not array indices** (`cube-opt-round2-scenarioday.md`) | `build_scenario_days` explodes every (set, factor) vector into a `ScenarioDays` table (DaySet, Factor, Day, DayDate); `PnL at day` = additive sum-product over it with `Net exposure` LIFTED over the day hierarchies (`tt.total`) so the book's 23 exposures are computed once, not per day. Date read off the `DayDate` LEVEL. Old `ScenarioDay` parameter hierarchy kept for the A/B. | book path 25 s / +14 G → 1.5 s cold / +0.9 G (quiet box); COVID 0.09 s; day × Sector 12.7 s (was: fails). Warm < 0.5 s and sector < 5 s NOT met — SDK floor ~0.5–0.8 ms per fact-joined member |
| 2.6 | **`/dims` off the fan-out** (`cube-opt-round2-dims.md`) | Nine cheap dimensions enumerated concurrently; Manager (the 24 s one — Book is an un-mapped partial-join key against 11.6 M Positions rows) answered by candidate names off the tiny Managers table + concurrent max_rows=1 existence checks; result cached on cube identity. A frames-derived first attempt was redirected on the cube-native rule. | 15–28 s every call → 1.4–3.2 s once per process, ~8 ms cached |
| — | Rejected: `Table.load_async`/threads for the bulk load (see 3.4), one flat `/dims` pool (worse than nested), lifting Security/PositionRank into the day exposure (8%, costs the sector drill) | | |

## Round 3 — vector plan, arrow cache, prewarm, API loops, legacy prune (2026-08-15, four parallel agents + orchestrator)

A finding made while scoping this round drove its shape: **the level-plan day path is
load-bound** — the same Vanguard query measured 1.5 s (loadavg 4), 3 s (loadavg 9), 7 s (fresh
service), 13–15 s (service an hour old). So item 3.2 stopped paying the per-member scan rather
than tuning it.

| # | Optimization | Mechanism | Measured (quiet box) |
|---|---|---|---|
| 3.1 | **Fast day path on the `/pivot` allowlist** | `Day`/`DayDate`/`DaySet` dims + `PnL at day` and three chart markers (`VaR line at day`, `Worst pnl at day`, `Worst date at day (epoch)` — book constants lifted over the day hierarchies); `DAY_DEP` set with its own DaySet-context warning (payload + Vite field list via `/dims.day_dependent`) and folded into the Date default; `/dims` enumerates the day dims off the ScenarioDays table (not the 16 s per-member fan-out); Day/DayDate filter members typed; `/ask` tool description + `ASK_SYSTEM` grounded on it; both COVID chart views re-authored on it. | COVID chart view 3.9 s → 0.5 s; HistFull × Sector: fails → works |
| 3.2 | **Vector plan for the canonical Day shape** | `_day_vector_shape` / `_day_vector_records`: rows `Day[,DayDate][,one breakout]`, single Date/Manager/DaySet, all measures in `DAY_DEP` → read the cube's own `Scenario PnL vector` + dates dual (per breakout member) + ONE single-cell markers query, unpack in numpy into records identical to the level plan; every other shape falls through; `plan=` param, payload `plan` key. Honest caveat: an API-side reshape of a cube vector (the `/backtest`/`/drawdown` idiom), not a cube-native level — disclosed. | Vanguard HistFull book 11.5 → 0.53 s; +3 markers 21 → 0.50 s; × Sector 29 → 2.1 s (199k rows); Soros 1.0/1.6/4.3 → 0.52/0.47/0.84 s; COVID ~0.45 s any book. Both round-2 gates MET; invariant to book size, measure count and load |
| 3.3 | **Arrow cache for the bulk load** | ~45 % of `Table.load(DataFrame)` is Python pandas→arrow; done once with atoti's own converter into `data/.cube_cache/<table>.arrow` (+ JSON key on source parquet mtime/size/shape/dtypes), `ArrowLoad` thereafter. Stale/corrupt → regenerate; any failure → plain load; `BARRA_CUBE_ARROW_CACHE=0` disables. First build after a rebuild writes it (~790 MB, gitignored). | `load.bulk` 12.0 → 7.4 s |
| 3.4 | **Persisted attribution regenerated** | `barra_persist_attribution.py --verify` (bit-identical) so 2.4 is actually in effect on the live data. | `prep.wloading_merge` 2.13 → 0.00 s, `prep.attribution_pandas` 1.57 → 0.1 s → **`build_cube` 26.9 → 18.9 s** |
| 3.5 | **Prewarm `/dims` + `/dq`** | `_prewarm()` daemon thread after `cube ready`; best-effort, never fails start-up. | first user call 1.4–3 s → 10 ms; `/dq` 6 s → 24 ms |
| 3.6 | **API-loop de-scanning** (`api-bench.md`) | `_frame_rows_by`: one `groupby().indices` per frame per process replaces 126× full-frame masks over the 11.6 M / 6 M-row frames in `_pred_book_vols` and `_name_attr` (daily 13 M-row window via `searchsorted`); PIT cube queries concurrent; `/trends` book path memoised per (set, book, measures) with a concurrent cold fill; `/dq` memoised. Identity gate: 66/68 payloads byte-identical (2 pre-existing `/span` set-order diffs). | calibration@Vanguard 120–370 → 7.0 s cold / 0.04 warm; @Soros 94 → 5.6 s; trends_book@Vanguard 34/36 → 13.0 / 0.01 s; residual 24–30 → 1.4 / 0.33 s; names 6.5 → 0.3 s |
| 3.7 | **`api_bench.py`** | The endpoint-level harness the cube harness didn't cover: 34 requests × 2 books, cold + warm, loadavg in meta, `--diff`, `--payloads`/`--same` identity gate. `docs/api_bench_merged_20260815.json` is the merged baseline. | — |
| 3.8 | **Legacy `ScenarioDay` path retired** | Every consumer migrated to Day/DayDate (Streamlit `scenario_pnl` feed migration mirrors ScenarioSet → DaySet; both demo notebooks' idiom cells; `test_notebook`, `test_pivot_app`, Vite `ChartMode` fixtures; `cube_bench` `day_path_*` twins), then `ScenarioDay` + the five `Scenario … at day` measures PRUNED from `DIM_NAMES`/`MEASURE_NAMES`/`SCEN_DEP` (400). The cube still defines them for the A/B. | the one UI/`/ask` path that could hit the 120 s timeout / BadArgumentException is gone |
| 3.9 | Housekeeping | seven merged agent worktrees/branches removed. | — |
| — | Rejected: `Table.load_async` (it is `to_thread(load)`; the JVM serialises datastore commits — 13.3 vs 12.5 s), threads (17.8 vs 16.9), direct `ParquetLoad` (Exposures 6.4 vs 3.1 s; Positions 286 s and drove loadavg to 191), serve-before-load (needs a guard on every route incl. `/dims`, no query completes sooner) | | |

## Round 4 — the open TODO list, items 1–7 (2026-08-21)

One session, no agents: the round-3 TODO's first seven items, in order. Two of them (4, 6) were
questions rather than fixes, and both answered **"no lever where we thought it was"** — recorded
here with the measurement, because a measured no is worth as much as a yes. Two of the biggest
wins were not on the list at all; they came out of measuring it — a fixed 0.35 s tax under every
pivot the grid issues, and a real book-scoping **bug** in `/whatchanged`.

Bench: `api_bench.py`, 34 requests × 2 books, cold then warm, same service before and after
(before loadavg 0.07, after 0.59 — the after run is the pessimistic side). Payload identity gate
(`--same`): **64 of 68 byte-identical**; the four that differ are the two intended changes.

| # | Item | Mechanism | Measured |
|---|---|---|---|
| 4.1 | **Repeat-view LRU on `/pivot`** (TODO 1) | A base-scenario pivot is a pure function of (axes, measures, slicers, totals, plan) and the cube never changes in-process, but nothing memoized it. Bounded LRU (`BARRA_PIVOT_CACHE`, default 48) keyed on that tuple + `id(cube)`; hypothetical branches (what-if trades / StressShock) bypass it; payloads over 25k records are served but not retained; callers get a shallow copy so `/ask`'s truncation can't poison it. | `pivot_var_trend_by_date@Vanguard` warm 2.37 → **0.00 s**; every other pivot warm 0.41–0.89 → 0.00–0.03 s. Cold is unchanged by design |
| 4.2 | **The 0.35 s tax under every pivot** (found measuring 4.1) | `_validate_pivot` called `_multi_book_cube()`, which ran `nunique()` over the **11.6M-row** positions Book column — on every `/pivot`, every `/analysis`, every `/ask` tool round-trip, cache hit or not. `_managers_meta` did the same `unique()` on every `/meta`, the first call each UI page load makes. Both now share one memoized `_book_names()`. | Pivot cold 0.34–0.95 → **0.04–0.52 s** across the board (`pivot_var_by_sector@Soros` 0.34 → 0.04); `/meta` 0.44 → **0.17 s** cold, 0.42 → 0.15 warm |
| 4.3 | **Row indices for `/pnl_attribution/linkage` and `/whatchanged`** (TODO 2) | The `_frame_rows_by` treatment round 3 gave `/calibration` and `_name_attr`: linkage's five full-frame masks (positions ×2, exposures, specific_var, and an `isin` over the ~10.4M-row specific_returns panel) and whatchanged's per-book/per-date scans now ride cached row indices. New `_book_date_rows(book)` (the per-book date index) and `_sr_rows_between(lo, hi)` (shared with `_name_attr`, which had its own copy). | `linkage@Soros` 1.94 → **0.57 s** cold / 1.15 → 0.34 warm; `linkage@Vanguard` 1.08 → **0.20 s**; `whatchanged@Vanguard` 1.50 → **0.91 s** cold and warm; `whatchanged@Soros` warm 1.46 → 0.76 s |
| 4.4 | **`/whatchanged` book-scoping BUG** (found doing 4.3) | `exposure_attribution` handed `_ud.book_at` the WHOLE positions frame. `book_at` has no Book concept, so `dict(zip(Position, Weight))` collapsed all 124 managers to one arbitrary weight per name — **every book returned the same wrong net exposures**. Now scoped to the requested book's rows (the fix `/drift` already carried). Correctness, not performance. | Before: Soros and Vanguard both read ResidVol after = 0.654. After: Soros +0.069, Vanguard −0.056, and both tie `/drift`'s independently-computed `late` exactly |
| 4.5 | **`/overview/analysis` assembly concurrent** (TODO 3) | The five input blocks (limits, risk+Euler, backtest, attribution headline, DQ) run in a threadpool and the reconcile (`/pnl_attribution/linkage`) runs alongside `collect()` instead of after it. Fragments merge in the original order, so the payload the model sees is key-for-key unchanged. | Proxy (the same six inputs over HTTP, warm): serial 1.10 s → concurrent 0.88 s, 1.2×. Modest **because 4.2/4.3 already cut the parts** — the prize shrank as the round went on. Live call verified end-to-end |
| 4.6 | **TODO 4's answer: it is the query, not serialisation** | Measured on the real payloads: building the records costs 62 ms and FastAPI's `jsonable_encoder` + `dumps` 20 ms of an 830–1450 ms call — ~6%. So the cold time is the SDK's per-member floor and stays. Two things kept anyway: `_records` rebuilt COLUMN-wise (`iterrows` materialises a Series per row) — **20× on that stage**, byte-identical output including the `iterrows` int→float coercion rule, checked on 8 frame shapes; and memos for `/trends?by=` and `/contributions` (both pure functions of their arguments). | `_records` 62 → 2.6 ms (5k rows); `trends_by_factor@Vanguard` warm 1.48 → **0.02 s**; `contributions@Vanguard` warm 0.83 → **0.03 s**; cold unchanged, as diagnosed |
| 4.7 | **Vector plan takes two breakouts and a day window** (TODO 5) | `_day_vector_shape` now admits a SECOND breakout (the vector query just takes two levels — cost follows output rows, not member count) and a `Day`/`DayDate` FILTER (the vector is per-set, so a chart zoom is a slice of the records the same query already produced; day numbering is not re-based). Both pinned record-for-record against `plan=levels` in `test_risk_measures.py`. | The two shapes that used to fall to the level plan's per-member floor are on the vector plan; `t_day_vector_plan_ties_level_plan` passes with the new cases |
| 4.8 | **TODO 6's answer: no lever** | Exposures carries exactly `Date, Position, Factor, Loading, FactorPnL` — three are the key, the other two back measures. **There is no unread column to drop.** Measured what one WOULD have bought (bare session, same arrow path): a 6M-row double column is 0.71 s of JVM ingest, ~4% of start-up. Also tried dictionary-encoding the string columns: the arrow file halves (477 → 235 MB) but atoti 0.9.15's `ArrowLoad` **rejects dictionary vectors** (`IntVector cannot be cast to TinyIntVector`). | `build_cube` 18.4 s, `load.bulk` 7.27 s (Exposures 3.02 / Positions 2.85 / SpecificVar 0.92 / SpecificPnL 0.48, all arrow-cache). The bulk load is a JVM-ingest floor; item closed |
| 4.9 | **`/span` scatter ordering** (TODO 7) | Both position sets are Python `set`s, so the scatter's point order followed the process's string hash seed. Sorted. | Payload is now reproducible across restarts; verified the diff is order-only (same multiset, same count, `scatter` the only key that moved) |

## Round 5 — the test suite made green (2026-08-21)

Twelve failures across five files, every one of them dating from a change that moved the world
without moving its tests. Ten were stale expectations; **two were real defects the tests had been
reporting correctly all along** and nobody had read. The suite now runs clean: 29 files, 0 failures.

| Test(s) | What was actually wrong | Fix |
|---|---|---|
| `test_pivot_app` × 6 | **A REAL DEFECT.** The Streamlit pivot seeds its rows from `"Book"`, but the 2026-08-14 API rename made `Manager` the canonical name and `/dims` lists only that. A multiselect default that is not among its options is silently dropped — so the app landed with **no row field selected**, and the graph builder, which reads the row field for its x-encoding, produced **no chart at all**. Six tests were reporting one broken screen. | `_book_dim()` in `risk_pivot_app.py` resolves the name off the live `/dims` (so it works against either vintage), and seeds rows + slicers from it. Tests read the same name rather than restating the rename |
| `test_risk_measures::t_incremental_total_is_subadditive` | **A REAL (documented-as-unknown) FINDING.** `multi-manager-plan.md` had it as "a real sub-additivity violation in a risk measure … not investigated". It is not a violation: Σ incremental < book is a property of a COHERENT measure, and a 99% quantile is the textbook measure that is not one — the removal also re-reads the reduced book at ITS OWN tail day, so each member is credited for shifting the tail too. Measured by Issuer: Scenario VaR Σ 0.0428 vs book 0.0353, Total VaR 0.0427 vs 0.0358; by Sector and Position likewise. The cube's own comment asserted the false theorem ("VaR is sub-additive") | Pin the property on the measure that HAS it — `Incremental Model vol`, a standard deviation: sum-under-book, non-negative, and top-name incremental < marginal, on Soros (181 names) AND Vanguard (3,617). Add `t_incremental_var_bounds` for what the VaR pair does satisfy, deliberately asserting nothing about the sum so a future coherent basis is not read as a regression. Correct the cube comment |
| `test_span`, `test_drift`, `test_funnel`, `test_universe` (`*_book_guard`) | Stale: each asserted that a named non-Soros book returns `book_mismatch`. Every loaded manager has had its own `<stem>.<Book>.parquet` since the 124-book precompute sweep, so `_resolve_artifact` correctly serves real data | Test BOTH branches: a book with its own artifact is served from it (and for `/drift`, its live attribution differs from the default book's), a book with no artifact still gets the clean guard payload. Uses an unbuilt name, which is the state the guard exists for |
| `test_attribution::t_pnl_attribution_book_guard` | Stale in the same way (AQR now has its own artifact) | Same two-branch treatment across all four `/pnl_attribution*` routes |
| `test_attribution::t_cube_measures_foot`, `::t_book_independent_measures_inert_with_one_book` | Stale premise: both query the three book-independent measures through `/pivot`, which the multi-book guard correctly rejects (400). Their docstrings already said "with today's single-book data" | Made conditional on the live book count: single-book runs the original identity/no-op assertions, multi-book asserts the rejection instead of asserting nothing |
| `test_model_trust::t_exposure_profile_shape` | Stale bound: `beyond3.share < 0.5` was a fact about the old coverage universe, not an invariant. Coverage loadings are uncapped BY DESIGN so an off-index name shows its true tilt; the 124-book universe is ~5k names far smaller than the S&P 500 estimation seed, so on Size a majority below −3 (measured 0.60, median −3.7) is the design working — the endpoint's own `note` says so | Assert what is invariant: the tail is internally consistent (`share == n / n_names`, every listed name really beyond ±3) and the quantiles are ordered |
| `test_notebook::t_latest_cob_is_D` | Stale copy: the test restated the notebook's `D = 2024-12-31` as its own constant. The build's calendar reached 2026-06-30, so it failed as a duplicated-constant artifact rather than as a signal about the notebook | Read `D` out of the notebook source (one source of truth) and assert IT is the latest COB — which now fails loudly if the notebook is not re-pointed after a rebuild. Re-pointed the notebook's `D` to 2026-06-30 (source only; its stored outputs still need the re-run in TODO 6) |

Method note: the inventory came from running all 29 files, not from the round-3 TODO list — which
had nine failures and missed three (`test_funnel`, and the two extra `test_pivot_app` cases beyond
the four graph-builder ones). `test_views_api` prints `12/12 passed` rather than `N passed, M
failed`, and reads as "no tally" to a grep-based sweep; it was green throughout.

## Lessons for the record

- Quote loadavg with every timing; a "MET" without a quiet-box confirmation is provisional.
- Same-process A/B only — sibling load moved individual timings up to 5–10×.
- Every measure stays cube-native unless the trade is disclosed (3.2 is the one disclosed exception).
- Parallel agents need a memory budget, not just port isolation: four 12 g cubes plus the 32 g service OOM-killed the service once (systemd restarted it).
- The pre-programme diagnosis held: routine queries were never the problem; a handful of pathological shapes and start-up were.
- A test that has failed for months stops being read as a signal. Two of round 5's twelve were live
  defects — one of them a whole UI screen landing empty — sitting inside a list labelled "known
  failures, none of them a live defect". Fix or retire them; do not carry them.

## TODO

Open items, roughly by payoff. None are regressions; all are measured. Items 1–7 of the round-3
list were done on 2026-08-21 (round 4 above); what follows is what is left, renumbered, plus what
round 4 turned up.

**Performance**
1. Cold paths nothing has touched: `trends_book@Vanguard` **11.6 s** (the date-by-date book loop —
   memoized, so it is paid once per process per (set, book, measures), but the first Trends view of
   the day waits), `calibration@Vanguard` **6.0 s**, `trends_book_modelvol@Vanguard` 2.8 s,
   `pivot_var_trend_by_date@Vanguard` 2.0 s. All are the SDK per-member/per-date floor, all are
   warm-free afterwards. The lever, if one is wanted: prewarm them per book on a background thread
   the way `/dims` is prewarmed, rather than making them faster.
2. `/meta` is 0.17 s after round 4, and what is left is two `contributors.COUNT` queries (levels
   Date, levels ScenarioSet) to enumerate members the frames already know. Serve from the frames
   or prewarm; it is the first call every UI page load makes.
3. `/whatchanged` is 0.9 s cold and warm on the widest book — the residue is `_book_inputs` +
   `_risk_from_weights` twice (the two filing dates) with no memo. Same treatment as
   `/contributions` would make it warm-free.
4. `/pnl_attribution/residual` (0.9–1.2 s cold) and `/limits` (0.4–0.7 s) are the next tier; both
   are cube-query-bound, not scan-bound.

**Correctness / hygiene**
5. ~~Pre-existing test failures~~ — **cleared 2026-08-21** (round 5 below). The suite is green:
   29 files, 0 failures. Two of the twelve were real defects, not stale expectations; see the
   round-5 table.
6. Re-execute `notebooks/soros_13f_risk.ipynb` and `vanguard_13f_risk.ipynb` — source was migrated
   to the Day idiom, outputs are from the last run (disclosed in-cell). `notebook_helpers.build()`
   defaults to cube port 9096, which a 9-day-old notebook JVM still binds — pick a free port or
   kill it.
7. `python_src/cube_bench_day.json` is still untracked (the round-2 harness output) — commit under
   `docs/` or delete.
8. `_frame_rows_by` keys its cache on `id(frame)`. Nothing exercises it with short-lived frames
   today, but that is the exact pattern that made round 4's first `_book_names()` cut return a
   dead stub's books (fixed there with a weakref). Give it the same treatment before anything
   starts swapping frames in-process.
9. Audit the rest of the API for the 4.4 class of bug: a helper with no Book concept handed the
   whole multi-book positions frame. `/drift` and `/whatchanged` are now scoped;
   `barra_universe_funnel._held_positions` was fixed in Phase 3. Nothing else is known to be
   wrong — but nothing has systematically checked, either.

**Operational**
10. After ANY frame rebuild: rerun `barra_persist_attribution.py` (or delete its outputs) and let
   the first cube start rewrite `data/.cube_cache` — both are keyed on the source parquet, so a
   stale cache regenerates itself, but the first start after a rebuild is ~35 s, not 19.
11. Serve-before-load remains unimplemented by choice; if start-up ever matters again, it needs a
    readiness guard on every route (incl. `/dims`, whose member lists pre-load would be a partial
    answer). Note round 4 closed the other start-up lever (4.8): the bulk load is a JVM floor.
12. The `:8010` service was OOM-killed once during round 3 with four 12 g cubes alongside it — set
    a memory budget (or `BARRA_CUBE_XMX` per agent) before the next parallel programme.
13. A freshly restarted service burns ~180% CPU for several minutes (G1 on an 8.6 G heap) before it
    settles. Benching inside that window reads 2–5× slow; wait for loadavg to fall first. This is
    why round 4's first after-run was discarded and re-taken.
