# Cube & API optimization log (2026-08-14 → 2026-08-15)

Every optimization applied to the 124-book cube and its API across the three rounds, with the
measured before/after, what was tried and rejected, and what is still open. Sources: the
per-round docs (`cube-optimization-plan.md`, `cube-opt-round2-*.md`, `api-bench.md`) and the
harness JSONs in `docs/` (`cube_bench_*.json`, `api_bench_merged_20260815.json`). Bench book is
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

Every routine sliced query (the shapes the Vite UI issues) was already 60–500 ms cold / 20–50 ms
warm before the programme and is unchanged; nothing regressed beyond harness noise, and every
number is pinned by the accuracy gates (`test_model_vol` 5e-10 tie-outs, `test_contributions`,
`test_stress`, `test_whatif`, `test_risk_measures`).

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

## Lessons for the record

- Quote loadavg with every timing; a "MET" without a quiet-box confirmation is provisional.
- Same-process A/B only — sibling load moved individual timings up to 5–10×.
- Every measure stays cube-native unless the trade is disclosed (3.2 is the one disclosed exception).
- Parallel agents need a memory budget, not just port isolation: four 12 g cubes plus the 32 g service OOM-killed the service once (systemd restarted it).
- The pre-programme diagnosis held: routine queries were never the problem; a handful of pathological shapes and start-up were.

## TODO

Open items, roughly by payoff. None are regressions; all are measured.

**Performance**
1. `pivot_var_trend_by_date` (2.6–3.5 s cold AND warm, both books) — a Date-series of scenario measures re-aggregates every call; candidates: memoise per (set, book, measures) like `/trends`, or serve `/trends`'s cache.
2. `/pnl_attribution/linkage` 1–2 s and `/whatchanged` 1.5 s — same `_frame_rows_by` row-index treatment as 3.6 applies; not yet done.
3. `/overview/analysis` still fans out its input endpoints serially — all inputs are fast now, but a threadpool would cut the pre-LLM assembly further.
4. `trends_by_factor@Vanguard` 1.8 s and `contributions@Vanguard` 1.2 s — single cube queries returning large payloads (300–600 KB); check serialisation vs query.
5. Level-plan day shapes (no DaySet, Day filter, two breakouts) still pay the SDK per-member floor (~11 s Vanguard HistFull); either extend the vector plan to two breakouts / Day-filtered windows, or accept and document.
6. Start-up floor is `session.start` 5.3 s + `load.bulk` 7.4 s (JVM ingest, serialised); the only untried lever is a smaller Exposures table (e.g. drop leaf columns the cube never reads) — measure before touching.

**Correctness / hygiene**
7. `/span` scatter-point order is nondeterministic (Python `set` iteration) — sort it; it is why the identity gate is 66/68.
8. Pre-existing test failures to fix or retire: `test_risk_measures::t_incremental_total_is_subadditive` (composite VaR is not sub-additive by construction on this data — documented in `multi-manager-plan.md`); the 6 Streamlit graph-builder tests in `test_pivot_app.py`; `test_notebook::t_latest_cob_is_D` (hard-coded 2024-12-31); `test_model_trust::t_exposure_profile_shape` (`beyond3.share` 0.60 ≥ 0.5 on the 124-book universe); three stale `test_attribution` cases (AQR now has its own artifact; two query book-independent measures the multi-book guard rejects).
9. Re-execute `notebooks/soros_13f_risk.ipynb` and `vanguard_13f_risk.ipynb` — source was migrated to the Day idiom, outputs are from the last run (disclosed in-cell). `notebook_helpers.build()` defaults to cube port 9096, which a 9-day-old notebook JVM still binds — pick a free port or kill it.
10. `python_src/cube_bench_day.json` is untracked (the round-2 harness output) — commit under `docs/` or delete.

**Operational**
11. After ANY frame rebuild: rerun `barra_persist_attribution.py` (or delete its outputs) and let the first cube start rewrite `data/.cube_cache` — both are keyed on the source parquet, so a stale cache regenerates itself, but the first start after a rebuild is ~35 s, not 19.
12. Serve-before-load remains unimplemented by choice; if start-up ever matters again, it needs a readiness guard on every route (incl. `/dims`, whose member lists pre-load would be a partial answer).
13. The `:8010` service was OOM-killed once during round 3 with four 12 g cubes alongside it — set a memory budget (or `BARRA_CUBE_XMX` per agent) before the next parallel programme.
