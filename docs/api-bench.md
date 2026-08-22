# API-layer bench — the endpoints the UI actually waits on (2026-08-15)

Cube optimization round 3, items 4 and 5. `cube_bench.py` (round 1–2) times cube queries
in-process; after two rounds every query family except the per-day path is sub-1.5 s. The Vite UI
never sees a cube query — it waits on `risk_api.py` endpoints, several of which loop over dates,
call other endpoints serially, or do numpy work on top of the cube. Nothing measured those. Now
`python_src/api_bench.py` does.

## The harness

`cd python_src && BARRA_API=http://127.0.0.1:8010 ../barra/bin/python api_bench.py out.json`

- 34 requests per manager × 2 managers (Soros = the reference manager, Vanguard = the largest), each cold
  then warm, over HTTP against a RUNNING `risk_api`. Covers: `/meta`, `/dims`; the Overview set
  (`/risk`, `/limits`, `/dq`, `/backtest`, `/contributions`, `/whatchanged`, `/pnl_attribution`,
  `/pnl_attribution/linkage|residual|names`, `/pnl_attribution?by=sector`); Trends (manager path,
  Model vol path, by=Factor); Stress/What-if (`/stress` naive + conditional, `/reverse_stress`,
  `/hedge`, `/whatif` empty trades); Universe/Drift (`/universe`, `/funnel`, `/span`, `/drift`);
  Model (`/calibration`, `/regression`, `/factor_cov`); six `/pivot` shapes the field list
  produces (VaR by Sector, marginals by Position, by-Factor with totals, VaR trend by Date, the
  stress board, the fast per-day COVID path).
- Records wall time, status, payload bytes, error text (a 4xx/5xx or a timeout is a data point),
  and the box loadavg at start and end. `--diff a.json b.json` prints the ratio table.
- `--payloads DIR` dumps every response body; `--same DIR_A DIR_B` is the **before/after identity
  gate** — every payload must be JSON-equal. That gate is what makes an API-side change safe to
  ship: numbers identical, only wall time moves.

## The ranked "before" (own instance, worktree code before any change, sibling agents running)

| request | cold s | warm s |
|---|---|---|
| `calibration@Vanguard` | 119.77 | 0.04 |
| `calibration@Soros` | 93.74 | 0.05 |
| `trends_book@Vanguard` | 33.64 | 35.67 |
| `pnl_attribution_residual@Soros` | 23.88 | 30.48 |
| `pnl_attribution_residual@Vanguard` | 15.64 | 14.48 |
| `trends_book@Soros` | 14.18 | 8.12 |
| `trends_book_modelvol@Vanguard` | 9.54 | 20.16 |
| `trends_book_modelvol@Soros` | 8.17 | 9.77 |
| `pnl_attribution_linkage@Soros` | 8.17 | 5.83 |
| `dq@Soros` | 7.86 | 9.17 |
| `dq@Vanguard` | 6.97 | 7.04 |
| `trends_by_factor@Vanguard` | 4.16 | 2.42 |
| `pivot_var_trend_by_date@Vanguard` | 4.01 | 3.86 |
| `whatchanged@Soros` | 3.41 | 3.10 |

Everything else was under 3 s cold. On the `:8010` service (quieter box, loadavg 1.4 at start)
the same shape: `calibration@Soros` 107.6 s, `calibration@Vanguard` 369.9 s (this call, under a
box carrying four cubes, ended with the service being **OOM-killed and auto-restarted by systemd**
— worth knowing: the pre-fix `/calibration` was not just slow, it was the heaviest thing the UI
could ask for), `trends_book@Vanguard` 70.4 / 46.9 s, `pnl_attribution/residual` 16–20 s every
call, `/dq` 6.7–14 s every call.

Where the time went, by reading the code against the numbers:

- **`/calibration`** → `_pred_book_vols` over the full calendar: per month, THREE full-frame boolean
  masks (positions 11.6M rows, exposures 6M, specific_var) plus two serial PIT cube queries. ~126
  months × (~0.5 s pandas + ~0.4 s cube) — most of it pandas scanning the same frames 126 times.
- **`/pnl_attribution/residual`** (and `/names`, `?by=`) → `_pred_book_vols` on its window PLUS
  `_name_attr`, which per month masks the DAILY `specific_returns` frame (~13M rows) — 12 scans of
  13M rows was most of the 16 s, and it recomputed on every call.
- **`/trends` manager path** → ~126 serial single-date cube queries (the OOM-avoiding loop, correct
  and kept), recomputed on every call although the cube never changes in-process.
- **`/dq`** → `barra_dq_checks.run` on the live frames, every call, 7–14 s of pure-function work.
- **`/dims`** was already cached after the first call (round 2); the first user still paid it.

## What changed (`python_src/risk_api.py`, API layer only — no cube change)

1. **`_pred_book_vols` memoized per (manager, month)** on `S`, shared by `/calibration` and
   `/pnl_attribution/residual`; the numpy half fed from **cached per-Date row indices**
   (`_frame_rows_by`: one `groupby(...).indices` per (frame, keys) per process, `iloc` thereafter)
   instead of full-frame masks; the per-month PIT cube queries run **concurrently** (8 workers).
   Same arithmetic per month, same cross-check max-diffs over the requested months
   (`pit_verification` payload identical). Split into `_pred_month_numpy` / `_pred_month_cube`.
2. **`_name_attr`** fed from the same cached row indices; the `specific_returns` (d0, next] window is
   assembled from per-day index arrays via `searchsorted` on the sorted day list. Same rows, same
   sums.
3. **`/trends` manager path memoized** per (set, manager, measures, cube identity), and a cold fill runs
   the per-date queries concurrently (8 workers), results re-assembled in date order.
4. **`/dq` memoized** (`_dq_checks`, keyed on the frames' identity).
5. **Start-up prewarm** (`_prewarm`, called from `lifespan` after `cube ready`): a daemon thread
   fills the `/dims` cache and the `/dq` memo so no user ever pays either cold call. Best-effort:
   a failure is logged and the endpoint computes lazily on first call as before; it can never
   fail start-up. Off the critical path (measured 3.4–4.1 s + 6.4 s on a loaded box; inline it
   would delay "ready" by that for nothing). Item 4 of the round-3 list.

## After (same instance, same worktree, sibling agents still running)

| request | `:8010` service, cold / warm | own instance BEFORE, cold / warm | own instance AFTER, cold / warm |
|---|---|---|---|
| `calibration@Vanguard` | 369.93 / 0.02 | 119.77 / 0.04 | **14.22 / 0.11** |
| `calibration@Soros` | 107.59 / 0.10 | 93.74 / 0.05 | **5.12 / 0.04** |
| `trends_book@Vanguard` | 70.38 / 46.93 | 33.64 / 35.67 | **19.72 / 0.04** |
| `trends_book@Soros` | 5.38 / 5.23 | 14.18 / 8.12 | **3.44 / 0.02** |
| `trends_book_modelvol@Vanguard` | 5.08 / 6.35 | 9.54 / 20.16 | 9.25 / **0.02** |
| `trends_book_modelvol@Soros` | 3.79 / 3.48 | 8.17 / 9.77 | 2.69 / **0.01** |
| `pnl_attribution_residual@Soros` | 16.33 / 16.26 | 23.88 / 30.48 | **1.22 / 0.44** |
| `pnl_attribution_residual@Vanguard` | 19.76 / 17.23 | 15.64 / 14.48 | **1.30 / 0.33** |
| `pnl_attribution_names@Soros` | 6.56 (one call) | — | **0.41 / 0.41** |
| `pnl_attribution_names@Vanguard` | 6.20 (one call) | — | **0.36 / 0.30** |
| `dq@Soros` (first call in the run) | 6.67 / 7.43 | 7.86 / 9.17 | 7.19 / **0.03** |
| `dq@Vanguard` | 14.20 / 14.49 | 6.97 / 7.04 | **0.02 / 0.02** |

Loadavg: `:8010` run 1.4 → (OOM); own BEFORE 46 → 20; own AFTER 4.5 → 25 (an earlier AFTER pass at
load 13 → 10 read `trends_book@Vanguard` 12.4 s cold, `calibration@Vanguard` 6.5 s — the cold
numbers here are load-bound; the warm numbers are not). Note `dq@Soros` still shows a 7 s "cold"
in the harness only because the harness's first `/dq` arrived before the start-up prewarm thread
had finished on that boot; on the final boot the first user call measured **0.027 s** (`/dims`
0.016 s), both prewarmed (`[risk_api] /dims prewarmed in 4.08s`, `/dq prewarmed in 6.36s`).

**Identity gate:** `api_bench.py --same payloads_before payloads_after` → 66 of 68 payloads
JSON-identical. The two that differ are `span@Soros` / `span@Vanguard`, and only in the ORDER of the
scatter points — `/span` iterates Python `set`s (`cloud_pos`, `held_pos`) whose order changes with
per-process hash randomisation; the point sets are identical (checked as sorted multisets, the rest
of the payload byte-identical). Pre-existing, untouched here; a `sorted()` there would make it
deterministic (out of this item's scope).

**Tests** (own instance): `test_trends.py` 4/4, `test_dq.py` 5/5, `test_model_trust.py` 11/12
(`t_exposure_profile_shape` fails on `beyond3.share` 0.60 ≥ 0.5 — data-dependent on the 124-manager
coverage universe, `/exposure_profile` untouched), `test_attribution.py` 21/24 (the three failures
are stale against the 124-manager build: `t_pnl_attribution_manager_guard` expects AQR to be a
`manager_mismatch` but `pnl_attribution.AQR.parquet` now exists and is served; `t_cube_measures_foot`
and `t_manager_independent_measures_inert_with_one_manager` query the manager-independent attribution
measures through `/pivot`, which the multi-manager guard rejects with 400 by design). None of the four
touches code changed here.

## What was NOT done (all of it done since — round 4, 2026-08-21)

Every line below was picked up on 2026-08-21; see `optimization-log.md` §"Round 4" for the
mechanisms and the measured after.

- `/pnl_attribution/linkage` (2–8 s) and `/whatchanged` (1.5–3.4 s) — next candidates; both mix
  cube calls with the same per-month frame masks and would take the `_frame_rows_by` treatment.
  → done (4.3): 1.94 → 0.57 s and 1.50 → 0.91 s. Doing it exposed a manager-scoping **bug** in
  `/whatchanged`'s exposure attribution (4.4).
- `/trends` `by=Factor` (2–4 s on Vanguard) is one query; nothing API-side to do.
  → confirmed: the query is 94% of the call (records 3 ms, encode 10 ms), so it was memoized
  rather than optimized — warm 1.48 → 0.02 s (4.6).
- Endpoints that fan out to other endpoints serially (`/overview/analysis` assembles ~6 calls
  before the LLM stream) — untouched; every one of its inputs got faster above.
  → done (4.5), though the prize had shrunk to 1.2× precisely because the inputs got faster.
- The `/span` set-order nondeterminism (see above). → sorted (4.9).

And one thing this bench did not look for, which round 4 found by accident: `_validate_pivot` ran
`nunique()` over the 11.6M-row positions column on EVERY guarded query, a flat 0.35 s under every
pivot the grid issues (4.2). Endpoint-level timing hid it — it was in all the "before" numbers and
all the "after" ones alike.
