# Cube optimization round 2 — the per-day drill (2026-08-15)

Round 1 (`docs/cube-optimization-plan.md`) left exactly one hotspot unaddressed: **hotspot 1,
the ScenarioDay unpacking**. Measured there: the book path costs ~10 s warm-cube / 25–50 s
cold-cube and grows the JVM by ~14 G, and `ScenarioDay × Sector` **fails** with an MdxException
after 20–60 s. The decisive finding was that an 82-day event set costs the SAME as the 2,618-day
HistFull — the cost is per-member evaluation machinery over a 2,618-member parameter hierarchy,
not data volume.

Two designs were already rejected in round 1 and are NOT retried here: NaN-padding the vectors
(atoti's array helpers propagate NaN, and `read_pandas` rejects NaN-in-vector outright), and a
physical `VecLen` column for the in-range gate (measured 3.4× worse — a joined-scalar read per
member costs more than the vector-length read).

Success metric for this round, fixed before the first attempt:

| gate | target |
|---|---|
| new per-day book path (Date+Book+set sliced, Vanguard/HistFull) | < 2.0 s cold on a fresh cube, < 0.5 s warm |
| new day × Sector | succeeds, < 5 s cold |
| JVM RSS delta over those queries | < 2 G |
| correctness | per-day values == the elements of `Scenario PnL vector` to 1e-12, HistFull + Evt:COVID2020, Vanguard + ThirdPoint, and the day count == each set's true length |
| no regression | full `cube_bench.py` suite, no other query worse by > 20% vs `docs/cube_bench_step6_20260814.json` |

The gate is `python_src/cube_bench_day.py` (fresh cube, timed cold/warm, psutil RSS sampling,
and the correctness check as a repeatable script — it exits non-zero if any gate fails).

## Attempt 1 — days as FACTS, not array indices

**The reframe.** The parameter hierarchy asks the engine the same question 2,618 times over
("what is element _i_ of this vector?"), and each answer materialises the whole vector — the
work is quadratic in the day count and nothing about it is cacheable. Explode the shocks into a
physical table at day grain instead, and a day stops being an array index: it is an ordinary
level, `PnL at day` is a plain additive sum-product over 23 factor rows, and `day × Sector` is a
plain pivot.

```
ScenarioDays   (DaySet, Factor, Day) -> ShockAtDay, DayEpoch        ~68k rows
PnL at day     = sum(Net exposure * single_value(ShockAtDay), scope=OriginScope({Factor}))
Date at day    = max(DayEpoch)     -- same value on all 23 factor rows; MAX avoids the fan-out
```

The table is derived from the `scenarios` / `scn_axis` frames the vector path already builds, so
element _i_ of a set's `ShockVec` and `Day = i` are the same number by construction rather than
by two code paths agreeing.

**Topology — three variants probed on synthetic data first** (a 4 G scratch session, no lock
needed; `atoti 0.9.15`):

| variant | join | result |
|---|---|---|
| A | `Scenarios.join(ScenarioDays, on ScenarioSet+Factor)` — the day table hanging off the existing set table, reusing the `ScenarioSet` hierarchy | **FAILS**: `IllegalArgumentException: ShockAtDay is not part of the cube's datastore selection`. The table is silently dropped from the cube's selection, and no `DayIndex` hierarchy is created. Declaring the join before the `Exposures → Scenarios` edge changes nothing. A second-hop join may not introduce a new key on this SDK. |
| C | `Exposures.join(ScenarioDays, on Factor)` + `ScenarioDays.join(Scenarios, on ScenarioSet+Factor)` — days at hop 1, the vector table hanging off THEM | works, and keeps ONE set hierarchy — but every existing vector measure would then read its `ShockVec` through a 2,618× day fan-out. Not worth the risk to the paths that are already fast. |
| B | `Exposures.join(ScenarioDays, on Factor)` alongside the untouched `Exposures → Scenarios` edge, with the day table's own key names (`DaySet`, `Day`) | **works**, and the existing scenario topology is bit-for-bit untouched. Chosen. |
| D | variant B + one manual two-level `Day` hierarchy (`DaySet`, `Day`) | **FAILS**: `The analysis hierarchies ScenarioDays/Day and ScenarioDays/DaySet are conflicting` — atoti auto-creates a hierarchy per un-mapped key and will not let a manual one reuse the field. Two flat hierarchies it is, exactly like `ScenarioSet`/`Book`. |

The cost of B is one duplicated concept: `DaySet` carries the same seven member names as
`ScenarioSet`, and the two per-day measures read `DaySet`. That is disclosed in the cube's
docstring comment rather than papered over.

**Measured (fresh cube, Vanguard, HistFull, 2,618 days).**

| | attempt 1 | old ScenarioDay path, same process |
|---|---|---|
| book path | 30.77 s cold / 32.31 s warm, **+20.4 G** | 25.30 / 28.05 s, peak 29 G |
| day × Sector | **198,968 rows in 35.8 s** | **FAILS** (BadArgument after 35 s, +15.0 G) |
| Evt:COVID2020 (82 days) | 0.83 / 0.29 s | — |
| small book (ThirdPoint), 2,618 days | 1.47 / 1.78 s | — |
| correctness | **exact**: max abs diff **0.0** and the date dual exact, on Vanguard and ThirdPoint × HistFull and Evt:COVID2020, day counts 2,618 / 82 | |

**Verdict: right numbers, wrong speed** — slower than the parameter hierarchy it replaces. But the
failure shape is informative and completely different from round 1's: an 82-day set now costs 37×
less than a 2,618-day set, and a small book 20× less than the largest one. Cost has stopped being
"per member of a big hierarchy" and become **days × book size** — i.e. the day table is joined to
the FACT table, so every one of the 2,618 day members re-derives the book's factor exposures from
its own cell's 84k (Position × Factor) leaves.

**Lesson feeding attempt 2:** the exposure term must be computed at a context that does not
contain Day, so it is computed once instead of 2,618 times.

## Attempt 2 — lift the day hierarchies inside the exposure term

One line: `tt.total(m["Net exposure"], h["DaySet"], h["Day"])` inside the sum-product, the same
`tt.total` idiom the decomposition measures already use to reach book totals from inside a cell.

| | attempt 1 | attempt 2 |
|---|---|---|
| book path | 30.77 / 32.31 s, +20.4 G | **12.51 / 12.87 s, +2.3 G** |
| day × Sector | 35.81 s | **19.07 s** |
| Evt:COVID2020 | 0.83 / 0.29 s | 0.41 / 0.29 s |
| small book, 2,618 days | 1.47 s | **0.37 s** |
| correctness | exact | exact |

2.5× on the book path and 9× less heap. Splitting the two measures showed they cost the same and
add up: `PnL at day` alone 6.78 s, `Date at day (epoch)` alone 6.33 s — a max over a joined column
costs as much as the whole risk arithmetic, so the per-member overhead, not the maths, is what is
left. Still 3× outside the gate.

**Lesson feeding attempt 3:** find out what the per-member floor actually is before designing
against it.

## Attempt 3 — measure the floor, and try the two remaining engine levers

Three probes on one fresh cube: (i) name the lifted exposure as its own measure (round 1's Step 3
question, re-asked for this shape); (ii) an aggregate provider at {Date, Book, Factor} — round 1's
un-implemented Step 7, which becomes relevant here because the cost is now exposure
re-aggregation, exactly what a provider pre-computes; (iii) scaling in the number of day members.

- **Named lifted measure: no effect** (21.4 s vs a 13.7 s control in the same process — noise, not
  a win). Round 1's finding stands: ActivePivot already dedupes the sub-expression.
- **Aggregate provider: HARD BLOCKED, not slow — impossible.**
  `Hierarchy[Positions, Book] cannot be part of the partial provider definition because it is an
  analysis hierarchy. This use case is not supported.` Every hierarchy this cube would want to
  provide on (Book, ScenarioSet, DaySet, Day) is an analysis hierarchy created by a partial join.
  That closes round 1's Step 7 for good, for any book-scoped measure, on this SDK.
- **Scaling is linear in day members** (Vanguard, HistFull): 21 days **0.56 / 0.14 s**, 250 days
  3.22 / 2.44 s, 1,000 days 6.81 / 4.25 s. ~5 ms per day member, and the warm/cold ratio decays as
  the member count grows — the aggregate cache helps a window and not a full history.

And the floor itself, on the same fan: **`contributors.COUNT` by Day costs ~16 s** — a measure that
does no arithmetic at all. Putting a fact-joined level with 2,618 members on the axis has an
irreducible per-member retrieval cost; no measure definition can go under it.

**Lesson feeding attempt 4:** if the per-member cost is a fact scan, cut what each member scans.

## Attempt 4 — is it the POSITION fan? (and the run-to-run variance that invalidated everything)

Lifted `h["Security"]` and `h["PositionRank"]` into the exposure term as well, giving a
book-level-only variant that cannot drill by sector, to see whether the per-member scan collapses.

| | control `PnL at day` | book-lifted variant | raw query mode |
|---|---|---|---|
| Vanguard, 2,618 days | 2.54 / 2.19 s | 2.29 / 2.10 s | 2.37 / 2.49 s |
| ThirdPoint, 2,618 days | 1.63 / 1.38 s | 1.43 / 1.52 s | 1.35 / 1.39 s |

The lift buys ~8%, inside the noise, and it costs the sector drill — **rejected**.

The real finding of attempt 4 is the control column. **The same query, same cube code, measured
13.68 s in attempt 3's process and 2.54 s here.** The variable is ambient load: this box has 20
cores shared with two sibling agents, and a sibling cube build or query burst moves these numbers
by up to 5×. Everything in attempts 1–3 was measured against a moving baseline; only same-process
A/Bs are trustworthy, which is why every table above quotes its own control. `cube_bench_day.py`
now records `loadavg` in its meta for exactly this reason.

Corrected for that, attempt 2's design was already at ~2.2 s/2,618 days, not 12.5 s.

**Lesson feeding attempt 5:** the remaining halving is not in the P&L measure — it is the date
label, which was still a measure.

## Attempt 5 (final) — the date as a LEVEL, not a measure

`DayDate` becomes a fourth key column on `ScenarioDays` (a real date, 1:1 with (DaySet, Day), so it
adds no rows), which auto-creates a `DayDate` hierarchy. The caller puts it on the axis beside
`Day` and reads the calendar date off the axis instead of aggregating a `max` per member.

Final state, fresh cube, `docs/cube_bench_day_20260815.json`:

| query | cold | warm | JVM Δ |
|---|---|---|---|
| book path, `levels=[Day, DayDate]` (the gated shape) | **2.48 s** | **2.10 s** | +0.9 G |
| book path, no date label | 2.17 s | 2.16 s | +0.0 G |
| book path with the date as a MEASURE (the attempt-2 shape) | 4.43 s | 4.41 s | +0.0 G |
| day × Sector, 198,968 rows | 14.09 s | 13.79 s | +1.3 G |
| **Evt:COVID2020, 82 days** | **0.12 s** | **0.04 s** | +0.0 G |
| small book, 2,618 days | 1.52 s | 1.59 s | +0.0 G |
| (`Scenario PnL vector`, the same 2,618 numbers in one cell) | 0.15 s | 0.02 s | +0.0 G |

Making the date a level halves the book path (4.43 → 2.48 s) — the cheapest win of the round, and
the honest one: a label belongs on an axis, not in an aggregation.

Correctness, re-run on the final design: per-day values equal the elements of
`Scenario PnL vector` **exactly** (max abs diff 0.0, not merely under 1e-12) for Vanguard and
ThirdPoint × HistFull and Evt:COVID2020, day counts 2,618 and 82, and the date dual exact. The
sector drill **foots**: the sector rows of a day sum to that day's book P&L to 3.5e-18.

## Verdict against the metric

| gate | target | measured | |
|---|---|---|---|
| book path cold | < 2.0 s | 2.48 s | **not met** (24% over) |
| book path warm | < 0.5 s | 2.10 s | **not met** |
| day × Sector | succeeds, < 5 s | succeeds, 14.09 s | **half met** — it succeeds where the old path FAILS; it is not < 5 s |
| JVM RSS delta | < 2 G | +0.9 G book, +1.3 G sector | **met** (was +14–20 G) |
| correctness | == vector to 1e-12, true day counts | exact (0.0), counts exact, drill foots to 3.5e-18 | **met** |

**The two timing gates are not met, and on the measured evidence they are not reachable on this
SDK for that shape.** The binding constraint is arithmetic: a fact-joined level costs ~0.5–0.8 ms
per member on this cube irreducibly (`contributors.COUNT` by Day, which computes nothing, costs
~16 s at 2,618 members under load and does not go below the same floor when quiet), so 2,618
members cannot be served in 0.5 s warm however the measure is written. Three independent designs
(vector-index parameter hierarchy, day facts, day facts with lifts) all land on the same wall, an
aggregate provider is refused outright for analysis hierarchies, and NaN padding was already
rejected in round 1.

What the round did buy, all of it measured on the same fresh-cube harness:

| | old ScenarioDay path | new Day path |
|---|---|---|
| book path, largest book, full history | 25.3 / 28.1 s | **2.48 / 2.10 s** (10×) |
| day × Sector | **FAILS**, +15.0 G | **works**, 198,968 rows |
| event replay (the shape the COVID notebook chart uses) | ~25–50 s | **0.12 / 0.04 s** |
| one month of days | — | 0.56 / 0.14 s |
| JVM growth | +14–20 G | +0.9–1.3 G |

So the hotspot is not eliminated, it is **de-fanged**: every per-day shape a person actually asks
for (an event window, a month, a mid-sized book) is now sub-second, the pathological shape is 10×
faster and no longer a memory event, and the drill that used to fail now works. The old
`ScenarioDay` parameter hierarchy and its five measures are kept, untouched, for backward
compatibility.

**And the standing advice is unchanged where it applies**: if you want all 2,618 numbers and not a
pivot, `Scenario PnL vector` + `Scenario dates (epoch)` returns them in one cell in 0.15 s cold /
0.02 s warm, which is what `/scenario_pnl` already does. The Day level is for when you want to
*drill* the days — by sector, by name, alongside other measures — which is precisely what the
vector cannot do.

## The no-regression gate

The full 44-query suite was run on the new cube (`docs/cube_bench_round2_20260815.json`) and, for a
same-session control, on the cube with the change stashed
(scratch `cube_bench_control_20260815.json`). **The suite-level diff is not decisive on this box**
and the two comparisons disagree: against round 1's `cube_bench_step6_20260814.json` the cross-book
family looks flat (`model_vol_by_book_123` 1.07 → 1.12 s, 1.05×), against today's control it looks
2.2× worse (0.51 → 1.12 s). Both cannot be true of the code.

So the family was A/B'd directly — 5 repetitions of each query in one process, on both builds,
back to back:

| query (median of 5) | control, no Day hierarchy | with the change |
|---|---|---|
| `model_vol_by_book_123` | 0.865 s | **0.319 s** |
| `net_exposure_by_book_all` | 0.269 s | **0.136 s** |
| `marginal_tvar_by_position` | 0.389 s | 0.425 s (+9%) |
| `var99_by_set_real7` | 0.022 s | 0.020 s |

**No regression**: nothing is worse by more than 9%, which is itself inside the repetition spread.
The suite's apparent 2× was run state, not code — the same lesson attempt 4 taught, now applied to
the gate itself. Build time is unchanged (58.3 / 59.7 s vs 58.2 s on the control; the day table is
68,425 rows, ~1 MB), JVM after build unchanged (7.7–8.3 G), suite total 129 s.

The two old ScenarioDay rows in the suite behave exactly as round 1 documented:
`scenario_day_path` printed 49.0 s on the control and 8.0 s on the new build — the same query, the
same measure, untouched code, once again swinging 6×; it is not a number to read off a diff.
`scenario_day_by_sector` **still fails with `BadArgumentException` on both** (59 s control, 15.6 s
new build) — the failure the new path fixes.

## Follow-ups — DONE 2026-08-15 (same day, after the merge)

Both items below are shipped:

- `/pivot` now exposes the fast path: `Day`/`DayDate`/`DaySet` in `DIM_NAMES`, `PnL at day` plus
  three chart markers (`VaR line at day`, `Worst pnl at day`, `Worst date at day (epoch)` — the
  book-level constants lifted over the day hierarchies too, so a `rows=[Day, DayDate]` chart query
  reads each once) in `MEASURE_NAMES`. A `DAY_DEP` set drives its own DaySet-context warning
  (server payload + the Vite field list via `/dims.day_dependent`) and the Manager×no-Date default.
  `/dims` enumerates the three day dimensions off the ScenarioDays table (one factor's rows), NOT
  the contributors.COUNT groupby — that fan-out is the ~16 s per-member floor. `/ask`'s tool
  description and `ASK_SYSTEM` point the model at the fast shape. Pinned by
  `test_risk_measures.py::t_day_path_ties_scenario_day_and_foots_by_sector` (day-for-day equality
  with the legacy path to 1e-12, marker equality, sector footing, the warning). Measured via HTTP
  on Soros: COVID path 0.5–0.8 s (legacy 3.9 s); HistFull `PnL at day` 2.4 s (legacy 4.7 s on this
  small book); each extra marker measure adds ~1.8 s on HistFull (the per-member floor, paid per
  measure); HistFull × Sector 5.9 s (legacy: fails).
- `author_chart_views.py`'s two COVID chart views author `rows=["Day","DayDate"(,"Sector")]` with
  the new measures; the calendar date is read off the `DayDate` level (`toDate(DayDate +
  'T00:00:00')` so the browser parses it in local time). The saved view is regenerated in `views/`.

## Follow-ups not done here (original list, kept for the record)

- `risk_api.py`'s `/pivot` allowlist still exposes only the OLD per-day path (`ScenarioDay` +
  `Scenario PnL at day`). Adding `Day`/`DaySet`/`DayDate` to `DIM_NAMES` and `PnL at day` to
  `MEASURE_NAMES` would put the fast drill in front of the Vite Pivot lens and `/ask`; it needs a
  `DaySet`-context warning of its own (the measures read `DaySet`, not `ScenarioSet`, so the
  existing scenario-context warning does not cover them) and is a separate, testable change.
- `author_chart_views.py`'s two scenario-path chart views still author `rows=["ScenarioDay"]`.
  They keep working; they would get ~10× faster by moving to `rows=["Day","DayDate"]`.
