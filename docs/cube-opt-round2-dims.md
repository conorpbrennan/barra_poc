# Cube optimization round 2 — `/dims` member enumeration (2026-08-15)

Follows on from `docs/cube-optimization-plan.md` Step 7's note: `/dims` costs ~15–28s on the
124-book build, ~85–90% of it a single `contributors.COUNT` groupby on the `Book` level (the
Manager dimension) — a fan-out `contributors.COUNT` over the 6M-row `Exposures` fact table
partially joined to the 11.6M-row `Positions` table (one exposure row can match MANY position
rows, one per book that holds that name that date). The other nine dimensions sum to a further
~1.5–3.9s (run sequentially, each cheap alone but ten in a row adds up). Target: `/dims` end-to-
end < 1.0s first call, < 50ms cached, member lists byte-identical to today's cube-derived
response (same sets, including the join-artifact `"N/A"` Book member and the `PIT:*` `ScenarioSet`
exclusion), no response-schema change.

Ground truth captured across four separate builds (124-book build, `BARRA_CUBE_XMX=24g`,
`contributors.COUNT` grouped by each dimension's level — today's pre-2026-08-15 `/dims` logic,
run in-process, sequential):

| dim | members | cold time (range across 4 runs) |
|---|---|---|
| Date | 126 | 0.61–0.82s |
| **Manager (Book)** | **124** (123 real + `"N/A"`) | **13.17–25.39s** |
| Country | 44 | 0.09–0.47s |
| Sector | 12 | 0.09–0.18s |
| Issuer | 4938 | 0.35–0.75s |
| Position | 5173 | 0.36–0.86s |
| FactorGroup | 3 | 0.05–0.14s |
| Factor | 23 | 0.05–0.09s |
| ScenarioSet | 7 | 0.06–0.11s |
| ScenarioDay | 2618 | 0.16–0.40s |
| **TOTAL** | | **15.04–27.84s** |

The spread run-to-run (e.g. Manager alone: 13.17s one run, 25.39s another, same code, same
data) is the box's own noise, not the query — this machine is heavily shared (a sibling agent's
own cube_bench + several unrelated CPU-bound processes; `uptime` showed load average 7.75–9.4 on
20 cores during this work). Every timing below should be read as "this much faster", not as an
absolute SLA, for that reason — flagged again in Attempt 4/5's numbers where it bites hardest.

## Attempt 1 — frames-derived (REDIRECTED by explicit user directive mid-attempt, not completed)

The original seed idea: every member list is already a key/dimension column in the pandas
frames `S["frames"]` holds (positions.Book, securities.Country/Sector/Issuer, exposures.Position/
Factor/Date, factor_meta.FactorGroup, the scenario-set names from `build_scenarios`/
`build_scenario_axis`), so compute distincts in pandas and skip the cube entirely.

Implemented and benchmarked (not committed): pandas-only distinct-value computation off the
already-loaded frames, replicating each hierarchy's exact join semantics (Security's Position
level is backed by Exposures not Securities, so Country/Sector/Issuer are filtered to positions
that actually carry an exposure row; Manager's `"N/A"` member is a join artifact — an exposure
`(Date, Position)` unheld by ANY book that date — detected via an anti-join). Measured (warm
frames already in memory, no cube):

| step | time |
|---|---|
| uniques (Position/Factor) | 0.26s |
| Date | 0.01s |
| Position | <0.01s |
| Country/Sector/Issuer (securities filtered to exposed positions) | <0.01s |
| FactorGroup/Factor | <0.01s |
| **Manager + N/A anti-join** (`merge(indicator=True)` on 527k/408k deduped Date×Position pairs) | **1.1s** (naive `drop_duplicates` on the full 6M/11.6M frames first: 0.4s + 0.58s + 0.09s merge) |
| ScenarioSet + ScenarioDay (`build_scenarios`/`build_scenario_axis` on `factor_returns`, <1MB) | 0.03s |
| **TOTAL** | **~1.4–1.6s** |

Correctness: the anti-join's `has_na=True` finding for the Manager `"N/A"` member matched the
live cube's ground truth exactly (both find it, later confirmed twice more) — the mechanism is
understood: it's a fan-out join artifact, not noise, matching `frontend/src/context/
AppContext.tsx`'s defensive filter comment ("the live /dims endpoint has been observed reporting
an extra 'N/A' Book member... cause not identified").

Timing was close to the <1.0s bar (~1.4–1.6s) but not yet under it, and the approach was cut
short here: **the user issued an explicit directive mid-attempt overriding the frames-derived
seed idea** — reading cube-owned data out of the parquet frames instead of the cube undermines
the project's thesis that the cube does the work. Discarded, not committed. Redirected to a
cube-native approach (engine-level queries only: `Level`/`Hierarchy` member APIs, MDX bare
`Members`, or `Table.query` against the live Atoti tables — never `S["frames"]`/
`pandas.read_parquet` as the source of the member lists).

## API surface explored (atoti 0.9.15)

`tt.Level` has no `.members` accessor; `tt.Hierarchy.members_indexed_by_name` is a config flag,
not a data accessor. `tt.Table.query(*columns, filter=, max_rows=, timeout=)` DOES exist — a
row-level query against the live in-memory table (the engine's own copy, not a parquet re-read),
with no server-side DISTINCT/GROUP BY. `Session.query_mdx` exists too (untested in the end — the
Table-level approach below won out before an MDX pass was needed).

## Attempt 2 — planned exploration battery (never ran — lock contention)

A combined script testing five candidates in one build (`cube.query(levels=[...])` with no
measure; the same with `include_empty_rows=True`; MDX bare `Members`; `Table.query` on
Positions/Exposures directly; a single-cell filtered `contributors.COUNT`) was written but never
executed — the shared memory lock (`/tmp/cube_bench_global.lock`) was held by a sibling agent's
own multi-minute benchmark for the whole window. Superseded by Attempt 3, which folds the two
candidates that mattered (`Table.query`, filtered single-cell) into one run.

## Attempt 3 — cube-native, first working version

Design: (a) the nine cheap dimensions run CONCURRENTLY via `ThreadPoolExecutor`, the exact same
`contributors.COUNT` query each — correctness untouched by construction, only wall time; (b)
Book/Manager's real members come from `Table.query` on the Positions table's `Book` column
directly (bypasses the Exposures↔Positions fan-out join entirely — reads the table, not the
cube); (c) the `"N/A"` member's existence is checked with a SINGLE-CELL FILTERED
`contributors.COUNT` (`Book.isin("N/A")`) instead of enumerating and counting all 124 members.

Measured (one build, in-session A/B against a fresh sequential ground truth):

| candidate | time | correct? |
|---|---|---|
| 9 cheap dims, concurrent | 0.635s | yes, all 9 match |
| Book real members, `Table.query` full column (11.6M rows → 123 distinct) | 3.481s | yes |
| `"N/A"` existence, single-cell filtered `contributors.COUNT` | 1.289s (vs 13.17s for the full 124-member groupby) | yes |
| Manager candidate total | 4.770s | **matches truth exactly (124)** |
| **FULL end-to-end, everything concurrent** | **4.193s** | **all 10 dims match** |

First real win: **15.04s → 4.193s** (3.6×), fully correct. Not yet under 1.0s — `Table.query`'s
11.6M-row transfer dominates.

## Attempt 4 — replace the full-column transfer with per-candidate existence checks

Instead of pulling the WHOLE Positions.Book column (11.6M rows) to dedupe client-side, fetch
candidate names from the tiny (~124-row) `Managers` table (near-instant), then check EACH
candidate's existence in Positions with its own `Table.query(filter=Book==name, max_rows=1)` —
letting the engine short-circuit on the first match — run CONCURRENTLY in a 32-worker pool.

Measured:

| step | time |
|---|---|
| candidate names from Managers table | 0.072s |
| 123 concurrent existence checks (max_rows=1) | 0.972s |
| `"N/A"` filtered check | 1.193s |
| Manager candidate total | 2.237s, **matches truth exactly** (123 real + `"N/A"` = 124; the one non-real candidate, correctly excluded, is MetLife — CLAUDE.md: "stays in MANAGERS but never reaches the cube/UI", confirmed independently here) |
| **FULL end-to-end (9 cheap dims + manager path, concurrent, nested pools)** | **1.524s cold, 1.394s on an immediate repeat** | **all 10 dims match** |

Second win: **15.04s → 1.524s** (9.9×), fully correct. Close to the 1.0s bar but not under it.

## Attempt 5 (final) — flatten to one pool (tested, REJECTED — made it worse)

Hypothesis: nesting a 32-worker pool inside a 10-worker outer pool adds coordination overhead;
submitting all ~133 tasks (9 cheap dims + 123 existence checks + 1 N/A check) to ONE big flat
pool should let the JVM see the whole workload at once and finish sooner.

Measured: **worse**, not better — COLD 2.755s, WARM (uncached repeat) 1.595s, both slower than
Attempt 4's nested version on the same box. Correctness still exact (all 10 dims match; cache-hit
timing separately confirmed at ~0.0001ms). Too much concurrent JVM query dispatch contends with
itself; small, separately-pooled batches of concurrent work beat one giant flat one on this SDK/
engine. **Rejected — Attempt 4's nested-pool structure is what shipped.**

## Final implementation (`python_src/risk_api.py`)

`_dims_response()` — cached on a cube-identity token (`id(cube)`; recomputes only if the cube is
ever rebuilt in-process, which never happens today: frames/cube load once at startup and are
held for the process lifetime). On a cache miss: the nine cheap dimensions run concurrently via
`_dim_members_via_count` (byte-identical `contributors.COUNT` query to the pre-2026-08-15 code,
just not serialized); Book/Manager is answered by `_manager_members` — candidates from the
`Managers` table, existence checked per-candidate in a 32-worker pool, `"N/A"` via the filtered
single-cell query — with a fallback to the Attempt-3 full-column `Table.query` scan when
`managers.parquet` isn't loaded (pre-Phase-2 builds / v1 data have no Managers table). A
try/except around the whole fast path falls back to `_dims_response_fallback()` — the original,
unmodified sequential `contributors.COUNT` logic — if anything about the cube-native fast path
raises, so `/dims` can never break outright even on an unanticipated cube shape.

**A transcription bug was caught and fixed by the final gate**: the first commit-candidate used
`max_workers=max(8, len(candidates))` (124 workers) for the existence-check pool instead of the
fixed 32 that Attempt 4 measured as best — one worker per candidate is exactly the "too much
concurrency" failure mode Attempt 5 already identified, just inside the manager path instead of
across the whole call. Fixed to a flat `max_workers=32`; re-validated below.

## Final validation (independent of the exploration scripts — imports the actual module)

Two full builds, each importing `risk_api` as a module, populating `S` exactly as `lifespan()`
does, calling the real `_dims_response()`, and diffing against an INDEPENDENTLY recomputed
sequential ground truth (not `_dims_response_fallback` — a fresh loop written in the gate script
itself):

| run | ground truth Manager time | `_dims_response()` COLD | cached (max of 5 calls) | all 10 dims match |
|---|---|---|---|---|
| gate 1 (pre-fix, 124-worker bug) | 13.49s | 2.863s | 0.0050ms | **yes** |
| gate 2 (post-fix, 32-worker) | 16.08s | 3.966s | 0.0031ms | **yes** |

Both gate runs pass correctness with zero discrepancies across all 10 dimensions, every run.
Both ran under confirmed heavy external contention on the shared box (`uptime` load average
7.75–9.4 on 20 cores, an unrelated CPU-pegged test suite mid-run) — the SAME code measured 1.4s
in Attempt 4 under lighter load and 2.9–4.0s here under heavier load. This is the honest, full
picture: **the query-time reduction is real and large (15–28s → 1.4–4.0s, 85–95%+ depending on
ambient load) and correctness is exact and independently re-verified twice, but the hard <1.0s
COLD bar is not reliably met under this box's contention** — it IS met when the box is quiet
(Attempt 4: 1.394–1.524s, close but technically over; no run in this whole program landed under
1.0s cold). The cached path meets its <50ms bar by four orders of magnitude margin (~0.003ms) in
every run, so the practical UI experience (one cold hit per process lifetime, everything after
instant) is solved regardless of ambient noise.

## What's committed

- `python_src/risk_api.py`: `_dims_response`, `_dim_members_via_count`, `_manager_members`,
  `_dims_response_fallback`, `_DIMS_CACHE`, `ThreadPoolExecutor` import. `/dims` itself is now a
  one-line `run_in_threadpool(_dims_response)`. Response schema is byte-identical to before
  (`dimensions`, `measures`, `scenario_dependent`, `members`, `dates`, `scenario_sets` — the same
  keys, same shapes; `frontend/src/pivot/FieldList.tsx` and `frontend/src/context/AppContext.tsx`
  consume it unchanged, including the pre-existing defensive `"N/A"` filter in AppContext, which
  still fires exactly as before since the member set is unchanged).
- Not committed (scratch-only): the five exploration scripts, kept under the session scratchpad,
  not the repo.

## STOP condition

Five attempts were run (frames-derived/redirected, the unexecuted battery, Table.query-full-
column, per-candidate existence checks, the flattened-pool rejection), per the task's cap. The
implementation that shipped is Attempt 4's design (with the worker-count bug from the final gate
fixed). Iterating further on raw query time was not pursued past this point — the honest
current floor demonstrated is ~1.4s under light ambient load and does not reliably clear 1.0s
under this shared box's typical contention.
