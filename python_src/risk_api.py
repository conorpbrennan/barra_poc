"""
risk_api.py
===========
FastAPI backend that OWNS the Atoti session and exposes a small, GUARDED JSON API.

Why this exists: the Atoti cube is an in-process object living inside *this* Python
process. The cube is built once at startup (lifespan) and held for the process lifetime;
every endpoint queries it via cube.query / the in-memory frames and returns tidy JSON.

The guardrails the raw Atoti UI lacks are baked in here:
  * scenario risk is ALWAYS sliced to a single ScenarioSet (+ a single Date),
  * only SCALAR measures cross the wire (never the raw P&L vectors),
so the frontend can never land in the empty/ragged state.

Run (separate process from the Streamlit frontend):
    cd python_src
    BARRA_CUBE_PORT=9091 ../barra/bin/uvicorn risk_api:app --port 8000

Endpoints:
    GET /meta                              -> dates, scenario_sets, factors, time-series measures
    GET /risk?date=&set=                   -> KPI scalars (Total VaR / Factor VaR / Specific vol / Worst)
    GET /scenarios?date=                   -> all scenario sets x {var99, worst, total}
    GET /exposures?date=                   -> net factor exposure, FactorGroup -> Factor
    GET /attribution?date=&set=&by=        -> standalone risk by Country|Sector|Issuer|Position
    GET /timeseries?set=&measure=          -> one measure across all dates for one set
    GET /position?date=&position=          -> per-name detail (weight, loadings, specific var)
    GET /validation                        -> 3-position cube-vs-pandas reconciliation
    GET /pivot                             -> generic tidy pivot (the saved-view engine)
    GET /limits?date=&set=&manager=        -> desk-limit RAG status (limits.json)
    GET /dq                                -> data-quality / trust report on the live frames
    GET /backtest?set=&alpha=&window=      -> rolling-window VaR backtest (Kupiec + Basel zone)
    GET /drawdown?set=&date=&manager=      -> constant-portfolio max drawdown over the scenario path
    GET /trends?set=&measures=&by=         -> tidy time series of portfolio measures over the calendar
    POST /stress                           -> custom one-day stress (user-defined per-factor sigmas)
    GET /reverse_stress?loss=              -> per-factor sigma move that breaches a target loss
    POST /whatif                           -> pre-trade portfolio risk before/after hypothetical trades
    GET /pnl_attribution?from=&to=&by=     -> realized PnL by factor + residual (Carino-linked)
    GET /pnl_attribution/residual          -> residual diagnostics (IR, autocorr, bias) with RAG
    GET /pnl_attribution/linkage?T=        -> risk decomposition at T vs PnL over T→T+h (surprise z)
    GET /pnl_attribution/drill?T=&to=&manager=&position=|factor=
                                            -> live per-manager reconcile drill (frames, not the cube)
    POST /analysis                         -> streamed risk-analyst commentary on ONE view's numbers

The /analysis endpoint runs the SAME guarded pivot the UI shows, then sends ONLY those tidy
numbers to the Anthropic Messages API (plain client.messages.create, NO tools) for a written
read. The model gets the figures as text and nothing else — it has zero access to the cube,
the filesystem, or any tool; it cannot re-query. Grounding rules live in ANALYST_SYSTEM.
"""
from __future__ import annotations
import os
import asyncio
import math
import json
import time
import uuid
import pathlib
import inspect
import datetime as _dt
import weakref
from collections import deque, OrderedDict
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor

import anthropic
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import barra_dq_checks
import barra_pnl_attribution as _pnl
from views_api import router as views_router
import barra_build_frames as _bf
import barra_universe_membership as _um
import barra_universe_funnel as _uf
import barra_universe_span as _us
import barra_universe_drift as _ud
from barra_factor_risk_cube import load_frames, build_cube, EVENT_WINDOWS, HYPO_SHOCKS, DOLLAR_MEASURES

CUBE_PORT = int(os.environ.get("BARRA_CUBE_PORT", "9091"))   # own port, distinct from the 9090 UI cube
TS_MEASURES = ["Model vol", "Scenario VaR 99", "Scenario worst loss", "Specific vol",
               "Total VaR 99"]
BY_LEVELS = {"country": "Country", "sector": "Sector", "issuer": "Issuer", "position": "Position"}

S: dict = {}   # process-wide state: session, cube, frames


# ----------------------------------------------------------------------------- helpers
def _clean(v):
    """numpy/NaN/dates -> JSON-safe python."""
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if math.isnan(f) else f
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, pd.Timestamp):
        return str(v.date())
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()[:10]
    return v


# pivotable dimensions (level names, all unique across hierarchies) and SCALAR measures only.
# The legacy `ScenarioDay` parameter hierarchy (+ `Scenario PnL at day` & co.) was PRUNED from this
# allowlist on 2026-08-15 (round-3): every consumer moved to the Day path below, and the legacy
# per-member unpacking was the one query that could still trip the cube's 120 s timeout or a
# BadArgumentException from the UI / `/ask`. The cube still defines it (cube_bench's A/B control).
DIM_NAMES = ["Date", "Manager", "Country", "Sector", "Issuer", "Position",
             "FactorGroup", "Factor", "ScenarioSet",
             # the per-day path (2026-08-15, docs/cube-opt-round2-scenarioday.md): days as facts on
             # the ScenarioDays table. `Day` = the set's day index, `DayDate` = its calendar date (a
             # LEVEL, 1:1 with Day -- read it off the axis, not via a measure), `DaySet` = that
             # table's OWN set key. Read `PnL at day` with rows=[Day, DayDate] (+ up to TWO
             # breakout dims, e.g. Sector) and a DaySet slice. That shape -- with or without a
             # Day/DayDate window -- is served by the VECTOR plan (`_day_vector_shape`: the cube's
             # own P&L vector unpacked, ~0.5 s any manager); anything else falls to the level plan.
             "Day", "DayDate", "DaySet",
             # PriceSet (docs/price-var-plan.md): the Price family's switch hierarchy, mirroring
             # ScenarioSet one-for-one (HistFull + every Evt:* window; no Hypo:* mirror). Pruned
             # from this list at startup like the Price measures themselves if stock_returns.parquet
             # wasn't built (see PRICE_DEP / the lifespan pruning loop).
             "PriceSet"]
# The Day-path measures read the DaySet hierarchy, NOT ScenarioSet, so the scenario-context warning
# below does not cover them; they get their own (see _pivot_result). Members of Day/DayDate are the
# union across sets, so a query with no DaySet context reads every set's days stacked -- blank
# P&L for the sets that don't hold that day, and multi-set sums where they overlap.
DAY_DEP = {"PnL at day", "VaR line at day", "Worst pnl at day", "Worst date at day (epoch)"}
DAY_DIMS = ["Day", "DayDate", "DaySet"]
# "Manager" is the USER-FACING name of the cube's Manager level (renamed at the API surface
# 2026-08-14 for the multi-manager demo — "Book" is desk jargon; prospects pick a manager; the
# cube's physical level/column followed on 2026-08-22 — see CLAUDE.md — so the two now match).
# DIM_ALIASES keeps "Book" accepted on INPUT forever — saved views, old URLs, and tests carry
# it; DIM_LEVELS is now empty (kept, not deleted, as the one seam a future alias would reuse).
DIM_ALIASES = {"Book": "Manager"}
DIM_LEVELS = {}


def _lvl(l, name: str):
    """Cube level for a canonical dimension name, resolving the INPUT alias first ("Book" ->
    "Manager") — the cube level itself has been named Manager since the 2026-08-22 physical
    rename, so this is just alias resolution + indexing now."""
    return l[DIM_LEVELS.get(_canon_dim(name), _canon_dim(name))]


def _canon_dim(name):
    return DIM_ALIASES.get(name, name)


# Mirrors DIM_ALIASES, but for the one measure whose name followed the cube's Book -> Manager
# rename (barra_factor_risk_cube.py, 2026-08-22): "Book MV" is accepted on INPUT forever (old
# saved views, old URLs, old tests), but only "Manager MV" is ever emitted — in MEASURE_NAMES,
# in query results, or anywhere else in this file.
MEASURE_ALIASES = {"Book MV": "Manager MV"}


def _canon_measure(name):
    return MEASURE_ALIASES.get(name, name)


def _coalesce_manager(manager: str | None, book: str | None, default: str = "Soros") -> str:
    """`manager` is the canonical param name everywhere in this API; `book` is still accepted
    silently (old links, saved views, callers not yet updated) but is never documented and never
    emitted."""
    if manager is not None:
        return manager
    if book is not None:
        return book
    return default


MEASURE_NAMES = ["Net exposure", "Scenario VaR 99", "Scenario worst loss", "Scenario mean PnL",
                 "Specific vol", "Specific variance", "Total VaR 99",
                 "Marginal Scenario VaR 99", "Marginal Total VaR 99", "VaR sensitivity",
                 "% of Scenario VaR 99", "% of Total VaR 99",
                 "Incremental Scenario VaR 99", "Incremental Total VaR 99",
                 # VaR ladder (95/97.5/99), Expected Shortfall, P&L dispersion + total-ES analogue:
                 "Scenario VaR 95", "Scenario VaR 97.5", "Scenario ES 97.5", "Scenario ES 99",
                 "Scenario PnL vol", "Total ES 97.5",
                 # model vol — THE reference risk number (sigma = sqrt(x'Fx + w'dw); slice to
                 # HistFull for the model sigma; degenerate on length-1 Hypo sets) + its Euler
                 # marginal (== the ch-09 CTR; sums exactly to Model vol; by-NAME views) and the
                 # diversification-aware incremental (vol released by removing the member):
                 "Model vol", "Marginal Model vol", "% of Model vol", "Incremental Model vol",
                 "Factor variance contribution",
                 # Tier-1 migrations: raw factor vol + the D6 hedge family (by-Factor views):
                 "Factor return vol", "Vol ex factor", "Min-variance hedge ratio",
                 "Vol at min-variance hedge",
                 # custom stress on a StressShock scenario (reads 0 on the Base branch — pass
                 # the /pivot `shocks` param to price a transient shock):
                 "Custom stress PnL",
                 # tail-fatness: share of scenario days beyond ±2σ of the cell's own vol:
                 "Exceedance rate 2s",
                 # correlation stress per cell (CorrStress params; Base = Model vol):
                 "Stressed model vol",
                 # concentration: 5 largest names' share of Total VaR (tt.rank over the flat
                 # PositionRank hierarchy; set-dependent like the marginals):
                 "Top-5 risk share",
                 # gross/net portfolio weight (scenario-independent, branch-sensitive):
                 "Gross weight", "Net weight",
                 # ES contribution split + risk-concentration HHI:
                 "Marginal Scenario ES 97.5", "% of Scenario ES 97.5", "Risk HHI",
                 "Scenario worst date (epoch)", "Scenario n",
                 # the FAST per-day path (read with Day/DayDate on an axis + a DaySet slice; see
                 # DIM_NAMES): the per-day portfolio P&L and its chart markers (portfolio VaR rule,
                 # worst P&L point + its date -- portfolio-level constants lifted over the day hierarchies):
                 "PnL at day", "VaR line at day", "Worst pnl at day", "Worst date at day (epoch)",
                 # PnL attribution (Step 15, v2-only; pruned at startup if the cube lacks them).
                 # Forward-month convention: the value at Date d0 is the PnL over the month after d0.
                 "Factor contribution", "Specific PnL", "Realized PnL",
                 # dollars (2026-08-22, Units-context refactor 2026-08-22): $ held per cell, the
                 # sliced manager's 13F value. Every weight-unit measure in DOLLAR_MEASURES (one list
                 # with the cube) toggles weight<->dollars on the cube's own `Units` context —
                 # there is no separate "<measure> $" measure name any more (see `Units` handling
                 # in `_pivot_query`/`_pivot_result` below).
                 "Market value", "Manager MV",
                 # Price family (docs/price-var-plan.md): historical sim on raw stock returns, no
                 # factor model — the model-free VaR/var_bridge compares against. v2-only; pruned
                 # at startup like the PnL-attribution trio if stock_returns.parquet is absent.
                 "Price VaR 95", "Price VaR 97.5", "Price VaR 99", "Price ES 97.5", "Price ES 99",
                 "Price worst loss", "Price mean PnL", "Price PnL vol",
                 "Marginal Price VaR 99", "% of Price VaR 99", "Incremental Price VaR 99",
                 "Marginal Price ES 97.5", "Price coverage at tail",
                 ]
SCEN_DEP = {"Scenario VaR 99", "Scenario worst loss", "Scenario mean PnL", "Total VaR 99",
            "Marginal Scenario VaR 99", "Marginal Total VaR 99", "VaR sensitivity",
            "% of Scenario VaR 99", "% of Total VaR 99",
            "Incremental Scenario VaR 99", "Incremental Total VaR 99",
            "Scenario VaR 95", "Scenario VaR 97.5", "Scenario ES 97.5", "Scenario ES 99",
            "Scenario PnL vol", "Total ES 97.5", "Model vol",
            "Marginal Model vol", "% of Model vol", "Incremental Model vol",
            "Factor variance contribution",
            "Factor return vol", "Vol ex factor", "Min-variance hedge ratio",
            "Vol at min-variance hedge", "Custom stress PnL", "Top-5 risk share",
            "Exceedance rate 2s", "Stressed model vol",
            "Marginal Scenario ES 97.5", "% of Scenario ES 97.5", "Risk HHI",
            "Scenario worst date (epoch)", "Scenario n",
            # the Day-path chart markers read the ScenarioSet-context portfolio VaR/worst loss (PnL at
            # day itself reads DaySet only -- see DAY_DEP):
            "VaR line at day", "Worst pnl at day", "Worst date at day (epoch)"}

# PriceSet dependency (docs/price-var-plan.md), the SAME "needs a single-set context" idiom as
# SCEN_DEP but for the Price family's own switch hierarchy — a Price measure with no PriceSet
# context is blank, same warning pattern _pivot_query already carries for SCEN_DEP/DAY_DEP.
PRICE_DEP = {"Price VaR 95", "Price VaR 97.5", "Price VaR 99", "Price ES 97.5", "Price ES 99",
            "Price worst loss", "Price mean PnL", "Price PnL vol",
            "Marginal Price VaR 99", "% of Price VaR 99", "Incremental Price VaR 99",
            "Marginal Price ES 97.5", "Price coverage at tail"}


def _records(df: pd.DataFrame, reset: bool = True) -> list[dict]:
    """Tidy JSON-safe records — COLUMN-WISE (api_bench 2026-08-21, TODO item 4).

    This used to be `[{k: _clean(v) for k, v in row.items()} for _, row in df.iterrows()]`, which
    materialises one Series per row: on the payloads the Vite grid actually pulls (a by-Position
    pivot on the largest manager, ~5k rows) that per-row construction, not the cube query, was the
    bulk of the response time. Cleaning per COLUMN is the same work done once per dtype.

    The one behavioural subtlety kept deliberately: `iterrows` coerces each row to a common dtype,
    so in an ALL-NUMERIC frame containing any float, integer columns came out as floats. Frames
    with a non-numeric column (every pivot with a string/date level) went to object dtype and kept
    their ints. `_int_as_float` reproduces exactly that, so payloads stay byte-identical."""
    if reset:
        df = df.reset_index()
    n = len(df)
    if not n:
        return []
    numeric_only = all(pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])
                       for c in df.columns)
    _int_as_float = numeric_only and any(pd.api.types.is_float_dtype(df[c]) for c in df.columns)
    cols, vals = list(df.columns), []
    for c in cols:
        s_ = df[c]
        if pd.api.types.is_float_dtype(s_):
            a = s_.to_numpy(dtype=float, copy=False)
            vals.append([None if v != v else float(v) for v in a])          # v != v == isnan
        elif pd.api.types.is_integer_dtype(s_) and not pd.api.types.is_bool_dtype(s_):
            a = s_.to_numpy()
            vals.append([float(v) for v in a] if _int_as_float else [int(v) for v in a])
        elif pd.api.types.is_datetime64_any_dtype(s_):
            d_ = s_.dt.strftime("%Y-%m-%d")
            vals.append([None if v is None or v != v else v for v in d_.to_numpy(dtype=object)])
        else:
            vals.append([_clean(v) for v in s_.to_numpy(dtype=object)])
    return [dict(zip(cols, row)) for row in zip(*vals)]


def _date(date: str):
    return pd.Timestamp(date).date()


def _ticker_map() -> dict:
    sec = S["frames"]["securities"]
    return dict(zip(sec["Position"], sec["Ticker"]))


# ----------------------------------------------------------------------------- lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    frames = load_frames()
    session, cube = build_cube(frames, port=CUBE_PORT)
    S.update(frames=frames, session=session, cube=cube)
    # v1-built data has no specific_returns frame -> no attribution measures; prune them from the
    # allowlist so /pivot, /ask and /analysis never offer a measure the cube can't answer.
    live = set(cube.measures)
    for _mn in [x for x in MEASURE_NAMES if x not in live]:
        MEASURE_NAMES.remove(_mn)
    # Same pruning for the Price family's switch DIMENSION: v1 data / a pre-Price-VaR build has
    # no PriceSet hierarchy at all, so leaving it in DIM_NAMES would let _validate_pivot accept a
    # dimension name the cube can't resolve (a raw KeyError, not a clean 400).
    if "PriceSet" not in {n for _, n in cube.hierarchies} and "PriceSet" in DIM_NAMES:
        DIM_NAMES.remove("PriceSet")
    print(f"[risk_api] cube ready on :{CUBE_PORT}; UI at {session.url}")
    _prewarm()
    yield
    session.close()


def _prewarm() -> None:
    """Pay the one-per-process cold costs at START-UP, in a daemon thread, so no user request
    ever sees them (docs/api-bench.md): /dims member enumeration (1.4-3 s, cached on the cube
    identity -- docs/cube-opt-round2-dims.md) and the /dq check battery (7-14 s on the 124-manager
    frames, memoized on the frames' identity). Off the start-up critical path -- inline they would
    delay 'ready' for nothing; in the thread the first request that arrives before a warm-up
    finishes simply computes it itself (same memo, same answer). Each step is best-effort: a
    failure is logged and the endpoint recomputes lazily on first call exactly as before;
    prewarming can never fail start-up."""
    import threading

    def _run():
        for name, fn in (("/dims", _dims_response), ("/dq", _dq_checks)):
            t0 = time.perf_counter()
            try:
                fn()
                print(f"[risk_api] {name} prewarmed in {time.perf_counter() - t0:.2f}s")
            except Exception as e:                   # noqa: BLE001 -- best-effort by design
                print(f"[risk_api] {name} prewarm failed ({type(e).__name__}: {e}); will compute lazily")

    threading.Thread(target=_run, name="api-prewarm", daemon=True).start()


app = FastAPI(title="Barra Factor Risk API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(views_router)   # saved-view CRUD over views_repo (no cube dep) — see views_api.py


def _has_pit(cube) -> bool:
    """Does this cube carry the split-out PIT hierarchy (2026-08-14, optimization Step 2)?"""
    return "PITSet" in {n for _, n in cube.hierarchies}


def _pit_set_names(cube) -> list[str]:
    """The PIT:* set names off their own hierarchy ([] on a pre-split cube)."""
    if not _has_pit(cube):
        return []
    l, m = cube.levels, cube.measures
    return sorted({str(s) for s in cube.query(m["contributors.COUNT"], levels=[l["PITSet"]]).index})


def _set_context(cube, l, set_name: str, mlist: list) -> tuple:
    """(filter condition, measure names to query) for a `set=` parameter. A PIT:* name selects the
    truncated-history hierarchy and the mirrored measures (Step 2, 2026-08-14); every real set is
    the ScenarioSet condition and the measures as asked. 400s on a PIT set + an unmirrored
    measure rather than quietly serving the full-history number."""
    if str(set_name).startswith("PIT:") and _has_pit(cube):
        bad = [x for x in mlist if x in SCEN_DEP and x not in PIT_MIRROR]
        if bad:
            raise HTTPException(400, f"{bad} are not available on the PIT:* truncated-history "
                                     f"sets — only {sorted(PIT_MIRROR)} are mirrored there.")
        return l["PITSet"] == set_name, [PIT_MIRROR.get(x, x) for x in mlist]
    return l["ScenarioSet"] == set_name, list(mlist)


def _reject_pit(set_name: str) -> None:
    """Guard for the routes that read the full scenario engine (P&L vectors, VaR/ES, the date
    dual) — none of that is mirrored onto the PIT hierarchy, so a PIT:* name is a 400 here rather
    than a silently empty response. The PIT sets are as-of-vol plumbing: use /pivot (or /trends)
    with Model vol / Scenario PnL vol."""
    if str(set_name).startswith("PIT:") and _has_pit(S["cube"]):
        raise HTTPException(400, f"{set_name} is a PIT truncated-history set: only "
                                 f"{sorted(PIT_MIRROR)} are served on those, via /pivot or "
                                 "/trends. This route needs one of the real scenario sets.")


def _managers_meta() -> list[dict]:
    """Available managers for the UI (multi-manager Phase 3, 2026-07-30) — the ONE source,
    like `hypo_shocks` below (see `t_meta_serves_managers`). Sourced from the live frames, never a
    hardcoded manager list, so it reflects whatever ACTIVE_MANAGERS scope the running build used.
    `manager` is always populated (from `positions`); the entity attributes (entity_name/firm_type/
    cik/n_positions_distinct) come from managers.parquet when present and are None otherwise —
    today's data has no managers.parquet, so this degrades to manager-name-only entries, same
    shape, same key set, just null attributes (not a different response shape the UI has to branch
    on)."""
    managers = _manager_names()
    mgr_frame = S["frames"].get("managers")
    by_manager = (mgr_frame.set_index("Manager").to_dict("index")
                  if mgr_frame is not None and len(mgr_frame) else {})
    out = []
    for mgr in managers:
        row = by_manager.get(mgr, {})
        out.append({"manager": mgr,
                    "entity_name": _clean(row.get("EntityName")),
                    "firm_type": _clean(row.get("FirmType")),
                    "cik": _clean(row.get("CIK")),
                    "n_positions_distinct": _clean(row.get("n_positions_distinct"))})
    return out


# ----------------------------------------------------------------------------- endpoints
@app.get("/meta")
async def meta():
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        dates = sorted({str(pd.Timestamp(d).date()) for d in
                        cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index})
        all_sets = sorted({str(s) for s in cube.query(m["contributors.COUNT"], levels=[l["ScenarioSet"]]).index})
        # PIT:* truncated-history sets are plumbing for as-of risk (one per month-end). Since
        # 2026-08-14 they live in their OWN hierarchy (PITSet), so ScenarioSet already holds just
        # the 7 real sets; the prefix filter stays as belt-and-braces for a pre-split cube.
        sets = [s for s in all_sets if not s.startswith("PIT:")]
        pit_sets = _pit_set_names(cube) or [s for s in all_sets if s.startswith("PIT:")]
        factors = sorted(S["frames"]["factor_meta"]["Factor"].tolist())
        return {"dates": dates, "scenario_sets": sets, "pit_sets": pit_sets, "factors": factors,
                "ts_measures": TS_MEASURES, "by_levels": list(BY_LEVELS),
                # the cube's baked-in hypothetical shock definitions ({set: {Factor: sigma}}) —
                # served so the Stress lens presets and the cube's Hypo:* sets share ONE source
                "hypo_shocks": HYPO_SHOCKS,
                # available managers (+ entity attributes when managers.parquet exists) —
                # Phase 4's UI context-bar manager picker reads this instead of hardcoding "Soros"
                "managers": _managers_meta()}
    return await run_in_threadpool(run)


@app.get("/risk")
async def risk(date: str, set: str, manager: str | None = None, book: str | None = None):
    manager = _coalesce_manager(manager, book)
    _reject_pit(set)
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        # Manager MUST be sliced. Every measure here is weight-dependent, and with more than one
        # manager loaded the no-manager grand total is not a portfolio: `single_value` refuses to
        # choose between two managers' differing weights for a shared name, so the aggregate
        # collapses (measured on the 11-manager build: Scenario VaR 99 read 0.0013 unsliced vs
        # 0.0352 for Soros). It read fine for years only because there was exactly one manager.
        df = cube.query(m["Total VaR 99"], m["Scenario VaR 99"], m["Scenario worst loss"], m["Specific vol"],
                        filter=(l["Date"] == _date(date)) & (l["ScenarioSet"] == set)
                               & (l["Manager"] == manager))
        if not len(df):
            return {"date": date, "set": set, "empty": True}
        r = df.iloc[0]
        return {"date": date, "set": set,
                "total_var": _clean(r["Total VaR 99"]), "factor_var": _clean(r["Scenario VaR 99"]),
                "worst_loss": _clean(r["Scenario worst loss"]), "specific_vol": _clean(r["Specific vol"])}
    return await run_in_threadpool(run)


@app.get("/scenarios")
async def scenarios(date: str, manager: str | None = None, book: str | None = None):
    manager = _coalesce_manager(manager, book)
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        # Manager slice required — see the note on /risk. These are weight-dependent measures, so
        # the multi-manager grand total collapses instead of aggregating into a portfolio.
        df = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], m["Total VaR 99"],
                        levels=[l["ScenarioSet"]],
                        filter=(l["Date"] == _date(date)) & (l["Manager"] == manager))
        return _records(df)
    return await run_in_threadpool(run)


@app.get("/exposures")
async def exposures(date: str, manager: str | None = None, book: str | None = None):
    manager = _coalesce_manager(manager, book)
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        # Net exposure is x_k = sum(w * L) — meaningless without a manager to supply the w.
        df = cube.query(m["Net exposure"], levels=[l["FactorGroup"], l["Factor"]],
                        filter=(l["Date"] == _date(date)) & (l["Manager"] == manager))
        return _records(df)
    return await run_in_threadpool(run)


@app.get("/attribution")
async def attribution(date: str, set: str, by: str = "sector",
                      manager: str | None = None, book: str | None = None):
    manager = _coalesce_manager(manager, book)
    _reject_pit(set)
    by = by.lower()
    if by not in BY_LEVELS:
        raise HTTPException(400, f"by must be one of {list(BY_LEVELS)}")
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        # Manager slice required — see the note on /risk.
        df = cube.query(m["Net exposure"], m["Scenario VaR 99"], m["Scenario worst loss"],
                        levels=[l[BY_LEVELS[by]]],
                        filter=(l["Date"] == _date(date)) & (l["ScenarioSet"] == set)
                               & (l["Manager"] == manager))
        recs = _records(df)
        if by == "position":           # decorate FIGI with a readable ticker
            tk = _ticker_map()
            for r in recs:
                r["Ticker"] = tk.get(r.get("Position"), "")
        return recs
    return await run_in_threadpool(run)


@app.get("/timeseries")
async def timeseries(set: str, measure: str = "Total VaR 99"):
    _reject_pit(set)
    if measure not in TS_MEASURES:
        raise HTTPException(400, f"measure must be one of {TS_MEASURES}")
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        df = cube.query(m[measure], levels=[l["Date"]], filter=l["ScenarioSet"] == set)
        df = df.reset_index().sort_values("Date")
        return [{"date": str(d), "value": _clean(v)} for d, v in zip(df["Date"], df[measure])]
    return await run_in_threadpool(run)


@app.get("/trends")
async def trends(set: str = "HistFull",
                 measures: str = "Scenario VaR 99,Scenario ES 97.5,Risk HHI",
                 by: str | None = None, manager: str | None = None, book: str | None = None):
    """Tidy time series of one or more portfolio measures over the whole calendar for one
    ScenarioSet — one cube query, so a trend panel needs a single round-trip. `by` (e.g. Factor)
    adds a breakdown dimension: levels become [Date, by] (used for factor-exposure-over-time).
    Measures/by are validated against the same allowlists as /pivot."""
    manager = _coalesce_manager(manager, book)
    mlist = _csv(measures)
    bad_m = [x for x in mlist if x not in MEASURE_NAMES]
    if bad_m:
        raise HTTPException(400, f"unknown measure(s): {bad_m}")
    if not mlist:
        raise HTTPException(400, "select at least one measure")
    by = _canon_dim(by) if by else by
    if by and by not in DIM_NAMES:
        raise HTTPException(400, f"unknown dimension: {by}")
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        # a PIT:* set names the truncated-history hierarchy + its mirrored measures (Step 2)
        set_cond, mnames = _set_context(cube, l, set, mlist)
        meas = [m[x] for x in mnames]
        back = dict(zip(mnames, mlist))
        if by:
            # additive breakdown (e.g. Net exposure by Factor) — one query is safe (no P&L vectors).
            # Manager sliced for the same reason as /risk: these are weight-dependent measures and
            # the multi-manager grand total collapses rather than aggregating into a portfolio.
            # Memoized on the same argument as the manager path below (api_bench 2026-08-21): the
            # measured cost is the cube query itself (~1.4 s for Date x Factor on the largest
            # manager — the SDK's per-member floor, NOT serialisation: building the records is 3 ms
            # and encoding the response 10 ms), so a repeat view should not pay it again.
            ck = ("_trends_memo", set, manager, tuple(mlist), by, id(cube))
            if ck in S:
                return {"set": set, "measures": mlist, "by": by, "records": S[ck]}
            df = (cube.query(*meas, levels=[l["Date"], _lvl(l, by)],
                             filter=set_cond & (l["Manager"] == manager))
                  .rename(columns=back)
                  .reset_index().sort_values("Date"))
            recs = _records(df, reset=False)
            S[ck] = recs
        else:
            # portfolio-level over the calendar, DATE-BY-DATE: the scenario/HHI measures pull the
            # full P&L vector per date, and asking for every date in one plan OOMs the cube — so
            # loop, one date (one vector) at a time. ~100 light scalar queries; cheap and cached
            # upstream. 2026-08-15 (api_bench): the loop is MEMOIZED per (set, manager, measures)
            # on S -- the cube never changes in-process, so the series is a constant -- and a cold
            # fill runs the per-date queries CONCURRENTLY (8 workers; results re-assembled in date
            # order, so records are identical to the serial loop). Measured: Vanguard HistFull
            # default measures 70 s -> see docs/api-bench.md; Soros 5.4 s -> ~1 s.
            ck = ("_trends_memo", set, manager, tuple(mlist), by, id(cube))
            if ck in S:
                return {"set": set, "measures": mlist, "by": by, "records": S[ck]}
            dates = sorted({pd.Timestamp(d).date() for d in S["frames"]["specific_var"]["Date"]})

            def _one(d):
                return cube.query(*meas, filter=(l["Date"] == d) & set_cond & (l["Manager"] == manager))

            with ThreadPoolExecutor(max_workers=8) as ex:
                results = list(ex.map(_one, dates))
            recs = []
            for d, r in zip(dates, results):
                if len(r):
                    row = r.iloc[0]
                    recs.append({"Date": d.isoformat(),
                                 **{x: _clean(row[y]) for y, x in back.items()}})
            S[ck] = recs
        return {"set": set, "measures": mlist, "by": by, "records": recs}
    return await run_in_threadpool(run)


@app.get("/position")
async def position(date: str, position: str):
    """Per-name detail straight from the frames (no cube needed): weight, loadings, specific var."""
    def run():
        f = S["frames"]; d = pd.Timestamp(date)
        sec = f["securities"].set_index("Position")
        if position not in sec.index:
            raise HTTPException(404, "unknown position")
        wrow = f["positions"][(f["positions"]["Position"] == position) & (f["positions"]["Date"] <= d)]
        weight = float(wrow.sort_values("Date")["Weight"].iloc[-1]) if len(wrow) else None
        load = (f["exposures"][(f["exposures"]["Position"] == position) & (f["exposures"]["Date"] == d)]
                [["Factor", "Loading"]])
        loadings = [{"Factor": r.Factor, "Loading": _clean(r.Loading)} for r in load.itertuples()]
        svr = f["specific_var"][(f["specific_var"]["Position"] == position) & (f["specific_var"]["Date"] <= d)]
        sv = float(svr.sort_values("Date")["SpecificVar"].iloc[-1]) if len(svr) else None
        s = sec.loc[position]
        return {"position": position, "ticker": s.get("Ticker"), "issuer": s.get("Issuer"),
                "sector": s.get("Sector"), "country": s.get("Country"),
                "weight": weight, "specific_var": sv, "loadings": loadings}
    return await run_in_threadpool(run)


@app.get("/validation")
async def validation(manager: str | None = None, book: str | None = None):
    """Top-3 sub-portfolio: cube scenario VaR vs an independent pandas reference (mirrors barra_excel_check)."""
    manager = _coalesce_manager(manager, book)
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        f = S["frames"]
        positions, securities = f["positions"], f["securities"]
        factor_ret, specific = f["factor_returns"], f["specific_var"]
        # Scope to one manager BEFORE picking the top 3: across 11 managers nlargest would mix
        # managers, and the cube side below would read the collapsed no-manager grand total. The
        # local holding the picked rows is `top3`, NOT `manager` — rebinding the parameter name
        # inside the closure is what made /span raise UnboundLocalError.
        if "Manager" in positions.columns:
            positions = positions[positions["Manager"] == manager]
            if positions.empty:
                raise HTTPException(404, f"no positions for manager {manager!r}")
        last = positions["Date"].max()
        top3 = (positions[positions["Date"] == last].nlargest(3, "Weight")
                .merge(securities[["Position", "Ticker"]], on="Position"))
        figs = top3["Position"].tolist()

        # --- cube side: 3-position slice, by scenario set ---
        cdf = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], levels=[l["ScenarioSet"]],
                         filter=(l["Date"] == pd.Timestamp(last).date()) & l["Position"].isin(*figs)
                                & (l["Manager"] == manager))

        # --- pandas reference: same math as the Excel workbook (Market INCLUDED: leaf loading 1.0) ---
        wide = (factor_ret
                .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
        factors = list(wide.columns)
        L = (f["exposures"][(f["exposures"]["Date"] == last) & (f["exposures"]["Position"].isin(figs))]
             .pivot(index="Position", columns="Factor", values="Loading")
             .reindex(index=figs, columns=factors).fillna(0.0))
        wts = top3.set_index("Position")["Weight"].reindex(figs)
        x = L.values.T @ wts.values
        pnl = wide.values @ x
        ref = {"HistFull": (-float(np.percentile(pnl, 1)), -float(pnl.min()))}
        for name, (a, b) in EVENT_WINDOWS.items():
            wv = pd.Series(pnl, index=wide.index).loc[a:b]
            if len(wv):
                ref[name] = (-float(np.percentile(wv, 1)), -float(wv.min()))

        rows = []
        for rec in _records(cdf):
            s = rec["ScenarioSet"]
            rv, rw = ref.get(s, (None, None))
            rows.append({"ScenarioSet": s,
                         "cube_var99": rec["Scenario VaR 99"], "ref_var99": rv,
                         "cube_worst": rec["Scenario worst loss"], "ref_worst": rw})
        return {"as_of": str(pd.Timestamp(last).date()),
                "holdings": [{"ticker": t, "weight": _clean(w)} for t, w in zip(top3["Ticker"], top3["Weight"])],
                "rows": rows}
    return await run_in_threadpool(run)


# ----------------------------------------------------------------------------- generic pivot
_DIMS_CACHE: dict = {}   # {"key": (id(cube), response_dict)} -- see _dims_response


def _dim_members_via_count(cube, l, m, d: str) -> list[str]:
    """One dimension's member list via the ORIGINAL contributors.COUNT groupby -- semantics
    byte-identical to the pre-2026-08-15 /dims, just factored out so callers can run several of
    these concurrently (see _dims_response)."""
    idx = cube.query(m["contributors.COUNT"], levels=[_lvl(l, d)]).index
    # deep levels (Factor under FactorGroup, Sector under Country) come back as a MultiIndex
    # hierarchy path -- the level's own member is the last component.
    vals = idx.get_level_values(-1) if isinstance(idx, pd.MultiIndex) else idx
    if d == "Date":
        return sorted({str(pd.Timestamp(x).date()) for x in vals})
    return sorted({str(x) for x in vals})


def _manager_members(cube, session, l, m) -> list[str]:
    """Manager members WITHOUT the fan-out contributors.COUNT groupby (2026-08-15 cube-opt
    round 2, docs/cube-opt-round2-dims.md). That groupby is ~85-90% of /dims's cost on the
    124-manager build (13-25s of 15-28s measured across runs): Manager is an UN-MAPPED key of the
    Positions table's partial join onto Exposures (Date+Position only, Manager left out -- the
    same partial-join trick ScenarioSet uses), so counting contributors per Manager member means fanning
    the 6M-row Exposures fact out against the matching slice of the 11.6M-row Positions table for
    EVERY member at once.

    Three cheap engine queries replace it (all round-trip into the live Atoti session, never
    S["frames"]/pandas.read_parquet -- the cube stays the source of truth). Two variants were
    tried and measured before this one:
      - fetching the WHOLE Positions.Manager column (Table.query, 11.6M rows) and de-duping
        client-side: correct, but the data volume alone costs ~3.5s;
        `t_pos.query(t_pos["Manager"], max_rows=20_000_000)` then `.unique()`.
      - the SAME idea flattened into one big worker pool alongside everything else: measured
        WORSE (2.8s cold) than the nested version below -- too much concurrent JVM query
        dispatch contends with itself; small, separately-pooled batches of concurrent work beat
        one giant flat one.
    What's here instead, and what's actually fast (measured ~1.4-1.5s for the whole /dims call
    including this): candidate names come off the tiny (~124-row) Managers table (near-instant),
    then EACH candidate's existence in Positions is checked with its own tightly-filtered,
    max_rows=1 Table.query (the engine can short-circuit on the first match instead of scanning),
    run CONCURRENTLY in their own worker pool. "N/A" (an Exposures (Date, Position) row that no
    manager holds that date -- the partial join's unmatched placeholder) is checked the same way as
    before: a SINGLE-CELL FILTERED contributors.COUNT (`Manager.isin("N/A")`) instead of enumerating
    and counting all 124 members, so the engine answers from its per-member index rather than a
    full fan-out scan. One measured, disclosed gap: the Managers table (managers.parquet, Phase-2
    optional) lists every manager the desk tracks, including ones with zero equity positions ever
    (e.g. MetLife -- 6 CUSIPs, no equity 13F row, "stays in MANAGERS but never reaches the
    cube/UI" per CLAUDE.md); the existence check is exactly what filters those back out, so the
    result still matches the fan-out truth, not the raw candidate list.
    """
    t_mgr = session.tables.get("Managers")   # None on pre-Phase-2 builds / v1 data (no managers.parquet)

    def _exists(t_pos, name: str) -> bool:
        r = t_pos.query(t_pos["Manager"], filter=t_pos["Manager"] == name, max_rows=1)
        return len(r) > 0

    t_pos = session.tables["Positions"]
    if t_mgr is not None:
        cand_df = t_mgr.query(t_mgr["Manager"], max_rows=1000)
        candidates = sorted({str(x) for x in cand_df["Manager"].unique()})
        # 32 workers, NOT one-per-candidate: measured worse (2.8s vs 1.4-1.5s) with a worker per
        # candidate (124 here) -- too much concurrent JVM query dispatch contends with itself.
        with ThreadPoolExecutor(max_workers=32) as ex:
            futs = {c: ex.submit(_exists, t_pos, c) for c in candidates}
            members = {c for c, f in futs.items() if f.result()}
    else:
        # no managers.parquet (pre-Phase-2 build / v1 data): fall back to the full-column scan --
        # slower (~3.5s) but still cube-native, and the only source of Manager candidates available.
        mgr_df = t_pos.query(t_pos["Manager"], max_rows=20_000_000)
        members = {str(x) for x in mgr_df["Manager"].unique()}

    na_df = cube.query(m["contributors.COUNT"], filter=l["Manager"].isin("N/A"))
    if len(na_df) and int(na_df.iloc[0, 0]) > 0:
        members.add("N/A")
    return sorted(members)


def _day_members(session) -> dict[str, list]:
    """Members of the three Day-path dimensions, off the ScenarioDays table itself (one factor's
    rows -- every factor carries the same (DaySet, Day, DayDate) triples). NOT the
    contributors.COUNT groupby: `Day` has ~2.6k members and that fan-out is the measured ~16 s
    per-member floor (docs/cube-opt-round2-scenarioday.md) -- it would undo the /dims work."""
    t = session.tables.get("ScenarioDays")
    if t is None:
        return {d: [] for d in DAY_DIMS}
    df = t.query(t["DaySet"], t["Day"], t["DayDate"], filter=t["Factor"] == "Market",
                 max_rows=1_000_000)
    return {"Day": sorted({int(x) for x in df["Day"]}),
            "DayDate": sorted({str(pd.Timestamp(x).date()) for x in df["DayDate"]}),
            "DaySet": sorted({str(x) for x in df["DaySet"]})}


def _dims_response_fallback() -> dict:
    """The pre-2026-08-15 sequential contributors.COUNT logic, byte-identical to the original
    /dims -- kept as a safety net if the cube-native fast path above raises for any reason (e.g.
    a cube variant with no Positions table registered under that name)."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    members = {d: _dim_members_via_count(cube, l, m, d) for d in DIM_NAMES if d not in DAY_DIMS}
    members.update(_day_members(S["session"]))
    members["ScenarioSet"] = [x for x in members["ScenarioSet"] if not x.startswith("PIT:")]
    return {"dimensions": DIM_NAMES, "measures": [x for x in MEASURE_NAMES
                         # the manager-independent attribution trio is ALWAYS rejected by
                         # _validate_pivot on a multi-manager build — don't offer it in the picker
                         if not (x in MANAGER_INDEPENDENT_MEASURES and len(_manager_names()) > 1)],
            "dollar_measures": [x for x in DOLLAR_MEASURES if x in MEASURE_NAMES],
            "scenario_dependent": sorted(SCEN_DEP), "day_dependent": sorted(DAY_DEP),
            "price_dependent": sorted(PRICE_DEP),
            "members": members,
            "dates": members["Date"], "scenario_sets": members["ScenarioSet"]}


def _dims_response() -> dict:
    """Member lists for every sliceable dimension, so the UI can offer single/multi selection on
    any of them (not just Date / ScenarioSet). Cached on a cube-identity token (recomputes only if
    the cube is ever rebuilt in-process -- never happens today, frames/cube are loaded once at
    startup and held for the process lifetime) so every call after the first is a dict lookup.

    2026-08-15 cube-opt round 2 (docs/cube-opt-round2-dims.md): this used to be ten sequential
    contributors.COUNT groupbys, one per DIM_NAMES entry, ~28s total on the 124-manager build (~24s
    of it the Manager dimension alone -- Step 7's flagged next candidate in
    docs/cube-optimization-plan.md). Now: the nine cheap dimensions run CONCURRENTLY (same exact
    query each, just not serialized -- correctness is untouched by construction), and Manager
    is answered by _manager_members's two targeted engine queries instead of the fan-out groupby.
    Measured end to end (124-manager build, cold): see the round-2 doc.
    """
    cube, session = S["cube"], S["session"]
    key = id(cube)
    cached = _DIMS_CACHE.get("key")
    if cached is not None and cached[0] == key:
        return cached[1]
    try:
        l, m = cube.levels, cube.measures
        cheap_dims = [d for d in DIM_NAMES if d != "Manager" and d not in DAY_DIMS]
        with ThreadPoolExecutor(max_workers=len(DIM_NAMES)) as ex:
            futs = {d: ex.submit(_dim_members_via_count, cube, l, m, d) for d in cheap_dims}
            f_mgr = ex.submit(_manager_members, cube, session, l, m)
            f_day = ex.submit(_day_members, session)
            members = {d: f.result() for d, f in futs.items()}
            members["Manager"] = f_mgr.result()
            members.update(f_day.result())
        members["ScenarioSet"] = [x for x in members["ScenarioSet"] if not x.startswith("PIT:")]
        resp = {"dimensions": DIM_NAMES, "measures": [x for x in MEASURE_NAMES
                         # the manager-independent attribution trio is ALWAYS rejected by
                         # _validate_pivot on a multi-manager build — don't offer it in the picker
                         if not (x in MANAGER_INDEPENDENT_MEASURES and len(_manager_names()) > 1)],
            "dollar_measures": [x for x in DOLLAR_MEASURES if x in MEASURE_NAMES],
                "scenario_dependent": sorted(SCEN_DEP), "day_dependent": sorted(DAY_DEP),
            "price_dependent": sorted(PRICE_DEP),
            "members": members,
                "dates": members["Date"], "scenario_sets": members["ScenarioSet"]}
    except Exception:
        resp = _dims_response_fallback()
    _DIMS_CACHE["key"] = (key, resp)
    return resp


@app.get("/dims")
async def dims():
    """Fields the pivot UI may use: dimensions, scalar measures, and slicer member lists."""
    return await run_in_threadpool(_dims_response)


def _csv(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _parse_filters(filters: str | None, date: str | None, set: str | None) -> dict:
    """Slicer spec {dimension: [members]} from the `filters` JSON, folding in the legacy
    single-value `date`/`set` params. Empty member lists are dropped."""
    fd: dict = {}
    if filters:
        try:
            raw = json.loads(filters)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"bad filters JSON: {e}")
        if not isinstance(raw, dict):
            raise HTTPException(400, "filters must be a JSON object {dimension: [members]}")
        for d, vals in raw.items():
            vals = vals if isinstance(vals, list) else [vals]
            vals = [str(v) for v in vals if v is not None and str(v) != ""]
            if vals:
                fd[d] = vals
    if date:
        fd.setdefault("Date", [date])
    if set:
        fd.setdefault("ScenarioSet", [set])
    return fd


def _build_filter(l, fd: dict):
    """AND across dimensions, OR (isin) within a dimension. Date members -> timestamps.
    Dimension keys are canonical API names (Manager, ...), mapped to cube levels via _lvl."""
    cond = None
    for d, vals in fd.items():
        if d in ("Date", "DayDate"):
            members = [_date(v) for v in vals]
        elif d == "Day":
            members = [int(v) for v in vals]     # the set's day index is an int level
        else:
            members = list(vals)
        c = _lvl(l, _canon_dim(d)).isin(*members)
        cond = c if cond is None else (cond & c)
    return cond


# The PIT:* truncated-history sets moved OFF the ScenarioSet hierarchy onto their own PITSet
# hierarchy (2026-08-14 cube optimization Step 2 — they were 123 of ScenarioSet's 130 members and
# every group-by-ScenarioSet paid for all of them). The API contract is unchanged: a PIT set is
# still hidden from the dropdown and still addressable BY NAME in a ScenarioSet filter. Only the
# honest-vol pair is mirrored onto the new hierarchy, so those are the only measures a PIT-filtered
# query can serve; anything else 400s rather than silently reading the full-history number.
PIT_MIRROR = {"Model vol": "PIT Model vol", "Scenario PnL vol": "PIT Scenario PnL vol"}


def _pit_addressing(cube, fdict: dict, axis: list, mlist: list):
    """Rewrite a ScenarioSet filter naming PIT:* sets onto the PITSet hierarchy + mirrored
    measures. Returns (fdict, axis, measure_names_to_query, pit_mode)."""
    vals = [str(v) for v in (fdict.get("ScenarioSet") or [])]
    pit = [v for v in vals if v.startswith("PIT:")]
    if not pit or not _has_pit(cube):
        return fdict, axis, list(mlist), False
    if len(pit) != len(vals):
        raise HTTPException(400, "cannot mix PIT:* sets with the real scenario sets in one filter")
    bad = [x for x in mlist if x in SCEN_DEP and x not in PIT_MIRROR]
    if bad:
        raise HTTPException(400, f"{bad} are not available on the PIT:* truncated-history sets — "
                                 f"only {sorted(PIT_MIRROR)} are mirrored there (as-of risk "
                                 "plumbing, not a browsable scenario family).")
    fdict = {k: v for k, v in fdict.items() if k != "ScenarioSet"} | {"PITSet": pit}
    axis = ["PITSet" if a == "ScenarioSet" else a for a in axis]
    return fdict, axis, [PIT_MIRROR.get(x, x) for x in mlist], True


# Factor contribution / Specific PnL / Realized PnL are baked PHYSICAL columns on tables keyed
# WITHOUT Manager (deliberately, so attribution stays immune to the what-if branch — see
# barra_factor_risk_cube.py's long "KNOWN LIMITATION" comment near `w = (positions[...]`). With
# more than one manager loaded, the merge that builds them takes the FIRST MANAGER ALPHABETICALLY's
# weight for any (Date, Position) — so they read that ONE manager's numbers under every manager's
# label, not "whichever manager is sliced." That is true even for an UNSLICED / grand-total query
# (there's no live per-manager weighting to fall back to, just the one baked column), so the guard
# below is unconditional on multi-manager, not just "manager-sliced" queries.
MANAGER_INDEPENDENT_MEASURES = {"Factor contribution", "Specific PnL", "Realized PnL"}


def _manager_names() -> list[str]:
    """The managers on the live positions frame, sorted — MEMOIZED per frame (api_bench 2026-08-21).
    `unique()`/`nunique()` over an 11.6M-row object column costs ~0.35 s on the 124-manager build,
    and two hot paths ran one per call: `_validate_pivot` (so EVERY /pivot, /analysis and /ask
    tool round-trip paid it, cache hit or not — it was the fixed floor under every grid query)
    and `_managers_meta` (so every /meta, the first call each UI page load makes). The frames are
    loaded once and never change in-process, so this is a constant."""
    # S.get, not S[...]: _validate_pivot calls this, and the pivot-spec unit tests exercise that
    # validator with no cube loaded at all, so "frames" is simply absent there.
    pos = (S.get("frames") or {}).get("positions")
    if pos is None or "Manager" not in pos.columns:
        return []
    # Keyed by a WEAKREF to the frame, not id(): the unit tests swap short-lived stub frames in
    # and out of S, and CPython reuses the address of a collected one — an id-keyed memo handed
    # the next stub the previous stub's managers. A weakref that no longer resolves to THIS object
    # is a miss, so a recycled address can never be a hit.
    hit = S.get("_manager_names_memo")
    if hit is not None and hit[0]() is pos:
        return hit[1]
    names = sorted(pos["Manager"].unique().tolist())
    S["_manager_names_memo"] = (weakref.ref(pos), names)
    return names


def _n_managers() -> int:
    return len(_manager_names())


def _multi_manager_cube() -> bool:
    """True once more than one Manager is loaded on the live positions frame."""
    return _n_managers() > 1


def _validate_pivot(rlist: list, clist: list, mlist: list, fdict: dict) -> None:
    """Allowlist guard shared by /pivot, /analysis and /ask's query_cube tool: only whitelisted
    dims/measures, a non-empty rows+measures selection, and (once >1 manager is loaded) a refusal
    of the three manager-independent attribution measures — they cannot be trusted per-manager (see
    MANAGER_INDEPENDENT_MEASURES above). Raises HTTPException(400) exactly as /pivot always has, so
    every caller of this guard inherits all three checks identically.

    Also canonicalizes dimension ALIASES in place ("Book" -> "Manager", 2026-08-14) and measure
    ALIASES in place ("Book MV" -> "Manager MV", 2026-08-22) so every guarded path — /pivot,
    /analysis, /ask — accepts legacy names (saved views, old URLs) while the query layer and the
    response only ever see canonical names."""
    rlist[:] = [_canon_dim(d) for d in rlist]
    clist[:] = [_canon_dim(d) for d in clist]
    for k in [k for k in fdict if _canon_dim(k) != k]:
        fdict[_canon_dim(k)] = fdict.pop(k)
    mlist[:] = [_canon_measure(x) for x in mlist]
    bad_d = [d for d in rlist + clist + list(fdict) if d not in DIM_NAMES]
    bad_m = [x for x in mlist if x not in MEASURE_NAMES]
    if bad_d:
        raise HTTPException(400, f"unknown dimension(s): {bad_d}")
    if bad_m:
        raise HTTPException(400, f"unknown measure(s): {bad_m}")
    if not mlist:
        raise HTTPException(400, "select at least one measure")
    if not rlist:
        raise HTTPException(400, "select at least one row field")
    if _multi_manager_cube():
        unsafe = [x for x in mlist if x in MANAGER_INDEPENDENT_MEASURES]
        if unsafe:
            n_managers = _n_managers()
            raise HTTPException(400,
                f"{unsafe} are manager-independent (baked columns with no Manager key — a known "
                f"atoti 0.9.15 limitation, see barra_factor_risk_cube.py) and cannot be trusted "
                f"per-manager with {n_managers} managers loaded: they would silently read one "
                "arbitrary manager's numbers under every manager's label. Use "
                "barra_pnl_attribution.py's manager= precompute for correct per-manager "
                "attribution instead.")


def _needs_date_default(mlist: list, axis: list, fdict: dict) -> bool:
    """Is this the measured pathology — a scenario measure, MANAGERS on an axis, and NO Date
    anywhere in context? Pure (no cube), so it is unit-testable.

    Measured on the 124-manager cube (docs/cube-optimization-plan.md, hotspot 3): `Scenario VaR 99`
    by Manager with no Date filter is **60.2 s** on an idle cube and 500s on a loaded one — it
    builds one P&L vector per manager over the whole calendar. The SAME query with a single Date is
    **0.76 s**. Nothing asks for the multi-date shape on purpose: it is what a field-list drag
    produces before the user picks a date."""
    return (any(x in SCEN_DEP or x in DAY_DEP or x in PRICE_DEP for x in mlist)
            and "Manager" in axis
            and "Date" not in axis and "Date" not in fdict)


# The Day-shape VECTOR PLAN (2026-08-15, follow-up 2 of docs/cube-opt-round2-scenarioday.md).
# `PnL at day` over the Day/DayDate LEVELS pays an irreducible per-member fact scan (~0.5-0.8 ms
# per fact-joined member on atoti 0.9.15, x 2,618 days, x every measure on the axis -- and it
# parallelises across every core, so on a shared box it runs 5-10x slower than the quiet-box
# bench). The SAME numbers are already in the cube as ONE cell: `Scenario PnL vector` (per
# breakout cell when there are breakouts) and its `Scenario dates (epoch)` dual -- ~0.15 s on the
# largest manager regardless of load. So when a /pivot query is exactly the Day shape, read the
# vector(s) and RESHAPE them into the identical tidy records (the /backtest, /drawdown and
# /scenario_pnl precedent: unpacking a cube vector is reshape, not analytics -- every number is
# still the cube's own vector element; the tie-out is pinned at 1e-12 by test_risk_measures).
# Anything else falls through to the level plan untouched, and `plan=levels` forces it.
_DAY_BREAKOUTS = ("Sector", "Issuer", "Position", "Factor", "FactorGroup", "Country")


_DAY_VECTOR_MAX_BREAKOUTS = 2


def _day_vector_shape(rlist: list, clist: list, mlist: list, fdict: dict):
    """Is (rows, cols, measures, filters) the Day shape the vector plan serves? Returns
    (with_daydate, breakouts, set_name, day_filter) or None. Pure (no cube), unit-testable.
    Shape: rows = Day [, DayDate] [, up to two breakouts]; no cols; every measure in DAY_DEP;
    filters carry exactly one Date, one Manager, and ONE DaySet (plus at most one ScenarioSet,
    equal to it -- the two hierarchies carry the same names for the 7 real sets).

    2026-08-21 (TODO item 5) — the two shapes that used to fall through to the level plan and its
    per-member floor now stay on the vector plan:
      * a SECOND breakout: the vector query just takes two levels, so the cost follows the output
        rows instead of the member count;
      * a Day / DayDate FILTER (a chart zoom): the vector is per-set, so the window is a slice of
        the records the same query already produced. Cheaper than asking the cube to re-scan.
    Both are pinned record-for-record against `plan=levels` by
    test_risk_measures.py::t_day_vector_plan_ties_level_plan."""
    if clist or not rlist or rlist[0] != "Day":
        return None
    if not mlist or any(x not in DAY_DEP for x in mlist):
        return None
    rest = list(rlist[1:])
    with_daydate = bool(rest) and rest[0] == "DayDate"
    if with_daydate:
        rest = rest[1:]
    if len(rest) > _DAY_VECTOR_MAX_BREAKOUTS or any(x not in _DAY_BREAKOUTS for x in rest):
        return None
    breakouts = list(rest)
    for k in ("Date", "Manager"):
        if len(fdict.get(k) or []) != 1:
            return None
    ds, ss = fdict.get("DaySet") or [], fdict.get("ScenarioSet") or []
    # DaySet is REQUIRED (a ScenarioSet-only query is the warned "no DaySet context" shape and
    # stays on the level plan, so the warning and the served result keep saying the same thing).
    if len(ds) != 1 or len(ss) > 1:
        return None
    if ds and ss and ds[0] != ss[0]:
        return None
    set_name = (ds or ss)[0]
    if str(set_name).startswith("PIT:"):
        return None                     # PIT sets are their own hierarchy; not a Day-path case
    day_filter = {k: list(fdict[k]) for k in ("Day", "DayDate") if fdict.get(k)}
    return with_daydate, breakouts, str(set_name), day_filter


def _day_vector_records(cube, l, m, shape, mlist: list, fdict: dict, query_kw: dict,
                        stress_scenario: str | None) -> list[dict]:
    """The vector plan's records: identical columns/order/types to the level plan (Day int,
    DayDate ISO date, the breakout's full hierarchy path as the cube emits it, then the measures
    in the caller's order). Manager-level markers (`VaR line at day` & co.) come from ONE single-cell
    query and are broadcast to every row, exactly what the cube's lifted markers evaluate to."""
    with_daydate, breakouts, set_name, day_filter = shape
    # the day window is applied to the RECORDS below, not to the cube filter: the P&L vector is
    # per-set, so a Day/DayDate slicer would only make the cube re-derive the same array.
    fd = {k: v for k, v in fdict.items()
          if k not in ("DaySet", "ScenarioSet", "Day", "DayDate")}
    fd["ScenarioSet"] = [set_name]
    filt = _build_filter(l, fd)
    if stress_scenario is not None:
        filt = filt & (l["StressShock"] == stress_scenario)
    dv = cube.query(m["Scenario dates (epoch)"], filter=(l["ScenarioSet"] == set_name))
    days = np.asarray(dv.iloc[0, 0], dtype=int) if len(dv) and dv.iloc[0, 0] is not None \
        else np.zeros(0, dtype=int)
    if not breakouts:
        pv = cube.query(m["Scenario PnL vector"], filter=filt, **query_kw)
        vecs = [((), np.asarray(pv.iloc[0, 0], dtype=float))] \
            if len(pv) and pv.iloc[0, 0] is not None else []
        path_names: list = []
    else:
        pv = cube.query(m["Scenario PnL vector"], levels=[_lvl(l, b) for b in breakouts],
                        filter=filt, **query_kw)
        path_names = list(pv.index.names)
        vecs = []
        for idx, row in pv.iterrows():
            arr = row.iloc[0]
            if arr is None:
                continue
            key = idx if isinstance(idx, tuple) else (idx,)
            vecs.append((tuple(key), np.asarray(arr, dtype=float)))
    markers = {}
    if any(x != "PnL at day" for x in mlist):
        sc = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"],
                        m["Scenario worst date (epoch)"], filter=filt, **query_kw)
        r = sc.iloc[0] if len(sc) else None
        def _f(name, sign=1.0):
            if r is None or r[name] is None:
                return None
            f = float(r[name]); return None if math.isnan(f) else sign * f
        wd = None if r is None or r["Scenario worst date (epoch)"] is None \
            else int(r["Scenario worst date (epoch)"])
        markers = {"VaR line at day": _f("Scenario VaR 99", -1.0),
                   "Worst pnl at day": _f("Scenario worst loss", -1.0),
                   "Worst date at day (epoch)": wd}
    n_days = int(min([len(days)] + [len(a) for _, a in vecs])) if vecs else 0
    # Build the dicts directly (no DataFrame/iterrows: 199k rows on Vanguard x Sector cost ~6 s
    # that way). Same key order and value types as _records: Day int, DayDate ISO string, the
    # path members as the cube emitted them, then the measures (float / marker / None).
    day_iso = [(_EPOCH + pd.Timedelta(days=int(days[d]))).date().isoformat() for d in range(n_days)]
    keys = [(tuple(_clean(kv) for kv in key), [float(x) for x in arr[:n_days]]) for key, arr in vecs]
    static = {x: markers.get(x) for x in mlist if x != "PnL at day"}
    # the day window: Day members are the vector's own indices and DayDate their date dual, so
    # both slicers are a filter over `range(n_days)`. Day numbering is NOT re-based — a filtered
    # row keeps the day index the level plan gives it.
    day_ix = range(n_days)
    if day_filter.get("Day"):
        want = {int(v) for v in day_filter["Day"]}
        day_ix = [d for d in day_ix if d in want]
    if day_filter.get("DayDate"):
        want_d = {str(_date(v)) for v in day_filter["DayDate"]}
        day_ix = [d for d in day_ix if day_iso[d] in want_d]
    out = []
    for day in day_ix:
        for key, vals in keys:
            rec = {"Day": day}
            if with_daydate:
                rec["DayDate"] = day_iso[day]
            for nm, kv in zip(path_names, key):
                rec[nm] = kv
            v = vals[day]
            for x in mlist:
                rec[x] = (None if v != v else v) if x == "PnL at day" else static[x]
            out.append(rec)
    return out


# The repeat-view cache (api_bench 2026-08-21, TODO item 1). A pivot on the BASE scenario is a
# pure function of (axes, measures, slicers, totals, plan) — the frames and the cube never change
# in-process — but nothing memoized it, so a Date-series of scenario measures (the Trends-style
# `rows=Date` shape, 2.5 s on Vanguard) re-aggregated on every call including an identical repeat.
# Bounded LRU: transient hypothetical branches are never cached, and a payload bigger than
# _PIVOT_CACHE_MAX_RECORDS is served but not retained (the 199k-row Day x Sector shapes).
# Cold time is unchanged — this buys repeat views, nothing else.
_PIVOT_CACHE_MAX = int(os.environ.get("BARRA_PIVOT_CACHE", "48"))       # 0 disables
_PIVOT_CACHE_MAX_RECORDS = 25_000


def _pivot_cache_key(rlist, clist, mlist, fdict, totals, plan, dollar=False):
    return (tuple(rlist), tuple(clist), tuple(mlist),
            tuple(sorted((k, tuple(str(x) for x in v)) for k, v in fdict.items())),
            bool(totals), plan, bool(dollar), id(S["cube"]))


def _pivot_result(rlist: list, clist: list, mlist: list, fdict: dict, totals: bool,
                  scenario: str | None = None, stress_scenario: str | None = None,
                  plan: str | None = None, dollar: bool = False) -> dict:
    """`_pivot_query` behind the bounded repeat-view LRU above. Callers get a shallow copy, so
    /ask's record truncation (and anything else that rebinds a top-level key) can't poison the
    cache. Hypothetical branches (what-if trades / a StressShock scenario) bypass it entirely.
    `dollar` (the Units="$" slice) is part of the cache key — a weight-unit and a dollar view of
    the same slice are cached separately."""
    if scenario is not None or stress_scenario is not None or _PIVOT_CACHE_MAX <= 0:
        return _pivot_query(rlist, clist, mlist, fdict, totals, scenario, stress_scenario, plan, dollar)
    ck = _pivot_cache_key(rlist, clist, mlist, fdict, totals, plan, dollar)
    cache = S.setdefault("_pivot_cache", OrderedDict())
    hit = cache.get(ck)
    if hit is not None:
        try:
            cache.move_to_end(ck)          # racy across threadpool workers; a miss is harmless
        except KeyError:
            pass
        return dict(hit)
    out = _pivot_query(rlist, clist, mlist, fdict, totals, scenario, stress_scenario, plan, dollar)
    if len(out.get("records") or []) <= _PIVOT_CACHE_MAX_RECORDS:
        cache[ck] = out
        while len(cache) > _PIVOT_CACHE_MAX:
            try:
                cache.popitem(last=False)
            except KeyError:
                break
    return dict(out)


def _pivot_query(rlist: list, clist: list, mlist: list, fdict: dict, totals: bool,
                 scenario: str | None = None, stress_scenario: str | None = None,
                 plan: str | None = None, dollar: bool = False) -> dict:
    """The tidy pivot result (records [+ per_row/per_col/grand margins when totals]). Extracted
    from /pivot so /analysis feeds the model the EXACT numbers the view renders. Synchronous —
    call via run_in_threadpool. Assumes _validate_pivot has already run.
    `scenario` = a SOURCE-scenario branch name (what-if trades — cube.query(scenario=...));
    `stress_scenario` = a StressShock PARAMETER-simulation scenario (custom sigmas — selected
    by slicing the StressShock level). Both default to the base.
    `plan` = None/"auto" (the Day shape takes the vector plan, see _day_vector_shape) or
    "levels" (force the level plan — the tie-out tests use it).
    `dollar` = slice the cube's `Units` context to its "$" scenario (2026-08-22 Units-context
    refactor — replaces the old "<measure> $" twin rename): every DOLLAR_MEASURES member in
    `mlist` then reads in dollars under its OWN (unchanged) name; everything else (ratios, dates,
    counts) is untouched, since only the raw "(wt)" internals respond to the slice."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    seen, axis = set(), []
    for name in rlist + clist:          # dedupe, preserve order
        if name not in seen:
            seen.add(name); axis.append(name)
    fdict, axis, mnames, pit_mode = _pit_addressing(cube, fdict, axis, mlist)
    warnings = []
    # Date-context DEFAULT (optimization Step 6): scenario measures × Managers × no Date is the
    # 60-second shape. Inject the latest COB rather than serve it, and say so — the same
    # warn-in-the-payload idiom as the missing-ScenarioSet note below.
    if _needs_date_default(mlist, axis, fdict):
        _d = _latest_date()
        fdict = {**fdict, "Date": [_d]}
        warnings.append(f"No Date in context with scenario measures across managers — that query "
                        f"builds a P&L vector per manager over the whole calendar (measured 60 s, "
                        f"and it times out on a loaded cube). Defaulted to the latest COB {_d}; "
                        f"pick a Date explicitly to override.")
    filt = _build_filter(l, fdict)
    if stress_scenario is not None:
        filt = (filt & (l["StressShock"] == stress_scenario)) if filt is not None \
            else (l["StressShock"] == stress_scenario)
    if dollar:
        filt = (filt & (l["Units"] == "$")) if filt is not None else (l["Units"] == "$")

    scen_ctx = ("ScenarioSet" in axis) or ("ScenarioSet" in fdict) or pit_mode
    if any(x in SCEN_DEP for x in mlist) and not scen_ctx:
        warnings.append("Scenario measures need a ScenarioSet context — put ScenarioSet on an "
                        "axis or pick a single scenario; otherwise those cells are blank.")
    day_ctx = ("DaySet" in axis) or ("DaySet" in fdict)
    if any(x in DAY_DEP for x in mlist) and not day_ctx:
        warnings.append("Per-day measures (PnL at day & co.) read the DaySet hierarchy, not "
                        "ScenarioSet — put DaySet on an axis or pick a single DaySet; otherwise "
                        "the Day/DayDate axis stacks every set's days.")
    price_ctx = ("PriceSet" in axis) or ("PriceSet" in fdict)
    if any(x in PRICE_DEP for x in mlist) and not price_ctx:
        warnings.append("Price measures need a PriceSet context (their own switch hierarchy, "
                        "not ScenarioSet) — put PriceSet on an axis or pick a single set "
                        "(HistFull / Evt:*); otherwise those cells are blank.")
    warning = " ".join(warnings) or None
    meas_objs = [m[x] for x in mnames]
    _back = dict(zip(mnames, mlist))     # PIT mirror -> the name the caller asked for (identity off PIT)
    _kw = {"scenario": scenario} if scenario is not None else {}

    def _canon_ax(df):
        # the cube's level has been named "Manager" since the 2026-08-22 physical rename, so
        # this is now a plain PIT-mirror column rename with no axis-name shim needed.
        return df.rename(columns=_back) if pit_mode else df

    shape = None if plan == "levels" or pit_mode else _day_vector_shape(rlist, clist, mlist, fdict)
    if shape is not None:
        recs = _day_vector_records(cube, l, m, shape, mlist, fdict, _kw, stress_scenario)
        out = {"rows": rlist, "cols": clist, "measures": mlist, "totals": bool(totals),
               "warning": warning, "plan": "vector", "records": recs}
        if totals:
            # the Day path has no meaningful margins (a sum over days is not a risk number);
            # keep the keys the UI expects.
            out["per_row"] = []; out["grand"] = {}
        return out
    df = _canon_ax(cube.query(*meas_objs, levels=[_lvl(l, a) for a in axis], filter=filt, **_kw))
    out = {"rows": rlist, "cols": clist, "measures": mlist, "totals": bool(totals),
           "warning": warning, "plan": "levels", "records": _records(df)}
    if totals:
        def _ax(names):     # margins ride the same PIT rewrite as the main axis
            return [("PITSet" if (pit_mode and x == "ScenarioSet") else x) for x in names]
        per_row = _canon_ax(cube.query(*meas_objs, levels=[_lvl(l, a) for a in _ax(rlist)],
                                       filter=filt, **_kw))
        out["per_row"] = _records(per_row)                              # Total column
        if clist:
            per_col = _canon_ax(cube.query(*meas_objs, levels=[_lvl(l, a) for a in _ax(clist)],
                                           filter=filt, **_kw))
            out["per_col"] = _records(per_col)                          # Total row
        grand = _canon_ax(cube.query(*meas_objs, filter=filt, **_kw))   # corner
        def _scalar(v):                                                 # null array-measures -> None
            try:
                f = float(v); return None if math.isnan(f) else f
            except (TypeError, ValueError):
                return None
        out["grand"] = {x: _scalar(grand.iloc[0][x]) for x in mlist} if len(grand) else {}
    return out


def _whatif_branch_rows(date: str, manager: str, trades: list) -> pd.DataFrame:
    """Positions rows for a transient what-if SOURCE-scenario branch: the traded names' as-of
    rows with Weight replaced (a fabricated row for a coverage name not currently held).
    Untraded names inherit the base — a branch is a delta, not a copy."""
    pos = S["frames"]["positions"]
    d_ts = pd.Timestamp(date)
    base = pos[(pos["Manager"] == manager) & (pos["Date"] == d_ts)]
    rows = []
    for t in trades:
        p, nw = t["position"], float(t["weight"])
        r0 = base[base["Position"] == p]
        if len(r0):
            r = r0.iloc[0].to_dict(); r["Weight"] = nw
        else:
            r = {"Date": d_ts, "Manager": manager, "Position": p, "Weight": nw,
                 "MV": np.nan, "ADV": np.nan}
        rows.append(r)
    # The branch load must match the CUBE table's width, which since 2026-08-14 is narrower than
    # the frame (MV/ADV never cross into the JVM — optimization Step 4). Read the columns off the
    # live table so this follows the cube rather than restating its schema.
    try:
        cols = [c for c in pos.columns if c in set(S["session"].tables["Positions"].columns)]
    except Exception:
        cols = list(pos.columns)
    return pd.DataFrame(rows, columns=cols or list(pos.columns))


def _parse_hypo(whatif: str | None, shocks: str | None, fdict: dict):
    """Validate the hypothetical params shared by /pivot and /analysis -> (trades, shocks).
    400s BEFORE any cube/LLM work on bad JSON, unknown names, or a missing single-Date filter."""
    wtrades_ = shk_ = None
    if whatif:
        try:
            wtrades_ = json.loads(whatif)
            assert isinstance(wtrades_, list) and all(
                isinstance(t, dict) and "position" in t and "weight" in t for t in wtrades_)
        except Exception:
            raise HTTPException(400, 'whatif must be a JSON list of {"position", "weight"}')
        if len(fdict.get("Date") or []) != 1:
            raise HTTPException(400, "whatif needs exactly one Date filter")
        secs = {str(p_) for p_ in S["frames"]["securities"]["Position"]}
        bad = [t["position"] for t in wtrades_ if str(t["position"]) not in secs]
        if bad:
            raise HTTPException(400, f"unknown position(s): {bad}")
    if shocks:
        try:
            shk_ = json.loads(shocks)
            assert isinstance(shk_, dict) and shk_ and all(
                isinstance(v, (int, float)) for v in shk_.values())
        except Exception:
            raise HTTPException(400, 'shocks must be a JSON object {"Factor": sigma}')
        known = {str(f_) for f_ in S["frames"]["factor_meta"]["Factor"]}
        bad = [f_ for f_ in shk_ if f_ not in known]
        if bad:
            raise HTTPException(400, f"unknown factor(s): {bad}")
    return wtrades_, shk_


def _hypothetical_pivot(rlist: list, clist: list, mlist: list, fdict: dict, totals: bool,
                        wtrades: list | None, shk: dict | None, dollar: bool = False) -> dict:
    """_pivot_result on a transient hypothetical: a what-if source-scenario branch (trades)
    and/or a StressShock parameter scenario (sigmas) — created per call, dropped in finally.
    Synchronous — call via run_in_threadpool."""
    session = S["session"]
    branch = stress_scen = None
    sim = None
    try:
        if wtrades:
            branch = f"pivot-wf-{uuid.uuid4().hex[:12]}"
            manager = (fdict.get("Manager") or fdict.get("Book") or ["Soros"])[0]
            session.tables["Positions"].scenarios[branch].load(
                _whatif_branch_rows(fdict["Date"][0], manager, wtrades))
        if shk:
            stress_scen = f"pivot-st-{uuid.uuid4().hex[:12]}"
            sim = session.tables["StressShock"]
            sim.append(*[(stress_scen, f_, float(v)) for f_, v in shk.items()])
        return _pivot_result(rlist, clist, mlist, fdict, bool(totals),
                             scenario=branch, stress_scenario=stress_scen, dollar=dollar)
    finally:
        if branch is not None:
            try:
                session.delete_scenario(branch)
            except Exception:
                pass
        if sim is not None and stress_scen is not None:
            try:
                sim.drop(sim["Scenario"] == stress_scen)
            except Exception:
                pass


@app.get("/pivot")
async def pivot(rows: str = "", cols: str = "", measures: str = "",
                date: str | None = None, set: str | None = None,
                filters: str | None = None, totals: bool = False,
                whatif: str | None = Query(None, description=
                    'JSON [{"position","weight"}] — run the pivot on a transient what-if branch'),
                shocks: str | None = Query(None, description=
                    'JSON {"Factor": sigma} — run the pivot under a transient custom stress'),
                plan: str | None = Query(None, description=
                    '"levels" forces the level plan for the Day shape (default: vector plan)'),
                units: str | None = Query(None, description=
                    '"dollar": slice the cube\'s Units context to "$" — every DOLLAR_MEASURES '
                    'member in `measures` is then priced at measure × Manager MV, read under its '
                    'own (unchanged) name; default "weight" (fractions of portfolio value)')):
    """Tidy long result of cube.query(measures, levels=rows+cols, filter=<slicers>).

    Slicers: `filters` is a JSON object {dimension: [members]} — AND across dimensions,
    OR within a dimension. Single-value `date`/`set` query params still work and fold in.
    `filters` also accepts `{"Units": ["$"|"Base"]}` as an UNADVERTISED alias for `units` (saved
    views round-trip their state through `filters`); an explicit `units` query param wins.

    Guardrails: only whitelisted dimensions/scalar measures; the frontend pivots the tidy
    records into a matrix. Returns a `warning` when a scenario-dependent measure is requested
    without a ScenarioSet context (it would be null) so the UI can flag it. A measure named with
    the legacy "<name> $" suffix (removed 2026-08-22 — see the Units-context refactor) 400s with
    a pointer at `units=dollar` instead of an opaque "unknown measure".

    totals=True adds CUBE-COMPUTED margins (not summed — VaR is non-additive, so the cube
    recomputes the measure at the aggregated level): `per_row` (levels=rows, aggregated over
    columns -> the Total column), `per_col` (levels=cols -> the Total row), and `grand`
    (no levels -> the corner).

    `whatif` / `shocks` run the SAME guarded pivot on a transient hypothetical: a what-if
    source-scenario branch (needs exactly one Date filter) and/or a StressShock parameter
    scenario. Created per request, dropped in finally — stateless, so the grid can drill any
    measure under a trade or a shock with no scenario lifecycle to manage.
    """
    rlist, clist, mlist = _csv(rows), _csv(cols), _csv(measures)
    fdict = _parse_filters(filters, date, set)
    legacy = [x for x in mlist if x in _LEGACY_DOLLAR_NAMES]
    if legacy:
        raise HTTPException(400, f"{legacy}: '<measure> $' twins were removed 2026-08-22 — pass "
                             f"units=dollar and ask for the plain measure name instead.")
    units_alias = fdict.pop("Units", None)
    dollar = _units_is_dollar(units) if units is not None else _units_filter_is_dollar(units_alias)
    _validate_pivot(rlist, clist, mlist, fdict)
    wtrades, shk = _parse_hypo(whatif, shocks, fdict)
    if not wtrades and not shk:
        data = await run_in_threadpool(_pivot_result, rlist, clist, mlist, fdict, bool(totals),
                                       None, None, plan, dollar)
    else:
        data = await run_in_threadpool(_hypothetical_pivot, rlist, clist, mlist, fdict,
                                       bool(totals), wtrades, shk, dollar)
    out = {**data, "units": "dollar" if dollar else "weight"}
    if dollar:
        out["dollar_measures"] = [x for x in mlist if x in DOLLAR_MEASURES]
    return out


# Legacy "<name> $" measure names (removed 2026-08-22 — see the Units-context refactor): a
# request naming one directly gets a clear 400 pointing at units=dollar, not an opaque "unknown
# measure" from _validate_pivot (which has never heard of them).
_LEGACY_DOLLAR_NAMES = {f"{_x} $" for _x in DOLLAR_MEASURES}


def _units_is_dollar(units: str | None) -> bool:
    """`units` query param -> True for the dollar view; 400 on anything but weight/dollar."""
    u = (units or "weight").strip().lower()
    if u in ("weight", "fraction", ""):
        return False
    if u in ("dollar", "dollars", "$", "usd"):
        return True
    raise HTTPException(400, f"units must be 'weight' or 'dollar', got {units!r}")


def _units_filter_is_dollar(val) -> bool:
    """`{"Units": [...]}` filters-key alias for the `units` query param — same accepted
    spellings, plus the cube's own member names ("$" / "Base"). Not advertised (never in
    DIM_NAMES/dims.dimensions); exists only so a saved view's `filters` blob round-trips its
    units choice without a separate top-level field. None/empty -> weight (Base)."""
    if not val:
        return False
    v = str(val[0] if isinstance(val, (list, tuple)) else val).strip().lower()
    if v in ("base", "weight", "fraction", ""):
        return False
    if v in ("$", "dollar", "dollars", "usd"):
        return True
    raise HTTPException(400, f"Units filter must be '$' or 'Base', got {val!r}")


set_ = set   # preserve builtin; the endpoint shadows `set` with the query param
_EPOCH = pd.Timestamp("1970-01-01")


@app.get("/scenario_pnl")
async def scenario_pnl(date: str, set: str, position: str | None = None,
                       sector: str | None = None, filters: str | None = None,
                       breakout: str | None = None,
                       manager: str | None = None, book: str | None = None):
    """Labeled scenario P&L PATH: the `Scenario PnL vector` zipped with its `Scenario dates`
    dual, so every point carries the date that produced it. Names the worst-loss date and the
    99% VaR breach. One ScenarioSet only (the vector constraint).

    Drill: legacy `position`/`sector` params still work; `filters` is the same JSON object
    {dimension: [members]} `/pivot` takes (AND across dims, OR within) so the chart can scope
    to a Manager and any Sector/Issuer/Position/Factor slice. Date/ScenarioSet stay the path axis
    and must NOT appear in `filters` (they're the `date`/`set` params).

    `breakout` (a dimension, e.g. "Sector") adds a `dist_stacked` dataset: the cube's P&L vector
    grouped by that dimension (each member's per-day P&L — a CUBE aggregation), reshaped into
    (date, member, pnl, rank). `rank` is the day's position once ordered by the PORTFOLIO total
    (worst→best), so the chart can keep DATE labels on x while drawing the sorted loss curve.
    Stacked, the members sum to the portfolio P&L. The only API steps are ordering + reshape."""
    manager = _coalesce_manager(manager, book)
    _reject_pit(set)
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        filt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == set)
        if position:
            filt = filt & (l["Position"] == position)
        if sector:
            filt = filt & (l["Sector"] == sector)
        fd = _parse_filters(filters, None, None)           # drill dims only (no date/set refold)
        fd = {_canon_dim(k): v for k, v in fd.items()}     # "Book" (legacy) -> "Manager"
        fd.pop("Date", None); fd.pop("ScenarioSet", None)  # those are the fixed path axis
        # Default the Manager slice when the caller didn't scope one. The P&L vector is
        # weight-driven, so an unscoped query over several managers returns the collapsed grand
        # total rather than any portfolio's path (see /risk). An explicit Manager in `filters`
        # still wins — that is how the chart scopes to a manager.
        fd.setdefault("Manager", [manager])
        extra = _build_filter(l, fd)
        if extra is not None:
            filt = filt & extra
        pv = cube.query(m["Scenario PnL vector"], filter=filt)
        dv = cube.query(m["Scenario dates (epoch)"], filter=l["ScenarioSet"] == set)
        if not len(pv) or pv.iloc[0, 0] is None or not len(dv):
            return {"set": set, "date": date, "points": [], "n": 0,
                    "datasets": {"points": [], "dist": [], "stat": []}}
        pnl = np.asarray(pv.iloc[0, 0], dtype=float)
        days = np.asarray(dv.iloc[0, 0], dtype=int)
        n = min(len(pnl), len(days))
        pnl, days = pnl[:n], days[:n]
        # Unpacking the vector into per-day points is RESHAPE, not analytics — the cube already
        # produced the per-day portfolio P&L. READABLE date = the epoch-int dual converted to ISO here.
        dates = [(_EPOCH + pd.Timedelta(days=int(x))).date().isoformat() for x in days]
        points = [{"date": dates[i], "pnl": float(pnl[i])} for i in range(n)]
        # ALL distribution analytics are CUBE post-processors (no numpy percentile/min/mean/argmin):
        # VaR 99, worst loss + its date, and mean P&L are read straight off the cube measures.
        sc = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], m["Scenario mean PnL"],
                        m["Scenario worst date (epoch)"], filter=filt)
        r = sc.iloc[0]
        var99 = float(r["Scenario VaR 99"])
        worst = {"date": (_EPOCH + pd.Timedelta(days=int(r["Scenario worst date (epoch)"]))).date().isoformat(),
                 "pnl": -float(r["Scenario worst loss"])}
        # DISTRIBUTION: the cube sorts the P&L vector (Scenario PnL sorted); we only pair each
        # sorted value with its rank-percentile (reshape, not analytics). A histogram COUNT can't
        # be a cube measure with vectors (no array count-in-range), so the loss curve replaces it.
        sv = cube.query(m["Scenario PnL sorted"], filter=filt)
        srt = (np.asarray(sv.iloc[0, 0], dtype=float)
               if len(sv) and sv.iloc[0, 0] is not None else pnl)
        ns = len(srt)
        dist = [{"p": (i / (ns - 1) if ns > 1 else 0.0), "pnl": float(srt[i])} for i in range(ns)]
        datasets = {
            "points": points,
            "dist": dist,
            "stat": [{"var": -var99, "worst_pnl": worst["pnl"], "worst_date": worst["date"]}],
        }
        # optional breakout: per-scenario-day P&L stacked by a dimension (e.g. Sector). The cube
        # aggregates each member's per-day P&L (levels=[breakout]); we pair each day with its date
        # and a `rank` = its position ordered by the PORTFOLIO total (worst→best, argsort —
        # ordering, not aggregation), so the chart shows the SORTED loss curve but with DATE
        # labels on x.
        if breakout and _canon_dim(breakout) in DIM_NAMES:
            bv = cube.query(m["Scenario PnL vector"], levels=[_lvl(l, _canon_dim(breakout))],
                            filter=filt)
            members = []
            for idx, brow in bv.iterrows():
                member = idx[-1] if isinstance(idx, tuple) else idx
                arr = brow.iloc[0]
                members.append((str(member), np.asarray(arr, dtype=float) if arr is not None else None))
            order = [int(i) for i in np.argsort(pnl)]      # day indices, ascending by PORTFOLIO total
            # EMIT IN RANK ORDER: each scenario day (worst→best), all its members. The dates thus
            # first-appear worst→best, so the chart's ordinal x (sort:null = data order) is sorted
            # by loss while still LABELLED by date. `rank` kept for reference.
            stacked = []
            for rk, day in enumerate(order):
                for member, a in members:
                    if a is not None and day < len(a):
                        stacked.append({"date": dates[day], breakout: member,
                                        "pnl": float(a[day]), "rank": rk})
            datasets["dist_stacked"] = stacked
        return {"set": set, "date": date, "n": n,
                "points": points, "worst": worst, "var99": var99,
                "mean": float(r["Scenario mean PnL"]), "datasets": datasets}
    return await run_in_threadpool(run)


# ============================================================================ desk limits (RAG)
# Compare the cube's portfolio numbers to a desk limit set (limits.json) and return a red/amber/green
# status per limit. Manager-level VaR/ES/HHI come from the cube (scenario-dependent -> one ScenarioSet);
# concentration (single-name / sector weight) comes from the positions overlay as-of the date.

def _load_limits() -> dict:
    """Desk limits from repo-root limits.json. Missing/broken file -> {} (the endpoint then reports
    'not configured' rather than erroring). Reloaded on every call so edits take effect live."""
    p = pathlib.Path(__file__).resolve().parent.parent / "limits.json"
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


_RAG_RANK = {"green": 0, "unknown": 1, "amber": 2, "breach": 3}


def _rag(value, warn, limit):
    """Traffic-light for an UPPER-bound limit (+ headroom = limit - value, negative once breached)."""
    if value is None or limit is None:
        return "unknown", None
    head = limit - value
    if value >= limit:
        return "breach", head
    if warn is not None and value >= warn:
        return "amber", head
    return "green", head


def _latest_date() -> str:
    """Latest cube date as ISO — read off the specific_var frame (the COB calendar)."""
    return pd.Timestamp(S["frames"]["specific_var"]["Date"].max()).date().isoformat()


def _limits_result(date: str, scen: str, manager: str) -> dict:
    """RAG status of every configured limit at (date, scenario set, manager). Synchronous — call
    via run_in_threadpool. Returns checks + the worst-of overall status + the breach list."""
    cfg = _load_limits()
    cube = S["cube"]; l, m = cube.levels, cube.measures
    checks: list[dict] = []
    have_manager = "Manager" in {n for _, n in cube.hierarchies}
    base = (l["Date"] == _date(date)) & ((l["Manager"] == manager) if have_manager else (l["Date"] == _date(date)))

    # portfolio-level scenario measures (VaR/ES/Top-5 need a single ScenarioSet; Top-5 risk share
    # is a cube measure since 2026-07-04 — tt.rank over the flat PositionRank hierarchy — so the
    # generic query below serves it and it is SET-DEPENDENT like the old Risk HHI was).
    bspec = dict(cfg.get("manager", {}))
    if bspec:
        df = cube.query(*[m[x] for x in bspec], filter=base & (l["ScenarioSet"] == scen))
        row = df.iloc[0] if len(df) else None
        for name, spec in bspec.items():
            v = row[name] if row is not None else None
            val = None if (v is None or pd.isna(v)) else _clean(v)
            status, head = _rag(val, spec.get("warn"), spec.get("limit"))
            checks.append({"name": name, "scope": "manager", "value": val, "warn": spec.get("warn"),
                           "limit": spec.get("limit"), "status": status, "headroom": head, "detail": None})

    # concentration from the positions overlay, as-of the latest filing on/before `date`
    conc = cfg.get("concentration", {})
    if conc:
        pos = S["frames"]["positions"]
        asof = pos[(pos["Manager"] == manager) & (pos["Date"] <= pd.Timestamp(date))]
        bp = asof[asof["Date"] == asof["Date"].max()][["Position", "Weight"]] if len(asof) else asof
        if "single_name_weight" in conc and len(bp):
            spec = conc["single_name_weight"]
            i = bp["Weight"].idxmax(); w = float(bp.loc[i, "Weight"]); nm = bp.loc[i, "Position"]
            status, head = _rag(w, spec.get("warn"), spec.get("limit"))
            checks.append({"name": "single-name weight", "scope": "concentration", "value": w,
                           "warn": spec.get("warn"), "limit": spec.get("limit"), "status": status,
                           "headroom": head, "detail": f"{_ticker_map().get(nm, nm)} {w:.1%}"})
        if "sector_weight" in conc and len(bp):
            spec = conc["sector_weight"]
            sec = S["frames"]["securities"][["Position", "Sector"]]
            g = bp.merge(sec, on="Position", how="left").groupby("Sector")["Weight"].sum()
            sname = str(g.idxmax()); sw = float(g.max())
            status, head = _rag(sw, spec.get("warn"), spec.get("limit"))
            checks.append({"name": "sector weight", "scope": "concentration", "value": sw,
                           "warn": spec.get("warn"), "limit": spec.get("limit"), "status": status,
                           "headroom": head, "detail": f"{sname} {sw:.1%}"})

    overall = max((c["status"] for c in checks), key=lambda s: _RAG_RANK[s], default="none")
    # Disclosure (2026-07-30, multi-manager Phase 3): limits.json is ONE flat threshold set,
    # tuned for the manager named in its own "calibrated_for" field (default "Soros" if the field
    # is ever absent -- pre-Phase-3 limits.json had no such field, and the thresholds WERE tuned
    # for Soros regardless). Additive fields only, so existing UI/API consumers reading
    # date/set/manager/status/checks/breaches see no shape change.
    calibrated_for = cfg.get("calibrated_for", "Soros")
    cross_manager = manager != calibrated_for
    return {"date": date, "set": scen, "manager": manager, "status": overall, "configured": bool(checks),
            "checks": checks, "breaches": [c for c in checks if c["status"] == "breach"],
            "calibrated_for": calibrated_for, "cross_manager_thresholds": cross_manager,
            "calibration_note": (
                f"These thresholds were calibrated for the {calibrated_for!r} manager, not "
                f"{manager!r} — the RAG verdict above is being computed against another manager's "
                "limits and has not been separately tuned for this manager's scale/strategy."
                if cross_manager else None)}


@app.get("/limits")
async def limits(date: str | None = None, set: str | None = None,
                 manager: str | None = None, book: str | None = None):
    """RAG status of the desk limits (limits.json) for one manager. Defaults: latest date, the
    config's scenario_set. `set` overrides the scenario set the VaR/ES/HHI limits are read against."""
    manager = _coalesce_manager(manager, book)
    scen = set or _load_limits().get("scenario_set", "HistFull")
    _reject_pit(scen)
    def run():
        return _limits_result(date or _latest_date(), scen, manager)
    return await run_in_threadpool(run)


# ============================================================================ data quality (trust)
# Run barra_dq_checks against the cube's LIVE in-memory frames (not a disk re-read) and add the
# known-stub counts + per-frame latest date, so the desk can see whether to trust the numbers.

def _dq_checks() -> list[dict]:
    """barra_dq_checks.run on the live frames, MEMOIZED once per process (2026-08-15, api_bench):
    the checks are a pure function of the in-memory frames (plus the regression_stats side
    artifact read inside run()) and cost 7-14 s per call on the 124-manager frames -- the Overview's
    RAG strip paid that on every load. Frames are loaded once and never change in-process; keyed
    on their identity. Prewarmed at start-up by _prewarm."""
    ck = ("_dq_memo", id(S["frames"]))
    if ck not in S:
        S[ck] = barra_dq_checks.run(S["frames"])
    return S[ck]


@app.get("/dq")
async def dq():
    """Data-quality report on the frames the cube is actually serving: PASS/WARN/FAIL checks, a
    worst-of status, the known stubs (Unknown sector, Country='US'), and each frame's latest date."""
    def run():
        checks = _dq_checks()                             # structured [{level,name,detail}]
        summary = {k: sum(1 for c in checks if c["level"] == k) for k in ("PASS", "WARN", "FAIL")}
        status = "fail" if summary["FAIL"] else ("warn" if summary["WARN"] else "pass")
        fr = S["frames"]
        latest = {n: (pd.Timestamp(fr[n]["Date"].max()).date().isoformat() if "Date" in fr[n] else None)
                  for n in ("exposures", "positions", "factor_returns", "specific_var")}
        sec = fr["securities"]
        stubs = {"n_securities": int(len(sec)),
                 "sector_unknown": int((sec["Sector"] == "Unknown").sum()),
                 "country_stub_US": int((sec["Country"] == "US").sum())}
        return {"status": status, "summary": summary, "checks": checks,
                "latest_date": latest, "stubs": stubs}
    return await run_in_threadpool(run)


# ============================================================================ single-manager artifact guard
# Multi-manager Phase 3 (2026-07-30). Several precomputed artifacts (universe_membership/funnel/span/
# drift.parquet, pnl_attribution.parquet) were built for ONE manager and carry no Manager column at
# all — barra_universe_membership.py hardcodes SOROS_CIK and never reads positions.parquet; barra_
# universe_funnel.py/_span.py/_drift.py and barra_pnl_attribution.py's default `run()` call all read
# (or were called against) whatever manager(s) happened to be in positions.parquet at build time,
# unfiltered. Serving any of them under a DIFFERENT manager's label would silently show that
# manager Soros's (or whichever manager's) numbers — worse than an error. This guard makes that
# impossible: every endpoint backed by one of these artifacts calls it and returns a clean status
# payload (HTTP 200, never a 500) instead of proceeding when the requested manager isn't verifiably
# the one the artifact covers.

def _artifact_manager(kind: str) -> tuple[str | None, str]:
    """(manager, basis) — the single Manager the named single-manager artifact was built against,
    and how that was determined. `kind` is "membership" (barra_universe_membership.py) or one of
    "funnel"/"span"/"drift"/"pnl_attribution" (all read positions.parquet with no, or only a
    default-value, Manager filter).

    membership: barra_universe_membership.py hardcodes SOROS_CIK and never reads positions.parquet
    at all, so its coverage is fixed and independent of whatever managers are in the LIVE frames —
    resolved via barra_build_frames.MANAGERS (the CIK->book table) rather than a bare "Soros"
    string literal, so a future rename of that manager's label can't silently desync the two.

    funnel/span/drift/pnl_attribution: none of these precomputes persist a manager marker on their
    artifact. The best signal available at REQUEST time is the live positions frame: if it holds
    exactly one Manager, that is (barring a stale artifact — see the WEAKNESS note below) what the
    artifact was built against. >1 manager live -> we cannot attribute a manager-oblivious artifact
    to any one of several, so `manager` comes back None ("can't verify").

    WEAKNESS (disclosed, not fixed here): this infers from TODAY's live data, not a build-time
    stamp on the artifact file. If the artifact on disk is stale relative to the live frames (e.g.
    built while Soros was the sole manager, then the frames were swapped to a different
    single-manager set without rerunning the precompute), this would wrongly report the NEW
    manager as a match. The seven/eight-frame contract has no artifact<->frame version linkage to
    catch that; a real gap for whoever builds the per-manager artifact story out further, flagged
    rather than papered over.
    """
    if kind == "membership":
        for m in _bf.MANAGERS:
            ciks = m["cik"] if isinstance(m["cik"], tuple) else (m["cik"],)
            if _um.SOROS_CIK in ciks:
                return m["book"], "SOROS_CIK hardcoded in barra_universe_membership.py, resolved via barra_build_frames.MANAGERS"
        return None, "SOROS_CIK not found in barra_build_frames.MANAGERS (unreachable in a healthy config)"
    pos = S["frames"].get("positions")
    if pos is None or pos.empty or "Manager" not in pos.columns:
        return None, "no positions frame loaded"
    managers = pos["Manager"].unique()
    if len(managers) == 1:
        return str(managers[0]), "inferred from the live positions frame (exactly one Manager present)"
    # Several managers live. Inferring from the frames is useless here, but the precomputes are NOT
    # manager-oblivious any more: funnel/span/drift/pnl_attribution each take `run(..., manager=...)`
    # with a default, and that default IS the contract for what an unattended rerun produced.
    # Read it off the signature rather than hardcoding "Soros", so renaming the default in one of
    # those scripts cannot silently desync the guard from the artifact it is describing.
    # Without this the guard fired for EVERY manager once the multi-manager build landed, including
    # the one the artifacts genuinely cover -- blocking Soros from its own Universe/Drift/
    # Attribution lenses.
    mod = {"funnel": _uf, "span": _us, "drift": _ud, "pnl_attribution": _pnl}.get(kind)
    if mod is not None:
        try:
            dflt = inspect.signature(mod.run).parameters["manager"].default
        except (AttributeError, KeyError, ValueError):
            dflt = inspect.Parameter.empty
        if isinstance(dflt, str) and dflt:
            return dflt, (f"{len(managers)} managers live; taken from {mod.__name__}.run()'s "
                          f"manager= default ({dflt!r}), which is what an unattended rerun of "
                          "that precompute built")
    return None, (f"live positions frame has {len(managers)} distinct Managers and {kind}'s "
                  "precompute exposes no manager= default to resolve against, so its artifact "
                  "cannot be attributed to any single manager")


def _manager_guard(kind: str, requested_manager: str) -> dict | None:
    """None when `requested_manager` is verifiably the manager the `kind` artifact covers (proceed
    normally — the covered-manager case is BYTE-IDENTICAL to pre-Phase-3 behaviour, since today's
    single-manager data always resolves to 'Soros' and every endpoint still defaults
    manager='Soros'). Otherwise a structured mismatch payload, HTTP 200, mirroring the repo's
    existing `/drawdown` status idiom (never a 500) so the UI can render it cleanly."""
    covered, basis = _artifact_manager(kind)
    if covered is not None and covered == requested_manager:
        return None
    if covered is not None:
        reason = (f"the {kind} artifact was computed for the {covered!r} manager, not "
                  f"{requested_manager!r} — serving it under another manager's label would be "
                  "silently wrong data, not just stale data")
    else:
        reason = (f"cannot verify which manager the {kind} artifact covers ({basis}) — refusing "
                  f"to serve it as {requested_manager!r} rather than risk mislabeling another "
                  "manager's data")
    return {"status": "manager_mismatch", "kind": kind, "requested_manager": requested_manager,
            "artifact_manager": covered, "basis": basis, "reason": reason}


def _resolve_artifact(mod, kind: str, manager: str):
    """(path, mismatch) — which artifact file to serve for `manager` (manager-aware precomputes,
    2026-08-14). Each precompute module exposes artifact_path(manager): the DEFAULT manager maps
    to the legacy unsuffixed file, any other manager to <stem>.<Manager>.parquet. A manager's own
    suffixed file wins outright when present — it was built FOR that manager, no inference needed.
    Otherwise fall back to the legacy file gated by the Phase-3 `_manager_guard` (unchanged
    single-manager behaviour, including all its can't-verify cases). Exactly one of (path,
    mismatch) is non-None."""
    p = mod.artifact_path(manager)
    if p != mod.ARTIFACT and p.exists():
        return p, None
    mism = _manager_guard(kind, manager)
    if mism is not None:
        return None, mism
    return mod.ARTIFACT, None


# ============================================================================ universe membership
# Bitemporal index-membership diagnostic (Phase 1; docs/universe-diagnostics-plan.md). Serves the
# precomputed artifact written by barra_universe_membership.py — for each 13F filing, the portfolio's
# weight split across {S&P 500 PIT, S&P 400/600 current, Outside S&P 1500, Unclassified}. No cube
# dependency and no network at request time (the artifact is built offline like the frames).

@app.get("/universe")
async def universe(date: str | None = Query(None, description="filing report_date; default latest"),
                   manager: str | None = Query(None, description="the artifact is single-manager — "
                                     "see /dq-style manager_mismatch status if this isn't the "
                                     "covered manager"),
                   book: str | None = Query(None)):
    """Index-membership of the Soros portfolio by filing: a weight-by-bucket time series, the
    latest (or `date`) filing's split + the 'outside S&P 1500' headline, and the names in the
    Outside/Unclassified buckets. Reads data/universe_membership.parquet (run
    barra_universe_membership.py to (re)build)."""
    manager = _coalesce_manager(manager, book)
    def run():
        art, mism = _resolve_artifact(_um, "membership", manager)
        if mism is not None:
            return mism
        if not art.exists():
            raise HTTPException(503, "universe_membership.parquet not built — "
                                     "run barra_universe_membership.py")
        df = pd.read_parquet(art)
        df["report_date"] = pd.to_datetime(df["report_date"])
        # weight-by-bucket time series, one record per filing (missing buckets -> 0.0)
        series = _um.aggregate(df)
        recs = []
        for rd, g in series.groupby("report_date"):
            wmap = dict(zip(g["bucket"], g["weight"]))
            row = {"report_date": str(rd.date())}
            row.update({b: float(wmap.get(b, 0.0)) for b in _um.BUCKETS})
            row["n_names"] = int(g["n_names"].sum())
            recs.append(row)
        recs.sort(key=lambda r: r["report_date"])

        sel = date or (recs[-1]["report_date"] if recs else None)
        latest = df[df["report_date"] == pd.Timestamp(sel)] if sel else df.iloc[0:0]
        split = {b: float(latest.loc[latest["bucket"] == b, "weight"].sum()) for b in _um.BUCKETS}
        detail = (latest[latest["bucket"].isin(["Outside S&P 1500", "Unclassified"])]
                  .sort_values("weight", ascending=False)
                  [["issuer", "ticker", "cusip", "weight", "bucket"]])
        return {
            "buckets": _um.BUCKETS,
            "series": recs,
            "selected_date": sel,
            "latest": {"report_date": sel, "n_names": int(len(latest)), "split": split,
                       "outside_sp1500": split.get("Outside S&P 1500", 0.0),
                       "unclassified": split.get("Unclassified", 0.0)},
            "detail": [{k: _clean(v) for k, v in row.items()}
                       for _, row in detail.iterrows()],
            "notes": _um.NOTES,
        }
    return await run_in_threadpool(run)


# ============================================================================ filtration funnel
# Phase 2 (docs/universe-diagnostics-plan.md). Serves the precomputed funnel: the PIT S&P 500 each
# month run through the documented DQ filter stack, tagged with the first stage that drops each name.
# Reads data/universe_funnel.parquet (built by barra_universe_funnel.py) — no cube, no network.

@app.get("/funnel")
async def funnel(date: str | None = Query(None, description="funnel month; default latest"),
                 manager: str | None = Query(None, description="the artifact's 'held' flag is "
                                     "single-manager"),
                 book: str | None = Query(None)):
    """Estimation-universe filtration funnel by month: a population→survivors waterfall with the drop
    count per stage, the survivor count (and how many are held), the selected month's drop list (name
    + the stage that dropped it + its metrics), and the documented thresholds. The funnel is near-flat
    by design — the S&P 500 is pre-curated, so the filters confirm a clean input rather than carve."""
    manager = _coalesce_manager(manager, book)
    def run():
        art, mism = _resolve_artifact(_uf, "funnel", manager)
        if mism is not None:
            return mism
        if not art.exists():
            raise HTTPException(503, "universe_funnel.parquet not built — run barra_universe_funnel.py")
        df = pd.read_parquet(art)
        df["month"] = pd.to_datetime(df["month"])
        cfg = _uf.load_cfg()
        recs = []
        for mth, g in df.groupby("month"):
            fc = _uf.funnel_counts(g, cfg)
            recs.append({"month": str(mth.date()), **{k: fc[k] for k in
                         ("population", "survivors", "held_survivors", "data_unavailable")},
                         **{f"drop:{s}": fc["drops"][s] for s in _uf.STAGES}})
        recs.sort(key=lambda r: r["month"])

        sel = date or (recs[-1]["month"] if recs else None)
        gm = df[df["month"] == pd.Timestamp(sel)] if sel else df.iloc[0:0]
        dropped = (gm[gm["stage_dropped"].isin(_uf.STAGES)]
                   .sort_values(["stage_dropped", "adv"], na_position="last")
                   [["issuer", "ticker", "stage_dropped", "mcap", "hist_days", "trade_freq",
                     "adv", "n_descriptors", "held"]])
        return {
            "stages": _uf.STAGES,
            "series": recs,
            "selected_date": sel,
            "latest": _uf.funnel_counts(gm, cfg),
            "dropped": [{k: _clean(v) for k, v in row.items()} for _, row in dropped.iterrows()],
            "config": cfg,
            "unavailable_stages": cfg.get("unavailable_stages", []),
            "note": ("Pre-filter population is the point-in-time S&P 500 (survivorship-free). The "
                     "funnel is near-flat by design — the S&P 500 is already committee-curated, so the "
                     "filters confirm clean data rather than carve much away. 'data unavailable' = PIT "
                     "members not in the built universe (delisted) or missing a share count; shown, "
                     "not counted as a filter drop. Free float and confirmed-M&A removal have no free "
                     "source and appear as inert, disclosed stages."),
        }
    return await run_in_threadpool(run)


# ============================================================================ span / high-confidence
# Phase 3 (docs/universe-diagnostics-plan.md). Does each holding sit inside the factor-space spanned by
# the estimation universe (Chris's VALUE/SIZE picture)? Squared Mahalanobis distance vs the funnel-
# survivor cloud, "inside" = within the cloud's 99th-pct edge. Precomputed series + per-name verdict
# read from data/universe_span.parquet; the 2D scatter is built live from the in-memory exposures frame
# so any factor pair can be picked.

@app.get("/span")
async def span(date: str | None = Query(None, description="month; default latest"),
               fx: str = Query("Size"), fy: str = Query("ResidVol"),
               manager: str | None = Query(None, description="the artifact + live scatter are "
                                     "single-manager"),
               book: str | None = Query(None)):
    """Span / high-confidence check: a per-month time series of the portfolio weight INSIDE the
    estimation universe's factor space, the selected month's per-name verdict (D², inside/outside,
    which factors push a name out), and a 2D `fx`×`fy` scatter of the estimation cloud vs the held
    portfolio — the literal version of Chris's VALUE/SIZE illustration. ~90% of the portfolio sits
    inside on average; it has drifted from ~95% pre-2021 to ~85% since."""
    manager = _coalesce_manager(manager, book)
    if fx not in _us.STYLE or fy not in _us.STYLE:
        raise HTTPException(400, f"fx/fy must be style factors: {_us.STYLE}")

    def run():
        art, mism = _resolve_artifact(_us, "span", manager)
        if mism is not None:
            return mism
        if not art.exists():
            raise HTTPException(503, "universe_span.parquet not built — run barra_universe_span.py")
        df = pd.read_parquet(art)
        df["month"] = pd.to_datetime(df["month"])
        series = []
        for mth, g in df.groupby("month"):
            series.append({"month": str(mth.date()),
                           "inside_wt": _us.inside_share(g["weight"].values, g["inside"].values),
                           "n_held": int(len(g)), "n_inside": int(g["inside"].sum())})
        series.sort(key=lambda r: r["month"])

        sel = date or (series[-1]["month"] if series else None)
        gm = df[df["month"] == pd.Timestamp(sel)] if sel else df.iloc[0:0]
        detail = (gm.sort_values("d2", ascending=False)
                  [["issuer", "ticker", "weight", "d2", "edge", "inside", "extreme"]])

        # live 2D scatter from the exposures frame (cloud = funnel survivors, held = the portfolio)
        exp = S["frames"]["exposures"]; D = pd.Timestamp(sel)
        w = (exp[(exp["Date"] == D) & (exp["Factor"].isin([fx, fy]))]
             .pivot_table(index="Position", columns="Factor", values="Loading"))
        cloud_pos = held_pos = set()
        if _uf.ARTIFACT.exists():
            fn = pd.read_parquet(_uf.ARTIFACT, columns=["month", "position", "survived"])
            fn = fn[(pd.to_datetime(fn["month"]) == D) & (fn["survived"] == True)]  # noqa: E712
            cloud_pos = set(fn["position"].dropna()) & set(w.index)
        held_pos = set(gm["position"]) & set(w.index)
        inside_map = dict(zip(gm["position"], gm["inside"]))
        iss = dict(zip(S["frames"]["securities"]["Position"], S["frames"]["securities"]["Issuer"]))

        def pt(p):
            return {"x": _clean(w.loc[p, fx]) if fx in w else None,
                    "y": _clean(w.loc[p, fy]) if fy in w else None}
        # sorted(): both position sets are Python `set`s, whose iteration order varies with the
        # process's string hash seed — the scatter payload was byte-nondeterministic across
        # restarts for no reason (it is why the round-3 identity gate read 66/68). Sorting costs
        # nothing at these sizes and makes the response reproducible.
        cloud = [pt(p) for p in sorted(cloud_pos)
                 if not (np.isnan(w.loc[p, fx]) or np.isnan(w.loc[p, fy]))]
        # NB not `manager`: that name is the endpoint's parameter, closed over by _manager_guard
        # above. Rebinding it here made Python treat it as local for the whole closure, so the
        # guard call raised UnboundLocalError and /span 500'd for every request.
        held_pts = [{**pt(p), "inside": bool(inside_map.get(p, False)), "issuer": iss.get(p, "")}
                    for p in sorted(held_pos)
                    if not (np.isnan(w.loc[p, fx]) or np.isnan(w.loc[p, fy]))]

        lat = {"month": sel, "n_held": int(len(gm)), "n_inside": int(gm["inside"].sum()),
               "inside_wt": _us.inside_share(gm["weight"].values, gm["inside"].values)} if len(gm) else {}
        return {
            "factors": _us.STYLE, "series": series, "selected_date": sel, "latest": lat,
            "detail": [{k: _clean(v) for k, v in row.items()} for _, row in detail.iterrows()],
            "scatter": {"fx": fx, "fy": fy, "cloud": cloud, "held": held_pts},
            "note": ("'Inside' = squared Mahalanobis distance within the estimation cloud's own 99th "
                     "percentile — the region the estimation universe populated, where model exposures "
                     "are well-supported. Outside = extrapolation. Cloud = funnel survivors; loadings "
                     "are z-scored/winsorized, so the space is in standardized-exposure terms."),
        }
    return await run_in_threadpool(run)


# ============================================================================ style-drift attribution
# Phase 4 (docs/universe-diagnostics-plan.md). The portfolio's net factor exposure x_k(t) over time, the
# pre/post-`split` drift per factor, and an attribution of each factor's drift into entered / exited /
# reweighted / loading_drift — making Chris's intentional-vs-not question empirical: drift dominated by
# NEW names rotating in leans intentional (→ benchmark); drift from HELD names' loadings drifting leans
# unintentional (→ hedge). Series read from data/universe_drift.parquet; attribution computed live.

@app.get("/drift")
async def drift(split: str = Query("2021-01-01", description="pre/post boundary for the drift"),
                manager: str | None = Query(None, description="both the artifact and the live "
                                  "attribution below (portfolio_at has no Manager filter) are "
                                  "single-manager"),
                book: str | None = Query(None)):
    """Style-drift attribution: per-factor net-exposure trend, the pre/post-`split` drift ranked by
    magnitude, and a decomposition of each factor's drift into entered / exited / reweighted /
    loading_drift — with a per-factor 'lean' (rotation → intentional → benchmark; re-pricing →
    unintentional → hedge). The final verdict needs desk knowledge; this lays out the evidence."""
    manager = _coalesce_manager(manager, book)
    def run():
        art, mism = _resolve_artifact(_ud, "drift", manager)
        if mism is not None:
            return mism
        if not art.exists():
            raise HTTPException(503, "universe_drift.parquet not built — run barra_universe_drift.py")
        df = pd.read_parquet(art); df["month"] = pd.to_datetime(df["month"])
        series = df.pivot_table(index="month", columns="factor", values="net_exposure").sort_index()
        sp = pd.Timestamp(split)

        exp, pos = S["frames"]["exposures"], S["frames"]["positions"]
        # The live attribution below (portfolio_at → decompose) has no Manager concept of its own; with
        # the multi-manager frames the requested manager must be filtered HERE or x_k sums across
        # every manager at once (the same bug barra_universe_drift.run fixed for the artifact side).
        if "Manager" in pos.columns:
            pos = pos[pos["Manager"] == manager]
            if pos.empty:
                return {"status": "manager_mismatch", "kind": "drift", "requested_manager": manager,
                        "artifact_manager": None, "basis": "no positions rows for this manager",
                        "reason": f"no positions for manager {manager!r} in the live frames"}
        months = pd.DatetimeIndex(sorted(pd.to_datetime(pos["Date"].unique())))
        pre = months[months < sp]
        t0 = pre[-1] if len(pre) else months[0]
        t1 = months[-1]
        w0, l0 = _ud.portfolio_at(exp, pos, t0)
        w1, l1 = _ud.portfolio_at(exp, pos, t1)
        attr = _ud.decompose(w0, l0, w1, l1)
        x0, x1 = _ud.portfolio_exposure(w0, l0), _ud.portfolio_exposure(w1, l1)   # exposure at t0 / t1

        srecs = [{"month": str(m.date()),
                  **{f: _clean(series.loc[m, f]) for f in series.columns}} for m in series.index]
        # rank by the t0→t1 drift the attribution decomposes; delta = sum of the four sources exactly.
        sumrecs = []
        for f in sorted(_ud.STYLE, key=lambda k: abs(attr[k]["delta"]), reverse=True):
            a = attr[f]
            cands = [("rotation (new / dropped names) — leans intentional → benchmark",
                      abs(a["entered"]) + abs(a["exited"])),
                     ("re-pricing (held names' loadings drifted) — leans unintentional → hedge",
                      abs(a["loading_drift"])),
                     ("resizing (active weight changes on held names)", abs(a["reweighted"]))]
            lean = max(cands, key=lambda kv: kv[1])[0]
            sumrecs.append({"factor": f, "early": _clean(x0[f]), "late": _clean(x1[f]),
                            "delta": _clean(a["delta"]),
                            **{f"src_{k}": _clean(a[k]) for k in _ud.SOURCES}, "lean": lean})
        return {
            "factors": _ud.STYLE, "sources": _ud.SOURCES, "split": str(sp.date()),
            "t0": str(t0.date()), "t1": str(t1.date()),
            "series": srecs, "summary": sumrecs,
            "note": ("Net portfolio exposure x_k = Σ w·L per factor. Drift Δx_k between the pre-split portfolio "
                     "and the latest is split into entered / exited (rotation) / reweighted (resizing) "
                     "/ loading_drift (held names' own loadings moving). Rotation-dominated drift leans "
                     "intentional (mandate shifted → update the benchmark); loading-drift-dominated "
                     "leans unintentional (re-pricing → update the hedge). The verdict is the desk's — "
                     "this is the evidence, per Chris (2026-06-23)."),
        }
    return await run_in_threadpool(run)


# ============================================================================ VaR backtest
# Constant-portfolio backtest: take the current portfolio's daily factor-P&L series (the HistFull
# scenario vector + its date dual), roll a window to estimate VaR each day, and count exceptions
# where the realized day beat VaR. Tests the VaR METHODOLOGY against history (the 13F portfolio has
# no live daily P&L track record). Kupiec POF test + Basel traffic-light from the binomial CDF.
# Pure stats split out (no cube) so they're unit-testable.

def _kupiec_lr(n_exc: int, n_obs: int, p: float) -> float:
    """Kupiec proportion-of-failures likelihood ratio (chi-square, 1 df; reject model > 3.841 @95%).
    p = expected failure rate (1 - confidence). Guards the n_exc in {0, n_obs} edges."""
    if n_obs == 0:
        return 0.0
    import math
    pi = n_exc / n_obs

    def ll(rate: float) -> float:                       # log-likelihood of n_exc failures @ rate
        a = (n_obs - n_exc) * math.log(1 - rate) if rate < 1 else (0.0 if n_exc == n_obs else -math.inf)
        b = n_exc * math.log(rate) if rate > 0 else (0.0 if n_exc == 0 else -math.inf)
        return a + b
    return -2.0 * (ll(p) - ll(pi))


def _basel_zone(n_exc: int, n_obs: int, p: float) -> tuple[str, float]:
    """Basel traffic-light via the binomial CDF P(X <= n_exc) at the expected rate p over n_obs:
    green < 95%, amber < 99.99%, else red. Generalizes the 250-day/99% zones to any window."""
    if n_obs == 0:
        return "unknown", None
    import math
    cdf = sum(math.comb(n_obs, k) * (p ** k) * ((1 - p) ** (n_obs - k)) for k in range(n_exc + 1))
    zone = "green" if cdf < 0.95 else ("amber" if cdf < 0.9999 else "red")
    return zone, cdf


def _var_thresholds(pnl: np.ndarray, window: int, alpha: float, method: str, lam: float) -> np.ndarray:
    """Per-day VaR loss threshold (a NEGATIVE number; a day is an exception when pnl[t] < thr[t]),
    computed from info BEFORE day t so the test is out-of-sample. Two methods:
      equal — rolling-window historical simulation: the (1-alpha) empirical quantile of the prior
              `window` days, equal-weighted (the baseline).
      ewma  — RiskMetrics parametric: variance recursion sigma2_t = lam*sigma2_{t-1} + (1-lam)*r_{t-1}^2
              seeded on the first `window` days, VaR_t = z_alpha * sigma_t. Reacts within days, but
              assumes a NORMAL tail — understates fat tails.
      fhs   — filtered historical simulation: EWMA vol for reactivity, but the (1-alpha) quantile is
              taken on the standardized residuals r_i/sigma_i (empirical, fat-tailed) and rescaled by
              today's sigma. Combines reactivity with the real tail shape."""
    n = len(pnl)
    thr = np.full(n, np.nan)
    p = 1.0 - alpha
    if method in ("ewma", "fhs"):
        sig = np.empty(n)
        s2 = float(np.var(pnl[:window])) if window <= n else float(np.var(pnl))
        sig[0] = s2 ** 0.5
        for t in range(1, n):
            s2 = lam * s2 + (1.0 - lam) * pnl[t - 1] ** 2       # predictive: uses up to t-1
            sig[t] = s2 ** 0.5
        if method == "ewma":
            import statistics
            z = statistics.NormalDist().inv_cdf(alpha)
            for t in range(window, n):
                thr[t] = -z * sig[t]
        else:                                                  # fhs
            resid = pnl / np.where(sig > 0, sig, np.nan)       # standardized shocks
            for t in range(window, n):
                rw = resid[t - window:t]
                rw = rw[~np.isnan(rw)]
                if len(rw):
                    thr[t] = sig[t] * np.quantile(rw, p)
    else:                                                       # equal-weight historical simulation
        for t in range(window, n):
            thr[t] = np.quantile(pnl[t - window:t], p)
    return thr


def _backtest_result(date: str, scen: str, manager: str, alpha: float, window: int,
                     method: str = "equal", lam: float = 0.94) -> dict:
    """Rolling-window VaR backtest of the portfolio's daily factor-P&L series for one scenario set."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    have_manager = "Manager" in {n for _, n in cube.hierarchies}
    flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == scen)
    if have_manager:
        flt = flt & (l["Manager"] == manager)
    pv = cube.query(m["Scenario PnL vector"], filter=flt)
    dv = cube.query(m["Scenario dates (epoch)"], filter=flt)
    base = {"set": scen, "manager": manager, "date": date, "alpha": alpha, "window": window,
            "method": method, "lam": (lam if method in ("ewma", "fhs") else None)}
    if not len(pv) or pv.iloc[0, 0] is None or not len(dv):
        return {**base, "tested": 0, "status": "insufficient", "exceptions": 0}
    pnl = np.asarray(pv.iloc[0, 0], dtype=float)
    days = np.asarray(dv.iloc[0, 0], dtype=int)
    n = min(len(pnl), len(days))
    order = np.argsort(days[:n])                          # ensure ascending by date
    pnl, days = pnl[:n][order], days[:n][order]
    if n <= window:
        return {**base, "tested": 0, "status": "insufficient", "n": int(n), "exceptions": 0}

    p = 1.0 - alpha
    thr = _var_thresholds(pnl, window, alpha, method, lam)
    exc_idx = [t for t in range(window, n) if pnl[t] < thr[t]]   # tested on [window, n) for both
    T = n - window
    N = len(exc_idx)
    lr = _kupiec_lr(N, T, p)
    zone, cdf = _basel_zone(N, T, p)
    exc_dates = [(_EPOCH + pd.Timedelta(days=int(days[i]))).date().isoformat() for i in exc_idx]
    return {**base, "status": "ok", "tested": T, "exceptions": N, "expected": round(p * T, 2),
            "rate": (N / T if T else None), "kupiec_LR": round(lr, 3), "kupiec_crit": 3.841,
            "kupiec_reject": lr > 3.841, "basel_zone": zone, "binom_cdf": cdf,
            "n_exception_dates": N, "exception_dates": exc_dates[:60]}


@app.get("/backtest")
async def backtest(set: str = "HistFull", date: str | None = None,
                   manager: str | None = None, book: str | None = None,
                   alpha: float = 0.99, window: int = 250, method: str = "fhs", lam: float = 0.94):
    """Rolling-window VaR backtest (Kupiec POF + Basel traffic-light) on a portfolio's daily
    factor-P&L series. method = fhs (default — filtered historical simulation, EWMA-vol-scaled
    empirical tail) | equal (plain historical sim) | ewma (RiskMetrics parametric-normal). `lam`
    is the EWMA decay. The fhs/lam=0.94 default was chosen by a sweep: at 99% it gives ~1.0%
    breaches (Kupiec-green), where equal HS under-covers (amber) and parametric ewma over-breaches
    on the fat tail (red). Defaults: HistFull (only set with a long daily history), latest date,
    99% / 250-day."""
    manager = _coalesce_manager(manager, book)
    _reject_pit(set)
    if not (0.5 < alpha < 1):
        raise HTTPException(400, "alpha must be in (0.5, 1)")
    if window < 30:
        raise HTTPException(400, "window must be >= 30")
    if method not in ("equal", "ewma", "fhs"):
        raise HTTPException(400, "method must be 'equal', 'ewma', or 'fhs'")
    if not (0.0 < lam < 1.0):
        raise HTTPException(400, "lam (EWMA decay) must be in (0, 1)")
    def run():
        return _backtest_result(date or _latest_date(), set, manager, alpha, window, method, lam)
    return await run_in_threadpool(run)


# ============================================================================ drawdown (path lens)
def _max_drawdown(pnl, days) -> dict | None:
    """Constant-portfolio max drawdown of the portfolio's simulated daily P&L PATH (geometric
    equity curve), with peak/trough dates, recovery, and the longest underwater run. pnl/days are
    the cube's `Scenario PnL vector` + its `Scenario dates` dual; date-ordered here. Pure stats (no
    cube) so it unit-tests directly. Drawdown is path-dependent — the lens VaR/ES can't see."""
    pnl = np.asarray(pnl, float); days = np.asarray(days, int)
    n = min(len(pnl), len(days))
    if n == 0:
        return None
    order = np.argsort(days[:n]); pnl, days = pnl[:n][order], days[:n][order]
    eq = np.cumprod(1.0 + pnl)                       # held portfolio compounded over the factor path
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0                              # <= 0 everywhere
    i_tr = int(np.argmin(dd)); max_dd = float(dd[i_tr])
    i_pk = int(np.argmax(eq[:i_tr + 1])) if i_tr > 0 else 0
    rec = np.where(eq[i_tr:] >= peak[i_tr])[0]        # first obs at/above the pre-trough peak
    i_rec = int(i_tr + rec[0]) if len(rec) else None
    longest = cur = 0                                 # longest consecutive underwater run (obs)
    for f in (eq < peak - 1e-15):
        cur = cur + 1 if f else 0
        longest = max(longest, cur)
    iso = lambda i: (_EPOCH + pd.Timedelta(days=int(days[i]))).date().isoformat()
    return {
        "n": int(n), "max_drawdown": max_dd,
        "peak_date": iso(i_pk), "trough_date": iso(i_tr),
        "drawdown_obs": int(i_tr - i_pk),             # trading-day observations peak->trough
        "recovered": i_rec is not None,
        "recovery_date": iso(i_rec) if i_rec is not None else None,
        "longest_underwater_obs": int(longest),
        "path": [{"date": iso(i), "equity": float(eq[i]), "drawdown": float(dd[i])}
                 for i in range(n)],
    }


def _drawdown_result(date: str, scen: str, manager: str) -> dict:
    """Pull the portfolio P&L vector for one (Date, ScenarioSet) and reduce to the drawdown
    summary + path. Same vector source as /backtest and /scenario_pnl."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    have_manager = "Manager" in {n for _, n in cube.hierarchies}
    flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == scen)
    if have_manager:
        flt = flt & (l["Manager"] == manager)
    pv = cube.query(m["Scenario PnL vector"], filter=flt)
    dv = cube.query(m["Scenario dates (epoch)"], filter=flt)
    base = {"set": scen, "manager": manager, "date": date}
    if not len(pv) or pv.iloc[0, 0] is None or not len(dv):
        return {**base, "status": "insufficient", "n": 0}
    dd = _max_drawdown(pv.iloc[0, 0], dv.iloc[0, 0])
    if dd is None or dd["n"] < 2:                      # length-1 (hypo) sets have no path
        return {**base, "status": "insufficient", "n": (dd["n"] if dd else 0)}
    return {**base, "status": "ok", **dd}


@app.get("/drawdown")
async def drawdown(set: str = "HistFull", date: str | None = None,
                   manager: str | None = None, book: str | None = None):
    """Constant-portfolio max drawdown: cumulate the current portfolio's daily factor-P&L over the
    scenario set's path (geometric equity curve) and take peak-to-trough. Like /backtest this is a
    what-if on the *held* portfolio over history, not a live track record. Drawdown is a path lens
    that VaR/ES miss. Most meaningful on HistFull (long path); event sets give the drawdown over
    that window; hypo (length-1) sets are degenerate -> status insufficient."""
    manager = _coalesce_manager(manager, book)
    _reject_pit(set)
    def run():
        return _drawdown_result(date or _latest_date(), set, manager)
    return await run_in_threadpool(run)


# ============================================================================ stress (custom / reverse)
# A hypothetical shock's portfolio P&L is linear: dPnL = Σ_k x_k * (sigma_k * vol_k), where x_k is
# the portfolio net exposure to factor k and vol_k is that factor's return vol (same convention
# build_scenarios uses for the baked-in Hypo sets). So custom (user-defined sigmas) and reverse
# (solve the sigma that breaches a loss) stress are computed in the API from exposures + vols — no
# cube rebuild.

def _factor_vols() -> dict:
    """Per-factor return vol — served from the cube's `Factor return vol` measure (std of the
    HistFull ShockVec; identical estimator to the old pandas wide.std(), one source of truth
    with the grid). Cached on S. Call from a threadpool context (cube query)."""
    if "factor_vols" not in S:
        cube = S["cube"]; l, m = cube.levels, cube.measures
        df = (cube.query(m["Factor return vol"], levels=[l["Factor"]],
                         filter=l["ScenarioSet"] == "HistFull").reset_index())
        S["factor_vols"] = {str(r["Factor"]): float(r["Factor return vol"])
                            for _, r in df.iterrows()}
    return S["factor_vols"]


def _factor_exposures(date: str, manager: str) -> dict:
    """Portfolio net factor exposure x_k by Factor at a date (cube Net exposure — scenario-independent)."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    flt = (l["Date"] == _date(date))
    if "Manager" in {n for _, n in cube.hierarchies}:
        flt = flt & (l["Manager"] == manager)
    df = cube.query(m["Net exposure"], levels=[l["Factor"]], filter=flt).reset_index()
    return {str(r["Factor"]): float(r["Net exposure"]) for _, r in df.iterrows()}


def _stress_result(shocks: dict, date: str, manager: str) -> dict:
    """Portfolio P&L under a user-defined set of per-factor sigma shocks (one-day hypothetical)."""
    vols = _factor_vols()
    x = _factor_exposures(date, manager)
    comps, total = [], 0.0
    for f, sigma in shocks.items():
        xf, vf = x.get(f, 0.0), vols.get(f, 0.0)
        shock_ret = float(sigma) * vf
        pnl = xf * shock_ret
        total += pnl
        comps.append({"factor": f, "exposure": xf, "sigma": float(sigma), "vol": vf,
                      "shock_return": shock_ret, "pnl": pnl})
    comps.sort(key=lambda c: c["pnl"])               # worst contributor first
    return {"date": date, "manager": manager, "shocks": shocks,
            "total_pnl": total, "loss": -total, "components": comps}


def _conditional_shock(F: np.ndarray, idx: list[int], s: np.ndarray) -> np.ndarray:
    """E[f | f_idx = s] under the factor covariance F: F[:, idx] @ inv(F[idx, idx]) @ s. The
    correlated stress — a shocked factor drags every co-moving factor with it instead of moving
    alone. Shocking every factor returns s itself (the naive case)."""
    sub = F[np.ix_(idx, idx)]
    return F[:, idx] @ np.linalg.solve(sub, np.asarray(s, dtype=float))


def _conditional_stress_result(shocks: dict, date: str, manager: str) -> dict:
    """Correlated version of _stress_result: condition the whole factor system on the shocked
    factors via the factor covariance, then dPnL = Σ x_k·E[f_k | shock]. The naive single-factor
    read understates a real event because the co-moving factors don't stay still."""
    L, w, _s, R = _manager_inputs(date, manager)
    factors = list(L.columns)
    missing = [f for f in shocks if f not in factors]
    if missing:
        raise HTTPException(400, f"no loadings for factor(s) at {date}: {missing}")
    F = np.cov(R, rowvar=False)
    vols = np.sqrt(np.clip(np.diag(F), 0.0, None))
    idx = [factors.index(f) for f in shocks]
    sh = np.array([float(sig) * vols[factors.index(f)] for f, sig in shocks.items()])
    f_cond = _conditional_shock(F, idx, sh)
    x = L.to_numpy().T @ w.to_numpy()
    pnl = x * f_cond
    comps = [{"factor": factors[i], "exposure": float(x[i]),
              "implied_return": float(f_cond[i]),
              "implied_sigma": (float(f_cond[i] / vols[i]) if vols[i] > 0 else None),
              "pnl": float(pnl[i]), "shocked": factors[i] in shocks}
             for i in range(len(factors))]
    comps.sort(key=lambda c: c["pnl"])
    return {"total_pnl": float(pnl.sum()), "loss": float(-pnl.sum()), "components": comps,
            "note": ("E[f | shock] = F[:,S]·F[S,S]⁻¹·s — the factor covariance propagates the "
                     "shock to every co-moving factor; the naive result holds them still.")}


class StressBody(BaseModel):
    shocks: dict[str, float]                          # {Factor: sigma}
    date: str | None = None
    manager: str | None = None
    book: str | None = None
    # correlated (conditional) mode: propagate the shock through the factor covariance and add a
    # `conditional` block — implied return per factor + the conditional portfolio P&L.
    conditional: bool = False
    # correlation-stress mode (Step 15 §4): scale factor vols by vol_mult and blend correlations
    # toward 1 by rho — adds a `correlation_stress` block (base vs stressed portfolio vol) to the
    # result.
    vol_mult: float | None = None
    rho: float | None = None


def _corr_stress_result(date: str, manager: str, vol_mult: float, rho: float) -> dict:
    """Portfolio daily vol under a vols-and-correlations shock: F' = _stressed_cov(F). The
    base↔stressed gap on the PORTFOLIO (not any single factor) is the diversification the
    portfolio leans on — where correlation risk lives. Normal-approx VaR99 = 2.326σ for scale."""
    L, w, s, R = _manager_inputs(date, manager)
    x = L.to_numpy().T @ w.to_numpy()
    F = np.cov(R, rowvar=False)
    svar = float(np.sum(w.to_numpy() ** 2 * s.to_numpy()))
    Fs = _pnl._stressed_cov(F, vol_mult, rho)
    base = float(np.sqrt(max(x @ F @ x + svar, 0.0)))
    stressed = float(np.sqrt(max(x @ Fs @ x + svar * vol_mult ** 2, 0.0)))
    return {"vol_mult": vol_mult, "rho_blend": rho,
            "base_vol_1d": base, "stressed_vol_1d": stressed,
            "base_var99_normal": _Z99 * base, "stressed_var99_normal": _Z99 * stressed}


def _corr_stress_cube(date: str, manager: str, vol_mult: float, rho: float) -> dict:
    """The correlation-stress read SERVED from the cube's `Stressed model vol` (a transient
    CorrStress parameter scenario), with the numpy _corr_stress_result as the live cross-check.
    Falls back to serving the numpy numbers on any cube failure."""
    ref = _corr_stress_result(date, manager, vol_mult, rho)
    scen = f"corr-{uuid.uuid4().hex[:12]}"
    sim = None
    try:
        cube = S["cube"]; l, mm = cube.levels, cube.measures
        sim = S["session"].tables["CorrStress"]
        sim.append((scen, float(vol_mult), float(rho)))
        flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == "HistFull")
        if "Manager" in {n for _, n in cube.hierarchies}:
            flt &= (l["Manager"] == manager)
        base = cube.query(mm["Model vol"], filter=flt)
        stressed = cube.query(mm["Stressed model vol"],
                              filter=flt & (l["CorrStress"] == scen))
        b, st = float(base.iloc[0, 0]), float(stressed.iloc[0, 0])
        return {"vol_mult": vol_mult, "rho_blend": rho,
                "base_vol_1d": b, "stressed_vol_1d": st,
                "base_var99_normal": _Z99 * b, "stressed_var99_normal": _Z99 * st,
                "source": "cube",
                "verification": {"base_abs_diff": abs(b - ref["base_vol_1d"]),
                                 "stressed_abs_diff": abs(st - ref["stressed_vol_1d"])}}
    except Exception as e:
        ref["source"] = "numpy_fallback"
        ref["verification"] = {"error": f"{e.__class__.__name__}: {e}"}
        return ref
    finally:
        if sim is not None:
            try:
                sim.drop(sim["Scenario"] == scen)
            except Exception:
                pass


@app.post("/stress")
async def stress(body: StressBody):
    """Custom one-day stress: portfolio P&L under user-defined per-factor sigma shocks (and a
    per-factor contribution breakdown). dPnL = Σ x_k·(sigma_k·vol_k) — the same math as the
    baked-in Hypo sets; vols come from the cube's `Factor return vol` measure (via _factor_vols).
    Optional vol_mult/rho add a correlation-stress read (vols up, correlations toward 1)."""
    manager = _coalesce_manager(body.manager, body.book)
    known = set(S["frames"]["factor_meta"]["Factor"].astype(str))
    bad = [f for f in body.shocks if f not in known]
    if bad:
        raise HTTPException(400, f"unknown factor(s): {bad}")
    if not body.shocks:
        raise HTTPException(400, "provide at least one factor shock")
    def run():
        d = body.date or _latest_date()
        # numpy reference — retained as the live cross-check
        ref = _stress_result(body.shocks, d, manager)
        # cube-served naive shock (the StressShock parameter simulation — one transient scenario
        # per request): per-factor components from Custom stress PnL / Net exposure /
        # Factor return vol, footing to the portfolio total. Falls back to serving the numpy
        # numbers.
        scen = f"req-{uuid.uuid4().hex[:12]}"
        sim = None
        try:
            cube = S["cube"]; l, mm = cube.levels, cube.measures
            sim = S["session"].tables["StressShock"]
            sim.append(*[(scen, f_, float(sig)) for f_, sig in body.shocks.items()])
            flt = ((l["Date"] == _date(d)) & (l["ScenarioSet"] == "HistFull")
                   & (l["StressShock"] == scen))
            if "Manager" in {n for _, n in cube.hierarchies}:
                flt &= (l["Manager"] == manager)
            dfF = (cube.query(mm["Custom stress PnL"], mm["Net exposure"], mm["Factor return vol"],
                              levels=[l["Factor"]], filter=flt).reset_index())
            byf = dfF.set_index("Factor")
            comps = []
            for f_, sig in body.shocks.items():
                r_ = byf.loc[f_]
                vol_ = float(r_["Factor return vol"])
                comps.append({"factor": f_, "exposure": float(r_["Net exposure"]),
                              "sigma": float(sig), "vol": vol_,
                              "shock_return": float(sig) * vol_,
                              "pnl": float(r_["Custom stress PnL"])})
            comps.sort(key=lambda c: c["pnl"])
            total = float(sum(c["pnl"] for c in comps))
            ref_pnl = {c["factor"]: c["pnl"] for c in ref["components"]}
            res = {"date": d, "manager": manager, "shocks": body.shocks,
                   "total_pnl": total, "loss": -total, "components": comps,
                   "source": "cube",
                   "verification": {
                       "total_abs_diff": abs(total - ref["total_pnl"]),
                       "max_component_abs_diff": max(
                           (abs(c["pnl"] - ref_pnl.get(c["factor"], 0.0)) for c in comps),
                           default=0.0)}}
        except Exception as e:
            res = dict(ref)
            res["source"] = "numpy_fallback"
            res["verification"] = {"error": f"{e.__class__.__name__}: {e}"}
        finally:
            if sim is not None:
                try:
                    sim.drop(sim["Scenario"] == scen)
                except Exception:
                    pass
        if body.conditional:
            res["conditional"] = _conditional_stress_result(body.shocks, d, manager)
        if body.vol_mult is not None or body.rho is not None:
            res["correlation_stress"] = _corr_stress_cube(
                d, manager, body.vol_mult or 1.0, body.rho or 0.0)
        return res
    return await run_in_threadpool(run)


@app.get("/reverse_stress")
async def reverse_stress(loss: float | None = None, date: str | None = None,
                         manager: str | None = None, book: str | None = None):
    """Reverse stress: for a target portfolio loss `L`, the single-factor sigma move that would
    produce it, per factor, ranked by |sigma| (smallest = the portfolio's most vulnerable factor).
    Default L = the Total VaR 99 desk limit (limits.json), else 0.05."""
    manager = _coalesce_manager(manager, book)
    L = loss if loss is not None else (_load_limits().get("manager", {})
                                       .get("Scenario VaR 99", {}).get("limit") or 0.05)
    def run():
        d = date or _latest_date()
        vols = _factor_vols(); x = _factor_exposures(d, manager)
        rows = []
        for f, vf in vols.items():
            denom = x.get(f, 0.0) * vf
            sigma = (-L / denom) if abs(denom) > 1e-12 else None
            rows.append({"factor": f, "exposure": x.get(f, 0.0), "vol": vf,
                         "sigma_to_breach": sigma, "abs_sigma": (abs(sigma) if sigma is not None else None)})
        ranked = sorted((r for r in rows if r["abs_sigma"] is not None), key=lambda r: r["abs_sigma"])
        return {"date": d, "manager": manager, "loss": L, "factors": ranked,
                "weakest": ranked[0] if ranked else None}
    return await run_in_threadpool(run)


# ============================================================================ pre-trade / what-if
# Recompute portfolio risk under a modified weight vector — the cube's risk math reproduced in numpy so a
# hypothetical trade (resize / add / drop) needs no cube rebuild. Factor P&L vector = R · (Lᵀ w), the
# diagonal specific block = Σ wᵢ²σᵢ², and HHI from the marginal-Total-VaR shares (self-consistent:
# the marginals sum to portfolio Total VaR, so shares sum to 1). "Before" ≈ the cube's reported
# figures (small quantile-interpolation differences); the value is the BEFORE→AFTER delta.

_Z99 = 2.326


def _manager_inputs(date: str, manager: str):
    """Universe loadings L (Position×Factor, incl Market), as-of weights w, specific var s, and the
    daily factor-return panel R aligned to L's factors — the pieces the risk math needs."""
    f = S["frames"]; d = pd.Timestamp(date)
    exp_d = f["exposures"][f["exposures"]["Date"] == d]
    L = exp_d.pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first").fillna(0.0)
    wide = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
    factors = [c for c in L.columns if c in wide.columns]
    L = L[factors]
    R = wide[factors].to_numpy()
    pos = f["positions"]; asof = pos[(pos["Manager"] == manager) & (pos["Date"] <= d)]
    bp = asof[asof["Date"] == asof["Date"].max()] if len(asof) else asof
    held = bp.set_index("Position")["Weight"] if len(bp) else pd.Series(dtype=float)
    w = pd.Series(0.0, index=L.index)
    w.loc[w.index.intersection(held.index)] = held.reindex(w.index.intersection(held.index))
    svd = f["specific_var"][f["specific_var"]["Date"] == d].set_index("Position")["SpecificVar"]
    s = pd.Series(0.0, index=L.index)
    s.loc[s.index.intersection(svd.index)] = svd.reindex(s.index.intersection(svd.index))
    return L, w, s, R


def _risk_from_weights(w: pd.Series, L: pd.DataFrame, s: pd.Series, R: np.ndarray) -> dict:
    """Portfolio risk for a weight vector. `model_vol_1d` (σ = √(x'Fx + w'Δw)) is the desk's
    REFERENCE risk number (2026-07-03 decision); the scenario VaR/ES quantiles are the LIMIT
    metrics; `total_var_99` is the legacy house composite, kept but demoted. Plus gross/net and
    the top-5 CTR share (the ch-09 concentration idiom: the 5 largest names' share of the
    marginal-Total-VaR contributions — replaced Risk HHI)."""
    wv, Lv, sv = w.to_numpy(), L.to_numpy(), s.to_numpy()
    x = Lv.T @ wv
    pnl = R @ x
    n = len(pnl)
    svar = float(np.sum(wv * wv * sv))
    specvol = svar ** 0.5
    F = np.cov(R, rowvar=False)
    model_vol = float(np.sqrt(max(x @ F @ x + svar, 0.0)))
    var99 = float(-np.quantile(pnl, 0.01))
    var975 = float(-np.quantile(pnl, 0.025))
    es = lambda a: float(-np.mean(np.sort(pnl)[:max(1, int(np.ceil((1 - a) * n)))]))
    total99 = (var99 * var99 + (_Z99 * specvol) ** 2) ** 0.5
    # top-5 share of the marginal-Total-VaR contributions (read off the portfolio's 1% tail day)
    ti = int(np.argsort(pnl)[int(np.floor(0.01 * (n - 1)))])
    msv = -(wv * (Lv @ R[ti]))                       # marginal Scenario VaR per name
    Fro = float(np.sum(msv))                          # = -pnl[ti]; portfolio factor-VaR read-off
    T = (Fro * Fro + _Z99 * _Z99 * svar) ** 0.5
    top5 = None
    if T > 0:
        mtv = msv * (Fro / T) + (_Z99 * _Z99) * (wv * wv * sv) / T
        tot = float(np.sum(mtv))
        if tot:
            top5 = float(np.sort(mtv)[::-1][:5].sum() / tot)
    return {"model_vol_1d": model_vol,
            "scenario_var_99": var99, "scenario_var_975": var975,
            "es_975": es(0.975), "es_99": es(0.99), "specific_vol": specvol,
            "total_var_99": total99, "top5_ctr_share": top5,
            "gross": float(np.sum(np.abs(wv))), "net": float(np.sum(wv))}


def _euler_contributions(w: np.ndarray, Lv: np.ndarray, F: np.ndarray, sv: np.ndarray) -> dict:
    """Euler decomposition of model vol (σ² = x'Fx + w'Δw). Per-position MCR_i = (Σw)_i/σ (a
    rate) and CTR_i = w_i·MCR_i, which sums EXACTLY to σ — the standard position-level report.
    Per-factor CTV_k = x_k·(Fx)_k, which sums to factor VARIANCE (cross-terms split 50/50,
    legitimately negative for hedging exposures). CTR is in vol units, CTV in variance units —
    different pairings, never compare directly."""
    x = Lv.T @ w
    Fx = F @ x
    fac_var = float(x @ Fx)
    svar = float(np.sum(w * w * sv))
    sigma = float(np.sqrt(max(fac_var + svar, 0.0)))
    ctv = x * Fx
    sig_w = Lv @ Fx + sv * w                      # (Σw)_i under Σ = LFL' + Δ
    mcr = sig_w / sigma if sigma > 0 else np.zeros_like(w)
    ctr = w * mcr
    return {"sigma": sigma, "factor_var": fac_var, "specific_var": svar,
            "x": x, "ctv": ctv, "mcr": mcr, "ctr": ctr}


@app.get("/contributions")
async def contributions(date: str | None = None, manager: str | None = None, book: str | None = None):
    """Euler risk contributions — the ch-09 standard reports, SERVED FROM THE CUBE measures
    (`Marginal Model vol` per name == CTR; `Factor variance contribution` per factor == CTV;
    `Model vol` portfolio σ) so this endpoint and the pivot grid can never disagree. The retained
    numpy implementation (_euler_contributions) is recomputed on every call as an independent
    cross-check and reported in `verification` — the tie-out made permanent.

    Memoized per (date, manager) (api_bench 2026-08-21): the payload is a pure function of the two,
    and its cost is the by-Position cube query (~0.8 s on the widest portfolio), not serialisation."""
    manager = _coalesce_manager(manager, book)
    def run():
        d = date or _latest_date()
        ck = ("_contrib_memo", d, manager, id(S["cube"]))
        if ck in S:
            return S[ck]
        # numpy reference — the independent implementation, kept as a live cross-check
        L, w, s, R = _manager_inputs(d, manager)
        if not float(np.abs(w.to_numpy()).sum()):
            raise HTTPException(404, f"no {manager} positions at {d}")
        F = np.cov(R, rowvar=False)
        e = _euler_contributions(w.to_numpy(), L.to_numpy(), F, s.to_numpy())
        ref_ctv = {str(f): float(e["ctv"][i]) for i, f in enumerate(L.columns)}
        ref_ctr = {str(p): float(e["ctr"][i]) for i, p in enumerate(L.index)}
        # cube-served numbers (single source of truth with the grid), HistFull = the model σ
        cube = S["cube"]; l, m = cube.levels, cube.measures
        flt = (l["Date"] == _date(d)) & (l["ScenarioSet"] == "HistFull")
        if "Manager" in {n for _, n in cube.hierarchies}:
            flt &= (l["Manager"] == manager)
        bk = cube.query(m["Model vol"], m["Scenario PnL vol"], m["Specific variance"], filter=flt)
        if not len(bk):
            raise HTTPException(404, f"no cube cell at {d} / HistFull")
        vol = float(bk.iloc[0]["Model vol"])
        fac_var = float(bk.iloc[0]["Scenario PnL vol"]) ** 2
        svar = float(bk.iloc[0]["Specific variance"])
        total_var = fac_var + svar
        dfF = (cube.query(m["Net exposure"], m["Factor variance contribution"],
                          levels=[l["Factor"]], filter=flt).reset_index())
        # (the Position query below is the expensive half on a wide portfolio: ~3.6k members, the
        #  SDK's per-member floor again — measured, not serialisation; see the memo at the top)
        dfP = (cube.query(m["Marginal Model vol"], levels=[l["Position"]], filter=flt)
               .reset_index())
        factors = sorted(
            [{"factor": str(r["Factor"]), "exposure": float(r["Net exposure"]),
              "ctv": float(r["Factor variance contribution"]),
              "pct_of_variance": (float(r["Factor variance contribution"] / total_var)
                                  if total_var > 0 else None)}
             for _, r in dfF.iterrows()],
            key=lambda r: -abs(r["ctv"]))
        tk = _ticker_map()
        held = w[w != 0.0]
        ctr_map = dict(zip(dfP["Position"].astype(str), dfP["Marginal Model vol"].astype(float)))
        positions = sorted(
            [{"position": p, "ticker": tk.get(p, p), "weight": float(wt),
              "mcr": (ctr_map.get(p, 0.0) / float(wt)) if wt else None,
              "ctr": ctr_map.get(p, 0.0),
              "pct_of_vol": (ctr_map.get(p, 0.0) / vol) if vol > 0 else None}
             for p, wt in held.items()],
            key=lambda r: -r["ctr"])
        verification = {
            "vol_abs_diff": abs(vol - e["sigma"]),
            "max_ctv_abs_diff": max((abs(f_["ctv"] - ref_ctv.get(f_["factor"], 0.0))
                                     for f_ in factors), default=0.0),
            "max_ctr_abs_diff": max((abs(p_["ctr"] - ref_ctr.get(p_["position"], 0.0))
                                     for p_ in positions), default=0.0),
        }
        out = {
            "date": d, "manager": manager, "source": "cube",
            "vol_1d": vol, "var99_normal": _Z99 * vol,
            "factor_variance": fac_var, "specific_variance": svar,
            "total_variance": total_var,
            "factor_share": (fac_var / total_var) if total_var > 0 else None,
            "sum_ctr": float(dfP["Marginal Model vol"].sum()),   # = vol_1d, Euler (all names)
            "sum_ctv": float(dfF["Factor variance contribution"].sum()),   # = factor_variance
            "factors": factors, "positions": positions,
            "verification": verification,
            "note": ("CTR (positions) is in VOL units and sums exactly to portfolio vol; CTV "
                     "(factors) is in VARIANCE units and sums to factor variance — different unit "
                     "pairings, never compare directly. Negative CTV = the exposure hedges the "
                     "portfolio. MCR is a rate (risk per unit weight), nothing to sum. Model vol "
                     "on the full factor-return history — distinct from the scenario-VaR views. "
                     "Served from the cube measures; `verification` is the live numpy cross-check."),
        }
        S[ck] = out
        return out
    return await run_in_threadpool(run)


_CUBE_RISK_KEYS = {"model_vol_1d": "Model vol", "scenario_var_99": "Scenario VaR 99",
                   "scenario_var_975": "Scenario VaR 97.5", "es_975": "Scenario ES 97.5",
                   "es_99": "Scenario ES 99", "specific_vol": "Specific vol",
                   "total_var_99": "Total VaR 99", "top5_ctr_share": "Top-5 risk share",
                   "gross": "Gross weight", "net": "Net weight"}
_WHATIF_AUX_KEYS: tuple = ()                            # every key is cube-served now


def _cube_risk_block(date: str, manager: str, scenario: str | None = None) -> dict:
    """The what-if risk keys read from the CUBE at (date, manager, HistFull) — optionally on a
    transient what-if source-scenario branch. One query."""
    cube = S["cube"]; l, mm = cube.levels, cube.measures
    flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == "HistFull")
    if "Manager" in {n for _, n in cube.hierarchies}:
        flt &= (l["Manager"] == manager)
    kw = {"scenario": scenario} if scenario is not None else {}
    q = cube.query(*[mm[v] for v in _CUBE_RISK_KEYS.values()], filter=flt, **kw)
    if not len(q):
        raise HTTPException(404, f"no cube cell at {date} / HistFull")
    row = q.iloc[0]
    return {k: float(row[v]) for k, v in _CUBE_RISK_KEYS.items()}


def _whatif_result(date: str, manager: str, trades: list) -> dict:
    """Before/after portfolio risk under a set of trades. The risk keys are SERVED FROM THE CUBE
    (base cell + a transient source-scenario branch carrying the trades), so /whatif, the grid
    and every other cube consumer share one implementation; the numpy engine
    (_risk_from_weights) is recomputed on every call as the live cross-check (`verification`)
    and still supplies the weight arithmetic (gross/net) and the mtv-based top-5 share. Falls
    back to serving the numpy numbers (source="numpy_fallback") if the cube path fails."""
    L, w, s, R = _manager_inputs(date, manager)
    tk = _ticker_map()
    unknown = [t["position"] for t in trades if t["position"] not in w.index]
    if unknown:
        raise HTTPException(400, f"position(s) not in the universe at {date}: {unknown}")
    w2 = w.copy()
    applied = []
    for t in trades:
        p = t["position"]; nw = float(t["weight"])
        applied.append({"position": p, "ticker": tk.get(p, p), "old": float(w.get(p, 0.0)), "new": nw})
        w2.loc[p] = nw
    ref_before = _risk_from_weights(w, L, s, R)
    ref_after = _risk_from_weights(w2, L, s, R) if trades else ref_before
    source, branch, session = "cube", None, S["session"]
    try:
        cube_before = _cube_risk_block(date, manager)
        if trades:
            branch = f"whatif-{uuid.uuid4().hex[:12]}"
            session.tables["Positions"].scenarios[branch].load(
                _whatif_branch_rows(date, manager, trades))
            cube_after = _cube_risk_block(date, manager, scenario=branch)
        else:
            cube_after = dict(cube_before)
        _vk = ("model_vol_1d", "specific_vol", "gross", "net")
        _tk_ = ("scenario_var_99", "scenario_var_975", "es_975", "es_99", "total_var_99")
        verification = {
            "max_abs_diff_vols": max(abs(c[k] - r[k]) for c, r in
                                     ((cube_before, ref_before), (cube_after, ref_after))
                                     for k in _vk),
            "max_rel_diff_tails": max(abs(c[k] - r[k]) / max(abs(r[k]), 1e-12) for c, r in
                                      ((cube_before, ref_before), (cube_after, ref_after))
                                      for k in _tk_),
            # top-5: cube (interpolated-quantile tail day) vs numpy (read-off) differ by
            # convention — loose bound, reported for the record
            "abs_diff_top5": max(abs(c["top5_ctr_share"] - (r["top5_ctr_share"] or 0.0))
                                 for c, r in ((cube_before, ref_before), (cube_after, ref_after))),
        }
    except Exception as e:
        source = "numpy_fallback"
        cube_before, cube_after = ref_before, ref_after
        verification = {"error": f"{e.__class__.__name__}: {e}"}
    finally:
        if branch is not None:
            try:
                session.delete_scenario(branch)
            except Exception:
                pass
    before = {**{k: cube_before[k] for k in _CUBE_RISK_KEYS},
              **{k: ref_before[k] for k in _WHATIF_AUX_KEYS}}
    after = {**{k: cube_after[k] for k in _CUBE_RISK_KEYS},
             **{k: ref_after[k] for k in _WHATIF_AUX_KEYS}}
    delta = {k: ((after[k] - before[k]) if isinstance(before[k], (int, float)) and before[k] is not None
                 and after[k] is not None else None) for k in before}
    holdings = [{"position": p, "ticker": tk.get(p, p), "weight": float(wt)}
                for p, wt in w[w != 0].sort_values(ascending=False).items()]
    # held names with NO loadings this date (foreign/unpriced on free data — e.g. a TSX-only
    # name) are invisible to the risk math; disclose them rather than let the portfolio quietly
    # sum below 1. holdings + unpriced together recover the full 13F weight.
    pos = S["frames"]["positions"]
    asof = pos[(pos["Manager"] == manager) & (pos["Date"] <= pd.Timestamp(date))]
    bp = asof[asof["Date"] == asof["Date"].max()] if len(asof) else asof
    unpriced = [{"position": p, "ticker": tk.get(p, p), "weight": float(wt)}
                for p, wt in bp.set_index("Position")["Weight"].items() if p not in L.index]
    unpriced.sort(key=lambda u: -u["weight"])
    # the full tradeable coverage universe (every name with loadings this date) — so the UI can add
    # a name that isn't currently held, not just resize/drop holdings.
    universe = [{"position": p, "ticker": tk.get(p, p)} for p in L.index]
    universe.sort(key=lambda u: u["ticker"])
    return {"date": date, "manager": manager, "trades": applied, "before": before, "after": after,
            "delta": delta, "holdings": holdings, "universe": universe,
            "unpriced": unpriced, "priced_weight": float(w.sum()),
            "source": source, "verification": verification}


class WhatIfBody(BaseModel):
    trades: list[dict] = []          # [{position, weight}] — absolute target weight (0 = drop)
    date: str | None = None
    manager: str | None = None
    book: str | None = None


@app.post("/whatif")
async def whatif(body: WhatIfBody):
    """Pre-trade what-if: portfolio VaR/ES/Total VaR/Specific vol/HHI before vs after a set of
    hypothetical trades (absolute target weight per position; 0 drops it; a universe name not
    currently held adds it), plus gross/net. Empty `trades` returns the current holdings so the UI
    can bootstrap the editor. Risk is recomputed in numpy from the same loadings/returns/specvar
    the cube uses."""
    manager = _coalesce_manager(body.manager, body.book)
    for t in body.trades:
        if "position" not in t or "weight" not in t:
            raise HTTPException(400, "each trade needs {position, weight}")
    return await run_in_threadpool(_whatif_result, body.date or _latest_date(), manager, body.trades)


# ============================================================================ liquidity risk
# Step 11: days-to-liquidate per held name = position $MV / (participation × ADV) — how many trading
# days to exit at a chosen % of average daily $ volume without dominating the tape. ADV is carried on
# the positions frame (the builder's trailing-63d mean dollar volume). Parameterized (participation /
# horizon are request args) so it's computed in the API from the live frame, like /stress, /drawdown,
# /whatif — not a fixed cube measure.

def _days_to_liquidate(mv: pd.Series, adv: pd.Series, participation: float) -> pd.Series:
    """Days to exit each position at `participation` of ADV: MV / (participation × ADV). NaN where ADV
    is missing or non-positive (unmeasurable, not zero)."""
    cap = participation * adv
    return (mv / cap).where(cap > 0)


@app.get("/liquidity")
async def liquidity(date: str | None = Query(None, description="as-of date; default latest"),
                    manager: str | None = Query(None), book: str | None = Query(None),
                    participation: float = Query(0.20, gt=0, le=1,
                                                 description="fraction of ADV traded per day"),
                    horizon: float = Query(5.0, gt=0, description="days to flag a name as illiquid")):
    """Days-to-liquidate for the held portfolio: per name MV / (participation·ADV), the share of
    portfolio value liquidatable within `horizon` days, the weighted-average days, and the worst
    (least-liquid) names. Names with no ADV are reported separately, never counted as instantly
    liquid."""
    manager = _coalesce_manager(manager, book)
    def run():
        f = S["frames"]; pos = f["positions"]
        d = pd.Timestamp(date) if date else pd.Timestamp(pos["Date"].max())
        bk = pos[(pos["Manager"] == manager) & (pos["Date"] == d)].copy()
        if bk.empty:
            raise HTTPException(404, f"no positions for {manager} on {d.date()}")
        if "ADV" not in bk.columns:
            raise HTTPException(503, "positions frame has no ADV column — rebuild with the Step-11 builder")
        sec = f["securities"][["Position", "Issuer", "Ticker", "Sector"]]
        bk = bk.merge(sec, on="Position", how="left")
        bk["days"] = _days_to_liquidate(bk["MV"], bk["ADV"], participation)
        measurable = bk[bk["days"].notna()]
        no_adv = bk[bk["days"].isna()]
        tot_mv = float(bk["MV"].sum())
        within = measurable[measurable["days"] <= horizon]
        wavg = (float((measurable["Weight"] * measurable["days"]).sum()
                      / measurable["Weight"].sum()) if len(measurable) else None)
        detail = (measurable.sort_values("days", ascending=False)
                  [["Issuer", "Ticker", "Sector", "Weight", "MV", "ADV", "days"]])
        return {
            "date": str(d.date()), "manager": manager,
            "participation": participation, "horizon_days": horizon,
            "n_names": int(len(bk)),
            "pct_mv_within_horizon": float(within["MV"].sum() / tot_mv) if tot_mv else None,
            "pct_weight_within_horizon": float(within["Weight"].sum()),
            "weighted_avg_days": wavg,
            "max_days": float(measurable["days"].max()) if len(measurable) else None,
            "n_no_adv": int(len(no_adv)),
            "weight_no_adv": float(no_adv["Weight"].sum()),
            "detail": [{k: _clean(v) for k, v in row.items()} for _, row in detail.iterrows()],
            "no_adv_names": [{"issuer": _clean(r["Issuer"]), "ticker": _clean(r["Ticker"]),
                              "weight": _clean(r["Weight"])}
                             for _, r in no_adv.sort_values("Weight", ascending=False).iterrows()],
            "note": ("Days-to-liquidate = position MV ÷ (participation × ADV); ADV is the trailing-63d "
                     "mean daily $ volume on the positions frame. A constant-portfolio liquidity read "
                     "on the held portfolio — not a live order book."),
        }
    return await run_in_threadpool(run)


# ============================================================================ PnL attribution (Step 15)
# Realized PnL split into factor + residual (docs/pnl-attribution-plan.md). The heavy daily engine
# (drifting weights, exact reconstruction) is the barra_pnl_attribution.py precompute; these
# endpoints read its artifact + the in-memory frames and add the statistics the cube can't express
# (Carino linking, the residual diagnostics, the §4 risk↔PnL linkage with the stressed band).
# The additive drill (Factor contribution / Specific PnL / Realized PnL) lives in the CUBE and is
# reached through /pivot — these endpoints are the period headline + diagnostics + reconcile.

def _attr_artifact(path=None) -> pd.DataFrame:
    """The precompute artifact, cached on S (keyed per file since the manager-aware precomputes,
    2026-08-14) and reloaded when the file changes. `path` defaults to the legacy Soros artifact;
    guarded endpoints pass the path `_resolve_artifact` picked for the requested manager."""
    p = path if path is not None else _pnl.ARTIFACT
    if not p.exists():
        raise HTTPException(404, f"{p.name} missing — run barra_pnl_attribution.py "
                                 "after a v2 build (needs the specific_returns frame)")
    key = str(p)
    mt = p.stat().st_mtime
    cache = S.setdefault("pnl_attr_by_path", {})
    hit = cache.get(key)
    if hit is None or hit[0] != mt:
        a = pd.read_parquet(p)
        a["Date"] = pd.to_datetime(a["Date"])
        cache[key] = (mt, a)
    return cache[key][1]


def _attr_window(art: pd.DataFrame, frm: str | None, to: str | None):
    """Daily contribution panel (day x Source) clipped to [from, to]; default trailing 12m."""
    c = (art[art["Kind"] == "contribution"]
         .pivot_table(index="Date", columns="Source", values="Value", aggfunc="first").sort_index())
    hi = min(pd.Timestamp(to), c.index.max()) if to else c.index.max()
    lo = pd.Timestamp(frm) if frm else hi - pd.DateOffset(years=1)
    w = c.loc[(c.index >= lo) & (c.index <= hi)]
    if w.empty:
        raise HTTPException(404, f"no attribution data in [{lo.date()}, {hi.date()}]")
    return w, lo, hi


def _name_attr(lo: pd.Timestamp, hi: pd.Timestamp, manager: str,
               monthly: bool = False):
    """Per-name factor/specific/realized PnL over the window, on the AS-OF monthly weights — the
    same convention (and numbers) as the cube's attribution measures. Index Position, columns
    factor_pnl / specific_pnl / realized. With monthly=True also returns the per-month specific
    panel (rows = exposure month d0, cols = Position) for persistence stats."""
    f = S["frames"]
    exp, pos, frt = f["exposures"], f["positions"], f["factor_returns"]
    sr = f.get("specific_returns")
    if sr is None:
        raise HTTPException(404, "specific_returns frame missing — rebuild with the v2 builder")
    exp_dates = np.sort(exp["Date"].unique())
    d0s = [pd.Timestamp(d) for d in exp_dates if lo <= pd.Timestamp(d) < hi]
    # 2026-08-15 (api_bench): the per-month selections come off cached row indices instead of
    # full-frame boolean masks -- specific_returns is the DAILY residual frame (~13M rows on the
    # 124-manager build) and scanning it once per month was most of /pnl_attribution/residual's
    # cost. Same rows selected, same arithmetic (see _frame_rows_by).
    pos_by = _frame_rows_by("positions", ("Manager", "Date"))
    exp_by = _frame_rows_by("exposures", ("Date",))
    parts = []
    for d0 in d0s:
        nxt = exp_dates[np.searchsorted(exp_dates, np.datetime64(d0)) + 1] \
            if np.searchsorted(exp_dates, np.datetime64(d0)) + 1 < len(exp_dates) else None
        pidx = pos_by.get((manager, d0))
        w_ = (pos.iloc[pidx] if pidx is not None else pos.iloc[:0]).groupby("Position")["Weight"].sum()
        if w_.empty:
            continue
        frd = frt[(frt["Date"] > d0) & ((frt["Date"] <= nxt) if nxt is not None else True)]
        fsum = frd.groupby("Factor")["Return"].sum()
        srd = _sr_rows_between(d0, nxt)
        eps = srd[srd["Position"].isin(w_.index)].groupby("Position")["SpecificReturn"].sum()
        eidx = exp_by.get(d0)
        exp_d = exp.iloc[eidx] if eidx is not None else exp.iloc[:0]
        Ld = (exp_d[exp_d["Position"].isin(w_.index)]
              .pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first"))
        facs = [c for c in Ld.columns if c in fsum.index]
        fac_i = (Ld[facs].fillna(0.0) @ fsum[facs]).reindex(w_.index).fillna(0.0)
        eps_i = eps.reindex(w_.index).fillna(0.0)
        part = pd.DataFrame({"factor_pnl": w_ * fac_i, "specific_pnl": w_ * eps_i})
        part["month"] = d0
        parts.append(part)
    if not parts:
        empty = pd.DataFrame(columns=["factor_pnl", "specific_pnl", "realized"])
        return (empty, pd.DataFrame()) if monthly else empty
    allp = pd.concat(parts)
    out = allp.groupby(level=0)[["factor_pnl", "specific_pnl"]].sum()
    out["realized"] = out["factor_pnl"] + out["specific_pnl"]
    if monthly:
        panel = (allp.reset_index(names="Position")
                 .pivot_table(index="month", columns="Position", values="specific_pnl",
                              aggfunc="sum"))
        return out, panel
    return out


def _drill_contrib(T: str, to: str, manager: str):
    """Per (Position, Factor) live PnL contribution over the window [T, to) (fwd-month
    convention), plus per-position specific PnL and the T-date loadings — the reconcile drawers'
    live replacement for the baked (manager-independent) `Factor contribution` cube measure
    (2026-08-22, see CLAUDE.md "manager-independent attribution limitation"). This is EXACTLY
    `_name_attr`'s per-month loop with the factor axis kept instead of collapsed
    (`Ld[facs] @ fsum[facs]` there == `Ld[facs].mul(fsum[facs])` summed over factors here), so a
    position's per-factor bars + its specific PnL sum to the SAME `realized` `_name_attr` (and
    therefore `/pnl_attribution/linkage`'s `positions[].realized`) reports for (manager, T, to,
    position) — that identity is the acceptance test, not a separate reconciliation.
    Returns (contrib: DataFrame Position×Factor, specific: Series Position, loadings_T: DataFrame
    Position×Factor loadings as-of T)."""
    f = S["frames"]
    exp, pos, frt = f["exposures"], f["positions"], f["factor_returns"]
    sr = f.get("specific_returns")
    if sr is None:
        raise HTTPException(404, "specific_returns frame missing — rebuild with the v2 builder")
    t0, t1 = pd.Timestamp(T), pd.Timestamp(to)
    exp_dates = np.sort(exp["Date"].unique())
    d0s = [pd.Timestamp(d) for d in exp_dates if t0 <= pd.Timestamp(d) < t1]
    if not d0s:
        raise HTTPException(404, f"no exposure dates in [{t0.date()}, {t1.date()})")
    pos_by = _frame_rows_by("positions", ("Manager", "Date"))
    exp_by = _frame_rows_by("exposures", ("Date",))
    contrib_parts, spec_parts = [], []
    for d0 in d0s:
        nxt = exp_dates[np.searchsorted(exp_dates, np.datetime64(d0)) + 1] \
            if np.searchsorted(exp_dates, np.datetime64(d0)) + 1 < len(exp_dates) else None
        pidx = pos_by.get((manager, d0))
        w_ = (pos.iloc[pidx] if pidx is not None else pos.iloc[:0]).groupby("Position")["Weight"].sum()
        if w_.empty:
            continue
        frd = frt[(frt["Date"] > d0) & ((frt["Date"] <= nxt) if nxt is not None else True)]
        fsum = frd.groupby("Factor")["Return"].sum()
        srd = _sr_rows_between(d0, nxt)
        eps = srd[srd["Position"].isin(w_.index)].groupby("Position")["SpecificReturn"].sum()
        eidx = exp_by.get(d0)
        exp_d = exp.iloc[eidx] if eidx is not None else exp.iloc[:0]
        Ld = (exp_d[exp_d["Position"].isin(w_.index)]
              .pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first"))
        facs = [c for c in Ld.columns if c in fsum.index]
        month_contrib = Ld[facs].fillna(0.0).mul(fsum[facs], axis=1).mul(w_.reindex(Ld.index), axis=0)
        contrib_parts.append(month_contrib)
        eps_i = eps.reindex(w_.index).fillna(0.0)
        spec_parts.append(w_ * eps_i)
    if not contrib_parts:
        raise HTTPException(404, f"no {manager} positions in [{t0.date()}, {t1.date()})")
    contrib = pd.concat(contrib_parts).groupby(level=0).sum()
    specific = pd.concat(spec_parts).groupby(level=0).sum()
    eidx0 = exp_by.get(t0)
    exp_t0 = exp.iloc[eidx0] if eidx0 is not None else exp.iloc[:0]
    loadings_T = exp_t0.pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first")
    return contrib, specific, loadings_T


def _attr_headline() -> dict | None:
    """Trailing-12m attribution headline for the /analysis payload. None when the artifact is
    absent (v1 build) so commentary is unaffected."""
    try:
        art = _attr_artifact()
        c, lo, hi = _attr_window(art, None, None)
        srcs = [s for s in c.columns if s != "Realized"]
        linked, rg = _pnl._carino_link(c[srcs].fillna(0.0), c["Realized"].fillna(0.0))
        u_m = _monthly(c["Specific"].dropna())
        spec = linked.get("Specific", 0.0)
        return {"window": f"{lo.date()} → {hi.date()} (trailing 12m)",
                "realized": rg, "factor": rg - spec,   # linked parts sum to rg exactly
                "specific": spec,
                "specific_share": (spec / rg) if abs(rg) > 1e-12 else None,
                "specific_ir_annualized": _pnl._info_ratio(u_m)}
    except Exception:
        return None


@app.get("/pnl_attribution")
async def pnl_attribution(frm: str | None = Query(None, alias="from"), to: str | None = None,
                          manager: str | None = None, book: str | None = None,
                          by: str | None = Query(None, description="sector|name for a breakdown")):
    """Period PnL attribution headline: realized portfolio return (geometric, Carino-linked) split
    into factor + specific, the cumulative hero series, the by-factor table (avg exposure,
    cumulative factor return, linked contribution, t-stat), coverage, and an optional sector/name
    breakdown. Default window: trailing 12 months of the artifact."""
    manager = _coalesce_manager(manager, book)
    def run():
        art_path, mism = _resolve_artifact(_pnl, "pnl_attribution", manager)
        if mism is not None:
            return mism
        art = _attr_artifact(art_path)
        c, lo, hi = _attr_window(art, frm, to)
        srcs = [s for s in c.columns if s != "Realized"]
        linked, rg = _pnl._carino_link(c[srcs].fillna(0.0), c["Realized"].fillna(0.0))
        factors = [s for s in srcs if s != "Specific"]
        styles = [s for s in factors if s != "Market"]
        # hero series — cumulative ARITHMETIC contributions (parts sum to the whole by identity);
        # the geometric period return is the headline number, reported separately.
        cum = c.fillna(0.0).cumsum()
        series = [{"date": _clean(d),
                   "market": float(cum.loc[d].get("Market", 0.0)),
                   "style": float(cum.loc[d, styles].sum()),
                   "specific": float(cum.loc[d].get("Specific", 0.0)),
                   "realized": float(cum.loc[d].get("Realized", 0.0))} for d in cum.index]
        # by-factor table
        expo = (art[(art["Kind"] == "exposure") & (art["Date"] >= lo) & (art["Date"] <= hi)]
                .pivot_table(index="Date", columns="Source", values="Value", aggfunc="first"))
        frt = S["frames"]["factor_returns"]
        frw = frt[(frt["Date"] >= lo) & (frt["Date"] <= hi)].groupby("Factor")["Return"].sum()
        fac_rows = []
        for f_ in factors:
            dc = c[f_].dropna()
            se = float(dc.std(ddof=1) / np.sqrt(len(dc))) if len(dc) > 2 else None
            fac_rows.append({
                "factor": f_,
                "avg_exposure": float(expo[f_].mean()) if f_ in expo else None,
                "cum_factor_return": float(frw.get(f_, 0.0)),
                "contribution": linked.get(f_, 0.0),
                "pct_of_total": (linked.get(f_, 0.0) / rg) if abs(rg) > 1e-12 else None,
                "t_stat": (float(dc.mean() / se) if se else None)})
        fac_rows.sort(key=lambda r: -abs(r["contribution"]))
        cov = art[(art["Kind"] == "coverage") & (art["Date"] >= lo) & (art["Date"] <= hi)]["Value"]
        unp = art[(art["Kind"] == "unpriced") & (art["Date"] >= lo) & (art["Date"] <= hi)]
        unp_latest = unp[unp["Date"] == unp["Date"].max()] if len(unp) else unp
        res = {
            "from": str(lo.date()), "to": str(hi.date()), "manager": manager,
            "n_days": int(len(c)),
            "calendar": {"min": _clean(art["Date"].min()), "max": _clean(art["Date"].max())},
            "headline": {
                "realized_geometric": rg,
                "factor": float(sum(linked.get(f_, 0.0) for f_ in factors)),
                "specific": linked.get("Specific", 0.0),
                "specific_share": (linked.get("Specific", 0.0) / rg) if abs(rg) > 1e-12 else None},
            "linked": {k: float(v) for k, v in linked.items()},
            "series": series,
            "factors": fac_rows,
            "coverage": {"mean_priced_share": float(cov.mean()) if len(cov) else None,
                         "min_priced_share": float(cov.min()) if len(cov) else None,
                         "unpriced": [{"name": r["Source"], "weight": float(r["Value"])}
                                      for _, r in unp_latest.iterrows()]},
            "note": ("Price-only, both sides (dividends excluded — the factor model is price-only). "
                     "Daily contributions are arithmetic and Carino-linked to the geometric period "
                     "return, so the by-factor contributions sum to it exactly. Drifting "
                     "buy-and-hold weights between 13F filings."),
        }
        if by in ("sector", "name"):
            na = _name_attr(lo, hi, manager)
            tk = _ticker_map()
            if by == "name":
                na = na.sort_values("realized", key=lambda s: s.abs(), ascending=False)
                res["by"] = [{"name": tk.get(p, p), "position": p,
                              **{k: float(v) for k, v in row.items()}}
                             for p, row in na.head(40).iterrows()]
            else:
                sec = S["frames"]["securities"][["Position", "Sector"]].set_index("Position")
                g = na.join(sec).groupby("Sector")[["factor_pnl", "specific_pnl", "realized"]].sum()
                g = g.sort_values("realized", key=lambda s: s.abs(), ascending=False)
                res["by"] = [{"name": s_, **{k: float(v) for k, v in row.items()}}
                             for s_, row in g.iterrows()]
        return res
    return await run_in_threadpool(run)


def _monthly(series: pd.Series) -> pd.Series:
    return series.resample("ME").sum(min_count=1).dropna()


def _frame_rows_by(name: str, keys: tuple) -> dict:
    """{key: int row positions} for a frame grouped by `keys` -- computed ONCE per process per
    (frame, keys) and cached on S (frames are loaded once and never change in-process). Replaces
    the per-month full-frame boolean masks in _pred_manager_vols: 126 months x (an 11.6M-row
    positions scan + a 6M-row exposures scan + a specific_var scan) was most of /calibration's
    107 s cold call (api_bench 2026-08-15)."""
    fr = S["frames"][name]
    ck = ("_rows_by", name, keys, id(fr))
    if ck not in S:
        S[ck] = fr.groupby(list(keys), sort=False).indices
    return S[ck]


def _manager_date_rows(manager: str) -> dict:
    """{Date: row positions in the positions frame} for ONE manager — the (Manager, Date) row
    index projected onto its manager. `pos[pos["Manager"] == manager]` is an 11.6M-row scan on the
    124-manager build and the endpoints that needed it did one per call (/whatchanged did several)."""
    fr = S["frames"]["positions"]
    ck = ("_manager_dates", manager, id(fr))
    if ck not in S:
        S[ck] = {pd.Timestamp(d): idx
                 for (b, d), idx in _frame_rows_by("positions", ("Manager", "Date")).items()
                 if b == manager}
    return S[ck]


def _sr_rows_between(lo, hi) -> pd.DataFrame:
    """specific_returns rows with lo < Date <= hi, off the cached per-Date row index — the frame
    is the DAILY residual panel (~13M rows on the 124-manager build) and a boolean mask over it is
    the single most expensive scan in the API. Same rows, same order (dates ascending, and the
    frame's own row order within a date)."""
    sr = S["frames"].get("specific_returns")
    if sr is None:
        return None
    by = _frame_rows_by("specific_returns", ("Date",))
    ck = ("_sr_days", id(sr))
    if ck not in S:
        S[ck] = np.array(sorted(by), dtype="datetime64[ns]")
    days = S[ck]
    i = np.searchsorted(days, np.datetime64(pd.Timestamp(lo)), side="right")
    j = (np.searchsorted(days, np.datetime64(pd.Timestamp(hi)), side="right")
         if hi is not None else len(days))          # hi=None -> every later date
    idx = [by[pd.Timestamp(d)] for d in days[i:j]]
    return sr.iloc[np.concatenate(idx)] if idx else sr.iloc[:0]


def _pred_month_numpy(d0, manager: str, frw: pd.DataFrame):
    """The numpy F(<=t) point-in-time manager/specific/per-factor daily vols for ONE month-start,
    or None when the manager is empty that month or history is under the 60-obs floor. Pure
    function of the frames -- the same arithmetic _pred_manager_vols always ran, just fed from
    the cached row index instead of full-frame masks."""
    f = S["frames"]
    pos_idx = _frame_rows_by("positions", ("Manager", "Date")).get((manager, d0))
    if pos_idx is None or len(pos_idx) == 0:
        return None
    w_ = f["positions"].iloc[pos_idx].groupby("Position")["Weight"].sum()
    if w_.empty:
        return None
    exp_idx = _frame_rows_by("exposures", ("Date",)).get(d0)
    exp_d = f["exposures"].iloc[exp_idx] if exp_idx is not None else f["exposures"].iloc[:0]
    exp_d = exp_d[exp_d["Position"].isin(w_.index)]
    L = exp_d.pivot_table(index="Position", columns="Factor", values="Loading",
                          aggfunc="first").reindex(w_.index).fillna(0.0)
    hist = frw.loc[frw.index <= d0]
    if len(hist) < 60:
        return None
    facs = [c for c in L.columns if c in hist.columns]
    x = L[facs].T @ w_
    F = hist[facs].cov().to_numpy()
    sv_idx = _frame_rows_by("specific_var", ("Date",)).get(d0)
    sv = (f["specific_var"].iloc[sv_idx] if sv_idx is not None
          else f["specific_var"].iloc[:0]).set_index("Position")["SpecificVar"]
    svar = float((w_ ** 2 * sv.reindex(w_.index).fillna(0.0)).sum())
    ref_vol = float(np.sqrt(max(x.to_numpy() @ F @ x.to_numpy() + svar, 0.0)))
    ref_spec = float(np.sqrt(svar))
    ref_fac = {f_: abs(float(x[f_])) * float(hist[f_].std()) for f_ in facs}
    return ref_vol, ref_spec, ref_fac


def _pred_month_cube(d0, manager: str, ref):
    """The cube-served PIT read for one month: (bv, sv, fv, diff) or None where the PIT set is
    absent (numpy stands). Raises on a cube error (the caller records it and falls back)."""
    ref_vol, ref_spec, ref_fac = ref
    cube = S["cube"]; l, mm = cube.levels, cube.measures
    have_manager = "Manager" in {n for _, n in cube.hierarchies}
    # The PIT sets moved to their own hierarchy + mirrored measures on 2026-08-14 (cube
    # optimization Step 2 — they were 95% of ScenarioSet's members and every group-by paid
    # for them). Same set NAMES, same numbers; a pre-split cube still answers via ScenarioSet.
    pit_split = _has_pit(cube)
    set_lvl = l["PITSet"] if pit_split else l["ScenarioSet"]
    vol_n, mvol_n = (("PIT Scenario PnL vol", "PIT Model vol") if pit_split
                     else ("Scenario PnL vol", "Model vol"))
    pit = f"PIT:{pd.Timestamp(d0).date()}"
    flt = (l["Date"] == _date(str(pd.Timestamp(d0).date()))) & (set_lvl == pit)
    if have_manager:
        flt &= (l["Manager"] == manager)
    q = cube.query(mm[mvol_n], mm["Specific vol"], filter=flt)
    if not len(q) or pd.isna(q.iloc[0][mvol_n]):
        return None                                    # no PIT set for this month — numpy stands
    qf = cube.query(mm[vol_n], levels=[l["Factor"]], filter=flt).reset_index()
    bv, sv_ = float(q.iloc[0][mvol_n]), float(q.iloc[0]["Specific vol"])
    fv = {str(r["Factor"]): float(r[vol_n]) for _, r in qf.iterrows()
          if not pd.isna(r[vol_n])}
    diff = {"manager": abs(bv - ref_vol), "specific": abs(sv_ - ref_spec),
            "factor": max([0.0] + [abs(fv[k] - v) for k, v in ref_fac.items() if k in fv])}
    return bv, sv_, {k: fv.get(k, ref_fac.get(k)) for k in ref_fac}, diff


def _pred_manager_vols(months: list, manager: str) -> tuple[dict, dict, dict]:
    """Per month-start d0: predicted DAILY portfolio vol sqrt(x'Fx + Σw²σ²), predicted daily
    specific vol, and per-factor daily vol |x_k|·σ_k — all POINT-IN-TIME (history ≤ d0, no
    look-ahead). SERVED FROM THE CUBE's PIT:* truncated-history sets (Model vol / Specific vol /
    per-factor Scenario PnL vol at (Date=d0, ScenarioSet=PIT:d0)); the numpy F(≤t) implementation
    is recomputed alongside as the live cross-check (max diffs stashed in
    S["pred_vols_verification"], served by /calibration), and is the per-month fallback where a
    PIT set is absent (early months under the 60-obs floor).

    2026-08-15 (api_bench, cube-opt round 3): MEMOIZED per (manager, month) on S -- /calibration
    (full calendar) and /pnl_attribution/residual (its window) share the months, and the frames
    and cube never change in-process -- with the numpy half fed from cached per-Date row indices
    and the per-month PIT cube queries run CONCURRENTLY (8 workers). Same arithmetic, same
    per-month values, same max-diff cross-check over the requested months; only the wall time
    changes (/calibration cold 107 s -> see docs/api-bench.md)."""
    memo = S.setdefault("_pred_vols_memo", {})
    f = S["frames"]
    todo = [d0 for d0 in months if (manager, d0) not in memo]
    err = None
    if todo:
        frw = (f["factor_returns"].pivot(index="Date", columns="Factor", values="Return")
               .dropna(how="any"))
        refs = {d0: _pred_month_numpy(d0, manager, frw) for d0 in todo}
        cube_res = {}

        def _one(d0):
            try:
                return d0, _pred_month_cube(d0, manager, refs[d0]), None
            except Exception as e:                       # noqa: BLE001 -- recorded, numpy stands
                return d0, None, f"{e.__class__.__name__}: {e}"

        with ThreadPoolExecutor(max_workers=8) as ex:
            for d0, res, e in ex.map(_one, [d for d in todo if refs[d] is not None]):
                cube_res[d0] = res
                err = err or e
        for d0 in todo:
            ref = refs[d0]
            if ref is None:
                memo[(manager, d0)] = None
                continue
            res = cube_res.get(d0)
            if res is None:
                memo[(manager, d0)] = (ref[0], ref[1], dict(ref[2]), None)
            else:
                memo[(manager, d0)] = res
    mgr_v, spec_v, fac_v = {}, {}, {}
    diffs = {"manager": 0.0, "specific": 0.0, "factor": 0.0, "months_from_cube": 0}
    for d0 in months:
        ent = memo.get((manager, d0))
        if ent is None:
            continue
        bv, sv_, fv, diff = ent
        mgr_v[d0], spec_v[d0], fac_v[d0] = bv, sv_, dict(fv)
        if diff is not None:
            for k in ("manager", "specific", "factor"):
                diffs[k] = max(diffs[k], diff[k])
            diffs["months_from_cube"] += 1
    if err:
        diffs["error"] = err
    S["pred_vols_verification"] = diffs
    return mgr_v, spec_v, fac_v



@app.get("/pnl_attribution/residual")
async def pnl_attribution_residual(frm: str | None = Query(None, alias="from"),
                                   to: str | None = None,
                                   manager: str | None = None, book: str | None = None):
    """§2 residual diagnostics with plain RAG verdicts: is the residual LARGE (specific share, IR,
    realized-vs-predicted specific vol, explained share) and is it CORRELATED (lag-1/2
    autocorrelation, residual-vs-factor regression) — plus the Barra bias statistics (portfolio /
    specific / per-factor) and residual concentration + hit rate. Thresholds start loose."""
    manager = _coalesce_manager(manager, book)
    def run():
        art_path, mism = _resolve_artifact(_pnl, "pnl_attribution", manager)
        if mism is not None:
            return mism
        art = _attr_artifact(art_path)
        c, lo, hi = _attr_window(art, frm, to)
        u_d, r_d = c["Specific"].dropna(), c["Realized"].dropna()
        u_m, r_m = _monthly(u_d), _monthly(r_d)
        srcs = [s for s in c.columns if s != "Realized"]
        linked, rg = _pnl._carino_link(c[srcs].fillna(0.0), c["Realized"].fillna(0.0))
        ir = _pnl._info_ratio(u_m)
        ac1, ac2 = _pnl._autocorr(u_m, 1), _pnl._autocorr(u_m, 2)
        expl = (1.0 - float(u_m.var()) / float(r_m.var())) if len(r_m) > 3 and r_m.var() > 0 else None
        # months in window (the artifact's forward-month convention: month d0 owns (d0, d1])
        f = S["frames"]
        exp_dates = [pd.Timestamp(d) for d in np.sort(f["exposures"]["Date"].unique())]
        months = [d for d in exp_dates if lo <= d < hi]
        mgr_v, spec_v, fac_v = _pred_manager_vols(months, manager)
        # realized vs predicted specific vol (daily)
        vr = (float(u_d.std(ddof=1)) / float(np.mean(list(spec_v.values())))
              if spec_v and float(np.mean(list(spec_v.values()))) > 0 else None)
        # residual vs the factors — DAILY resolution (a 12-month window has too few monthly obs
        # to support an 11-factor regression; daily gives ~252). Chris's test #2.
        frw = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return")
        fwin = frw.loc[(frw.index >= lo) & (frw.index <= hi)].dropna(how="all")
        reg = _pnl._resid_factor_regression(u_d, fwin)
        # bias statistics — z scaled month by month by sqrt(days in month)
        ndays = c["Realized"].resample("ME").count()

        def _z(realized_m: pd.Series, pred_daily: dict) -> tuple[float | None, float | None]:
            # the vol predicted at month-end d0 covers the FOLLOWING month (d0, d1] — label it
            # with that month's end so it lines up with the realized monthly sums.
            pv = pd.Series({(pd.Timestamp(k) + pd.offsets.MonthEnd(1)): v for k, v in pred_daily.items()})
            pred_m = (pv * np.sqrt(ndays.reindex(pv.index).astype(float))).dropna()
            return _pnl._bias_stat(realized_m.reindex(pred_m.index), pred_m)
        bias_mgr, bw_mgr = _z(r_m, mgr_v)
        bias_spec, bw_spec = _z(u_m, spec_v)
        fac_bias = []
        if fac_v:
            fac_names = sorted({k for d in fac_v.values() for k in d})
            cm = c.resample("ME").sum(min_count=1)
            for f_ in fac_names:
                if f_ not in cm:
                    continue
                b, bw = _z(cm[f_].dropna(), {d: v.get(f_) for d, v in fac_v.items()
                                             if v.get(f_) is not None})
                if b is not None:
                    fac_bias.append({"factor": f_, "bias": b, "band": bw})
            fac_bias.sort(key=lambda r: -abs(r["bias"] - 1.0))
        # concentration + hit rate of the specific PnL across names
        na = _name_attr(lo, hi, manager)
        conc = _pnl._concentration_hhi(na["specific_pnl"]) if len(na) else {"hhi": None,
                                                                            "top5_share": None, "n": 0}
        hit_names = _pnl._hit_rate(na["specific_pnl"]) if len(na) else None
        hit_months = _pnl._hit_rate(u_m)
        spec_share = (linked.get("Specific", 0.0) / rg) if abs(rg) > 1e-12 else None

        def _chk(name, value, status, verdict, fmt="num"):
            return {"name": name, "value": value, "status": status, "verdict": verdict, "fmt": fmt}
        checks = []
        if ir is not None:
            st_ = "green" if ir >= 0.3 else ("red" if ir <= -0.3 else "amber")
            checks.append(_chk("Information ratio (ann.)", ir, st_,
                               "reliable alpha" if st_ == "green" else
                               ("stock-picking destroys value" if st_ == "red"
                                else "indistinguishable from noise")))
        if vr is not None:
            st_ = ("green" if 0.8 <= vr <= 1.25 else
                   "red" if (vr > 1.5 or vr < 0.6) else "amber")
            checks.append(_chk("Specific vol — realized / predicted", vr, st_,
                               "sized about right" if st_ == "green" else
                               ("model UNDER-states specific risk" if vr > 1 else
                                "model over-states specific risk")))
        for nm, ac in (("Lag-1 autocorrelation", ac1), ("Lag-2 autocorrelation", ac2)):
            if ac is None:
                continue
            st_, vd_ = _pnl._autocorr_verdict(ac)
            checks.append(_chk(nm, ac, st_, vd_))
        if reg["r2"] is not None:
            st_ = "green" if reg["r2"] < 0.10 else ("red" if reg["r2"] > 0.25 else "amber")
            top = reg["loadings"][0] if reg["loadings"] else None
            checks.append(_chk("Residual-vs-factor R²", reg["r2"], st_,
                               "orthogonal — clean alpha" if st_ == "green" else
                               (f"hidden beta — loads on {top['factor']}" if top else "hidden beta")))
        for nm, b, bw in (("Bias stat — portfolio", bias_mgr, bw_mgr),
                          ("Bias stat — specific", bias_spec, bw_spec)):
            if b is None:
                continue
            st_ = ("green" if abs(b - 1.0) <= (bw or 0.3) else
                   "red" if abs(b - 1.0) > 1.5 * (bw or 0.3) else "amber")
            checks.append(_chk(nm, b, st_,
                               "calibrated" if st_ == "green" else
                               ("risk UNDER-forecast" if b > 1 else "risk over-forecast")))
        order = {"red": 0, "amber": 1, "green": 2}
        overall = min((ch["status"] for ch in checks), key=lambda s: order[s], default="green")
        return {
            "from": str(lo.date()), "to": str(hi.date()), "manager": manager,
            "n_months": int(len(u_m)), "status": overall, "checks": checks,
            "specific_share": spec_share, "explained_share": expl,
            "factor_regression": reg, "factor_bias": fac_bias,
            "concentration": conc,
            "hit_rate": {"names": hit_names, "months": hit_months},
            "note": ("Uncorrelated, well-sized residual = genuine diversified stock-picking. "
                     "Autocorrelation = a slow unhedged bet; factor correlation = hidden beta; "
                     "thresholds start loose (tighten once the portfolio's distribution is seen)."),
        }
    return await run_in_threadpool(run)


def _driver_text(x_t: float, x_win: float | None, cum_f: float | None, drv: dict) -> str:
    """One-sentence driver read for a reconcile-band breach (see _pnl._linkage_driver)."""
    fs, zw = drv["factor_sigma"], drv["z_window"]
    fs_s = f"{fs:+.1f}σ" if fs is not None else "n/a"
    if drv["kind"] == "exposure_migration":
        return (f"exposure-timing artifact — x was {x_t:+.3f} at T but averaged {x_win:+.3f} "
                f"in-window ({drv['ratio']:.0f}×); on the in-window exposure z = {zw:+.1f}, "
                f"within ±2σ, and the factor moved {fs_s} (ordinary). Check Δx, not the factor.")
    if drv["kind"] == "factor_move":
        cf_s = f"{cum_f:+.1%}" if cum_f is not None else "n/a"
        return (f"genuine factor move — exposure stable ({x_t:+.2f} at T, {x_win:+.2f} "
                f"in-window); the factor returned {cf_s} = {fs_s} of the window.")
    return (f"mixed — exposure {x_t:+.2f} at T vs {x_win:+.2f} in-window "
            f"({drv['ratio']:.1f}×) and the factor moved {fs_s}; part exposure-timing, "
            f"part factor move.")


@app.get("/pnl_attribution/linkage")
async def pnl_attribution_linkage(T: str | None = None,
                                  horizon: int = Query(3, ge=1, le=24, description="months"),
                                  manager: str | None = None, book: str | None = None,
                                  vol_mult: float = Query(1.25, gt=0),
                                  rho: float = Query(0.75, ge=0, le=1),
                                  min_weight: float = Query(
                                      0.001, ge=0, le=0.1,
                                      description="materiality floor on w(T) for the position "
                                                  "surprises (z is scale-invariant, so dust "
                                                  "positions would otherwise crowd the table); "
                                                  "sub-floor breaches are disclosed, not dropped")):
    """§4 linkage: the risk decomposition at T read against the realized PnL over T→T+horizon.
    Per factor (plus Specific and the portfolio total): the start-of-period ±2σ BASE band, a
    STRESSED band (vols ×vol_mult, correlations blended toward 1 by rho — correlations only enter
    the aggregate, so the portfolio band widens more than any factor's), the realized contribution
    (dot), the surprise z-score, and a within/stress/investigate verdict. Plus per-position
    surprises (weight ≥ min_weight; sub-floor breaches listed in `dust_excluded`)."""
    manager = _coalesce_manager(manager, book)
    def run():
        art_path, mism = _resolve_artifact(_pnl, "pnl_attribution", manager)
        if mism is not None:
            return mism
        art = _attr_artifact(art_path)
        c = (art[art["Kind"] == "contribution"]
             .pivot_table(index="Date", columns="Source", values="Value", aggfunc="first").sort_index())
        f = S["frames"]
        exp_dates = [pd.Timestamp(d) for d in np.sort(f["exposures"]["Date"].unique())
                     if pd.Timestamp(d) <= c.index.max()]
        if T is not None:
            t0 = max((d for d in exp_dates if d <= pd.Timestamp(T)), default=None)
        else:
            tgt = c.index.max() - pd.DateOffset(months=horizon)
            t0 = max((d for d in exp_dates if d <= tgt), default=None)
        if t0 is None:
            raise HTTPException(404, "no exposure date at or before T")
        t1 = min(t0 + pd.DateOffset(months=horizon), c.index.max())
        win = c.loc[(c.index > t0) & (c.index <= t1)]
        if win.empty:
            raise HTTPException(404, f"no realized days in ({t0.date()}, {t1.date()}]")
        h = float(len(win))
        # in-window exposure path for the driver read — the band freezes x at T, this is what
        # the portfolio actually carried (the artifact's daily drifting exposures)
        xe = (art[art["Kind"] == "exposure"]
              .pivot_table(index="Date", columns="Source", values="Value", aggfunc="first")
              .sort_index())
        xwin = xe.loc[(xe.index > t0) & (xe.index <= t1)]
        # ex-ante at T: exposures, factor covariance on history <= T, specific block.
        # Every frame selection here rides the cached row indices (api_bench 2026-08-21) — the
        # same treatment /calibration and _name_attr already had; same rows, same arithmetic.
        pos = f["positions"]
        pos_by = _manager_date_rows(manager)
        w_ = (pos.iloc[pos_by[t0]] if t0 in pos_by
              else pos.iloc[:0]).groupby("Position")["Weight"].sum()
        if w_.empty:
            raise HTTPException(404, f"no {manager} positions at {t0.date()}")
        exp_by = _frame_rows_by("exposures", ("Date",))
        exp_d = f["exposures"].iloc[exp_by[t0]] if t0 in exp_by else f["exposures"].iloc[:0]
        Lu = exp_d.pivot_table(index="Position", columns="Factor", values="Loading",
                               aggfunc="first")
        frw = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
        hist = frw.loc[frw.index <= t0]
        facs = [c_ for c_ in Lu.columns if c_ in hist.columns]
        L = Lu.reindex(w_.index)[facs].fillna(0.0)
        x = L.T @ w_
        F = hist[facs].cov().to_numpy()
        Fs = _pnl._stressed_cov(F, vol_mult, rho)
        sv_by = _frame_rows_by("specific_var", ("Date",))
        sv = (f["specific_var"].iloc[sv_by[t0]] if t0 in sv_by
              else f["specific_var"].iloc[:0]).set_index("Position")["SpecificVar"]
        svar = float((w_ ** 2 * sv.reindex(w_.index).fillna(0.0)).sum())
        sig = np.sqrt(np.diag(F));  sig_s = np.sqrt(np.diag(Fs))
        xv = x.to_numpy()
        sd_mgr = float(np.sqrt(max(xv @ F @ xv + svar, 0.0)) * np.sqrt(h))
        sd_mgr_s = float(np.sqrt(max(xv @ Fs @ xv + svar * vol_mult ** 2, 0.0)) * np.sqrt(h))

        def _verdict(r, b, s):
            if b and abs(r) <= 2 * b:
                return "within"
            if s and abs(r) <= 2 * s:
                return "stress"
            return "investigate"
        rows = []
        standalone = {f_: abs(float(x[f_])) * float(sig[i]) for i, f_ in enumerate(facs)}
        tot_sa = sum(standalone.values()) + np.sqrt(svar)
        fwin = frw.loc[(frw.index > t0) & (frw.index <= t1)]
        for i, f_ in enumerate(facs):
            if f_ not in win.columns:
                continue
            realized = float(win[f_].sum())
            sd_b = abs(float(x[f_])) * float(sig[i]) * np.sqrt(h)
            sd_s = abs(float(x[f_])) * float(sig_s[i]) * np.sqrt(h)
            verdict = _verdict(realized, sd_b, sd_s)
            x_win = float(xwin[f_].mean()) if f_ in xwin.columns and len(xwin) else None
            row = {"name": f_, "kind": "factor",
                   "exposure": float(x[f_]), "exposure_window_avg": x_win,
                   "risk_share": (standalone[f_] / tot_sa) if tot_sa > 0 else None,
                   "realized": realized, "sd_base": sd_b, "sd_stressed": sd_s,
                   "z": (realized / sd_b) if sd_b > 0 else None,
                   "verdict": verdict}
            if verdict != "within":
                cf = float(fwin[f_].sum()) if f_ in fwin.columns else None
                drv = _pnl._linkage_driver(float(x[f_]), x_win, realized, float(sig[i]), h, cf)
                if drv is not None:
                    drv["text"] = _driver_text(float(x[f_]), x_win, cf, drv)
                    row["driver"] = drv
            rows.append(row)
        def _agg_driver(realized_, sd_, verdict_, what):
            # specific/manager rows have no exposure to migrate — a breach there is the risk
            # forecast itself; point at the calibration machinery
            if verdict_ == "within" or sd_ <= 0:
                return None
            z_ = realized_ / sd_
            return {"kind": "vol_underforecast", "migrated": False, "ratio": None,
                    "z_window": None, "factor_sigma": z_,
                    "text": (f"no exposure to migrate at this level — realized is {z_:+.1f}σ "
                             f"against the start-of-period vol; cross-check {what}.")}
        sd_sp = float(np.sqrt(svar) * np.sqrt(h))
        real_sp = float(win["Specific"].sum()) if "Specific" in win else 0.0
        sp_verdict = _verdict(real_sp, sd_sp, sd_sp * vol_mult)
        sp_row = {"name": "Specific", "kind": "specific", "exposure": None,
                  "risk_share": (float(np.sqrt(svar)) / tot_sa) if tot_sa > 0 else None,
                  "realized": real_sp, "sd_base": sd_sp, "sd_stressed": sd_sp * vol_mult,
                  "z": (real_sp / sd_sp) if sd_sp > 0 else None,
                  "verdict": sp_verdict}
        drv_sp = _agg_driver(real_sp, sd_sp, sp_verdict, "the specific bias stat")
        if drv_sp:
            sp_row["driver"] = drv_sp
        rows.append(sp_row)
        rows.sort(key=lambda r: -abs(r["z"] or 0.0))
        real_mgr = float(win["Realized"].sum()) if "Realized" in win else 0.0
        mgr_verdict = _verdict(real_mgr, sd_mgr, sd_mgr_s)
        mgr_row = {"name": "Manager total", "kind": "manager", "exposure": None, "risk_share": 1.0,
                   "realized": real_mgr, "sd_base": sd_mgr, "sd_stressed": sd_mgr_s,
                   "z": (real_mgr / sd_mgr) if sd_mgr > 0 else None,
                   "verdict": mgr_verdict}
        drv_bk = _agg_driver(real_mgr, sd_mgr, mgr_verdict, "the portfolio bias stat and /backtest")
        if drv_bk:
            mgr_row["driver"] = drv_bk
        # per-position surprises: realized name PnL vs its own ex-ante sd at T
        na = _name_attr(t0, t1, manager)
        tk = _ticker_map()
        Lv = L.to_numpy()
        name_var = np.einsum("ij,jk,ik->i", Lv, F, Lv) + sv.reindex(w_.index).fillna(0.0).to_numpy()
        name_sd = np.abs(w_.to_numpy()) * np.sqrt(np.clip(name_var, 0.0, None)) * np.sqrt(h)
        # in-window average as-of weight per name (the band froze w at T; the 13F re-anchor /
        # resizes inside the window are the position analogue of exposure migration)
        mwin = [d for d in exp_dates if t0 <= d < t1]
        widx = [pos_by[d] for d in mwin if d in pos_by]
        wrows = pos.iloc[np.concatenate(widx)] if widx else pos.iloc[:0]
        wpath = (wrows.pivot_table(index="Date", columns="Position", values="Weight", aggfunc="sum")
                 .reindex(mwin).fillna(0.0))
        w_win_avg = wpath.mean() if len(wpath) else pd.Series(dtype=float)
        fsum = fwin.sum()

        Ld_link = L  # T-date loadings, Position × Factor (already restricted to fit factors)
        fac_verdict = {r["name"]: r["verdict"] for r in rows}

        def _top_factor(p):
            """The name's largest factor-PnL contributor over the window (T loadings × factor
            sums), for the driver text and the hidden-beta inference."""
            if p not in Ld_link.index:
                return None, None
            fc = Ld_link.loc[p] * fsum.reindex(Ld_link.columns).fillna(0.0)
            if not len(fc):
                return None, None
            tf = str(fc.abs().idxmax())
            return tf, float(fc[tf])

        def _pos_driver_text(w_t, w_win, drv, fac_pnl, spec_pnl, r_i, top_f, top_v):
            if drv["kind"] == "weight_migration":
                return (f"weight-timing artifact — weight was {w_t:.1%} at T but averaged "
                        f"{w_win:.1%} in-window (13F re-anchor / resize); on the in-window "
                        f"weight z = {drv['z_window']:+.1f}, within ±2. Check the filing, "
                        f"not the name.")
            top_s = (f"{top_f} {top_v * w_t:+.2%} of portfolio" if top_f else "n/a")
            if drv["kind"] == "specific_move":
                return (f"idiosyncratic — specific is {drv['specific_share']:.0%} of the move "
                        f"({spec_pnl:+.2%} of {r_i:+.2%}); a stock event the factor block can't "
                        f"see. Cross-check the name in the residual explorer and the specific "
                        f"bias stat.")
            if drv["kind"] == "factor_move":
                base = (f"factor-driven — {1 - drv['specific_share']:.0%} of the move is the "
                        f"name's loadings carrying factor returns (largest: {top_s})")
                if drv.get("hidden_beta"):
                    return (base + f"; but the {top_f} row itself sits WITHIN its band — the "
                            f"name moved further with the factor than its T loading predicts. "
                            f"Suspect the loading (hidden beta), not the factor.")
                return base + "; systematic, not stock news — read it with the factor rows above."
            base = (f"mixed — factor {fac_pnl:+.2%} and specific {spec_pnl:+.2%} both material "
                    f"(largest factor: {top_s})")
            if drv.get("hidden_beta"):
                return (base + f"; and the {top_f} row itself sits WITHIN its band, so the "
                        f"factor half also points at a mis-measured loading (hidden beta).")
            return base + "."
        positions = []
        dust = []    # sub-floor names that still breached — disclosed, never silently dropped
        for p, sd_i in zip(w_.index, name_sd):
            if p not in na.index or sd_i <= 0:
                continue
            r_i = float(na.loc[p, "realized"])
            fac_i = float(na.loc[p, "factor_pnl"])
            spec_i = float(na.loc[p, "specific_pnl"])
            w_t = float(w_[p])
            w_win = float(w_win_avg.get(p, 0.0)) if len(w_win_avg) else None
            verdict = _verdict(r_i, float(sd_i), float(sd_i) * vol_mult)
            row = {"name": tk.get(p, p), "position": p, "weight": w_t,
                   "weight_window_avg": w_win,
                   "realized": r_i, "factor_pnl": fac_i, "specific_pnl": spec_i,
                   "sd_base": float(sd_i), "z": r_i / float(sd_i), "verdict": verdict}
            # materiality floor: z is scale-invariant (band σ ∝ weight), so a 1bp position can
            # out-rank real holdings on |z| while being unable to move the portfolio. Below the floor
            # the row skips the table (and the co-movement set) but a breach is still disclosed.
            if w_t < min_weight:
                if verdict != "within":
                    dust.append(row)
                continue
            if verdict != "within":
                drv = _pnl._position_driver(r_i, fac_i, spec_i, w_t, w_win, float(sd_i))
                if drv is not None:
                    top_f, top_v = _top_factor(p)
                    drv["top_factor"] = top_f
                    # hidden beta: the factor COMPONENT of the breach ran while the driving
                    # factor's own row sat within band — the factor moved normally, so the name's
                    # realized comovement exceeded its modeled loading (mis-measured exposure).
                    # Checked on factor_move AND mixed (a mixed breach's factor half can carry
                    # the same loading error).
                    drv["hidden_beta"] = bool(drv["kind"] in ("factor_move", "mixed") and top_f
                                              and fac_verdict.get(top_f) == "within")
                    drv["text"] = _pos_driver_text(w_t, w_win, drv, fac_i, spec_i, r_i,
                                                   top_f, top_v or 0.0)
                    row["driver"] = drv
            positions.append(row)
        positions.sort(key=lambda r: -abs(r["z"]))
        dust.sort(key=lambda r: -abs(r["z"]))
        # co-movement among the idiosyncratic breaches (Chris's missing-factor test, the cheap
        # version): if the specific/mixed breach names' daily residuals co-move over the window,
        # that's one common driver the model has no factor for — not several stock events
        comove = None
        breach_ids = [q["position"] for q in positions
                      if q.get("driver") and q["driver"]["kind"] in ("specific_move", "mixed")]
        if len(breach_ids) >= 2:
            srw = _sr_rows_between(t0, t1)      # date-indexed slice, not a mask over ~13M rows
            if srw is not None:
                sub = srw[srw["Position"].isin(breach_ids)]
                panel = sub.pivot_table(index="Date", columns="Position", values="SpecificReturn")
                st_ = _pnl._pairwise_mean_corr(panel)
                if st_ is not None:
                    sec_ = S["frames"]["securities"][["Position", "Sector"]].set_index("Position")
                    secs = [str(sec_["Sector"].get(pp, "?")) for pp in breach_ids]
                    top_sec = max(set(secs), key=secs.count)
                    common = st_["mean_corr"] >= 0.25
                    st_.update({
                        "names": [tk.get(pp, pp) for pp in breach_ids],
                        "shared_sector": (top_sec if secs.count(top_sec) >= 2 else None),
                        "verdict": "common_thread" if common else "independent",
                        "text": ((f"the breach names' residuals CO-MOVE (mean pairwise ρ "
                                  f"{st_['mean_corr']:+.2f} over {st_['n_obs']}d) — one common "
                                  f"driver the model has no factor for, not several stock "
                                  f"events; a missing-factor signal")
                                 if common else
                                 (f"the breach names' residuals are independent (mean pairwise "
                                  f"ρ {st_['mean_corr']:+.2f} over {st_['n_obs']}d) — separate "
                                  f"stock events, not a hidden common driver")),
                    })
                    comove = st_
        return {
            "T": str(t0.date()), "to": str(t1.date()), "horizon_months": horizon,
            "n_days": int(h), "manager": manager,
            "stress": {"vol_mult": vol_mult, "rho_blend": rho},
            "manager_total": mgr_row, "rows": rows, "positions": positions[:15],
            "min_weight": min_weight,
            "dust_excluded": {"n": len(dust),
                              "names": [{"name": r["name"], "weight": r["weight"],
                                         "z": r["z"], "verdict": r["verdict"]}
                                        for r in dust[:10]]},
            "breach_comovement": comove,
            "surprises": [r for r in rows + [mgr_row] if r["verdict"] == "investigate"],
            "note": ("Bands are the start-of-period risk made visible: half-width = 2σ where σ² = "
                     "x'Fx (+ the diagonal specific block), scaled √days. The model forecasts "
                     "dispersion, not direction — bands centre at zero. Realized inside base = "
                     "risk understood; outside base but inside stressed = a stress regime; outside "
                     "stressed = a risk the decomposition missed — investigate (gain or loss "
                     "alike). Correlations only enter the aggregate rows, so the portfolio band "
                     "widens under the correlation shock even where no single factor's does. Rows "
                     "outside the base band carry a driver read: the band freezes x at T, so a "
                     "breach is either a genuine factor move or the exposure migrating inside the "
                     "window (loading refresh / 13F re-anchor) — an ill-conditioned z, not a "
                     "factor event."),
        }
    return await run_in_threadpool(run)


@app.get("/pnl_attribution/drill")
async def pnl_attribution_drill(T: str, to: str, manager: str | None = None, book: str | None = None,
                                position: str | None = None, factor: str | None = None):
    """Live per-manager reconcile drill for the Vite Attribution drawers (2026-08-22). The baked
    `Factor contribution` cube measure is manager-INDEPENDENT above one manager (it carries ONE
    arbitrary manager's weight per name — see CLAUDE.md "manager-independent attribution
    limitation") and `_validate_pivot` rejects it outright once >1 manager is loaded, which 400'd
    the drawers on the 123-manager build. This recomputes the identical forward-month-convention
    math live from `S["frames"]` for the REQUESTED manager's own as-of weights (`_drill_contrib`,
    which mirrors `_name_attr`/`/pnl_attribution/linkage` exactly), so it works on ANY loaded
    manager — no artifact, no `_manager_guard` (unlike /universe, /funnel, /span, /drift, the
    other /pnl_attribution* routes: those read a single-manager PRECOMPUTED artifact with no
    manager concept of its own; this reads the live per-manager frames directly, so there is
    nothing to mismatch).

    `position=` (PositionDrawer): that name's per-factor contribution over [T, to) + its specific
    PnL + its T-date loadings; `bars`' contributions plus `specific_pnl` sum to `realized` to
    float precision by construction (same arithmetic as `_name_attr`, factor axis kept instead of
    collapsed). `factor=` (FactorDrawer): the inverse who-carried-it view — that factor's
    contribution over [T, to) for the manager, by Issuer; `bars` sum to `total`."""
    manager = _coalesce_manager(manager, book)
    if (position is None) == (factor is None):
        raise HTTPException(400, "pass exactly one of position= or factor=")
    def run():
        contrib, specific, loadings_T = _drill_contrib(T, to, manager)
        if position is not None:
            row = contrib.loc[position] if position in contrib.index else pd.Series(dtype=float)
            if row.empty and position not in specific.index:
                raise HTTPException(404, f"no {manager} exposure/PnL for {position} in [{T}, {to})")
            spec_pnl = float(specific.get(position, 0.0))
            lt = loadings_T.loc[position] if position in loadings_T.index else pd.Series(dtype=float)
            bars = [{"factor": f_, "contribution": float(v),
                     "loading_at_T": (float(lt[f_]) if f_ in lt.index and pd.notna(lt[f_]) else None)}
                    for f_, v in row.items() if v != 0.0]
            bars.sort(key=lambda b: -abs(b["contribution"]))
            realized = float(row.sum()) + spec_pnl
            tk = _ticker_map()
            return {"manager": manager, "T": T, "to": to, "position": position,
                    "ticker": tk.get(position, position),
                    "bars": bars, "specific_pnl": spec_pnl, "realized": realized,
                    "n_factors_at_T": int(lt.notna().sum())}
        if factor not in contrib.columns:
            raise HTTPException(404, f"no {factor} exposure for {manager} in [{T}, {to})")
        col = contrib[factor]
        sec = S["frames"]["securities"][["Position", "Issuer"]].set_index("Position")["Issuer"]
        by_issuer = col.groupby(sec.reindex(col.index).fillna("Unknown")).sum()
        bars = [{"issuer": str(iss), "contribution": float(v)} for iss, v in by_issuer.items() if v != 0.0]
        bars.sort(key=lambda b: -abs(b["contribution"]))
        return {"manager": manager, "T": T, "to": to, "factor": factor,
                "bars": bars, "total": float(col.sum())}
    return await run_in_threadpool(run)


# ============================================================================ model trust
# The fit-for-purpose family: /calibration (rolling bias + exceedances; NB /validation was
# already taken by the scenario cross-check), /regression
# (the builder's WLS fit health from the regression_stats side artifact), /factor_cov (the F
# matrix made visible — correlations + vols, full window vs recent year).

REG_ARTIFACT = _pnl.OUT / "regression_stats.parquet"


def _reg_artifact() -> pd.DataFrame:
    """regression_stats.parquet, cached on S and reloaded when the file changes."""
    if not REG_ARTIFACT.exists():
        raise HTTPException(404, "regression_stats.parquet missing — rebuild with the v2 builder "
                                 "(barra_build_frames.py now persists the WLS fit stats)")
    mt = REG_ARTIFACT.stat().st_mtime
    if S.get("reg_stats_mtime") != mt:
        a = pd.read_parquet(REG_ARTIFACT)
        a["Date"] = pd.to_datetime(a["Date"])
        S["reg_stats"], S["reg_stats_mtime"] = a, mt
    return S["reg_stats"]


@app.get("/regression")
async def regression_health():
    """The cross-sectional WLS fit health: monthly-mean weighted R² trend (NB ours is a DAILY
    regression — daily single-stock R² runs lower than the monthly 0.2–0.4 rule of thumb; the
    trend matters more than the level), per-factor share of days with |t| > 2 (the admission
    bar: ≥ 1/3 of periods justifies inclusion), and the cross-section breadth N."""
    def run():
        df = _reg_artifact()
        day = df.groupby("Date")[["R2", "N"]].first()
        r2m = day["R2"].resample("ME").mean().dropna()
        fac = []
        for f_, g in df.dropna(subset=["TStat"]).groupby("Factor"):
            t = g["TStat"].abs()
            fac.append({"factor": str(f_), "pct_days_t_gt2": float((t > 2).mean()),
                        "mean_abs_t": float(t.mean()), "n_days": int(len(t))})
        fac.sort(key=lambda r: -r["pct_days_t_gt2"])
        return {
            "from": _clean(day.index.min()), "to": _clean(day.index.max()),
            "n_days": int(len(day)),
            "r2_monthly": [{"date": _clean(d), "r2": float(v)} for d, v in r2m.items()],
            "r2_mean": float(day["R2"].mean()),
            "n_names": {"min": int(day["N"].min()), "median": float(day["N"].median()),
                        "max": int(day["N"].max())},
            "factors": fac,
            "note": ("Weighted cross-sectional R² of the daily WLS on the estimation universe "
                     "(sqrt-cap weights). Factors clearing |t|>2 on a meaningful share of days "
                     "earn their place; a persistently insignificant factor is a candidate to "
                     "drop. N is the estimation cross-section that day — thin days make noisy "
                     "factor returns."),
        }
    return await run_in_threadpool(run)


@app.get("/calibration")
async def calibration(window: int = Query(24, ge=6, le=60, description="rolling window, months"),
                      manager: str | None = None, book: str | None = None):
    """Fit-for-purpose calibration over time: the ROLLING bias statistic b = std(realized /
    predicted vol) over a trailing window, with the 1 ± √(2/window) acceptance band — run for
    the whole portfolio and the specific block — plus 2σ exceedance counts (expected ≈ 4.6%)."""
    manager = _coalesce_manager(manager, book)
    def run():
        art = _attr_artifact()
        c = (art[art["Kind"] == "contribution"]
             .pivot_table(index="Date", columns="Source", values="Value", aggfunc="first")
             .sort_index())
        r_m = _monthly(c["Realized"].dropna())
        u_m = _monthly(c["Specific"].dropna())
        f = S["frames"]
        months = [pd.Timestamp(d) for d in np.sort(f["exposures"]["Date"].unique())
                  if pd.Timestamp(d) <= c.index.max()]
        key = ("pred_vols_full", manager, str(months[-1].date()) if months else "")
        if S.get("pred_vols_key") != key:
            S["pred_vols"], S["pred_vols_key"] = _pred_manager_vols(months, manager), key
            # pin THIS computation's cross-check to the cache (the stash is last-writer-wins
            # and /pnl_attribution/residual also calls _pred_manager_vols on its own window)
            S["pred_vols_verif_cal"] = S.get("pred_vols_verification")
        mgr_v, spec_v, _fv = S["pred_vols"]
        ndays = c["Realized"].resample("ME").count()

        def pred_m(pred_daily: dict) -> pd.Series:
            pv = pd.Series({(pd.Timestamp(k) + pd.offsets.MonthEnd(1)): v
                            for k, v in pred_daily.items()})
            return (pv * np.sqrt(ndays.reindex(pv.index).astype(float))).dropna()

        out = {}
        for name, realized, pred in (("manager", r_m, pred_m(mgr_v)),
                                     ("specific", u_m, pred_m(spec_v))):
            r_ = realized.reindex(pred.index).dropna()
            p_ = pred.reindex(r_.index)
            z = (r_ / p_).replace([np.inf, -np.inf], np.nan).dropna()
            rb = _pnl._rolling_bias(r_, p_, window)
            out[name] = {
                "bias": [{"date": _clean(r["date"]), "b": r["b"]} for r in rb],
                "band": (rb[0]["band"] if rb else float(np.sqrt(2.0 / window))),
                "exceedance_2s": (float((z.abs() > 2).mean()) if len(z) else None),
                "n_months": int(len(z)),
            }
        return {
            "window": window, "manager": manager, "expected_exceedance_2s": 0.0455,
            "source": "cube",
            "pit_verification": S.get("pred_vols_verif_cal"),
            "series": out,
            "note": ("b ≈ 1 = calibrated; b > 1 = risk under-forecast (the dangerous direction); "
                     "the band is the 95% acceptance range 1 ± √(2/window). Exceedances are "
                     "months beyond ±2 predicted σ — a fat-tail read the std-based b can miss. "
                     "Realized is the attribution artifact's portfolio return (drifting weights, "
                     "price-only); predicted is the model risk at each prior month-end."),
        }
    return await run_in_threadpool(run)


@app.get("/factor_cov")
async def factor_cov(date: str | None = None):
    """The factor covariance made visible: the correlation matrix and per-factor daily vols on
    the full history ≤ date, with the recent-1y vols beside them (vol clustering — where the
    full-window estimate understates the current regime)."""
    def run():
        fr = S["frames"]["factor_returns"]
        wide = fr.pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
        if date:
            wide = wide.loc[wide.index <= pd.Timestamp(date)]
        if len(wide) < 60:
            raise HTTPException(404, "not enough factor-return history")
        recent = wide.loc[wide.index > wide.index.max() - pd.DateOffset(years=1)]
        facs = list(wide.columns)
        C, C1 = wide.corr(), recent.corr()
        off = ~np.eye(len(facs), dtype=bool)
        return {
            "date": _clean(wide.index.max()), "n_days": int(len(wide)),
            "n_days_recent": int(len(recent)), "factors": facs,
            "corr": [[float(C.iloc[i, j]) for j in range(len(facs))] for i in range(len(facs))],
            "vol_full": {f_: float(wide[f_].std()) for f_ in facs},
            "vol_recent": {f_: float(recent[f_].std()) for f_ in facs},
            "avg_abs_corr": {"full": float(np.abs(C.to_numpy()[off]).mean()),
                             "recent": float(np.abs(C1.to_numpy()[off]).mean())},
            "note": ("Daily vols; recent = trailing year. A recent/full vol ratio well above 1 "
                     "is the vol-clustering warning — full-window bands (backtest, reconcile) "
                     "understate the current regime there. Correlations rising toward the "
                     "recent window is the diversification the portfolio leans on decaying — "
                     "the stressed band's ρ→1 blend is the deliberate exaggeration of that."),
        }
    return await run_in_threadpool(run)


# ---- exposure profile (ch 03: what each factor IS, and where the portfolio sits in it) ----

FACTOR_RECIPES = {
    "Market": "intercept — every name loads 1.0; carries the cross-sectional average return",
    "Beta": "252d regression beta of daily returns on the market index",
    "ResidVol": "annualized std of the 252d market-regression residual",
    "RateBeta": "partial duration beta: stock daily return on the TLT return residualized "
                "against the market, 252d window (2026-07-04 — the missing rates/duration "
                "factor behind the shared Leverage/Liquidity/MegaCap hidden-beta carriers)",
    "NdxBeta": "partial mega-complex beta: stock daily return on the QQQ return residualized "
               "against the market, 252d window (2026-07-04 — the mega complex co-moves as a "
               "group beyond any smooth function of log-mcap; realized comovement prices the "
               "club directly, incl. imputed-size names like TSM)",
    "Momentum": "12-1 month LOG relative strength ln(P(t−21d)/P(t−252d)) — log, not arithmetic, so "
                "the z-scored winner tail is not stretched by return skew (2026-07-04 respec)",
    "Liquidity": "log turnover (trailing-63d avg dollar volume / market cap), orthogonalized to "
                 "Size on the estimation fit (2026-07-04 respec — raw ADV was a second Size)",
    "Size": "log market cap (close × PIT shares); coverage names with no share count (foreign "
            "filers/ETFs) are imputed from the estimation log-ADV fit, disclosed via /dq",
    "NonLinSize": "cube of the WINSORIZED (±3) standardized Size loading (USE4), orthogonalized "
                  "to Size on the estimation fit (2026-07-04 respecs — cubing raw log-mcap left a "
                  "quadratic U-shape; cubing the uncapped coverage tail made −10 loadings with no "
                  "realized comovement, so the cube is evaluated at the estimation edge)",
    # MegaCap (size-curve spline knot) added AND dropped 2026-07-04: NdxBeta took over the
    # mega club's pricing (admission 13%) and its last flag was carried by imputed-size names
    # whose hinge loading the log-ADV fit cannot estimate.
    "Value": "book-to-price: PIT equity / mcap",
    "EarnYield": "earnings yield: PIT net income / mcap",
    "Leverage": "liabilities / assets (PIT; bounded ~[0,1] — 2026-07-04 respec: assets/equity "
                "was unbounded as equity→0 and saturated into a financials-sector dummy)",
    # Growth dropped 2026-07-04 (9% admission, 21.3% held-weight coverage)
}


def _snap_exposure_date(exp: pd.DataFrame, date: str | None) -> pd.Timestamp:
    dts = np.sort(exp["Date"].unique())
    if date is None:
        return pd.Timestamp(dts[-1])
    d = pd.Timestamp(date)
    prior = [t for t in dts if pd.Timestamp(t) <= d]
    if not prior:
        raise HTTPException(404, f"no exposure date at or before {date}")
    return pd.Timestamp(prior[-1])


@app.get("/exposure_profile")
async def exposure_profile(factor: str, date: str | None = None,
                           manager: str | None = None, book: str | None = None):
    """One factor's cross-section at a date: the loading distribution (histogram + quantiles),
    the ±3 estimation winsor bounds, the uncapped tail beyond them (coverage names showing their
    true tilt), and the held portfolio overlaid — the 'model-conditional: this is what OUR
    {factor} means' view, with the descriptor recipe attached."""
    manager = _coalesce_manager(manager, book)
    def run():
        f = S["frames"]; exp = f["exposures"]
        d0 = _snap_exposure_date(exp, date)
        sub = (exp[(exp["Date"] == d0) & (exp["Factor"] == factor)]
               .set_index("Position")["Loading"].dropna())
        if sub.empty:
            raise HTTPException(400, f"unknown factor or no loadings: {factor}")
        pos = f["positions"]
        w_ = pos[(pos["Manager"] == manager) & (pos["Date"] == d0)].groupby("Position")["Weight"].sum()
        tk = _ticker_map()
        held = sorted(
            [{"ticker": tk.get(p, p), "weight": float(wt), "loading": float(sub[p])}
             for p, wt in w_.items() if p in sub.index],
            key=lambda r: -abs(r["loading"]))
        edges = np.linspace(min(float(sub.min()), -3.5), max(float(sub.max()), 3.5), 41)
        cnt, _ = np.histogram(sub, bins=edges)
        beyond = sub[sub.abs() > 3].abs().sort_values(ascending=False)
        return {
            "factor": factor, "date": _clean(d0), "manager": manager,
            "recipe": FACTOR_RECIPES.get(factor, ""),
            "n_names": int(len(sub)),
            "quantiles": {q: float(np.percentile(sub, p))
                          for q, p in (("p01", 1), ("p25", 25), ("p50", 50),
                                       ("p75", 75), ("p99", 99))},
            "hist": [{"x0": float(edges[i]), "x1": float(edges[i + 1]), "n": int(cnt[i])}
                     for i in range(len(cnt))],
            "beyond3": {"n": int(len(beyond)), "share": float(len(beyond) / len(sub)),
                        "names": [{"ticker": tk.get(p, p), "loading": float(sub[p])}
                                  for p in beyond.index[:8]]},
            "held": held,
            "note": ("Loadings are z-scores vs the ESTIMATION cross-section (median/MAD); "
                     "estimation names winsorized at ±3, coverage names uncapped (±10 backstop) "
                     "— so anything beyond ±3 is an off-index name showing its true tilt. "
                     "Model-conditional: this distribution defines what the factor means here."),
        }
    return await run_in_threadpool(run)


# ---- hedging (appendix D6 + mini-example §7–8: remove the risk you don't want) ----

def _hedge_table(x: np.ndarray, F: np.ndarray, svar: float, factors: list[str]) -> dict:
    """Per factor: portfolio vol before/after NEUTRALIZING it (x_k → 0 via -x_k units of the pure
    factor-k portfolio — ch-07's investable dual), ranked by vol saved. Plus the D6 single-
    instrument minimum-variance hedge with the pure Market portfolio as the instrument:
    h* = −Cov(r_h, r_p)/Var(r_h) = −(Fx)_mkt/F_mm."""
    base = float(np.sqrt(max(x @ F @ x + svar, 0.0)))
    rows = []
    for k, f_ in enumerate(factors):
        x2 = x.copy(); x2[k] = 0.0
        after = float(np.sqrt(max(x2 @ F @ x2 + svar, 0.0)))
        rows.append({"factor": f_, "exposure": float(x[k]), "hedge_units": float(-x[k]),
                     "vol_after": after, "vol_reduction": base - after})
    rows.sort(key=lambda r: -r["vol_reduction"])
    mkt = None
    if "Market" in factors:
        m = factors.index("Market")
        if F[m, m] > 0:
            Fx = F @ x
            h = float(-Fx[m] / F[m, m])
            xh = x.copy(); xh[m] += h
            after = float(np.sqrt(max(xh @ F @ xh + svar, 0.0)))
            mkt = {"h_star": h, "vol_after": after, "vol_reduction": base - after}
    return {"vol_base": base, "rows": rows, "market_hedge": mkt}


@app.get("/hedge")
async def hedge(date: str | None = None, manager: str | None = None, book: str | None = None):
    """What hedging each factor would do — SERVED FROM THE CUBE measures (`Vol ex factor` per
    factor = vol after zeroing that net exposure with the specific block kept; `Min-variance
    hedge ratio` / `Vol at min-variance hedge` = the D6 single-instrument hedge), ranked by vol
    saved. The retained numpy `_hedge_table` is recomputed on every call as an independent
    cross-check (`verification`). Specific risk is untouched by construction."""
    manager = _coalesce_manager(manager, book)
    def run():
        d = date or _latest_date()
        # numpy reference — the independent implementation, kept as a live cross-check
        L, w, s, R = _manager_inputs(d, manager)
        if not float(np.abs(w.to_numpy()).sum()):
            raise HTTPException(404, f"no {manager} positions at {d}")
        F = np.cov(R, rowvar=False)
        x = L.to_numpy().T @ w.to_numpy()
        svar = float(np.sum(w.to_numpy() ** 2 * s.to_numpy()))
        ref = _hedge_table(x, F, svar, list(L.columns))
        ref_after = {r["factor"]: r["vol_after"] for r in ref["rows"]}
        # cube-served numbers (HistFull = the model σ)
        cube = S["cube"]; l, m = cube.levels, cube.measures
        flt = (l["Date"] == _date(d)) & (l["ScenarioSet"] == "HistFull")
        if "Manager" in {n for _, n in cube.hierarchies}:
            flt &= (l["Manager"] == manager)
        bk = cube.query(m["Model vol"], m["Specific vol"], filter=flt)
        if not len(bk):
            raise HTTPException(404, f"no cube cell at {d} / HistFull")
        vol_base = float(bk.iloc[0]["Model vol"])
        spec_vol = float(bk.iloc[0]["Specific vol"])
        dfF = (cube.query(m["Net exposure"], m["Vol ex factor"],
                          m["Min-variance hedge ratio"], m["Vol at min-variance hedge"],
                          levels=[l["Factor"]], filter=flt).reset_index())
        rows = sorted(
            [{"factor": str(r["Factor"]), "exposure": float(r["Net exposure"]),
              "hedge_units": -float(r["Net exposure"]),
              "vol_after": float(r["Vol ex factor"]),
              "vol_reduction": vol_base - float(r["Vol ex factor"])}
             for _, r in dfF.iterrows() if pd.notna(r["Vol ex factor"])],
            key=lambda r: -r["vol_reduction"])
        mkt = None
        mrow = dfF[dfF["Factor"] == "Market"]
        if len(mrow) and pd.notna(mrow.iloc[0]["Min-variance hedge ratio"]):
            after = float(mrow.iloc[0]["Vol at min-variance hedge"])
            mkt = {"h_star": float(mrow.iloc[0]["Min-variance hedge ratio"]),
                   "vol_after": after, "vol_reduction": vol_base - after}
        verification = {
            "vol_base_abs_diff": abs(vol_base - ref["vol_base"]),
            "max_vol_after_abs_diff": max((abs(r["vol_after"] - ref_after.get(r["factor"], 0.0))
                                           for r in rows), default=0.0),
            "h_star_abs_diff": (abs(mkt["h_star"] - ref["market_hedge"]["h_star"])
                                if mkt and ref.get("market_hedge") else None),
        }
        return {
            "date": d, "manager": manager, "source": "cube",
            "vol_base": vol_base, "specific_vol": spec_vol,
            "rows": rows, "market_hedge": mkt,
            "verification": verification,
            "note": ("hedge_units = −x_k of the pure factor-k portfolio (ch 07's f̂ = Pr dual) — "
                     "implementable in principle, but pure portfolios carry real leverage/turnover "
                     "cost. The market h* is the D6 single-instrument minimum-variance hedge "
                     "(h* = −β of the portfolio on the Market factor). Vol is model vol σ² = x'Fx "
                     "+ w'Δw; the specific block survives any factor hedge. Served from the cube "
                     "measures; `verification` is the live numpy cross-check."),
        }
    return await run_in_threadpool(run)


# ============================================================================ Model vs Price (the bridge)
# docs/price-var-plan.md. The MODEL prices the portfolio on a linear factor block + a Gaussian
# diagonal specific block; the PRICE family prices the SAME portfolio on raw historical stock
# returns — no model at all. Five numbers in a fixed order, each changing ONE thing, so the
# differences are the whole gap by construction: T0 Scenario VaR 99 (factor-only) -> T1 Total VaR
# 99 (+ Gaussian specific) -> T2 full-sim VaR (the Gaussian specific block replaced by REALIZED
# daily residual paths) -> T3 Price VaR on covered names (today's fixed loadings replaced by each
# day's own — r_t = L_i(t)·f_t + u_i,t exactly, the model's own identity) -> T4 Price VaR 99
# (names priced but uncovered by the model are added). T0/T1/T4 are cube measures; T2/T3 are
# numpy on the SAME population/calendar `_manager_inputs` already uses, so every step prices the
# identical portfolio.

def _var_bridge_result(date: str | None, manager: str, set_: str, alpha: float) -> dict:
    cube = S["cube"]
    if "Price VaR 99" not in cube.measures:
        raise HTTPException(404, "Price family not available — rebuild with stock_returns.parquet "
                                 "present (see barra_build_stock_returns.py / docs/price-var-plan.md).")
    if set_ != "HistFull" and set_ not in EVENT_WINDOWS:
        raise HTTPException(400, f"set={set_!r} has no Price mirror — /var_bridge supports HistFull "
                                 "and Evt:* windows only (Hypo:* sets are sigma-shocks on factors, "
                                 "not a historical window).")
    d = date or _latest_date()
    l, m = cube.levels, cube.measures
    L, w, s_, R = _manager_inputs(d, manager)
    if not float(np.abs(w.to_numpy()).sum()):
        raise HTTPException(404, f"no {manager} positions at {d}")
    f = S["frames"]
    has_manager_hier = "Manager" in {n for _, n in cube.hierarchies}

    # T0 / T1 -- straight from the cube (ScenarioSet=set_)
    flt_scn = (l["Date"] == _date(d)) & (l["ScenarioSet"] == set_)
    if has_manager_hier:
        flt_scn &= (l["Manager"] == manager)
    scn_row = cube.query(m["Scenario VaR 99"], m["Total VaR 99"], filter=flt_scn)
    if not len(scn_row):
        raise HTTPException(404, f"no cube cell at {d} / ScenarioSet={set_}")
    t0 = float(scn_row.iloc[0]["Scenario VaR 99"])
    t1 = float(scn_row.iloc[0]["Total VaR 99"])

    # T4 + the manager's own coverage-at-tail -- straight from the cube (PriceSet=set_)
    flt_price = (l["Date"] == _date(d)) & (l["PriceSet"] == set_)
    if has_manager_hier:
        flt_price &= (l["Manager"] == manager)
    price_row = cube.query(m["Price VaR 99"], m["Price coverage at tail"], filter=flt_price)
    if not len(price_row):
        raise HTTPException(404, f"no cube cell at {d} / PriceSet={set_}")
    t4 = float(price_row.iloc[0]["Price VaR 99"])
    coverage_at_tail = float(price_row.iloc[0]["Price coverage at tail"])

    # T2 / T3 -- numpy from the frames, SAME population (L.index, w) and calendar T0/T1 used.
    factors = list(L.columns)
    Lv, wv = L.to_numpy(), w.to_numpy()
    x = Lv.T @ wv
    wide_fr = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return") \
        .dropna(how="any")[factors]
    if set_ == "HistFull":
        dates = wide_fr.index
    else:
        a, b = EVENT_WINDOWS[set_]
        dates = wide_fr.loc[a:b].index
    fac_pnl = wide_fr.loc[dates].to_numpy() @ x

    sr = f.get("specific_returns")
    if sr is not None and len(sr):
        u_wide = sr.pivot_table(index="Date", columns="Position", values="SpecificReturn", aggfunc="last")
        spec_pnl_t2 = u_wide.reindex(index=dates, columns=L.index).fillna(0.0).to_numpy() @ wv
    else:
        spec_pnl_t2 = np.zeros(len(dates))
    pnl_t2 = fac_pnl + spec_pnl_t2
    t2 = float(-np.quantile(pnl_t2, alpha))

    stk = f.get("stock_returns")
    if stk is None or not len(stk):
        raise HTTPException(404, "stock_returns.parquet not loaded — rebuild to enable /var_bridge")
    r_wide = stk.pivot_table(index="Date", columns="Position", values="Return", aggfunc="last")
    r_cov = r_wide.reindex(index=dates, columns=L.index)
    pnl_t3 = r_cov.fillna(0.0).to_numpy() @ wv
    t3 = float(-np.quantile(pnl_t3, alpha))

    # numpy verification twin of T4 (Price VaR 99, ALL priced names — /contributions' pattern)
    pos = f["positions"]; dts = pd.Timestamp(d)
    asof = pos[(pos["Manager"] == manager) & (pos["Date"] <= dts)]
    bp = asof[asof["Date"] == asof["Date"].max()] if len(asof) else asof
    held = bp.set_index("Position")["Weight"] if len(bp) else pd.Series(dtype=float)
    priced_names = set(r_wide.columns)
    covered_names = set(L.index)
    held_priced = [p for p in held.index if p in priced_names]
    w_all = held.reindex(held_priced).fillna(0.0)
    r_all = r_wide.reindex(index=dates, columns=held_priced)
    priced_mask = r_all.notna().to_numpy()
    pnl_t4_ref = r_all.fillna(0.0).to_numpy() @ w_all.to_numpy()
    t4_ref = float(-np.quantile(pnl_t4_ref, alpha)) if len(pnl_t4_ref) else 0.0
    if len(pnl_t4_ref):
        tail_i = int(np.argsort(pnl_t4_ref)[int(np.floor(alpha * (len(pnl_t4_ref) - 1)))])
        cov_ref = float((priced_mask[tail_i].astype(float) * w_all.to_numpy()).sum())
        cov_series = (priced_mask.astype(float) * w_all.to_numpy()[None, :]).sum(axis=1)
    else:
        cov_ref = 0.0
        cov_series = np.zeros(0)

    # coverage disclosure: held names never priced at all (contribute 0 everywhere), and names
    # priced-but-uncovered (the T3->T4 population change) — never silently absorbed, /whatif idiom.
    tick = _ticker_map()
    never_priced = [p for p in held.index if p not in priced_names]
    added_by_coverage = [p for p in held_priced if p not in covered_names]
    never_priced_weight = float(held.reindex(never_priced).abs().sum()) if never_priced else 0.0
    added_weight = float(held.reindex(added_by_coverage).abs().sum()) if added_by_coverage else 0.0

    # per-name disagreement table -- served from the cube (real numbers, never recomputed here);
    # `likely_driver` is a per-name HEURISTIC (coverage is exact; specific vs exposure/distribution
    # is a magnitude comparison, not a true per-name T2/T3 split — disclosed, see docs).
    flt_names = (l["Date"] == _date(d)) & (l["ScenarioSet"] == set_) & (l["PriceSet"] == set_)
    if has_manager_hier:
        flt_names &= (l["Manager"] == manager)
    dfN = cube.query(m["Marginal Total VaR 99"], m["Marginal Price VaR 99"], m["Specific variance"],
                     m["Net weight"],
                     levels=[l["Position"]], filter=flt_names).reset_index()
    iss = dict(zip(f["securities"]["Position"], f["securities"]["Issuer"]))
    # NaN on any member the cube leaves blank (e.g. a Position with no factor loadings — Marginal
    # Total VaR 99 is undefined there); treat as 0 so gap/specvar_term stay finite and comparable.
    for _c in ("Marginal Total VaR 99", "Marginal Price VaR 99", "Specific variance", "Net weight"):
        dfN[_c] = dfN[_c].astype(float).fillna(0.0)
    dfN["gap"] = dfN["Marginal Price VaR 99"] - dfN["Marginal Total VaR 99"]
    dfN["ticker"] = dfN["Position"].map(tick)
    dfN["issuer"] = dfN["Position"].map(iss)
    dfN["specvar_term"] = (_Z99 ** 2) * dfN["Specific variance"]
    never_priced_set, added_set = set(never_priced), set(added_by_coverage)

    def _driver(row) -> str:
        p = row["Position"]
        if p in never_priced_set:
            return "coverage (never priced)"
        if p in added_set:
            return "coverage (priced, no loadings)"
        if row["gap"] != 0.0 and abs(row["specvar_term"]) > 0.5 * abs(row["gap"]):
            return "specific_risk_dropped"
        return "exposure_drift_or_distribution"
    dfN["likely_driver"] = dfN.apply(_driver, axis=1)
    disagreements = (dfN.sort_values("gap", key=lambda s: s.abs(), ascending=False)
                     .head(25).reset_index(drop=True)
                     .rename(columns={"Position": "position", "Net weight": "weight"})
                     [["position", "ticker", "issuer", "Marginal Total VaR 99",
                       "Marginal Price VaR 99", "gap", "weight", "likely_driver"]])

    terms = {"specific_risk_dropped": t1 - t0, "specific_distribution": t2 - t1,
             "exposure_drift": t3 - t2, "coverage": t4 - t3}
    steps = [
        {"step": "T0", "measure": "Scenario VaR 99", "value": t0,
         "what_changes": "— (factor-only, today's exposures)"},
        {"step": "T1", "measure": "Total VaR 99", "value": t1,
         "what_changes": "+ Gaussian diagonal specific block"},
        {"step": "T2", "measure": "full-sim VaR (today's loadings + realized residuals)", "value": t2,
         "what_changes": "the Gaussian specific block is replaced by the REALIZED daily residual "
                         "paths (fat tails + residual correlation)"},
        {"step": "T3", "measure": "Price VaR on covered names", "value": t3,
         "what_changes": "today's fixed loadings are replaced by each day's OWN loadings "
                         "(r_t = L_i(t)·f_t + u_i,t exactly, on covered names)"},
        {"step": "T4", "measure": "Price VaR 99", "value": t4,
         "what_changes": "names with prices but no loadings are added"},
    ]
    return {
        "date": d, "manager": manager, "set": set_, "alpha": alpha,
        "steps": steps, "terms": terms,
        "coverage": {
            "at_tail": coverage_at_tail,
            "never_priced_weight": never_priced_weight,
            "never_priced_names": [{"position": p, "ticker": tick.get(p, p),
                                    "weight": float(held[p])} for p in never_priced[:25]],
            "added_by_coverage_weight": added_weight,
            "added_by_coverage_names": [{"position": p, "ticker": tick.get(p, p),
                                         "weight": float(held[p])} for p in added_by_coverage[:25]],
            # the day-by-day series for the UI sparkline — an API-served reshape of the same
            # (dates, priced_mask, weight) arrays above, the /backtest-/drawdown vector-unpack
            # idiom; not a cube-native Day-path measure (see barra_factor_risk_cube.py's Price
            # family comment on the scoped-down Day path).
            "series": {"dates": [str(pd.Timestamp(dt).date()) for dt in dates],
                      "coverage": [float(v) for v in cov_series]},
        },
        "disagreements": _records(disagreements, reset=False),
        "verification": {"price_var_99_numpy": t4_ref, "diff": abs(t4 - t4_ref),
                         "coverage_at_tail_numpy": cov_ref,
                         "coverage_diff": abs(coverage_at_tail - cov_ref)},
        "note": ("Five numbers, four terms, fixed order (docs/price-var-plan.md): T1-T0 specific "
                 "risk DROPPED (the Gaussian block itself, not yet measured against reality); "
                 "T2-T1 specific DISTRIBUTION (fat tails + realized residual correlation — "
                 "correlated residuals are the missing-factor signal, cf. /pnl_attribution/"
                 "linkage's breach_comovement); T3-T2 EXPOSURE DRIFT (today's fixed loadings vs "
                 "each day's own — rotation vs re-pricing, cf. /drift); T4-T3 COVERAGE (priced "
                 "names the model doesn't cover). Terms sum to T4-T0 exactly by construction."),
    }


@app.get("/var_bridge")
async def var_bridge(date: str | None = None, manager: str | None = None, book: str | None = None,
                     set: str = "HistFull", alpha: float = 0.01):
    """The Model-vs-Price bridge (docs/price-var-plan.md): T0 Scenario VaR 99 -> T1 Total VaR 99
    -> T2 full-sim (realized residuals) -> T3 Price VaR on covered names -> T4 Price VaR 99, the
    four sequential differences, coverage on the portfolio's own Price-VaR tail day, the per-name
    disagreement table, and a numpy verification twin of T4 (like /contributions)."""
    manager = _coalesce_manager(manager, book)
    return await run_in_threadpool(_var_bridge_result, date, manager, set, alpha)


# ---- factor portfolio inspector (ch 07: a factor return IS a portfolio return, f̂ = Pr) ----

@app.get("/factor_portfolio")
async def factor_portfolio(factor: str, date: str | None = None):
    """Reconstruct the pure factor portfolio for one factor at a date: P = (X'W²X)⁻¹X'W² over
    the fit cross-section (funnel survivors when the artifact exists, else all names), W the
    builder's sqrt-cap proxy exp(Size/4). Row k has unit exposure to its own factor and ~zero
    to every other (PX = I) — the top longs/shorts, gross leverage, and the purity check."""
    def run():
        f = S["frames"]; exp = f["exposures"]
        d0 = _snap_exposure_date(exp, date)
        Ld = (exp[exp["Date"] == d0]
              .pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first"))
        styles = [c for c in Ld.columns if c != "Market" and not str(c).startswith("Ind:")]
        ind_cols = [c for c in Ld.columns if str(c).startswith("Ind:")]
        keep = Ld[styles].notna().sum(axis=1) >= 6            # mirror the builder's floor
        Ld = Ld[keep]
        fit_idx = Ld.index
        approx_fit = "all coverage names"
        if _uf.ARTIFACT.exists():
            fn = pd.read_parquet(_uf.ARTIFACT, columns=["month", "position", "survived"])
            surv = set(fn[(pd.to_datetime(fn["month"]) == d0) & (fn["survived"] == True)]  # noqa: E712
                       ["position"].dropna())
            cand = Ld.index.intersection(surv)
            if len(cand) >= 30:
                fit_idx, approx_fit = cand, "funnel survivors (≈ estimation universe)"
        Xd = Ld.loc[fit_idx, styles].fillna(0.0)
        cols = [c for c in styles if Xd[c].std() > 0.05]      # builder's degeneracy guard
        if "Size" not in cols:
            raise HTTPException(404, "Size missing from the fit — cannot form the WLS weights")
        W2 = np.exp(Xd["Size"].values / 2.0)                  # (exp(Size/4))² — the builder's W²
        # Industry dummies enter under the builder's Barra constraint (Σ c_s·f_s = 0, reference
        # sector substituted out) — raw dummies + the intercept are EXACTLY collinear on a fit
        # cross-section where every name has a sector, and the solve error lands in the Market
        # row (observed: Market self-exposure −0.11 pre-fix). The reference sector's portfolio
        # is implied, not a free row, so it is not requestable here.
        Dd = (Ld.loc[fit_idx, ind_cols].fillna(0.0).values
              if ind_cols else np.zeros((len(Xd), 0)))
        cmass = (W2[:, None] * Dd).sum(axis=0)
        ok_c = cmass > 0
        inds = [c for c, k_ in zip(ind_cols, ok_c) if k_]
        Dd, cmass = Dd[:, ok_c], cmass[ok_c]
        if len(inds) >= 2:
            iref = int(np.argmax(cmass))
            nr = [j for j in range(len(inds)) if j != iref]
            Dt = Dd[:, nr] - np.outer(Dd[:, iref], cmass[nr] / cmass[iref])
            ind_names = [inds[j] for j in nr]
        else:
            iref, Dt, ind_names = None, np.zeros((len(Xd), 0)), []
        X = np.column_stack([np.ones(len(Xd)), Xd[cols].values, Dt])
        names = ["Market"] + cols + ind_names
        if factor not in names:
            detail = (f"reference sector at {d0.date()} — its return is implied by the "
                      f"constraint, no free portfolio row" if iref is not None
                      and factor == inds[iref] else f"factor not in the fit at {d0.date()}")
            raise HTTPException(400, f"{detail}: {factor}")
        try:
            P = np.linalg.solve(X.T @ (X * W2[:, None]), (X * W2[:, None]).T)
        except np.linalg.LinAlgError:
            raise HTTPException(500, "singular fit cross-section")
        p = P[names.index(factor)]
        expo = p @ X                                          # should be e_k (PX = I)
        k = names.index(factor)
        cross = float(np.max(np.abs(np.delete(expo, k))))
        tk = _ticker_map()
        order = np.argsort(p)
        pos_list = list(Xd.index)
        def side(idx):
            return [{"ticker": tk.get(pos_list[i], pos_list[i]), "weight": float(p[i])}
                    for i in idx if abs(p[i]) > 1e-9]
        return {
            "factor": factor, "date": _clean(d0),
            "fit_universe": approx_fit, "n_names": int(len(Xd)),
            "gross_leverage": float(np.abs(p).sum()), "net": float(p.sum()),
            "self_exposure": float(expo[k]), "max_cross_exposure": cross,
            "longs": side(order[::-1][:10]), "shorts": side(order[:10]),
            "note": ("The regression dual made visible: this long-short portfolio's daily return "
                     "IS (approximately) the published factor return. Reconstruction — the "
                     "production fit used the builder's internal estimation flag and per-day "
                     "return availability, so weights are approximate; the PX = I purity check "
                     "(self exposure 1, cross ~0) is exact for this cross-section. High gross "
                     "leverage is the ch-07 purity price."),
        }
    return await run_in_threadpool(run)


# ---- residual explorer (ch 13's question: what can't the model explain, name by name) ----

@app.get("/pnl_attribution/names")
async def pnl_attribution_names(frm: str | None = Query(None, alias="from"), to: str | None = None,
                                manager: str | None = None, book: str | None = None,
                                top: int = Query(12, ge=3, le=50)):
    """The specific PnL name by name over the window: top winners and losers by |specific|, each
    with sign persistence (share of consecutive same-sign months — a real edge or a stale 13F
    reads persistent; noise mean-reverts) and the share of months positive."""
    manager = _coalesce_manager(manager, book)
    def run():
        art_path, mism = _resolve_artifact(_pnl, "pnl_attribution", manager)
        if mism is not None:
            return mism
        art = _attr_artifact(art_path)
        _c, lo, hi = _attr_window(art, frm, to)
        na, panel = _name_attr(lo, hi, manager, monthly=True)
        if na.empty:
            raise HTTPException(404, "no attribution rows in the window")
        tk = _ticker_map()
        ranked = na.reindex(na["specific_pnl"].abs().sort_values(ascending=False).index)
        rows = []
        for p, r in ranked.head(top * 2).iterrows():
            m = panel[p].dropna() if p in panel.columns else pd.Series(dtype=float)
            m = m[m != 0.0]
            sgn = np.sign(m.to_numpy())
            persist = (float((sgn[1:] == sgn[:-1]).mean()) if len(sgn) > 3 else None)
            rows.append({"ticker": tk.get(p, p), "position": p,
                         "factor_pnl": float(r["factor_pnl"]),
                         "specific_pnl": float(r["specific_pnl"]),
                         "realized": float(r["realized"]),
                         "months": int(len(m)), "sign_persistence": persist,
                         "hit_rate": (float((m > 0).mean()) if len(m) else None)})
        winners = [r for r in rows if r["specific_pnl"] > 0][:top]
        losers = [r for r in rows if r["specific_pnl"] < 0][:top]
        return {
            "from": str(lo.date()), "to": str(hi.date()), "manager": manager,
            "winners": winners, "losers": losers,
            "note": ("Specific = the part of each name's PnL the factors don't explain, on the "
                     "as-of monthly weights (the cube convention). sign_persistence is the share "
                     "of consecutive months with the same specific sign: ≈0.5 = memoryless "
                     "(re-underwritten bets), well above = a persistent unexplained driver — "
                     "a real edge, a stale 13F weight, or a missing factor."),
        }
    return await run_in_threadpool(run)


# ============================================================================ analysis (LLM)
# A written risk-manager read of ONE view. The model is the plain Anthropic Messages API with
# NO tools: it receives the view's tidy numbers as text and returns prose. It has no access to
# the cube, the filesystem, or any tool, and cannot re-query — the only thing it can do is read
# the figures we hand it. All domain grounding lives in ANALYST_SYSTEM below.

# The shared persona for EVERY LLM feature in this service (see CLAUDE.md): the voice and
# doctrine of the desk's senior quantitative risk manager, modelled on the It's Just Beta
# primer's editorial discipline and the reviewer's documented corrections. Prepended to every
# system prompt; new LLM endpoints must start from CHRIS_VOICE.
CHRIS_VOICE = """\
VOICE AND DOCTRINE — this governs everything you write here.

You write as the desk's senior quantitative risk manager: two decades running factor risk at
major banks and multi-strategy funds, trained in the Fama tradition, author of an equity
factor-model primer. Emulate the discipline and the tone. Never sign a name or claim to be a
specific person.

Tone:
- Plain declarative sentences. Short. No hedging filler ("it seems", "arguably", "somewhat"),
  no hype, no exclamation marks, no emoji.
- Dry and occasionally aphoristic — one compressed line that lands ("most of this portfolio is
  one bet on the market") beats a paragraph.
- Cite the figure next to every claim. A sentence without a number is a candidate to cut.
- If the honest read is one line, write one line. Never pad.

Doctrine — the lens for every read:
- The risk team's job is to understand ALL the risks the portfolio is taking, not to avoid losses.
  Money made or lost on a bet you didn't know you had is the same failure; the direction was
  luck. An unexplained GAIN gets investigated with the same energy as an unexplained loss.
- It's usually just beta. Before crediting skill or blaming stock-picking, check what the
  factor block explains. "Specific" means what THIS model's factors don't span —
  model-conditional, not alpha by definition.
- Exposure is not risk contribution. A large loading on a quiet factor can matter less than a
  small loading on a wild one; allocate blame with CTV/CTR, not raw exposures.
- Correlated residuals across names are a missing factor until proven otherwise.
- Statistical humility: t = IR·√T — one good quarter is noise. Calibration (bias statistics,
  exceedance counts) outranks anecdote. Prefer the cheap, readable statistic to the clever one.
- Artifacts before alarms: a breach can be frozen-band arithmetic (weight or exposure migrated
  mid-window) rather than a risk event — say which it is before recommending action.
- Consistency beats sophistication. Quote the model and the convention alongside the number.
"""

ANALYST_SYSTEM = CHRIS_VOICE + """
You are writing a short commentary on one view from a Barra-style
equity factor-risk model. The portfolio is the Soros Fund Management 13F holdings, run as a
long-only weight overlay; monthly calendar from 2016 to the latest build.

The model has two risk blocks: a linear FACTOR P&L block and a diagonal SPECIFIC (idiosyncratic)
block. Read the measures as follows:
- Numbers are fractions of portfolio value. 0.035 means 3.5%. VaR/ES/vol are losses, reported
  positive.
- Net exposure: aggregated factor loading (weight x loading). Market carries a loading of 1.0 per
  name, so a fully invested portfolio has ~unit Market exposure.
- Scenario VaR 95/97.5/99: loss at that confidence. Scenario ES 97.5/99: expected shortfall — the
  mean loss in the tail beyond VaR (coherent; Basel FRTB's VaR replacement). Scenario worst loss:
  the single worst scenario. Scenario PnL vol: dispersion of scenario P&L. Scenario mean PnL: ~0
  for historical sets, the shock P&L for hypotheticals.
- Specific vol / Specific variance: the diagonal idiosyncratic block. Total VaR 99 / Total ES 97.5:
  factor risk combined in quadrature with the idiosyncratic tail — a HOUSE COMPOSITE, kept for
  continuity. The desk's REFERENCE risk number is model vol σ = √(x'Fx + w'Δw) with its
  factor/specific split; the LIMITS are written on Scenario VaR 99 / ES 97.5 (Kupiec-backtested).
  Quote Total VaR only when the view offers nothing better.
- Marginal Scenario VaR 99 / Marginal Scenario ES 97.5 / Marginal Total VaR 99: a member's ADDITIVE
  contribution to the portfolio number (the contributions sum to the portfolio total). "% of ..."
  is that share, summing to 100%. Incremental VaR: the risk RELEASED by removing a member —
  diversification-aware, NOT additive (it does not sum to the portfolio total), so there is no
  "% of" for it.
- Marginal Model vol: the member's EULER contribution to portfolio model vol (per name this IS the
  ch-09 CTR = w·(Σw)/σ); sums exactly to Model vol; read it in by-NAME views (by Factor the
  specific block fans out). Incremental Model vol: the vol released by removing the member —
  sub-additive like Incremental VaR, no "% of".
- VaR sensitivity: per-unit dVaR/dexposure. Risk HHI: Herfindahl index of each name's share of
  portfolio Total VaR — 1/N for an evenly diversified portfolio up to 1.0 for a single name; 1/HHI
  ~ the effective number of independent risk bets.
- drawdown (separate `drawdown` block, not a pivot measure): max peak-to-trough of the portfolio's
  cumulative P&L if the *current* portfolio had been held over the scenario set's daily path — a
  path-dependent lens VaR/ES cannot see. `max_drawdown` is a negative fraction; `longest_underwater_obs`
  is the longest run (trading days) below a prior peak; `recovered` says whether it climbed back.
- Factor contribution / Specific PnL / Realized PnL: REALIZED monthly PnL attribution (not risk).
  Additive; Realized = Σ factor contributions + Specific. FORWARD-month convention: the value at
  Date d0 is the PnL over the month AFTER d0. Specific PnL is per-name (it fans out by Factor —
  read it in by-name or portfolio views). No ScenarioSet needed.
- Price VaR/ES family (Price VaR 95/97.5/99, Price ES 97.5/99, Price worst loss, Price mean PnL,
  Price PnL vol, Marginal/Incremental Price VaR 99, Price coverage at tail): the MODEL-FREE twin of
  the Scenario family — historical simulation on raw daily STOCK returns, no factor model at all.
  Reads a PriceSet context (its own switch hierarchy, mirroring ScenarioSet: HistFull + Evt:*
  only — Hypo:* sets don't apply). The `/var_bridge` endpoint (not a pivot view) explains the gap
  between Total VaR 99 (model) and Price VaR 99 (price) in four terms, in order: specific risk
  dropped (the Gaussian block itself), specific distribution (realized fat tails + residual
  correlation — the strongest missing-factor signal when large), exposure drift (today's loadings
  vs each day's own — rotation vs re-pricing, see /drift), and coverage (priced names the model
  doesn't span). If a `bridge` block is present in the payload, read it in that order and lead
  with whichever term is largest.

Scenario sets (the shock source):
- HistFull: full historical simulation. Evt:* : a past window replayed (COVID2020, Rates2022,
  Selloff2018). Hypo:* : hand-set sigma shocks (ValueRotation, RiskOff, MomentumCrash).
- KEY CAVEAT: every name shares the uniform Market loading of 1.0, so in any set that contains real
  market moves (HistFull, the Evt:* replays), Market dominates portfolio risk (~95%) and risk is as
  diversified as the weights — high effective-name count, low HHI. The Hypo:* shocks set the Market
  move to zero and bump only style factors, so risk collapses onto the few names carrying those
  tilts — concentration (HHI) jumps sharply. If you see a Hypo:* set reading far more concentrated
  than the historical sets, that is the mechanism, not a data problem.

Hard rules:
- Reason ONLY from the numbers in the user's payload. Cite the figures you reference. Never invent a
  position, issuer, date, or value that is not in the data.
- If a `limits` block is present, LEAD with it: call out every breach (status "breach") by name with
  its value vs limit, then any amber warnings. If everything is green, say so in one line. These are
  the desk's hard limits — they outrank anything else in the view.
- If a `drawdown` block is present, work it into the read: a deep `max_drawdown` or a long
  `longest_underwater_obs` is a path risk the VaR/ES numbers don't show — name the trough date and
  whether it recovered. Note it's a constant-portfolio what-if over history, not a live track record.
- If a `pnl_attribution` block is present (trailing-12m realized attribution): say where the return
  came from — factor bets vs stock-picking (`specific_share`) — and read the specific IR plainly
  (>0.3 reliable alpha, ~0 noise, negative destroys value). Price-only, dividends excluded.
- If a `hypothetical` block is present, the WHOLE VIEW is priced under those what-if trades
  and/or factor shocks on a transient scenario branch — SAY SO IN THE HEADLINE, and read the
  numbers as the hypothetical portfolio, not the held one. Limits/drawdown/attribution context
  blocks remain the BASE portfolio.
- If a `warning` field is present, the requested scenario measures had no single-ScenarioSet context
  and those cells are blank — say so plainly rather than guessing. Scenario risk is only meaningful
  sliced to one ScenarioSet.
- Known model limits to flag when relevant: the universe is capped at 250 names; Country is stubbed
  to "US"; ~5 names fall back to "Unknown" sector. Do not over-read precision.

Output: tight GitHub-flavoured markdown for a risk desk. Lead with a one-line headline read of what
the view shows. Then 3-5 bullets of what is notable in THESE numbers. Then a short "So what" — the
risk-management implication (concentration, tail, what to watch or cut). No preamble, no restating
the question, no filler. Write plainly: direct, short sentences."""


def _anthropic_key() -> str | None:
    """The API key: the process env wins; otherwise read ONLY this one var out of the repo .env.
    (The service deliberately does NOT source the whole .env — its ATOTI_LICENSE path is broken
    and would break the cube — so we extract just the key here.)"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    envf = pathlib.Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in envf.read_text().splitlines():
            s = line.strip()
            if s.startswith("ANTHROPIC_API_KEY=") and not s.startswith("#"):
                return s.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _anthropic():
    """Lazily build and cache the Anthropic client. 502 if no key, so a missing key reads as a
    clean UI message instead of a crash, and the rest of the API is unaffected."""
    if "anthropic" not in S:
        key = _anthropic_key()
        if not key:
            raise HTTPException(502, "ANTHROPIC_API_KEY not set — view analysis is unavailable.")
        S["anthropic"] = anthropic.Anthropic(api_key=key)
    return S["anthropic"]


_ANALYSIS_HITS: deque = deque()
_ANALYSIS_RATE = (20, 60.0)        # <=20 analyses / 60s — backstop, the endpoint is public-facing


def _rate_limit() -> None:
    now = time.monotonic()
    n, win = _ANALYSIS_RATE
    while _ANALYSIS_HITS and now - _ANALYSIS_HITS[0] > win:
        _ANALYSIS_HITS.popleft()
    if len(_ANALYSIS_HITS) >= n:
        raise HTTPException(429, "analysis rate limit reached — wait a moment and retry.")
    _ANALYSIS_HITS.append(now)


class AnalysisBody(BaseModel):
    rows: str = ""
    cols: str = ""
    measures: str = ""
    filters: str | None = None     # same JSON {dimension: [members]} /pivot takes
    totals: bool = False
    name: str | None = None        # view name, for the prompt header
    notes: str | None = None       # optional desk context typed by the user
    whatif: str | None = None      # same JSON trades /pivot takes — commentary on a hypothetical
    shocks: str | None = None      # same JSON sigmas /pivot takes


@app.post("/analysis")
async def analysis(body: AnalysisBody):
    """Streamed risk-analyst commentary on ONE view. Runs the SAME guarded pivot the UI renders,
    then hands only those tidy numbers to the Messages API (no tools) for a written read. Streams
    markdown back. Off-allowlist dims/measures are rejected by _validate_pivot, identical to /pivot;
    the model gets the figures and nothing else."""
    _rate_limit()
    rlist, clist, mlist = _csv(body.rows), _csv(body.cols), _csv(body.measures)
    fdict = _parse_filters(body.filters, None, None)
    _validate_pivot(rlist, clist, mlist, fdict)
    wtrades, shk = _parse_hypo(body.whatif, body.shocks, fdict)
    client = _anthropic()          # raise the 502 BEFORE the (slow) cube query if there's no key
    if wtrades or shk:
        data = await run_in_threadpool(_hypothetical_pivot, rlist, clist, mlist, fdict,
                                       bool(body.totals), wtrades, shk)
    else:
        data = await run_in_threadpool(_pivot_result, rlist, clist, mlist, fdict, bool(body.totals))
    # desk-limit status + drawdown for the view's own date/set/manager (so the model can lead with a
    # breach and cite the path-drawdown lens VaR/ES miss). Headline only — the dd path is dropped.
    lim = dd = None
    ldate = (fdict.get("Date") or [None])[0]
    lmanager = (fdict.get("Manager") or fdict.get("Book") or ["Soros"])[0]
    if ldate:
        lset = (fdict.get("ScenarioSet") or [_load_limits().get("scenario_set", "HistFull")])[0]
        try:
            lim = await run_in_threadpool(_limits_result, ldate, lset, lmanager)
        except Exception:
            lim = None
        ddset = (fdict.get("ScenarioSet") or ["HistFull"])[0]
        try:
            d = await run_in_threadpool(_drawdown_result, ldate, ddset, lmanager)
            dd = ({k: d[k] for k in ("set", "max_drawdown", "peak_date", "trough_date",
                                     "recovered", "longest_underwater_obs")}
                  if d.get("status") == "ok" else None)
        except Exception:
            dd = None
    attr = await run_in_threadpool(_attr_headline)
    payload = json.dumps({
        "view": body.name or "(unnamed view)",
        "hypothetical": ({"trades": wtrades, "shocks": shk} if (wtrades or shk) else None),
        "filters": fdict, "rows": rlist, "cols": clist, "measures": mlist,
        "warning": data.get("warning"),
        "limits": lim,
        "drawdown": dd,
        "pnl_attribution": attr,
        "records": data["records"],
        "margins": {k: data[k] for k in ("per_row", "per_col", "grand") if k in data},
        "desk_notes": body.notes or "",
    }, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-5", max_tokens=4000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": ANALYST_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],   # cached: stable across views
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                yield from stream.text_stream
        except anthropic.APIError as e:                              # mid-stream: 200 already sent
            yield f"\n\n_[analysis failed: {e.__class__.__name__}]_"
    return StreamingResponse(gen(), media_type="text/markdown")


OVERVIEW_SYSTEM = CHRIS_VOICE + """
You are writing the MORNING RISK SUMMARY of the whole portfolio — the read a risk manager gives
the desk from the monitor screen. The portfolio is the Soros Fund Management 13F holdings, run
long-only as a weight overlay; monthly calendar from 2016 to the latest build, Barra-style factor
model (linear factor block + diagonal specific block). Numbers are fractions of portfolio value
unless marked.

The payload mirrors the daily loop — read it in this order:
1. `limits` — the hard desk limits. LEAD with any breach (value vs limit), then ambers. All
   green = one line.
2. `risk` + `variance_split` — the decomposition. `model_vol_1d` (σ = √(x'Fx + w'Δw)) is the
   REFERENCE risk number — lead the risk read with it and its factor_share split; scenario
   VaR/ES are the limit metrics; total_var_99 is a legacy house composite, quote it only
   against its limit history. top_ctv are contributions to variance (negative = hedges the
   portfolio); top5_ctr_share is the 5 largest names' share of Total VaR.
3. `reconcile` — realized PnL vs the start-of-period risk bands (the risk-understood check).
   `flagged` rows/positions are outside their base band; each carries a driver read: an
   exposure/weight migration is a band ARTIFACT (frozen at T), a factor_move is systematic,
   a specific_move is a stock event, hidden_beta means the loading is suspect. `comovement`
   says whether the idiosyncratic breaches share one driver (missing-factor signal).
4. `calibration_and_backtest` — is the risk forecast itself right: Kupiec verdict + exception
   rate vs expected, and the trailing attribution headline (factor vs specific, specific IR).
5. `dq` — data trust; mention only if not clean.

Hard rules:
- Reason ONLY from the payload. Cite the figures. Never invent a name, date, or value.
- Distinguish artifacts from risk events before recommending anything.
- End with "**Do next:**" — the one or two most valuable actions, drawn from the numbers.

Output: tight GitHub-flavoured markdown. One-line headline first (the state of the portfolio in
a sentence). Then short sections following the loop order. 150–300 words unless a breach demands
more."""


class OverviewAnalysisBody(BaseModel):
    date: str | None = None
    manager: str | None = None
    book: str | None = None
    set: str | None = None         # scenario set for the limits read
    notes: str | None = None


@app.post("/overview/analysis")
async def overview_analysis(body: OverviewAnalysisBody):
    """Streamed morning-summary commentary on the WHOLE portfolio — the Overview monitor narrated
    in the desk's risk-manager voice (CHRIS_VOICE). Assembles the same numbers the Overview shows
    (limits, Euler decomposition, reconcile verdicts + drivers, backtest, attribution headline,
    DQ) and hands them to the Messages API with no tools."""
    manager = _coalesce_manager(body.manager, body.book)
    _rate_limit()
    client = _anthropic()
    d = body.date or _latest_date()
    scen = body.set or _load_limits().get("scenario_set", "HistFull")

    # The five input blocks are independent of each other and of the reconcile below, and each
    # is a cube query or a frame pass — so they run CONCURRENTLY (api_bench 2026-08-21, TODO
    # item 3; every one of them was already fast on its own, this is the assembly). Fragments are
    # merged in the original order, so the payload the model sees is key-for-key what it was.
    def _f_limits():
        try:
            lim = _limits_result(d, scen, manager)
            return {"limits": {"status": lim["status"],
                               "checks": [{k: c[k] for k in ("name", "value", "warn", "limit",
                                                             "status")} for c in lim["checks"]]}}
        except Exception:
            return {"limits": None}

    def _f_risk():
        try:
            L, w, s, R = _manager_inputs(d, manager)
            risk = _risk_from_weights(w, L, s, R)
            F = np.cov(R, rowvar=False)
            e = _euler_contributions(w.to_numpy(), L.to_numpy(), F, s.to_numpy())
            tv = e["factor_var"] + e["specific_var"]
            order = np.argsort(-np.abs(e["ctv"]))
            return {"risk": {k: risk[k] for k in ("model_vol_1d", "scenario_var_99", "es_975",
                                                  "specific_vol", "top5_ctr_share", "gross", "net",
                                                  "total_var_99")},
                    "variance_split": {
                        "factor_share": (e["factor_var"] / tv) if tv > 0 else None,
                        "vol_1d": e["sigma"],
                        "top_ctv": [{"factor": str(L.columns[i]),
                                     "pct_of_variance": float(e["ctv"][i] / tv)}
                                    for i in order[:6]]}}
        except Exception:
            return {"risk": None, "variance_split": None}

    def _f_backtest():
        try:
            bt = _backtest_result(d, "HistFull", manager, 0.01, 250, "fhs", 0.94)
            return {"calibration_and_backtest": (
                {k: bt.get(k) for k in ("kupiec_reject", "rate", "exceptions", "expected",
                                        "tested")} if bt.get("status") == "ok" else None)}
        except Exception:
            return {"calibration_and_backtest": None}

    def _f_dq():
        try:
            checks = barra_dq_checks.run(S["frames"])
            summ = {k: sum(1 for c in checks if c["level"] == k) for k in ("PASS", "WARN", "FAIL")}
            return {"dq": {"status": ("fail" if summ["FAIL"] else "warn" if summ["WARN"]
                                      else "pass"), "summary": summ}}
        except Exception:
            return {"dq": None}

    def collect():
        out: dict = {"as_of": d, "manager": manager, "scenario_set": scen}
        blocks = [_f_limits, _f_risk, _f_backtest,
                  lambda: {"pnl_attribution_t12m": _attr_headline()}, _f_dq]
        with ThreadPoolExecutor(max_workers=len(blocks)) as ex:
            for frag in ex.map(lambda fn: fn(), blocks):
                out.update(frag)
        return out

    # reconcile (risk↔PnL) — reuse the linkage route's computation, trimmed to verdicts + drivers.
    # It shares nothing with collect(), so the two run concurrently.
    lk_task = asyncio.ensure_future(pnl_attribution_linkage(
        T=None, horizon=3, manager=manager, vol_mult=1.25, rho=0.75, min_weight=0.001))
    try:
        payload = await run_in_threadpool(collect)
    except BaseException:
        lk_task.cancel()                     # never leave the reconcile task orphaned
        raise
    try:
        lk = await lk_task
        def trim(r):
            o = {"name": r["name"], "z": r.get("z"), "verdict": r["verdict"]}
            if r.get("driver"):
                o["driver"] = {"kind": r["driver"]["kind"], "text": r["driver"]["text"]}
            return o
        payload["reconcile"] = {
            "window": f"{lk['T']} → {lk['to']}",
            "manager_total": trim(lk["manager_total"]),
            "flagged": [trim(r) for r in lk["rows"] if r["verdict"] != "within"],
            "positions_flagged": [trim(p) for p in lk["positions"] if p.get("driver")][:8],
            "comovement": (lk.get("breach_comovement") or {}).get("text"),
        }
    except Exception:
        payload["reconcile"] = None
    payload["desk_notes"] = body.notes or ""

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-5", max_tokens=3000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": OVERVIEW_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            ) as stream:
                yield from stream.text_stream
        except anthropic.APIError as e:
            yield f"\n\n_[analysis failed: {e.__class__.__name__}]_"
    return StreamingResponse(gen(), media_type="text/markdown")


TRENDS_SYSTEM = CHRIS_VOICE + """
You are writing a short read of the portfolio's RISK TRENDS — monthly time series over the whole
calendar (2016 → the latest build) for one scenario set. The portfolio is the Soros 13F overlay
on a Barra-style factor model. Numbers are fractions of portfolio value; VaR/ES/vol are 1-day
losses.

The payload:
- `risk_series` — monthly portfolio measures. `Model vol` (σ = √(x'Fx + w'Δw)) is the REFERENCE
  series — lead with it; Scenario VaR 99 / ES 97.5 are the limit metrics; Total VaR 99 is the
  legacy composite. The trend matters more than the level: where each series sits NOW vs its
  own history, and when it last shifted regime.
- `exposure_series` — net factor exposures by month (quarterly-sampled) + per-factor start/end.
  Exposure paths are the mandate made visible: a persistent move is the portfolio changing
  character, not noise. Whether drift is intentional (rotation) or re-pricing belongs to the
  drift attribution — flag the move here, don't guess the cause.
- `limits` — the standing desk limits, so a rising series can be read against its ceiling
  (headroom shrinking is the story before the breach is).

Hard rules:
- Reason ONLY from the payload; cite figures WITH their dates ("VaR peaked 2020-03 at 6.1%").
- Name the regimes you can see (a spike window, a quiet stretch, a step-change) by date range.
- Say where each headline series is now relative to its history (near its lows / median / highs)
  and its direction over the trailing year.
- Call out the factor exposures whose paths moved most since 2021, with start → end values.

Output: tight GitHub-flavoured markdown. One-line headline (what the trend history says about
today's portfolio). Then short sections: risk trend, exposure drift, headroom. End with
"**Watch:**" — the one or two series most likely to matter next. 120–250 words."""


class TrendsAnalysisBody(BaseModel):
    set: str = "HistFull"
    manager: str | None = None
    book: str | None = None
    notes: str | None = None


@app.post("/trends/analysis")
async def trends_analysis(body: TrendsAnalysisBody):
    """Streamed CHRIS_VOICE read of the risk-trends lens: the monthly portfolio-measure series and
    the factor-exposure paths, narrated — regimes, current level vs history, drift, headroom vs
    the desk limits. Same no-tools Messages-API pattern and rate limit as /analysis."""
    manager = _coalesce_manager(body.manager, body.book)
    _rate_limit()
    client = _anthropic()
    mgr_ts = await trends(set=body.set,
                          measures="Model vol,Scenario VaR 99,Scenario ES 97.5,"
                                   "Specific vol,Total VaR 99",
                          manager=manager)
    fac_ts = await trends(set=body.set, measures="Net exposure", by="Factor", manager=manager)

    def rnd(v):
        return round(v, 4) if isinstance(v, (int, float)) else v
    risk_series = [{k: rnd(v) for k, v in r.items()} for r in mgr_ts["records"]]
    # exposures: quarterly-sampled monthly paths per factor + start/end, to keep the payload lean
    fr: dict[str, list] = {}
    for r in fac_ts["records"]:
        fr.setdefault(str(r.get("Factor")), []).append(
            {"date": str(r.get("Date"))[:10], "x": rnd(r.get("Net exposure"))})
    exposure_series = {
        f_: {"start": pts[0], "end": pts[-1], "quarterly": pts[::3]}
        for f_, pts in fr.items() if pts
    }
    payload = json.dumps({
        "scenario_set": body.set,
        "manager": manager,
        "risk_series": risk_series,
        "exposure_series": exposure_series,
        "limits": _load_limits().get("manager", {}),
        "desk_notes": body.notes or "",
    }, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-5", max_tokens=3000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": TRENDS_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                yield from stream.text_stream
        except anthropic.APIError as e:
            yield f"\n\n_[analysis failed: {e.__class__.__name__}]_"
    return StreamingResponse(gen(), media_type="text/markdown")


# ============================================================================ pnl attribution (LLM)
# The PnL-attribution lens narrated: the Cariño-linked window split, the by-factor table, the
# residual RAG diagnostics, and the linkage verdicts — same no-tools Messages-API pattern.

PNLATTR_SYSTEM = CHRIS_VOICE + """
You are writing a short read of the portfolio's REALIZED PnL ATTRIBUTION over one window — the
Soros 13F overlay on a Barra-style factor model. Returns are fractions (0.12 = 12%). The parts
are Cariño-linked: factor contributions + specific sum to the geometric window return EXACTLY.

The payload:
- `headline` — realized geometric return, linked factor total, linked specific total. Specific is
  stock-selection money the factor block can't explain. Understand ALL of it: an unexplained GAIN
  gets investigated exactly like a loss — it is risk that happened to pay.
- `factors` — per factor: avg_exposure (portfolio's mean net loading over the window),
  cum_factor_return (what the factor itself did — portfolio-agnostic), contribution (the money,
  linked), pct_of_total, t_stat (mean daily contribution / SE — t = IR·√T humility: a big
  contribution with |t| < 2 is one good year, not proof; only |t| > 2 is a reliable flow).
  Read exposure-without-return (a tilt that paid nothing) and return-without-exposure (a factor
  that ran while the portfolio stood flat) as findings, not trivia.
- `residual_checks` — the RAG diagnostics on the specific stream (IR, realized/predicted specific
  vol, autocorrelation, residual-vs-factor regression, bias stats, residual HHI, hit rate).
  Correlated residuals = a missing factor. A red here outranks any contribution number.
- `linkage` — the risk↔PnL reconcile verdicts at T (factor rows + positions outside their ex-ante
  bands, with driver reads: exposure_migration is a band artifact, not an event; hidden_beta means
  suspect the loading, not the factor).
- `coverage` — priced share of the portfolio; name the unpriced weight if material.

Hard rules:
- Reason ONLY from the payload; cite the figure next to every claim. Never invent a name or value.
- It's usually just beta: if Market dominates the factor total, say so first and plainly.
- Exposure ≠ risk contribution ≠ PnL contribution — do not conflate them.
- Artifacts before alarms: check exposure_migration / coverage / thin-t before calling an event.

Output: tight GitHub-flavoured markdown. One-line headline (where the money came from and whether
to believe it). Short sections: factor flows (with t-stat humility), the specific stream (residual
verdicts), reconcile breaches worth a look. End with "**Do next:**" — the one or two checks a desk
risk manager would run first. 130–260 words."""


class PnlAttrAnalysisBody(BaseModel):
    frm: str | None = None
    to: str | None = None
    horizon: int = 3
    manager: str = "Soros"      # matches the other endpoints' own default; the UI always sends it
    notes: str | None = None


@app.post("/pnl_attribution/analysis")
async def pnl_attribution_analysis(body: PnlAttrAnalysisBody):
    """Streamed CHRIS_VOICE read of the PnL-attribution lens: the linked window split, factor
    table, residual RAG and linkage verdicts. Same no-tools pattern and rate limit as /analysis."""
    _rate_limit()
    client = _anthropic()          # 502 before the work if there's no key
    # NB internal calls must pass EVERY Query-defaulted param explicitly — a bare call would
    # receive the FastAPI Query objects, not their values (the /overview min_weight lesson)
    # The manager comes from the request: this route hardcoded "Soros" through 2026-08-24, so the
    # commentary described Soros's book no matter which manager the lens was showing.
    attr = await pnl_attribution(body.frm, body.to, manager=body.manager, by=None)
    resid = await pnl_attribution_residual(body.frm, body.to, manager=body.manager)
    link = await pnl_attribution_linkage(None, body.horizon, manager=body.manager,
                                         vol_mult=1.25, rho=0.75, min_weight=0.001)

    def rnd(v):
        return round(v, 5) if isinstance(v, (int, float)) else v
    payload = json.dumps({
        "manager": body.manager,
        "window": {"from": attr["from"], "to": attr["to"], "n_days": attr["n_days"]},
        "headline": {k: rnd(v) for k, v in attr["headline"].items()},
        "factors": [{k: rnd(v) for k, v in r.items()} for r in attr["factors"]],
        "coverage": {"mean_priced_share": rnd(attr["coverage"]["mean_priced_share"]),
                     "unpriced": attr["coverage"]["unpriced"][:5]},
        "residual_checks": [{k: rnd(v) for k, v in c.items()} for c in resid["checks"]],
        "residual_status": resid["status"],
        "linkage": {
            "T": link["T"], "to": link["to"],
            "factor_breaches": [
                {"name": r["name"], "z": rnd(r["z"]), "verdict": r["verdict"],
                 "driver": (r.get("driver") or {}).get("kind"),
                 "text": (r.get("driver") or {}).get("text")}
                for r in link["rows"] + [link["manager_total"]] if r["verdict"] != "within"],
            "position_breaches": [
                {"name": p["name"], "weight": rnd(p["weight"]), "z": rnd(p["z"]),
                 "verdict": p["verdict"], "driver": (p.get("driver") or {}).get("kind"),
                 "hidden_beta": (p.get("driver") or {}).get("hidden_beta"),
                 "text": (p.get("driver") or {}).get("text")}
                for p in link["positions"] if p["verdict"] != "within"],
            "breach_comovement": (link.get("breach_comovement") or {}).get("text"),
            "dust_excluded": link.get("dust_excluded"),
        },
        "desk_notes": body.notes or "",
    }, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-5", max_tokens=3000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": PNLATTR_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                yield from stream.text_stream
        except anthropic.APIError as e:
            yield f"\n\n_[analysis failed: {e.__class__.__name__}]_"
    return StreamingResponse(gen(), media_type="text/markdown")


# ============================================================================ what changed (QoQ, LLM)
# Step 9: diff this 13F filing against the prior and narrate the risk delta. The deterministic diff
# (/whatchanged) is positions in/out/resized + the factor-exposure drift decomposed with Phase 4's
# attribution + the portfolio risk delta from the what-if math (cube-consistent).
# /whatchanged/analysis hands that tidy diff to the Messages API (no tools, streamed) for a written
# read, like /analysis.

WHATCHANGED_SYSTEM = CHRIS_VOICE + """
You are writing a short "what changed" note between two consecutive
Soros 13F filings of a Barra-style equity factor-risk model. You receive only a tidy diff; reason
ONLY from it and cite the figures. Never invent a position, issuer, date, or value.

The payload has:
- positions: names that ENTERED (new), EXITED (dropped), or were RESIZED (weight change) between the
  `from` and `to` filings, with 13F weights (fractions of portfolio, 0.03 = 3%).
- exposure_attribution: the portfolio's net factor exposure (Σ weight·loading) before/after per
  style factor, and the drift Δ split into four sources that sum to Δ exactly —
  `src_entered`/`src_exited` (names rotated in/out = ROTATION), `src_reweighted` (held names
  resized), `src_loading_drift` (held names whose own loadings moved = RE-PRICING).
  Rotation-dominated drift is a deliberate tilt → the desk may update the BENCHMARK;
  loading-drift-dominated is market re-pricing → update the HEDGE.
- risk: portfolio Scenario VaR 99/97.5, ES 97.5/99, Specific vol, Total VaR 99, Risk HHI, gross/net
  — before vs after vs delta, computed on the full factor-return history (HistFull-equivalent, the
  Market factor included so these read as real long-equity portfolio risk). All are losses,
  positive.

Hard rules:
- LEAD with the single biggest change (a big new/dropped position, the factor that drifted most, or
  the largest risk move). Then 3-5 bullets of what's notable. Then a short "So what" for the desk.
- For the factor drift, say whether it looks intentional (rotation) or not (loading drift) and name
  the implied action (benchmark vs hedge) — but only where the attribution actually supports it.
- Cite weights and deltas. Tie risk moves back to the position/exposure changes that drove them.
- Write plainly: direct, short sentences, tight GitHub-flavoured markdown. No preamble."""


def _prior_filing_date(bpos: pd.DataFrame, d1: pd.Timestamp, by: dict | None = None):
    """The latest date strictly before d1 whose held-name set differs from d1's — i.e. the previous
    distinct 13F filing (the positions frame is monthly and flat between quarterly filings).

    `by` is an optional {Date: row positions into `bpos`} index (from `_manager_date_rows`):
    without it this re-scanned the whole frame once per candidate date walking backwards."""
    if by is None:
        by = {pd.Timestamp(k): v for k, v in bpos.groupby("Date").indices.items()}
    dates = sorted(d for d in by if d < d1)
    if not dates:
        return None
    col = bpos["Position"]

    def at(d):
        return frozenset(col.take(by[d]))       # positional take: never materialises the column
    cur = at(d1)
    for d in reversed(dates):
        if at(d) != cur:
            return d
    return dates[0]


def _whatchanged_result(date: str | None, prev: str | None, manager: str = "Soros") -> dict:
    f = S["frames"]; exp, pos, sec = f["exposures"], f["positions"], f["securities"]
    # every positions selection below rides the cached per-manager row index rather than masking
    # the 11.6M-row frame (api_bench 2026-08-21): same rows, same order.
    by = _manager_date_rows(manager)
    alldates = sorted(by)
    if not alldates:
        raise HTTPException(404, f"no positions for manager {manager}")
    d1 = max(d for d in alldates if d <= pd.Timestamp(date)) if date else alldates[-1]
    d0 = (max(d for d in alldates if d <= pd.Timestamp(prev)) if prev
          else _prior_filing_date(pos, d1, by))
    if d0 is None or d0 >= d1:
        raise HTTPException(400, "no prior filing before this date")

    issuer = dict(zip(sec["Position"], sec["Issuer"]))
    ticker = dict(zip(sec["Position"], sec["Ticker"]))
    p0, p1 = pos.iloc[by[d0]], pos.iloc[by[d1]]
    b0 = p0.set_index("Position")["Weight"]
    b1 = p1.set_index("Position")["Weight"]
    set0, set1 = set(b0.index), set(b1.index)

    def nm(p):
        return {"issuer": issuer.get(p, ""), "ticker": ticker.get(p, "")}
    entered = sorted(({**nm(p), "weight": float(b1[p])} for p in set1 - set0),
                     key=lambda r: -r["weight"])
    exited = sorted(({**nm(p), "weight": float(b0[p])} for p in set0 - set1),
                    key=lambda r: -r["weight"])
    resized = sorted(({**nm(p), "w0": float(b0[p]), "w1": float(b1[p]),
                       "delta": float(b1[p] - b0[p])}
                      for p in set0 & set1 if abs(float(b1[p] - b0[p])) > 0.005),
                     key=lambda r: -abs(r["delta"]))

    # factor-exposure attribution (Phase 4 machinery) — delta = sum of the four sources exactly.
    # portfolio_at has no Manager concept of its own, so it must be handed the REQUESTED MANAGER's
    # rows: on the multi-manager frames it was reading `pos` whole, and
    # `dict(zip(Position, Weight))` collapsed all 124 managers' rows to one arbitrary weight per
    # name — every manager returned the same (wrong) net exposure. Same fix /drift already
    # carries. Slicing exposures by date too: the attribution only ever reads the two dates.
    exp_by = _frame_rows_by("exposures", ("Date",))
    e0 = exp.iloc[exp_by[d0]] if d0 in exp_by else exp.iloc[:0]
    e1 = exp.iloc[exp_by[d1]] if d1 in exp_by else exp.iloc[:0]
    w0d, l0d = _ud.portfolio_at(e0, p0, d0)
    w1d, l1d = _ud.portfolio_at(e1, p1, d1)
    attr = _ud.decompose(w0d, l0d, w1d, l1d)
    x0, x1 = _ud.portfolio_exposure(w0d, l0d), _ud.portfolio_exposure(w1d, l1d)
    exposure = [{"factor": fc, "before": _clean(x0[fc]), "after": _clean(x1[fc]),
                 "delta": _clean(attr[fc]["delta"]),
                 **{f"src_{k}": _clean(attr[fc][k]) for k in _ud.SOURCES}}
                for fc in sorted(_ud.STYLE, key=lambda k: abs(attr[k]["delta"]), reverse=True)]

    # portfolio risk delta — cube-consistent (the what-if math), full factor-return history
    L0, wv0, s0, R = _manager_inputs(str(d0.date()), manager)
    L1, wv1, s1, _ = _manager_inputs(str(d1.date()), manager)
    r0, r1 = _risk_from_weights(wv0, L0, s0, R), _risk_from_weights(wv1, L1, s1, R)
    risk = {k: {"before": _clean(r0[k]), "after": _clean(r1[k]),
                "delta": _clean(r1[k] - r0[k]) if (r0[k] is not None and r1[k] is not None) else None}
            for k in r0}

    return {
        "manager": manager, "from": str(d0.date()), "to": str(d1.date()),
        "positions": {"entered": entered[:25], "exited": exited[:25], "resized": resized[:25],
                      "n_entered": len(set1 - set0), "n_exited": len(set0 - set1),
                      "n_before": len(set0), "n_after": len(set1)},
        "exposure_attribution": exposure, "risk": risk,
        "note": ("Factor drift is split into entered/exited (rotation) vs reweighted/loading_drift; "
                 "rotation-led changes are a deliberate tilt (→ benchmark), loading-drift-led are "
                 "re-pricing (→ hedge)."),
    }


@app.get("/whatchanged")
async def whatchanged(date: str | None = Query(None, description="the 'to' filing; default latest"),
                      prev: str | None = Query(None, description="the 'from' filing; default prior"),
                      manager: str | None = Query(None), book: str | None = Query(None)):
    """Deterministic quarter-over-quarter diff between two 13F filings: positions entered / exited /
    resized, the net factor-exposure drift attributed (rotation vs loading drift, Phase 4), and the
    portfolio risk delta (VaR/ES/HHI/specific vol, what-if math). Grounds /whatchanged/analysis."""
    manager = _coalesce_manager(manager, book)
    return await run_in_threadpool(_whatchanged_result, date, prev, manager)


class WhatChangedBody(BaseModel):
    date: str | None = None
    prev: str | None = None
    manager: str | None = None
    book: str | None = None
    notes: str | None = None


@app.post("/whatchanged/analysis")
async def whatchanged_analysis(body: WhatChangedBody):
    """Streamed risk-manager 'what changed' read between two filings. Computes the same deterministic
    diff /whatchanged returns, then hands only those tidy numbers to the Messages API (no tools) for a
    written read. Streams markdown. The model gets the diff and nothing else."""
    manager = _coalesce_manager(body.manager, body.book)
    _rate_limit()
    client = _anthropic()          # 502 before the work if there's no key
    diff = await run_in_threadpool(_whatchanged_result, body.date, body.prev, manager)
    payload = json.dumps({**diff, "desk_notes": body.notes or ""}, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-5", max_tokens=4000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": WHATCHANGED_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                yield from stream.text_stream
        except anthropic.APIError as e:
            yield f"\n\n_[analysis failed: {e.__class__.__name__}]_"
    return StreamingResponse(gen(), media_type="text/markdown")


# ============================================================================ Model vs Price (LLM)
# /var_bridge/analysis narrates the deterministic bridge above — same plain Messages-API, no-tools
# pattern as /whatchanged/analysis: compute the tidy bridge, hand it to the model, stream markdown.

BRIDGE_SYSTEM = CHRIS_VOICE + """
You are writing a short "Model vs Price" read for a Barra-style equity
factor-risk model. The MODEL prices the portfolio on a linear factor block + a Gaussian diagonal
specific block; the PRICE family prices the SAME portfolio on raw historical stock returns — no
model at all, pure historical simulation. You receive only the tidy bridge between them; reason
ONLY from it and cite the figures. Never invent a position, issuer, date, or value.

The payload has:
- `steps`: five numbers in a FIXED order, each changing ONE thing from the previous: T0 Scenario
  VaR 99 (factor-only) -> T1 Total VaR 99 (+ Gaussian specific) -> T2 full-sim VaR (the Gaussian
  specific block replaced by REALIZED daily residual paths) -> T3 Price VaR on covered names
  (today's fixed loadings replaced by each day's own — the model's own identity r_t = L_i(t)·f_t +
  u_i,t) -> T4 Price VaR 99 (names priced but not covered by the model are added). All are losses,
  positive fractions of portfolio value.
- `terms`: the four sequential differences (T1-T0, T2-T1, T3-T2, T4-T3) — they sum to T4-T0
  EXACTLY, by construction. `specific_risk_dropped` is the Gaussian specific block itself (not yet
  tested against reality). `specific_distribution` is fat tails + REALIZED RESIDUAL CORRELATION —
  large here is the missing-factor signal (correlated residuals across names that a single
  Gaussian diagonal block cannot represent). `exposure_drift` is today's loadings vs each day's
  own realized loadings — large here means the portfolio's exposures moved a lot over the window
  (rotation, if deliberate → update the benchmark; re-pricing, if not → update the hedge; the
  /drift endpoint has the rotation-vs-loading-drift split). `coverage` is priced names the model
  doesn't span (no factor loadings that date) — large here is a model BLIND SPOT, not a risk
  number to act on directly.
- `coverage`: `at_tail` is the weight share that was actually priced on the portfolio's own
  Price-VaR tail day (below 100% means part of the portfolio's worst day is imputed as zero
  return, understating that day); `never_priced_weight`/`never_priced_names` are held names with
  NO price history at all (contribute nothing to either VaR); `added_by_coverage_weight`/`_names`
  are held names with a price but no factor loading (the T3→T4 population change).
- `disagreements`: the largest per-name gaps between Marginal Total VaR 99 (model) and Marginal
  Price VaR 99 (price), with a `likely_driver` label — "coverage (...)" is an exact population
  fact; "specific_risk_dropped" and "exposure_drift_or_distribution" are a MAGNITUDE HEURISTIC on
  that one name, not a certainty — say so if you lean on it.
- `verification`: the numpy cross-check of T4 and the tail-day coverage; `diff`/`coverage_diff`
  should read near zero — flag only if they don't.

Hard rules:
- LEAD with which term is LARGEST and what that means (see the `terms` guidance above). If the
  gap is small end to end (T4 close to T0), say the model and the raw tape roughly agree — that is
  itself a useful, reportable read.
- If `specific_distribution` is the largest term, name it as the strongest missing-factor
  candidate the desk has and point at the residual-correlation diagnostics in /pnl_attribution.
- If `coverage.never_priced_weight` or `added_by_coverage_weight` is non-trivial (say, above a few
  percent), name the actual position(s) from the list — don't just cite the aggregate.
- Cite the top 2-3 disagreement names with their gap and driver label; qualify the heuristic label
  as noted above.
- Write plainly: direct, short sentences, tight GitHub-flavoured markdown. No preamble."""


class VarBridgeBody(BaseModel):
    date: str | None = None
    manager: str | None = None
    book: str | None = None
    set: str = "HistFull"
    alpha: float = 0.01
    notes: str | None = None


@app.post("/var_bridge/analysis")
async def var_bridge_analysis(body: VarBridgeBody):
    """Streamed 'Model vs Price' read of the deterministic bridge (/var_bridge) — same plain
    Messages-API, no-tools pattern as /whatchanged/analysis. The model gets the bridge and
    nothing else."""
    manager = _coalesce_manager(body.manager, body.book)
    _rate_limit()
    client = _anthropic()          # 502 before the work if there's no key
    bridge = await run_in_threadpool(_var_bridge_result, body.date, manager, body.set, body.alpha)
    payload = json.dumps({**bridge, "desk_notes": body.notes or ""}, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-5", max_tokens=4000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": BRIDGE_SYSTEM,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": payload}],
            ) as stream:
                yield from stream.text_stream
        except anthropic.APIError as e:
            yield f"\n\n_[analysis failed: {e.__class__.__name__}]_"
    return StreamingResponse(gen(), media_type="text/markdown")


# ---------------------------------------------------------------- Step 10: scoped Q&A drill-down
# The first LLM endpoint with a tool. The model gets EXACTLY ONE tool — query_cube — which is the
# /pivot allowlist behind _validate_pivot + _pivot_result. So it can pull its own slices to answer a
# free-text question, but it still cannot reach an off-allowlist dim/measure, the filesystem, the
# network, or any other tool. We run a manual agentic loop (not the SDK tool runner) because each
# tool call must go through the same guard the UI uses and the cube query is a slow synchronous call;
# the loop is bounded (ASK_MAX_ROUNDS) and each result is trimmed (ASK_MAX_RECORDS) to cap tokens.

ASK_MAX_ROUNDS = 8          # tool round-trips before we stop and let the model answer with what it has
ASK_MAX_RECORDS = 250       # rows handed back per query_cube call (the portfolio is ~105 names; this is slack)

QUERY_CUBE_TOOL = {
    "name": "query_cube",
    "description": (
        "Pull one slice of the Barra factor-risk cube — the SAME guarded pivot the dashboard renders. "
        "Returns tidy records: one row per cell with the row/col members and the requested measures.\n\n"
        "Allowed dimensions (rows/cols/filters keys): " + ", ".join(DIM_NAMES) + ".\n"
        "Allowed measures: " + ", ".join(MEASURE_NAMES) + ".\n\n"
        "Rules that mirror the dashboard:\n"
        "- `rows` and `measures` are required (at least one each); `cols` is optional.\n"
        "- `filters` is {dimension: [members]} — AND across dimensions, OR within one. Slice Date to a "
        "single month (e.g. \"2024-12-31\") and, for any scenario measure, slice ScenarioSet to ONE set "
        "(HistFull / Evt:* / Hypo:*) — scenario measures are blank without a single-ScenarioSet context.\n"
        "- For a per-day scenario path use rows [\"Day\", \"DayDate\"] (+ \"Sector\" to break it out) with "
        "measure \"PnL at day\" and filter DaySet to ONE set (same set names as ScenarioSet).\n"
        "- Off-allowlist names are rejected; read the error and retry with a valid name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": {"type": "string"},
                     "description": "dimensions on rows (>=1, from the allowed list)"},
            "cols": {"type": "array", "items": {"type": "string"},
                     "description": "dimensions on columns (optional)"},
            "measures": {"type": "array", "items": {"type": "string"},
                         "description": "measures (>=1, from the allowed list)"},
            "filters": {"type": "object",
                        "description": "{dimension: [members]} slicers; values are member strings"},
            "totals": {"type": "boolean",
                       "description": "add cube-computed margins (per_row/per_col/grand)"},
        },
        "required": ["rows", "measures"],
    },
}


def _run_query_cube(args: dict) -> dict:
    """Execute one query_cube tool call. Validation runs FIRST (no cube needed) and on failure
    returns an {"error": ...} dict — NOT a raise — so the model sees the message and can retry on
    a valid name instead of the loop dying. The records are capped to keep the context bounded."""
    rlist = [str(x) for x in (args.get("rows") or [])]
    clist = [str(x) for x in (args.get("cols") or [])]
    mlist = [str(x) for x in (args.get("measures") or [])]
    raw_f = args.get("filters") or {}
    fdict = _parse_filters(json.dumps(raw_f) if raw_f else None, None, None)
    try:
        _validate_pivot(rlist, clist, mlist, fdict)
    except HTTPException as e:
        return {"error": e.detail}
    res = _pivot_result(rlist, clist, mlist, fdict, bool(args.get("totals")))
    recs = res.get("records") or []
    if len(recs) > ASK_MAX_RECORDS:        # never silently drop — tell the model it was truncated
        res["records"] = recs[:ASK_MAX_RECORDS]
        res["truncated"] = f"showed {ASK_MAX_RECORDS} of {len(recs)} rows — narrow the slice for the rest"
    return res


ASK_SYSTEM = CHRIS_VOICE + """
You are answering a desk question about a Barra-style equity factor-risk
model. The portfolio is the Soros Fund Management 13F holdings, run as a long-only weight
overlay; monthly calendar, 2016 → the latest build.

You have ONE tool, `query_cube`, which pulls slices of the live cube (the allowed dimensions and
measures are listed in its description). You have nothing else — no filesystem, no web, no other tool,
and no figures beyond what query_cube returns. To answer, pull the slices you need, then write the read.

How to use the cube:
- Numbers are fractions of portfolio value. 0.035 means 3.5%. VaR/ES/vol are losses, reported
  positive.
- Net exposure: aggregated factor loading (weight x loading); additive, no ScenarioSet needed. Market
  carries a loading of 1.0 per name, so a fully invested portfolio has ~unit Market exposure.
- Scenario VaR 95/97.5/99 and ES 97.5/99 are losses at that confidence / tail means. Total VaR 99 /
  Total ES 97.5 fold in the diagonal SPECIFIC (idiosyncratic) block. Marginal/% measures are a member's
  additive share of the portfolio number (they sum to the total); Incremental VaR is
  diversification-aware and does NOT sum. Risk HHI is the Herfindahl of per-name Total-VaR shares;
  1/HHI ~ effective bets.
- Factor contribution / Specific PnL / Realized PnL (if listed): REALIZED monthly PnL attribution,
  additive (Realized = factor + specific). Forward-month convention: the value at Date d0 is the PnL
  over the month AFTER d0. No ScenarioSet needed; read Specific PnL in by-name or portfolio views.
- Price VaR/ES (if listed): the MODEL-FREE twin of Scenario VaR/ES — historical sim on raw daily
  stock returns, no factor model. Needs a PriceSet context (its own hierarchy: HistFull or Evt:*
  only, no Hypo:*). Not a pivot measure but relevant if asked "does the model agree with the raw
  tape": the /var_bridge endpoint (not reachable via query_cube) explains Total VaR 99 vs Price
  VaR 99 in four ordered terms — specific risk dropped, specific distribution (fat tails +
  residual correlation — a missing-factor signal when large), exposure drift (today's loadings vs
  each day's own), coverage (priced names outside the model). Say so and point at it if asked.
- EVERY scenario measure is blank unless you slice ScenarioSet to ONE set. Sets: HistFull (full
  historical sim), Evt:* (a past window — COVID2020, Rates2022, Selloff2018), Hypo:* (hand-set sigma
  shocks — ValueRotation, RiskOff, MomentumCrash). Slice Date to one month for a point-in-time read.
- PnL at day (if listed) is the per-day scenario P&L path: rows Day + DayDate, filter DaySet (not
  ScenarioSet) to ONE set; add Sector on rows for the day x sector breakout. Read the path, the
  worst days, and the sector split on a bad day — the sum across days is not a risk number.
- KEY CAVEAT: every name shares the uniform Market loading of 1.0, so in any set with real market
  moves (HistFull, Evt:*) Market dominates portfolio risk (~95%) and HHI is low. The Hypo:* shocks
  zero Market and bump only style factors, so risk collapses onto the few names with those tilts
  and HHI jumps. A Hypo:* set reading far more concentrated than HistFull is that mechanism, not a
  data problem.

Hard rules:
- Reason ONLY from numbers query_cube returned. Cite the figures you used. Never invent a position,
  issuer, date, or value. If a query errored or came back empty, say so and adjust — don't guess.
- Be economical: a few well-chosen queries beat many. Don't pull Date x Position grids you won't use.
- Known limits to flag when relevant: universe capped at 250 names; Country stubbed "US"; ~5 names fall
  back to "Unknown" sector. Don't over-read precision.

Output: tight GitHub-flavoured markdown for a risk desk. Lead with a one-line direct answer, then the
supporting figures as a few bullets, then a short "So what" if it helps. No preamble, no restating the
question, no filler. Write plainly: direct, short sentences."""


class AskBody(BaseModel):
    question: str
    notes: str | None = None       # optional desk context typed by the user


@app.post("/ask")
async def ask(body: AskBody):
    """Streamed scoped Q&A. The model gets one tool — query_cube — and answers a free-text desk
    question by pulling its own cube slices through the SAME _validate_pivot/_pivot_result guard the
    UI uses. Manual agentic loop, bounded to ASK_MAX_ROUNDS tool round-trips; off-allowlist names are
    rejected inside the tool (the model retries), so it can never reach anything off the allowlist."""
    _rate_limit()
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "ask a question")
    client = _anthropic()          # 502 before any work if there's no key
    user0 = q if not body.notes else f"{q}\n\nDesk context: {body.notes.strip()}"
    messages = [{"role": "user", "content": user0}]

    def gen():
        for _ in range(ASK_MAX_ROUNDS):
            try:
                with client.messages.stream(
                    model="claude-opus-5", max_tokens=4000,
                    thinking={"type": "adaptive"},
                    system=[{"type": "text", "text": ASK_SYSTEM,
                             "cache_control": {"type": "ephemeral"}}],   # cached: stable across asks
                    tools=[QUERY_CUBE_TOOL],
                    messages=messages,
                ) as stream:
                    yield from stream.text_stream                        # text deltas only; thinking hidden
                    final = stream.get_final_message()
            except anthropic.APIError as e:                             # mid-stream: 200 already sent
                yield f"\n\n_[ask failed: {e.__class__.__name__}]_"
                return
            if final.stop_reason != "tool_use":
                return                                                  # model answered — done
            messages.append({"role": "assistant", "content": final.content})  # keep thinking+tool_use
            results = []
            for blk in final.content:
                if getattr(blk, "type", None) != "tool_use":
                    continue
                args = blk.input if isinstance(blk.input, dict) else {}
                yield (f"\n\n> 🔎 `query_cube` rows={args.get('rows')} "
                       f"cols={args.get('cols') or '—'} measures={args.get('measures')} "
                       f"filters={args.get('filters') or '—'}\n\n")
                out = _run_query_cube(args)
                results.append({"type": "tool_result", "tool_use_id": blk.id,
                                "content": json.dumps(out, default=str)})
            messages.append({"role": "user", "content": results})
        yield "\n\n_[reached the query limit — answering with what I have]_"
    return StreamingResponse(gen(), media_type="text/markdown")
