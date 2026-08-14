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

**Step 2 — split the PIT sets out of the browsable ScenarioSet.** (Hotspot 2.)
Move `PIT:*` rows to their own table/hierarchy (`PITSet`) with a mirrored vector measure used
only by the honest-vol path, leaving `ScenarioSet` with the 7 real sets. Every `group by
ScenarioSet` (the notebook idiom, the Risk HHI OOM, `/dims` members) drops 130 → 7
evaluations. Expected: `hhi_by_set` goes from *failure* to ~0.5 s; the Vanguard notebook's HHI
cell un-patches. Touches: `build_scenarios`/`build_cube`, `/meta.pit_sets`, `/trends`' PIT
addressing — the set NAMES don't change, only which hierarchy carries them.

**Step 3 — name the shared decomposition sub-expressions.** (Analysis item, unmeasured — the
harness diff IS the experiment.) `tt.total(m["Marginal Total VaR 99"], …)` is constructed four
times (`_tot_r`, `book_total`, and inside two `%`-measures); `tt.array.std(book_pnl_vec)` is
rebuilt inside `_cov_book`. Define each once as a hidden named measure and reference it.
Zero-risk numerically (same DAG); the diff shows whether ActivePivot was already deduplicating
anonymous subtrees or not. If warm decomposition times don't move, revert for readability's
sake or keep for maintainability — either way the question is settled by data.

**Step 4 — slim the Positions load + build-time prep.** Feed `read_pandas` only
(Date, Book, Position, Weight) [−25% on that stage, −~185 MB JVM]; move the attribution prep
(the `w` dedupe, FactorPnL/SpecPnL derivation, lines 175–213) into the builder as persisted
columns so `build_cube` stops doing pandas work on every restart. Target: build 56 s → ~40 s.
Optionally: `partitioning=` on the two big tables (0.9.15 supports it) — measure with the
harness before keeping.

**Step 5 — memory hygiene.** Add `-Xms2g -XX:G1PeriodicGCInterval=300000` to the
`java_options` so idle heap returns to the OS between bursts (today: manual service bounces).
Bound `cube.aggregate_cache` explicitly once Step 3's data shows how much the cache is worth.

**Step 6 — policy, not engine: Date-context enforcement in `/pivot`.** (Hotspot 3.) When a
scenario-family measure is queried with Book on an axis and NO Date in context, `/pivot`
already warns about missing ScenarioSet context; extend the same warning-or-default to Date
(default = latest COB, disclosed in the response). Turns the 60-s/borderline-failure shape
into the 0.76-s shape without touching the cube.

**Step 7 — evaluate an aggregate provider (only if Steps 1–4 leave a gap).** A partial bitmap
provider at (Date, Book, Factor) pre-aggregates `Net exposure` for the scenario engine. The
baseline says routine queries don't need it — this is insurance for future scale (more books,
daily calendar), gated like everything else on a harness diff and a what-if branch check
(providers must respect source scenarios, or the branch-sensitivity design breaks).

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
