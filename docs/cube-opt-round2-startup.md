# Cube optimization round 2 — start-up time (2026-08-15)

Round 1 (`docs/cube-optimization-plan.md`) fixed the query pathologies and left **one hotspot
unaddressed: `build_cube` at ~57–64 s**, with the belief that `read_pandas` of the 6 M-row
exposures table dominated it. That belief had never been tested — there was no per-stage clock
inside `build_cube`. This round starts by building one.

**Result: `build_cube` 57.4 s → 22.8 s (median of 3 locked runs each, ±0.2 s), every accuracy
identity held to 3.3e-16, no query in the 44-query suite regressed.** The dominant term was never
the data: **21 s of the 57 s was Python↔JVM round-trips for measure/hierarchy registration**, and
another **11 s was model-definition work being redone against already-loaded tables**.

Harness: `python_src/cube_bench.py` (unchanged), plus `BARRA_CUBE_TIMINGS=1` which makes
`build_cube` emit a per-stage table and leave it on `BUILD_TIMINGS` (the harness folds it into
`stages.build_stages_s`). The flag costs one env check and two floats per stage when off. Box:
62 G / 20 cores, `BARRA_CUBE_XMX=32g`, bench book Vanguard, every build serialized behind the
shared `/tmp/cube_bench_global.lock`.

## Attempt 1 — measurement only

Three runs, 57.62 / 56.97 / 57.42 s (spread 0.7 s — the ±5 s "noise floor" quoted in round 1 was
run-to-run *environment* variance, not measurement variance; back-to-back builds are stable).

| stage | median s | share | what it is |
|---|---:|---:|---|
| `measures.*` (13 stages) | **16.40** | 29% | defining ~55 measures |
| `hierarchies` | 7.15 | 12% | 5 `h[name] = {...}` assignments |
| `session.start` | 5.30 | 9% | JVM + Spring boot |
| `load.Positions` | 5.06 | 9% | 11.6 M rows, 4 columns |
| `prep.build_scenarios` | 4.31 | 8% | *(mostly not what its name says — see below)* |
| `joins` | 4.05 | 7% | 7 `Table.join` calls |
| `load.Exposures` | **3.46** | **6%** | 6 M rows — **the assumed hotspot** |
| `param_hierarchy.ScenarioDay` | 3.21 | 6% | 2,618 members |
| `prep.wloading_merge` | 2.21 | 4% | the attribution pandas prep |
| `param_sim.*` (2) | 1.78 | 3% | CorrStress + StressShock |
| `prep.attribution_pandas` | 1.57 | 3% | the attribution pandas prep |
| all other loads | 1.47 | 3% | securities, scenarios, PIT, spec-PnL, managers |
| `create_cube` | 0.77 | 1% | |

**The lesson, and it is the whole round:** `read_pandas` of exposures is **6%** of the build. Data
loading in total is 10 s (18%). **Model definition — measures, hierarchies, parameter dimensions —
is 28 s (49%)**, and it is not computing anything: it is talking to the server.

## Attempt 2 — one publish instead of ~55 → **48.4 s**

Reading atoti 0.9.15's source explains attempt 1's table exactly:

- `cube.measures[name] = expr` → `Measures._update_delegate` distils that one definition and then
  calls **`py4j_client.publish_measures(cube)` — a full republish of the measure DAG, per measure**.
- every `m["X"]` **read** is a `find_measure` GraphQL round-trip, and so is every `.formatter =`.

Micro-probe on a trivial 1,000-row cube: 17 ms per individual measure vs 6 ms via
`Measures.update()`, 7 ms per checked lookup vs 0 unchecked. On the real cube the per-measure cost
is ~300 ms.

Two public APIs fix it: `Measures.update({...})` (distil many, publish once) and
`tt.mapping_lookup(check=False)` (documented as "saving a roundtrip with the server", and it also
legalises referencing a measure defined later in the same batch). `_DeferredMeasures` in
`barra_factor_risk_cube.py` collects every `m[name] = ...` and `m.fmt(name, spec)` and flushes them
in one `update()` — **the definitions, their order, and the published DAG are unchanged; only the
number of round-trips changes.**

One constraint found the hard way: `tt.array.*` helpers call `check_array_type(measure)` →
`Measure.data_type`, which asks the server about a measure the pending batch has not published
(`NoSuchElementException: No value present`). Expressions built on *operations* (`book_pnl_vec`,
`_up`, `_shock_vec`) never trip it — only three measures are ever passed to an array helper
directly. Attempt 2 flushed at those three points (4 publishes).

`measures.*` 16.40 → 7.65 s. Build **48.43 s**.

## Attempt 3 — three cheap wins → **40.8 s** (41.24 / 40.49 / 40.84)

- **`prep.build_scenarios` 4.31 → 0.02 s.** The stage was misnamed by its own mark: almost all of
  it was `set(exposures["Factor"])`, iterating **6 M boxed Python strings to produce a 23-element
  set**. `set(exposures["Factor"].unique())` gives the same answer. (Split out as
  `prep.factor_lists`, 0.12 s.)
- **Persisted attribution columns** (round-1 Step 4's forward-compatible hook, finally fed).
  New `python_src/barra_persist_attribution.py` derives `FactorPnL` (a column on `exposures`) and
  the optional `specific_pnl` frame with the *same statements* `build_cube` uses, and `--verify`
  asserts bit-identical re-derivation. `prep.wloading_merge` + `prep.attribution_pandas`
  3.78 → 0.07 s. The exposures parquet grows 48 → 91 MB; `load_frames` and `load.Exposures` did
  not measurably change.
- **Hierarchies in one `h.update({...})`** instead of 5 assignments: **measured flat** (7.15 →
  7.03 s, inside noise). Kept — it is 5 cube refreshes collapsed to 1 and reads better — but it is
  not where hierarchy cost lives. Recorded as a non-win.

## Attempt 4 — one early publish + two rejected JVM knobs → **38.8 s**

Hoisting `Scenario dates (epoch)` and `PIT Scenario PnL vector` up beside `Scenario PnL vector`
means **all three array-typed measures are published by a single early flush**, so the build has
exactly two publishes (4.0 s of intermediate flushes → 1.07 s). Sub-instrumenting hierarchies
showed the 7 s is `h.update` itself (5.5 s), not the surrounding lookups.

Two knobs measured and **rejected**:

- **`-Xms12g`** (theory: stop heap expansion during a 17.5 M-row load) — **worse**, 40.4 s vs
  38.8 s. Round 1's `-Xms2g` stands. Kept as the `BARRA_CUBE_XMS` override so the finding is
  reproducible.
- **Application CDS** (`-XX:+AutoCreateSharedArchive`, JDK 21) against `session.start`'s 5.3 s —
  **no archive is ever written** by this jdk4py runtime (verified down to a bare `java -version`;
  the JDK's own base archive is already mapped), and `session.start` measured 5.29 → 5.30 s. The
  flags were removed rather than left in as cargo.

Two ideas killed by cheap measurement before any code was written:

- **`partitioning=`** on the big tables: a clean one-variant-per-JVM A/B gave 6.38 s (none) vs
  6.34 / 6.20 / 7.13 s (hash4/8/16) on Positions — no reliable effect, and the Exposures numbers
  swung 3.4–16.4 s run to run. No basis to keep it.
- **Dropping unreachable Positions rows** (the row analogue of Step 4's dead columns): measured
  **1,274 of 11,566,348 rows (0.01%)** have no Exposures leaf. Nothing there.
- **`Session.read_parquet`** (round-1 lever 3): faster on Exposures (3.55 vs 4.35 s) but it is
  **deprecated in 0.9.15** (`FutureWarning`), so it was not built on. Attempt 5 made it moot.

## Attempt 5 — model first, data last → **22.8 s** ✅

The remaining 38.8 s was almost entirely JVM-side work whose cost is **proportional to the data
already in the tables**: joins, `create_cube`, hierarchy creation, parameter dimensions and measure
publishes all re-index or refresh whatever the tables hold. A probe confirmed it — the same
joins + `create_cube` + hierarchies on tables holding 1,000 rows instead of 17.5 M:

| | full tables | seeded tables |
|---|---:|---:|
| joins | 2.36 s | 0.32 s |
| `create_cube` | 0.82 s | 0.43 s |
| hierarchies | 4.37 s | 0.31 s |
| the load itself | 9.07 s | 0.78 s + 11.40 s after |

So `build_cube` now creates the three big tables from `df.head(1000)`, defines **everything** —
joins, hierarchies, the ScenarioDay parameter hierarchy, both parameter simulations, every measure
— and then calls `Table.load(full_frame)` at the very end. Seeding (rather than `create_table` with
a hand-written schema) keeps atoti's pandas type inference as the one source of the schema, and
inference reads **dtypes, not values**, so `head()` infers exactly what the full frame would. The
tables are keyed, so re-loading the seed rows inside the full frame upserts them.

| stage | attempt 1 | final |
|---|---:|---:|
| pandas prep (4 stages) | 8.20 | 0.32 |
| `session.start` | 5.30 | 5.31 |
| table loads | 10.99 | 13.24 |
| `joins` | 4.05 | 0.58 |
| `create_cube` | 0.77 | 0.40 |
| hierarchies | 7.15 | 0.61 |
| parameter dimensions (3) | 4.99 | 0.38 |
| measure definition + publish | 16.40 | 1.75 |
| **build_cube** | **57.42** | **22.73** |

Final: 22.81 / 22.66 / 22.86 s (median **22.81 s**), and 22.63 / 22.73 / 23.03 s across three
separate full-harness runs.

## Gates

**Accuracy — passed at 3.3e-16** (`worst relative diff`, 78 pinned values), against a **pristine
`build_cube` from HEAD 663ac6b** built on the same frames, Vanguard / latest date / HistFull:
`Model vol`, `Scenario VaR 99`, `Total VaR 99`, `Specific vol`, `Top-5 risk share`, `Net exposure`
by Factor (23), `Factor contribution` by Factor (23), `Factor variance contribution` by Factor
(23), and `Σ Marginal Total VaR 99` over 3,635 positions. The `Σ Marginal Total VaR 99` vs
`Total VaR 99` gap reads **1.68e-03 in both cubes, digit for digit** — the documented
read-off-vs-interpolated-quantile convention, unchanged.

**Both frame paths tie out.** The gate was run twice: once on frames WITH the persisted
`FactorPnL`/`specific_pnl` (the skip path) and once on frames WITHOUT them (the derive path, i.e.
today's production data). Both match the reference at 3.3e-16 — so the persisted columns are
numerically the same thing, and **the first-run/no-cache path is not a fallback that drifts.**

**Transient branches — passed.** The two mechanisms a restructured load could plausibly break were
exercised in-process: a Positions **source-scenario branch** (halve the largest holding → book
`Model vol` 0.012707 → 0.012117, `Gross weight` falls by exactly half the traded weight, base cell
unchanged) and a **StressShock parameter scenario** (`Custom stress PnL` == `x·σ·vol` to < 1e-12).

**Queries — no regression.** `docs/cube_bench_round2_20260815.json` vs
`docs/cube_bench_step6_20260814.json`: **identical row counts on all 44 queries, no new failures.**
Every query slower by >20% is a sub-0.13 s query where the absolute difference is ≤ 60 ms — and a
self-diff of two runs of the *identical* final code shows the harness's own spread reaches 3.5× on
exactly those queries, so nothing there is signal. Genuine improvements: `scenario_day_path`
25.6 → 9.0 s, `scenario_day_by_sector` 35.0 → 18.9 s, `model_vol_by_book_123` 1.07 → 0.61 s. JVM
RSS after build 7.55 → 6.04 G; suite peak 40.9 → 38.4 G; whole harness 203 → 96 s.

## What production rollout needs

1. **Nothing, for the 22.8 s itself.** The `build_cube` changes are self-contained and need no new
   data: on frames without the persisted columns the build still derives them (measured 3.8 s of
   the 22.8 s) and produces identical numbers.
2. **To bank that last 3.8 s, persist the columns.** Either run
   `barra_persist_attribution.py` after each build, or — better — fold its ~20 lines into
   `barra_build_frames.py`'s `build_frames`, which already has every input. **Invalidation rule:
   the outputs are a pure function of `exposures`, `positions`, `factor_returns` and
   `specific_returns`; regenerate or delete BOTH outputs after any rebuild.** The cube trusts the
   column's presence, so a stale `FactorPnL` would be used silently. This is why the script is a
   deliberate separate step and not a cube-side cache.
3. **No warm-up, no cache to prime, no deploy step.** The round-1 "cube-ready snapshot cache" idea
   was never needed: the cost it targeted (pandas→arrow conversion) is 13 s of a 22.8 s build and
   the part worth removing was model definition, which no snapshot can skip.
4. **`flexagg-api.service` is unchanged** — same flags, same heap, `-Xms2g` re-confirmed by
   measurement.

## Where the floor is now

`session.start` **5.3 s** (JVM + Spring boot; CDS does not help on this runtime) and
`load.bulk` **11.9 s** (17.5 M rows across four tables) are **85% of what remains**. Both are
genuinely doing work. The next real lever, if start-up ever matters again, is loading those four
tables **concurrently or asynchronously** (`Table.load_async` exists) — or serving before the bulk
load finishes, since the model is fully defined by then. Everything else in the build now totals
under 5 s.

## Round 3 — bulk load (2026-08-15, agent B, same day)

Round 2's floor note named two levers: load the four tables concurrently, or serve before the
bulk load finishes. Both were tested; neither is where the win was. All timings below were taken
on a SHARED box with three sibling agents building 12 g cubes at the same time (loadavg 11–35 —
recorded on every run; the round-2 22.8 s was at loadavg ~4). Only back-to-back same-condition
pairs are quoted as A/Bs. **A quiet-box confirmation pass is still needed** — the orchestrator
runs it after the merge.

### Attempt 1 — `Table.load_async` / threads across the four tables: no gain

`Table.load_async` in atoti 0.9.15 is literally `await to_thread(self.load, data)`, so it is the
same as a thread pool over `Table.load`. Back-to-back A/Bs (fresh process each, `BARRA_CUBE_LOAD`
switch): serial **13.33 s** vs async **12.53 s** (loadavg 1→4 / 4→8); serial **17.77 s** vs threads
**16.87 s** (7→10 / 10→14). Inside the noise. The per-table timings in threads mode show why —
they COMPLETE serially (4.8 / 5.8 / 10.7 / 11.8 s cumulative on the cached run below): the JVM
serialises the datastore transactions, one load commits at a time. Async with the arrow cache on
was actually worse (25.4 s vs 11.4 s serial, and it pushed loadavg 15 → 23). **Default stays
serial**; the switch is kept for the record.

### Attempt 2 — where the 12 s actually goes (bare session, no cube)

`Table.load(DataFrame)` = `pandas_to_arrow` (Python) → `write_arrow_to_file` (temp dir) →
`ArrowLoad` (JVM reads the IPC file). Measured per table at loadavg 18:

| table | rows | to_arrow | to_file | JVM ingest | total |
|---|---|---|---|---|---|
| Exposures | 6.0 M | 1.46 s | 0.11 s (242 MB) | 1.85 s | 3.43 s |
| Positions | 11.6 M | 2.82 s | 0.22 s (477 MB) | 2.82 s | 5.85 s |
| SpecificVar | 0.5 M | 0.11 s | 0.01 s | 0.12 s | 0.23 s |

So ~45% of the bulk load is Python-side arrow conversion — a pure function of the frame, redone
identically on every restart — ~50% is JVM ingest, and the difference to the in-build 12–13 s is
cube-side indexing.

### Attempt 3 — direct `ParquetLoad` of cube-ready parquet: REJECTED

Skip pandas entirely and let the JVM read parquet. The frames' own parquet fails type validation
(`timestamp[ns]` vs the tables' `LocalDate`: "Incompatible Type: datastore type
'java.time.LocalDate' doesn't match parquet type 'long'"), so cube-ready copies were written
(date32 Date, cube columns only, 4.3 s to write). Result: Exposures **6.35 s** (vs 3.07 s pandas
control, same process), Positions **285.9 s** (vs 6.73 s) — the JVM's vectorized parquet reader is
pathological on the 11.6 M-row string-keyed table and drove the box to loadavg 191. Contents were
exact (slice check maxdiff 0.0), speed disqualifying. Not pursued further.

### Attempt 4 — cache the arrow bytes: **load.bulk ~15 s → ~9 s under load** ✅

`_bulk_load` / `_load_one` in `barra_factor_risk_cube.py`: on a miss, the frame is converted with
atoti's OWN `pandas_to_arrow` and written with its OWN `write_arrow_to_file` into
`data/.cube_cache/<table>.arrow` (+ a JSON sidecar), then loaded from that file; on a hit the JVM
`ArrowLoad`s the cached file directly. Bytes on the wire are identical to the uncached path (same
converter, same writer), so the table contents cannot differ. The key is the source parquet's
path/mtime_ns/size (stamped on each frame by `load_frames` in `df.attrs["source"]`, which rides
through the column selection / `head()` the build applies) + row count + columns + dtypes + the
table's data types; a frame that lost its stamp (e.g. exposures merged in the un-persisted
attribution path) simply gets no cache; a stale/missing/corrupt entry is regenerated; any failure
falls back to the plain load. Sidecar written last, so no key exists without its file.
`BARRA_CUBE_ARROW_CACHE=0` disables it. ~790 MB on disk under the gitignored `data/`.

Back-to-back A/B, fresh process each, serial schedule, attribution persisted (see below):

| run | loadavg | session.start | load.bulk | build_cube |
|---|---|---|---|---|
| cache off | 21.3 | 12.05 s | **15.65 s** | 42.5 s |
| cache on | 16.6 | 6.80 s | **8.92 s** | 25.2 s |
| cache off | 19.3 | 7.45 s | **14.57 s** | 33.9 s |
| cache on | 15.7 | 5.27 s | **9.32 s** | 21.0 s |

Row counts equal the frames on every run (Exposures 6,004,074 / Positions 11,566,348 /
SpecificVar 497,305 / SpecificPnL 392,489) and the Soros HistFull `Scenario VaR 99` at 2026-06-30
is 0.03523562876608036 on all of them. The first build after a rebuild pays the conversion once
plus one file write (measured 34.8 s bulk at loadavg 34 — the write run — vs 17.9 s on the next
build in the same conditions).

### Attribution persisted (item 3)

`barra_persist_attribution.py --verify` was run against the MAIN checkout's `data/` ("re-derivation
is bit-identical"; `exposures.parquet` +FactorPnL, `specific_pnl.parquet` written). Every build
above shows `prep.wloading_merge 0.00 s` / `prep.attribution_pandas 0.08–0.20 s` (was 2.13 / 1.57 s):
confirmed skipped. `data/` is shared with the running `flexagg-api.service`, which picks it up on
its next restart — intended. Regenerate or delete both after any rebuild (unchanged rule).

### Serve-before-load: evaluated, NOT implemented

The model is fully defined before the bulk load, so in principle `/meta`, `/views*` and health
could answer ~9 s earlier. It was not done because (a) it needs a middleware that gates EVERY
other route (all ~40 read `S["cube"]`/`S["frames"]`, and `/dims` enumerates cube members — a
pre-load answer would be a partial member list, i.e. exactly the "partially-loaded answer" that
must never happen), (b) `build_cube` would have to return before the load, changing the contract
every notebook, `cube_bench`, and test relies on (a fully loaded cube), and (c) the user-visible
gain is the UI shell appearing earlier while every lens then waits on 503s — no query completes
sooner. Not worth the guard surface. Revisit only if start-up matters again after the arrow cache.

### Where the floor is now (round 3)

With attribution persisted and the arrow cache warm, the build is `session.start` (~5.3 s quiet)
+ JVM ingest of ~790 MB of arrow (~7–9 s, the JVM's own datastore commit rate — serial by design
of the engine) + ~4 s of model definition. Estimated **~17–19 s on a quiet box** vs round 2's
22.8 s; **to be confirmed by the orchestrator's quiet pass.** The next lever would be the ingest
rate itself (fewer/wider columns, or ActiveViam-side tuning), which is out of the SDK's reach.
