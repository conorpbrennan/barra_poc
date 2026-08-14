# Cube optimization — the measured plan (2026-08-14)

The 124-book expansion made cube performance a first-order question. This plan is built on a
**benchmark harness** (`python_src/cube_bench.py`) that stands up the full cube from `data/`,
instruments every build stage, runs a fixed 44-query suite covering every measure family (each
query cold + warm, JVM RSS before/after/peak sampled at 200ms), and emits a comparable JSON.
The harness is the gate for every optimization below: change one thing, re-run, `--diff` the
two JSONs. Baseline: `bench_baseline.json` (session scratchpad, 2026-08-14; box: 62G RAM,
20 cores, `BARRA_CUBE_XMX=32g`; bench book = Vanguard, 3,635 names — the largest manager).

## What the data actually says

**The routine paths are fast.** Every single-book, date-sliced, HistFull-sliced query — the
shapes the Vite UI and risk_api actually issue — runs in **60–500 ms cold, 20–50 ms warm**.
That includes the by-position decomposition grids (3,635 rows of Marginal Total VaR: 0.49 s)
and the whole scenario-scalar family. The demo does not have a general performance problem.

**Build**: `load_frames` 0.7 s (parquet is cheap), `build_cube` **56 s** (dominated by
`read_pandas` into the JVM), JVM 7.8 G after build. The full 44-query suite drove the JVM to a
**41 G peak**; G1 reclaims slowly on its own (13.5 G → 9 G by the end of the suite).

**The aggregate cache is highly effective where it applies**: warm/cold is 3–10× on scalars and
factor-level decompositions, but ~1× on by-position, by-all-sets, and ScenarioDay shapes —
those re-aggregate every time.

The measured pathologies, ranked:

| # | Pathology | Evidence (cold, Vanguard unless noted) |
|---|---|---|
| 1 | **ScenarioDay unpacking** | book path **10 s warm-cube / ~50 s cold-cube, +14 G JVM**; day × Sector **fails** (~20–60 s then MdxException). Probes: COVID (82 real days) **52 s** vs HistFull (2,618 days) **49 s** — cost is per-member evaluation over the full-size 2,618-member parameter hierarchy, NOT vector length; small book 24.6 s → scales with book size too. |
| 2 | **All-sets enumeration of heavy measures** | `Risk HHI` × all 130 sets **fails** at ~11 s (reproducible); × the 7 real sets **0.48 s**. `Model vol` × 130 = 1.05 s vs × 7 = 0.12 s. The 123 PIT sets are 95% of the set count and are only ever addressed by name (`/trends` honest-vol), never enumerated. |
| 3 | **Date-unsliced cross-book vector queries** | `Scenario VaR 99` by Book with **no Date filter**: 60.2 s (succeeded on an idle 32 G cube; the same shape 500'd on the loaded production cube — it sits exactly at the limits). **Date-sliced, the same query over all 123 books is 0.76 s.** This revises the earlier "all-books queries are out of contract" claim: they are fine — *unsliced-Date* × books × vectors is the killer. |
| 4 | **Dead columns in the Positions load** | positions with MV+ADV: 6.6 s; Weight-only: 5.0 s (−25%; MV/ADV are never read by any measure). |
| 5 | **Memory retention after bursts** | suite peak 41 G, settling only slowly; the production cube has twice needed a bounce after heavy use. |

## The staged plan

Each step: implement → `cube_bench.py <out>.json` → `--diff` vs baseline → accuracy gates
(`test_model_vol.py`'s 5e-10 tie-outs, `test_contributions.py`, `test_stress.py`,
`test_whatif.py` verification blocks — every cube number is twinned by numpy, so a silent
numeric change cannot hide). One step per commit, never two at once.

**Step 1 — pad the scenario vectors, delete the ScenarioDay clamp.** (Hotspot 1.)
Today every ScenarioDay member evaluates `tt.where(_day < n, vector[_safe], None)` — three
array reads + a branch per member × 2,618 members × every measure on the axis. Pad every set's
ShockVec/DateVec to N with NaN **at build time** (`build_scenarios`), and the per-day measures
collapse to a bare `vector[_day]` (NaN past the set's real length; `NON EMPTY` already trims).
Expected: large constant-factor cut on the 10–50 s path and the +14 G growth; possibly makes
day × Sector viable. Risks: `Scenario n` must switch from `array.len` to a real length column
on ScenarioAxis (else tail sizes `_k975`/`_k99` and the exceedance rate read padded lengths);
`array.std/quantile/n_lowest` NaN semantics must be verified — if any helper doesn't skip NaN,
padding needs a companion mask or this step is off. Verify first on a scratch session.

**Results (2026-08-14): NOT DONE — the verification gate failed, and the safe fallback measured
worse.** The scratch probe (atoti 0.9.15, one 5-element vector loaded twice, once padded to 8
with NaN) says the array helpers **propagate NaN, they do not skip it**. NB the padded vector
can't even be loaded through `read_pandas` — pandas→arrow maps NaN to arrow NULL and the loader
rejects it (`IllegalStateException: Value at index is null`); it takes `read_arrow` with
`from_pandas=False`. Measured, padded vs true:

| helper | true (len 5) | padded (len 8, 3×NaN) | verdict |
|---|---|---|---|
| `array.len` | 5 | 8 | reads the padding |
| `array.mean` / `std` / `sum` | 0.006 / 0.03647 / 0.03 | NaN / NaN / NaN | propagates |
| `array.quantile(0.2)` | −0.024 | −0.008 | wrong (quantile over n=8) |
| `array.quantile_index(0.2)` | 3 | 1 | wrong |
| `array.min`, `n_lowest(2)` mean | −0.04, −0.03 | −0.04, −0.03 | right by luck (NaN never wins a min) |
| `vector[6]` (past real length) | error | NaN | the one thing padding would buy |

So VaR/ES/vol on every non-HistFull set would go NaN. A companion mask can't rescue it either —
there is no elementwise NaN filter in the array API, only length-preserving `positive_values`/
`negative_values`. Padding is off, permanently, on this SDK.

The one numerically-safe half of the step was then tried alone and **also reverted**: making
`Scenario n` a physical `VecLen` column on `ScenarioAxis` (instead of `tt.array.len` of the P&L
vector), so the ScenarioDay in-range gate stops depending on the whole vector being evaluated.
Harness (`docs/cube_bench_step1_20260814.json`) says it is **3.4× worse**: `scenario_day_path`
10.00 s → 33.93 s cold (warm 10.91 → 33.52), `scenario_day_by_sector` still fails but now at
42.9 s instead of 19.5 s, and the set-enumeration family regressed too (`var99_by_set_all130`
0.59 → 1.44 s, `model_vol_by_set_all130` 1.05 → 2.75 s, `hhi_small_book_all130` 0.17 → 1.69 s).
Reading a joined scalar column per parameter-hierarchy member is evidently dearer than reading
the length off the vector the cell has already materialised. Build time and JVM RSS were
unchanged (56.2 s, 7.8 G). **Hotspot 1 is therefore unaddressed** — it needs a different idea
(see Step 7's note), not a variant of this one.

**Step 2 — split the PIT sets out of the browsable ScenarioSet.** (Hotspot 2.)
Move `PIT:*` rows to their own table/hierarchy (`PITSet`) with a mirrored vector measure used
only by the honest-vol path, leaving `ScenarioSet` with the 7 real sets. Every `group by
ScenarioSet` (the notebook idiom, the Risk HHI OOM, `/dims` members) drops 130 → 7
evaluations. Expected: `hhi_by_set` goes from *failure* to ~0.5 s; the Vanguard notebook's HHI
cell un-patches. Touches: `build_scenarios`/`build_cube`, `/meta.pit_sets`, `/trends`' PIT
addressing — the set NAMES don't change, only which hierarchy carries them.

**Results (2026-08-14): DONE, kept** (`docs/cube_bench_step2_20260814.json`). `build_pit_scenarios`
emits the PIT rows keyed (PITSet, Factor) into their own `PITScenarios` table, partial-joined on
Factor exactly like `Scenarios`, with three mirrored measures — `PIT Scenario PnL vector` /
`PIT Scenario PnL vol` / `PIT Model vol`, precisely what `_pred_book_vols` consumes. ScenarioSet
is down to the 7 real sets. Measured, cold/warm:

| query | baseline | step 2 |
|---|---|---|
| `hhi_by_set_all130` | **FAILS** at 10.98 s | **0.70 / 0.02 s** (7 rows) |
| `model_vol_by_set_all130` | 1.05 / 0.98 | 0.24 / 0.02 |
| `var99_by_set_all130` | 0.59 / 0.47 | 0.26 / 0.02 |
| `hhi_small_book_all130` | 0.17 / 0.18 | 0.19 / 0.03 |
| build_cube | 55.98 s | 60.45 s (+8%, the extra table load) |
| JVM after build | 7.84 G | 7.34 G |

The `*_real7` queries are unchanged warm (0.03 s) and a shade slower cold (0.11→0.21, 0.48→0.64)
— first-touch noise, they now hit a colder cache. Everything else in the suite is flat.

Two things the run flagged that are NOT regressions, both re-measured in a controlled A/B (same
process, same query order, only the cube code differing):
- `scenario_day_path` printed 10.0 s at baseline and 51.5 s here — but on an isolated probe the
  same query is **42.0 s pre-split vs 44.3 s post-split**. The harness figure for this one query
  is heap-state noise (it runs when the JVM sits at 13 G in one run and 30 G in the other); the
  PIT split costs it ~5%.
- `/dims` takes ~20 s on this build, which is why several integration test-modules silently SKIP
  (their `_backend_up()` probes `/dims` with a 5 s timeout). Pre-existing and unrelated: the cost
  is `contributors.COUNT` **by Book** over 6 M leaf rows. Step 2 in fact **improves** it —
  measured 19.9 s → 14.9 s total (Book alone 17.5 → 13.1 s), because a fact now fans over 7
  ScenarioSet members instead of 130.

API side: `/meta.pit_sets` reads the new hierarchy; `_pred_book_vols` reads the mirrored measures
(the whole point of the step is that this path's behaviour is unchanged); and the "addressable by
name" contract is preserved — `_pit_addressing` rewrites a `PIT:*` ScenarioSet filter in
`/pivot`/`/analysis` onto PITSet + the mirrored measures and renames the columns back, `/trends`
does the same via `_set_context`, and the routes that need the full engine (`/risk`,
`/attribution`, `/timeseries`, `/scenario_pnl`, `/limits`, `/backtest`, `/drawdown`) 400 on a PIT
name via `_reject_pit` rather than returning a silently empty body. Accuracy gate: all of
`test_model_vol` (15/15, incl. `t_pit_sets_identities`, which addresses a PIT set through
`/pivot`), `test_contributions`, `test_whatif`, `test_analysis` pass; `test_stress` is 16/1 with
the one failure `t_meta_serves_managers` pre-existing (it reads `dims["members"]["Book"]`, stale
since the Book→Manager API rename).

**Step 3 — name the shared decomposition sub-expressions.** (Analysis item, unmeasured — the
harness diff IS the experiment.) `tt.total(m["Marginal Total VaR 99"], …)` is constructed four
times (`_tot_r`, `book_total`, and inside two `%`-measures); `tt.array.std(book_pnl_vec)` is
rebuilt inside `_cov_book`. Define each once as a hidden named measure and reference it.
Zero-risk numerically (same DAG); the diff shows whether ActivePivot was already deduplicating
anonymous subtrees or not. If warm decomposition times don't move, revert for readability's
sake or keep for maintainability — either way the question is settled by data.

**Results (2026-08-14): implemented, measured, REVERTED — the answer is "ActivePivot was already
deduplicating"** (`docs/cube_bench_step3_20260814.json`, diffed against Step 2). Twelve book-level
lifts were named as hidden measures via a `_book(name, measure)` helper — `Book Marginal Total
VaR 99` (4 call sites), `Book Scenario VaR 99` (2), `Book Specific variance` (2), `Book PnL
vector`, `Book PnL vol` (the `tt.array.std` rebuilt inside `_cov_book`), `Book Model vol`, `Book
Total VaR 99`, and the three `Book Marginal *` denominators. The decomposition family did not
move: `marginal_tvar_by_position` warm 0.39 → 0.41 s, `pct_model_vol_by_position` 0.42 → 0.41,
`top5_share_scalar` 0.01 → 0.01, `hhi_scalar_histfull` 0.01 → 0.01, `marginal_var_by_factor`
0.05 → 0.06. Nothing else in the suite moved beyond the ±30% cold-query noise band either
(`model_vol_by_book_123` 0.60 → 1.01 s is the largest, and it sits inside the run-to-run spread
seen across the four runs so far). **Build time went the wrong way: 60.5 s → 71.3 s** — twelve
more registered measures is not free at cube-construction time. No query win, a build cost, and a
dozen extra measures on the cube surface: reverted. The duplication in the source is a
readability question, not a performance one.

**Step 4 — slim the Positions load + build-time prep.** Feed `read_pandas` only
(Date, Book, Position, Weight) [−25% on that stage, −~185 MB JVM]; move the attribution prep
(the `w` dedupe, FactorPnL/SpecPnL derivation, lines 175–213) into the builder as persisted
columns so `build_cube` stops doing pandas work on every restart. Target: build 56 s → ~40 s.
Optionally: `partitioning=` on the two big tables (0.9.15 supports it) — measure with the
harness before keeping.

**Results (2026-08-14): DONE, kept** (`docs/cube_bench_step4_20260814.json`). `read_pandas` now
takes `positions[POSITION_CUBE_COLS]` = (Date, Book, Position, Weight); MV and ADV never cross
into the JVM. The harness's own H3 micro-bench is the clean A/B for this (same process, same
frame): **6.42 s full-width → 5.14 s slim, −20%**. `load_frames` is untouched, so
`S["frames"]["positions"]` keeps MV/ADV for `/liquidity`, `/whatif`'s editor and the funnel.
One coupled change: `_whatif_branch_rows` reads the live table's column list
(`session.tables["Positions"].columns`) and emits exactly those, so a branch load matches the
narrower table instead of restating the schema — `test_whatif` (9/9, incl. the branch-backed
`t_whatif_served_from_cube`) is the gate on that.

The attribution prep is **forward-compatible, not moved**: `build_cube` skips the `w` dedupe and
the FactorPnL/SpecPnL derivation when the frames already carry them — `FactorPnL` as a column on
`exposures` plus an optional `specific_pnl` frame (Date, Position, SpecPnL), now listed in
`OPTIONAL_FRAMES`. Nothing writes them today (no rebuild was in scope), so today's path is
byte-identical; a builder that persists them takes that pandas work out of every cube start-up
with no further cube change.

Build time did NOT visibly move: 60.5 s (Step 2) → 63.7 s. That is the harness's noise floor
talking — `build_cube` has now been measured at 56.0, 56.2, 60.5, 71.3 and 63.7 s on identical
data, so a genuine ~1.3 s saving is invisible in it; the micro-bench is where this step's win is
legible. **The "build 56 s → ~40 s" target in the original plan is therefore not met and will not
be met by this step**: `read_pandas` of the 6 M-row `exposures` table is the dominant term, not
Positions. No query in the suite regressed.

**Step 5 — memory hygiene.** Add `-Xms2g -XX:G1PeriodicGCInterval=300000` to the
`java_options` so idle heap returns to the OS between bursts (today: manual service bounces).

**Results (2026-08-14): DONE, kept** (`docs/cube_bench_step5_20260814.json`). The harness cannot
see this step — it never idles, so the periodic cycle never fires: `jvm_rss_after_suite` 10.62 →
10.59 G, suite peak 41 G either way, every query inside the run-to-run noise band. The gate is
the **live service**, sampled once a minute after the accuracy suite drove it hard:

| t after suite | JVM RSS |
|---|---|
| 0 | 18.36 G |
| 1–5 min | 9.89 G (flat) |
| 6 min | 9.68 G |
| 7–9 min | **8.07 G** (flat) |

The step change at 6–7 min is the periodic concurrent cycle firing on the 5-minute interval and
uncommitting to the OS — 1.8 G that G1 would otherwise have sat on until the next allocation
pressure. Flags confirmed live on the process (`-Xmx32g -Xms2g -XX:G1PeriodicGCInterval=300000`).
`cube.aggregate_cache` is left unbounded: Step 3 showed the warm/cold gap is not where the cost
is, so there is no measured basis for a bound yet.
Bound `cube.aggregate_cache` explicitly once Step 3's data shows how much the cache is worth.

**Step 6 — policy, not engine: Date-context enforcement in `/pivot`.** (Hotspot 3.) When a
scenario-family measure is queried with Book on an axis and NO Date in context, `/pivot`
already warns about missing ScenarioSet context; extend the same warning-or-default to Date
(default = latest COB, disclosed in the response). Turns the 60-s/borderline-failure shape
into the 0.76-s shape without touching the cube.

**Results (2026-08-14): DONE, kept.** `_needs_date_default(mlist, axis, fdict)` — pure, so it is
unit-tested with no cube (`test_analysis.py::t_date_default_only_for_the_pathological_shape`) —
fires only on the measured shape: a `SCEN_DEP` measure **and** Manager on an axis **and** no Date
in the filters or on an axis. `_pivot_result` then injects the latest COB and appends a sentence
to the response `warning` (which became a joined list, so it can carry the ScenarioSet note too).
Live gate, `GET /pivot?rows=Manager&measures=Scenario VaR 99&filters={"ScenarioSet":["HistFull"]}`:
**1.08 s, 124 rows, HTTP 200**, against the 60.2 s (idle) / HTTP 500 (loaded) baseline for the
same request. A Date filter, a Date on the axis, an additive-only measure list or a non-Manager
axis all leave the query exactly as the caller wrote it — this defaults one shape, it does not
rewrite requests. `docs/cube_bench_step6_20260814.json` is the final-state harness run (the cube
is byte-identical to Step 5; it doubles as the post-program baseline).

**Step 7 — evaluate an aggregate provider (only if Steps 1–4 leave a gap).** A partial bitmap
provider at (Date, Book, Factor) pre-aggregates `Net exposure` for the scenario engine. The
baseline says routine queries don't need it — this is insurance for future scale (more books,
daily calendar), gated like everything else on a harness diff and a what-if branch check
(providers must respect source scenarios, or the branch-sensitivity design breaks).

**Assessment (2026-08-14): NOT warranted, not implemented.** After Steps 2/4/5/6 every routine
query in the suite is 0.03–0.5 s cold and 0.01–0.05 s warm, the all-sets family no longer fails,
and the one query shape that could take a minute is now bounded by policy at 1.1 s. A provider
would buy nothing there. The two things still slow are the two a `Net exposure` provider does not
address:
- **ScenarioDay unpacking** (10–50 s, and the × Sector variant still fails) is a *per-member*
  evaluation over a 2,618-member parameter hierarchy, not an exposure-aggregation cost — which is
  exactly why its warm time equals its cold time (nothing to reuse) and why pre-aggregating
  `Net exposure` at (Date, Book, Factor) leaves the 2,618 vector index reads untouched. Note also
  how narrow this hotspot is in practice: the UI's per-day path comes from `/scenario_pnl`, which
  reads the whole P&L vector + its date dual in ONE query (`pnl_vector_book`: **0.04 s cold**).
  The ScenarioDay dimension is used only by the notebook idiom and a hand-built pivot. Step 1
  proved padding can't fix it on this SDK; the next idea worth testing is capping the parameter
  hierarchy (e.g. the last 500 days) or serving the drill from the vector in the API.
- **Build time** (~64 s) is dominated by `read_pandas` of the 6 M-row `exposures` table. A
  provider adds build time, it doesn't remove any.

Also unresolved by a provider and worth flagging as the real next candidate: `/dims` at ~15 s,
whose cost is `contributors.COUNT` **by Book** over those 6 M rows (13.1 s of the 14.9 s total,
measured). That is a member-enumeration problem — the cheap fix is to enumerate Book from the
positions frame instead of the cube, not to build an aggregate store. Revisit the provider only
if the calendar goes daily or the book count grows again, and gate it on the same harness diff
plus a what-if branch check (a provider that ignores source scenarios silently breaks the
branch-sensitivity design).

**Explicitly not doing**: renaming/restructuring the vector tail read-off (`Marginal *` family)
— measured fast (≤0.5 s for 3,635 names); the notebook-vs-API duplicate cube (a per-context
choice, not an engine cost); `build_scenarios` pandas micro-optimizations (seconds, once).

## Measurement discipline

- Baseline and every step's JSON live beside the harness run (`cube_bench.py --diff a b`).
- The harness runs on an otherwise idle box (bounce `flexagg-api` first) — the production 500s
  vs probe successes on identical queries show ambient load moves borderline results.
- Cold numbers on a fresh cube differ from warm-cube colds (ScenarioDay: 50 s vs 10 s) — diffs
  compare like with like because the suite order is fixed.
- Accuracy gates run after every step; a performance win that moves a tie-out beyond its
  pinned tolerance is a bug, not a win.

## What the programme actually delivered (2026-08-14)

Kept: **Steps 2, 4, 5, 6**. Rejected on measurement: **Steps 1 and 3**. Not implemented: **Step 7**
(assessed above). Final state = `docs/cube_bench_step6_20260814.json`, diffed against
`docs/cube_bench_baseline_20260814.json`:

| | baseline | final | |
|---|---|---|---|
| `hhi_by_set_all130` | **FAILS** at 10.98 s | 0.48 / 0.02 s | the failure is gone |
| `model_vol_by_set_all130` | 1.05 / 0.98 s | 0.14 / 0.02 s | 7.5× / 49× |
| `var99_by_set_all130` | 0.59 / 0.47 s | 0.15 / 0.03 s | 3.9× / 16× |
| `hhi_small_book_all130` | 0.17 / 0.18 s | 0.04 / 0.03 s | 4× / 6× |
| `Scenario VaR 99` by Manager, no Date (live API) | 60.2 s idle / 500 loaded | **1.08 s** | Step 6 |
| positions load (micro-bench) | 6.59 s | 4.89 s | −26% |
| `/dims` member enumeration | 19.9 s | 14.9 s | side-effect of Step 2 |
| idle JVM RSS after a heavy burst | held at ~18 G | 8.1 G after ~7 min | Step 5 |
| `build_cube` | 55.98 s | 64.38 s | **worse** — see below |
| every routine sliced query | 0.03–0.5 s cold | 0.03–0.5 s cold | flat |

Two honest caveats on those numbers.

**Build time went up ~8 s.** The PIT table is a second 123-set × 23-factor vector load (~+4.5 s)
and the slim Positions load gives ~1.3 s back. `build_cube` measured 56.0, 56.2, 60.5, 71.3,
63.7, 64.4 s across the six runs, so the run-to-run spread is ±5 s and only the direction is
reliable. This was a deliberate trade: a one-off 8 s at start-up bought a query that used to fail
outright.

**`scenario_day_path` is not a reliable harness number.** It printed 10.0, 33.9, 51.5, 44.6,
13.6, 24.1, 25.6 s across the seven runs on three different builds. Isolated A/B (same process,
same order, cube code the only variable) puts the PIT split at 42.0 → 44.3 s, i.e. ~5%. Whatever
drives the 4× spread is JVM heap/GC state at the point in the suite where the query runs, not the
code under test. Read that row only against a controlled probe, never off the diff.
