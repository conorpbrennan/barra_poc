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
    GET /limits?date=&set=&book=           -> desk-limit RAG status (limits.json)
    GET /dq                                -> data-quality / trust report on the live frames
    GET /backtest?set=&alpha=&window=      -> rolling-window VaR backtest (Kupiec + Basel zone)
    GET /drawdown?set=&date=&book=         -> constant-portfolio max drawdown over the scenario path
    GET /trends?set=&measures=&by=         -> tidy time series of book measures over the calendar
    POST /stress                           -> custom one-day stress (user-defined per-factor sigmas)
    GET /reverse_stress?loss=              -> per-factor sigma move that breaches a target loss
    POST /whatif                           -> pre-trade book risk before/after hypothetical trades
    GET /pnl_attribution?from=&to=&by=     -> realized PnL by factor + residual (Carino-linked)
    GET /pnl_attribution/residual          -> residual diagnostics (IR, autocorr, bias) with RAG
    GET /pnl_attribution/linkage?T=        -> risk decomposition at T vs PnL over T→T+h (surprise z)
    POST /analysis                         -> streamed risk-analyst commentary on ONE view's numbers

The /analysis endpoint runs the SAME guarded pivot the UI shows, then sends ONLY those tidy
numbers to the Anthropic Messages API (plain client.messages.create, NO tools) for a written
read. The model gets the figures as text and nothing else — it has zero access to the cube,
the filesystem, or any tool; it cannot re-query. Grounding rules live in ANALYST_SYSTEM.
"""
from __future__ import annotations
import os
import math
import json
import time
import uuid
import pathlib
import datetime as _dt
from collections import deque
from contextlib import asynccontextmanager

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
import barra_universe_membership as _um
import barra_universe_funnel as _uf
import barra_universe_span as _us
import barra_universe_drift as _ud
from barra_factor_risk_cube import load_frames, build_cube, EVENT_WINDOWS, HYPO_SHOCKS

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
# ScenarioDay is the synthetic per-array-element dimension: levels=[ScenarioDay] UNPACKS the
# scenario P&L vector into a tabular per-day series (so the scenario path is a normal /pivot query).
DIM_NAMES = ["Date", "Book", "Country", "Sector", "Issuer", "Position",
             "FactorGroup", "Factor", "ScenarioSet", "ScenarioDay"]
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
                 # gross/net book weight (scenario-independent, branch-sensitive):
                 "Gross weight", "Net weight",
                 # ES contribution split + risk-concentration HHI:
                 "Marginal Scenario ES 97.5", "% of Scenario ES 97.5", "Risk HHI",
                 # per-day unpacked scenario series (read with ScenarioDay on an axis):
                 "Scenario PnL at day", "Scenario date at day (epoch)",
                 "Scenario VaR line at day", "Scenario worst pnl at day",
                 "Scenario worst date at day (epoch)",
                 "Scenario worst date (epoch)", "Scenario n",
                 # PnL attribution (Step 15, v2-only; pruned at startup if the cube lacks them).
                 # Forward-month convention: the value at Date d0 is the PnL over the month after d0.
                 "Factor contribution", "Specific PnL", "Realized PnL"]
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
            "Scenario PnL at day", "Scenario date at day (epoch)",
            "Scenario VaR line at day", "Scenario worst pnl at day",
            "Scenario worst date at day (epoch)",
            "Scenario worst date (epoch)", "Scenario n"}


def _records(df: pd.DataFrame) -> list[dict]:
    df = df.reset_index()
    return [{k: _clean(v) for k, v in row.items()} for _, row in df.iterrows()]


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
    print(f"[risk_api] cube ready on :{CUBE_PORT}; UI at {session.url}")
    yield
    session.close()


app = FastAPI(title="Barra Factor Risk API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(views_router)   # saved-view CRUD over views_repo (no cube dep) — see views_api.py


# ----------------------------------------------------------------------------- endpoints
@app.get("/meta")
async def meta():
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        dates = sorted({str(pd.Timestamp(d).date()) for d in
                        cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index})
        all_sets = sorted({str(s) for s in cube.query(m["contributors.COUNT"], levels=[l["ScenarioSet"]]).index})
        # PIT:* truncated-history sets are plumbing for as-of risk (one per month-end) — kept
        # out of the main dropdown list; still addressable by name in any filter.
        sets = [s for s in all_sets if not s.startswith("PIT:")]
        pit_sets = [s for s in all_sets if s.startswith("PIT:")]
        factors = sorted(S["frames"]["factor_meta"]["Factor"].tolist())
        return {"dates": dates, "scenario_sets": sets, "pit_sets": pit_sets, "factors": factors,
                "ts_measures": TS_MEASURES, "by_levels": list(BY_LEVELS),
                # the cube's baked-in hypothetical shock definitions ({set: {Factor: sigma}}) —
                # served so the Stress lens presets and the cube's Hypo:* sets share ONE source
                "hypo_shocks": HYPO_SHOCKS}
    return await run_in_threadpool(run)


@app.get("/risk")
async def risk(date: str, set: str):
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        df = cube.query(m["Total VaR 99"], m["Scenario VaR 99"], m["Scenario worst loss"], m["Specific vol"],
                        filter=(l["Date"] == _date(date)) & (l["ScenarioSet"] == set))
        if not len(df):
            return {"date": date, "set": set, "empty": True}
        r = df.iloc[0]
        return {"date": date, "set": set,
                "total_var": _clean(r["Total VaR 99"]), "factor_var": _clean(r["Scenario VaR 99"]),
                "worst_loss": _clean(r["Scenario worst loss"]), "specific_vol": _clean(r["Specific vol"])}
    return await run_in_threadpool(run)


@app.get("/scenarios")
async def scenarios(date: str):
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        df = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], m["Total VaR 99"],
                        levels=[l["ScenarioSet"]], filter=l["Date"] == _date(date))
        return _records(df)
    return await run_in_threadpool(run)


@app.get("/exposures")
async def exposures(date: str):
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        df = cube.query(m["Net exposure"], levels=[l["FactorGroup"], l["Factor"]],
                        filter=l["Date"] == _date(date))
        return _records(df)
    return await run_in_threadpool(run)


@app.get("/attribution")
async def attribution(date: str, set: str, by: str = "sector"):
    by = by.lower()
    if by not in BY_LEVELS:
        raise HTTPException(400, f"by must be one of {list(BY_LEVELS)}")
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        df = cube.query(m["Net exposure"], m["Scenario VaR 99"], m["Scenario worst loss"],
                        levels=[l[BY_LEVELS[by]]],
                        filter=(l["Date"] == _date(date)) & (l["ScenarioSet"] == set))
        recs = _records(df)
        if by == "position":           # decorate FIGI with a readable ticker
            tk = _ticker_map()
            for r in recs:
                r["Ticker"] = tk.get(r.get("Position"), "")
        return recs
    return await run_in_threadpool(run)


@app.get("/timeseries")
async def timeseries(set: str, measure: str = "Total VaR 99"):
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
                 by: str | None = None):
    """Tidy time series of one or more book measures over the whole calendar for one ScenarioSet —
    one cube query, so a trend panel needs a single round-trip. `by` (e.g. Factor) adds a breakdown
    dimension: levels become [Date, by] (used for factor-exposure-over-time). Measures/by are
    validated against the same allowlists as /pivot."""
    mlist = _csv(measures)
    bad_m = [x for x in mlist if x not in MEASURE_NAMES]
    if bad_m:
        raise HTTPException(400, f"unknown measure(s): {bad_m}")
    if not mlist:
        raise HTTPException(400, "select at least one measure")
    if by and by not in DIM_NAMES:
        raise HTTPException(400, f"unknown dimension: {by}")
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        meas = [m[x] for x in mlist]
        if by:
            # additive breakdown (e.g. Net exposure by Factor) — one query is safe (no P&L vectors).
            df = (cube.query(*meas, levels=[l["Date"], l[by]], filter=l["ScenarioSet"] == set)
                  .reset_index().sort_values("Date"))
            recs = [{k: _clean(v) for k, v in row.items()} for _, row in df.iterrows()]
        else:
            # book-level over the calendar, DATE-BY-DATE: the scenario/HHI measures pull the full P&L
            # vector per date, and asking for every date in one plan OOMs the cube — so loop, one
            # date (one vector) at a time. ~100 light scalar queries; cheap and cached upstream.
            dates = sorted({pd.Timestamp(d).date() for d in S["frames"]["specific_var"]["Date"]})
            recs = []
            for d in dates:
                r = cube.query(*meas, filter=(l["Date"] == d) & (l["ScenarioSet"] == set))
                if len(r):
                    row = r.iloc[0]
                    recs.append({"Date": d.isoformat(), **{x: _clean(row[x]) for x in mlist}})
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
async def validation():
    """Top-3 sub-book: cube scenario VaR vs an independent pandas reference (mirrors barra_excel_check)."""
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        f = S["frames"]
        positions, securities = f["positions"], f["securities"]
        factor_ret, specific = f["factor_returns"], f["specific_var"]
        last = positions["Date"].max()
        book = (positions[positions["Date"] == last].nlargest(3, "Weight")
                .merge(securities[["Position", "Ticker"]], on="Position"))
        figs = book["Position"].tolist()

        # --- cube side: 3-position slice, by scenario set ---
        cdf = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], levels=[l["ScenarioSet"]],
                         filter=(l["Date"] == pd.Timestamp(last).date()) & l["Position"].isin(*figs))

        # --- pandas reference: same math as the Excel workbook (Market INCLUDED: leaf loading 1.0) ---
        wide = (factor_ret
                .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
        factors = list(wide.columns)
        L = (f["exposures"][(f["exposures"]["Date"] == last) & (f["exposures"]["Position"].isin(figs))]
             .pivot(index="Position", columns="Factor", values="Loading")
             .reindex(index=figs, columns=factors).fillna(0.0))
        wts = book.set_index("Position")["Weight"].reindex(figs)
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
                "book": [{"ticker": t, "weight": _clean(w)} for t, w in zip(book["Ticker"], book["Weight"])],
                "rows": rows}
    return await run_in_threadpool(run)


# ----------------------------------------------------------------------------- generic pivot
@app.get("/dims")
async def dims():
    """Fields the pivot UI may use: dimensions, scalar measures, and slicer member lists."""
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        # member lists for every sliceable dimension, so the UI can offer single/multi
        # selection on any of them (not just Date / ScenarioSet).
        members = {}
        for d in DIM_NAMES:
            idx = cube.query(m["contributors.COUNT"], levels=[l[d]]).index
            # deep levels (Factor under FactorGroup, Sector under Country) come back as a
            # MultiIndex hierarchy path — the level's own member is the last component.
            vals = idx.get_level_values(-1) if isinstance(idx, pd.MultiIndex) else idx
            if d == "Date":
                members[d] = sorted({str(pd.Timestamp(x).date()) for x in vals})
            else:
                members[d] = sorted({str(x) for x in vals})
        # PIT:* plumbing sets stay out of the pivot's slicer list (addressable by name)
        members["ScenarioSet"] = [x for x in members["ScenarioSet"] if not x.startswith("PIT:")]
        return {"dimensions": DIM_NAMES, "measures": MEASURE_NAMES,
                "scenario_dependent": sorted(SCEN_DEP), "members": members,
                "dates": members["Date"], "scenario_sets": members["ScenarioSet"]}
    return await run_in_threadpool(run)


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
    """AND across dimensions, OR (isin) within a dimension. Date members -> timestamps."""
    cond = None
    for d, vals in fd.items():
        members = [_date(v) for v in vals] if d == "Date" else list(vals)
        c = l[d].isin(*members)
        cond = c if cond is None else (cond & c)
    return cond


def _validate_pivot(rlist: list, clist: list, mlist: list, fdict: dict) -> None:
    """Allowlist guard shared by /pivot and /analysis: only whitelisted dims/measures, and a
    non-empty rows+measures selection. Raises HTTPException(400) exactly as /pivot always has,
    so /analysis can never reach an off-allowlist dimension or measure either."""
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


def _pivot_result(rlist: list, clist: list, mlist: list, fdict: dict, totals: bool,
                  scenario: str | None = None, stress_scenario: str | None = None) -> dict:
    """The tidy pivot result (records [+ per_row/per_col/grand margins when totals]). Extracted
    from /pivot so /analysis feeds the model the EXACT numbers the view renders. Synchronous —
    call via run_in_threadpool. Assumes _validate_pivot has already run.
    `scenario` = a SOURCE-scenario branch name (what-if trades — cube.query(scenario=...));
    `stress_scenario` = a StressShock PARAMETER-simulation scenario (custom sigmas — selected
    by slicing the StressShock level). Both default to the base."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    seen, axis = set(), []
    for name in rlist + clist:          # dedupe, preserve order
        if name not in seen:
            seen.add(name); axis.append(name)
    filt = _build_filter(l, fdict)
    if stress_scenario is not None:
        filt = (filt & (l["StressShock"] == stress_scenario)) if filt is not None \
            else (l["StressShock"] == stress_scenario)

    scen_ctx = ("ScenarioSet" in axis) or ("ScenarioSet" in fdict)
    warning = None
    if any(x in SCEN_DEP for x in mlist) and not scen_ctx:
        warning = ("Scenario measures need a ScenarioSet context — put ScenarioSet on an "
                   "axis or pick a single scenario; otherwise those cells are blank.")
    meas_objs = [m[x] for x in mlist]
    _kw = {"scenario": scenario} if scenario is not None else {}
    df = cube.query(*meas_objs, levels=[l[a] for a in axis], filter=filt, **_kw)
    out = {"rows": rlist, "cols": clist, "measures": mlist, "totals": bool(totals),
           "warning": warning, "records": _records(df)}
    if totals:
        per_row = cube.query(*meas_objs, levels=[l[a] for a in rlist], filter=filt, **_kw)
        out["per_row"] = _records(per_row)                              # Total column
        if clist:
            per_col = cube.query(*meas_objs, levels=[l[a] for a in clist], filter=filt, **_kw)
            out["per_col"] = _records(per_col)                          # Total row
        grand = cube.query(*meas_objs, filter=filt, **_kw)              # corner
        def _scalar(v):                                                 # null array-measures -> None
            try:
                f = float(v); return None if math.isnan(f) else f
            except (TypeError, ValueError):
                return None
        out["grand"] = {x: _scalar(grand.iloc[0][x]) for x in mlist} if len(grand) else {}
    return out


def _whatif_branch_rows(date: str, book: str, trades: list) -> pd.DataFrame:
    """Positions rows for a transient what-if SOURCE-scenario branch: the traded names' as-of
    rows with Weight replaced (a fabricated row for a coverage name not currently held).
    Untraded names inherit the base — a branch is a delta, not a copy."""
    pos = S["frames"]["positions"]
    d_ts = pd.Timestamp(date)
    base = pos[(pos["Book"] == book) & (pos["Date"] == d_ts)]
    rows = []
    for t in trades:
        p, nw = t["position"], float(t["weight"])
        r0 = base[base["Position"] == p]
        if len(r0):
            r = r0.iloc[0].to_dict(); r["Weight"] = nw
        else:
            r = {"Date": d_ts, "Book": book, "Position": p, "Weight": nw,
                 "MV": np.nan, "ADV": np.nan}
        rows.append(r)
    return pd.DataFrame(rows, columns=list(pos.columns))


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
                        wtrades: list | None, shk: dict | None) -> dict:
    """_pivot_result on a transient hypothetical: a what-if source-scenario branch (trades)
    and/or a StressShock parameter scenario (sigmas) — created per call, dropped in finally.
    Synchronous — call via run_in_threadpool."""
    session = S["session"]
    branch = stress_scen = None
    sim = None
    try:
        if wtrades:
            branch = f"pivot-wf-{uuid.uuid4().hex[:12]}"
            book = (fdict.get("Book") or ["Soros"])[0]
            session.tables["Positions"].scenarios[branch].load(
                _whatif_branch_rows(fdict["Date"][0], book, wtrades))
        if shk:
            stress_scen = f"pivot-st-{uuid.uuid4().hex[:12]}"
            sim = session.tables["StressShock"]
            sim.append(*[(stress_scen, f_, float(v)) for f_, v in shk.items()])
        return _pivot_result(rlist, clist, mlist, fdict, bool(totals),
                             scenario=branch, stress_scenario=stress_scen)
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
                    'JSON {"Factor": sigma} — run the pivot under a transient custom stress')):
    """Tidy long result of cube.query(measures, levels=rows+cols, filter=<slicers>).

    Slicers: `filters` is a JSON object {dimension: [members]} — AND across dimensions,
    OR within a dimension. Single-value `date`/`set` query params still work and fold in.

    Guardrails: only whitelisted dimensions/scalar measures; the frontend pivots the tidy
    records into a matrix. Returns a `warning` when a scenario-dependent measure is requested
    without a ScenarioSet context (it would be null) so the UI can flag it.

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
    _validate_pivot(rlist, clist, mlist, fdict)
    wtrades, shk = _parse_hypo(whatif, shocks, fdict)
    if not wtrades and not shk:
        return await run_in_threadpool(_pivot_result, rlist, clist, mlist, fdict, bool(totals))
    return await run_in_threadpool(_hypothetical_pivot, rlist, clist, mlist, fdict,
                                   bool(totals), wtrades, shk)


set_ = set   # preserve builtin; the endpoint shadows `set` with the query param
_EPOCH = pd.Timestamp("1970-01-01")


@app.get("/scenario_pnl")
async def scenario_pnl(date: str, set: str, position: str | None = None,
                       sector: str | None = None, filters: str | None = None,
                       breakout: str | None = None):
    """Labeled scenario P&L PATH: the `Scenario PnL vector` zipped with its `Scenario dates`
    dual, so every point carries the date that produced it. Names the worst-loss date and the
    99% VaR breach. One ScenarioSet only (the vector constraint).

    Drill: legacy `position`/`sector` params still work; `filters` is the same JSON object
    {dimension: [members]} `/pivot` takes (AND across dims, OR within) so the chart can scope
    to a Book and any Sector/Issuer/Position/Factor slice. Date/ScenarioSet stay the path axis
    and must NOT appear in `filters` (they're the `date`/`set` params).

    `breakout` (a dimension, e.g. "Sector") adds a `dist_stacked` dataset: the cube's P&L vector
    grouped by that dimension (each member's per-day P&L — a CUBE aggregation), reshaped into
    (date, member, pnl, rank). `rank` is the day's position once ordered by the BOOK total
    (worst→best), so the chart can keep DATE labels on x while drawing the sorted loss curve.
    Stacked, the members sum to the book P&L. The only API steps are ordering + reshape."""
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        filt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == set)
        if position:
            filt = filt & (l["Position"] == position)
        if sector:
            filt = filt & (l["Sector"] == sector)
        fd = _parse_filters(filters, None, None)           # drill dims only (no date/set refold)
        fd.pop("Date", None); fd.pop("ScenarioSet", None)  # those are the fixed path axis
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
        # produced the per-day book P&L. READABLE date = the epoch-int dual converted to ISO here.
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
        # and a `rank` = its position ordered by the BOOK total (worst→best, argsort — ordering,
        # not aggregation), so the chart shows the SORTED loss curve but with DATE labels on x.
        if breakout and breakout in DIM_NAMES:
            bv = cube.query(m["Scenario PnL vector"], levels=[l[breakout]], filter=filt)
            members = []
            for idx, brow in bv.iterrows():
                member = idx[-1] if isinstance(idx, tuple) else idx
                arr = brow.iloc[0]
                members.append((str(member), np.asarray(arr, dtype=float) if arr is not None else None))
            order = [int(i) for i in np.argsort(pnl)]      # day indices, ascending by BOOK total
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
# Compare the cube's book numbers to a desk limit set (limits.json) and return a red/amber/green
# status per limit. Book-level VaR/ES/HHI come from the cube (scenario-dependent -> one ScenarioSet);
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


def _limits_result(date: str, scen: str, book: str) -> dict:
    """RAG status of every configured limit at (date, scenario set, book). Synchronous — call via
    run_in_threadpool. Returns checks + the worst-of overall status + the breach list."""
    cfg = _load_limits()
    cube = S["cube"]; l, m = cube.levels, cube.measures
    checks: list[dict] = []
    have_book = "Book" in {n for _, n in cube.hierarchies}
    base = (l["Date"] == _date(date)) & ((l["Book"] == book) if have_book else (l["Date"] == _date(date)))

    # book-level scenario measures (VaR/ES/Top-5 need a single ScenarioSet; Top-5 risk share is
    # a cube measure since 2026-07-04 — tt.rank over the flat PositionRank hierarchy — so the
    # generic query below serves it and it is SET-DEPENDENT like the old Risk HHI was).
    bspec = dict(cfg.get("book", {}))
    if bspec:
        df = cube.query(*[m[x] for x in bspec], filter=base & (l["ScenarioSet"] == scen))
        row = df.iloc[0] if len(df) else None
        for name, spec in bspec.items():
            v = row[name] if row is not None else None
            val = None if (v is None or pd.isna(v)) else _clean(v)
            status, head = _rag(val, spec.get("warn"), spec.get("limit"))
            checks.append({"name": name, "scope": "book", "value": val, "warn": spec.get("warn"),
                           "limit": spec.get("limit"), "status": status, "headroom": head, "detail": None})

    # concentration from the positions overlay, as-of the latest filing on/before `date`
    conc = cfg.get("concentration", {})
    if conc:
        pos = S["frames"]["positions"]
        asof = pos[(pos["Book"] == book) & (pos["Date"] <= pd.Timestamp(date))]
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
    return {"date": date, "set": scen, "book": book, "status": overall, "configured": bool(checks),
            "checks": checks, "breaches": [c for c in checks if c["status"] == "breach"]}


@app.get("/limits")
async def limits(date: str | None = None, set: str | None = None, book: str = "Soros"):
    """RAG status of the desk limits (limits.json) for one book. Defaults: latest date, the config's
    scenario_set. `set` overrides the scenario set the VaR/ES/HHI limits are read against."""
    scen = set or _load_limits().get("scenario_set", "HistFull")
    def run():
        return _limits_result(date or _latest_date(), scen, book)
    return await run_in_threadpool(run)


# ============================================================================ data quality (trust)
# Run barra_dq_checks against the cube's LIVE in-memory frames (not a disk re-read) and add the
# known-stub counts + per-frame latest date, so the desk can see whether to trust the numbers.

@app.get("/dq")
async def dq():
    """Data-quality report on the frames the cube is actually serving: PASS/WARN/FAIL checks, a
    worst-of status, the known stubs (Unknown sector, Country='US'), and each frame's latest date."""
    def run():
        checks = barra_dq_checks.run(S["frames"])          # structured [{level,name,detail}]
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


# ============================================================================ universe membership
# Bitemporal index-membership diagnostic (Phase 1; docs/universe-diagnostics-plan.md). Serves the
# precomputed artifact written by barra_universe_membership.py — for each 13F filing, the book's
# weight split across {S&P 500 PIT, S&P 400/600 current, Outside S&P 1500, Unclassified}. No cube
# dependency and no network at request time (the artifact is built offline like the frames).

@app.get("/universe")
async def universe(date: str | None = Query(None, description="filing report_date; default latest")):
    """Index-membership of the Soros book by filing: a weight-by-bucket time series, the latest (or
    `date`) filing's split + the 'outside S&P 1500' headline, and the names in the Outside/Unclassified
    buckets. Reads data/universe_membership.parquet (run barra_universe_membership.py to (re)build)."""
    def run():
        if not _um.ARTIFACT.exists():
            raise HTTPException(503, "universe_membership.parquet not built — "
                                     "run barra_universe_membership.py")
        df = pd.read_parquet(_um.ARTIFACT)
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
async def funnel(date: str | None = Query(None, description="funnel month; default latest")):
    """Estimation-universe filtration funnel by month: a population→survivors waterfall with the drop
    count per stage, the survivor count (and how many are held), the selected month's drop list (name
    + the stage that dropped it + its metrics), and the documented thresholds. The funnel is near-flat
    by design — the S&P 500 is pre-curated, so the filters confirm a clean input rather than carve."""
    def run():
        if not _uf.ARTIFACT.exists():
            raise HTTPException(503, "universe_funnel.parquet not built — run barra_universe_funnel.py")
        df = pd.read_parquet(_uf.ARTIFACT)
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
               fx: str = Query("Size"), fy: str = Query("ResidVol")):
    """Span / high-confidence check: a per-month time series of the book weight INSIDE the estimation
    universe's factor space, the selected month's per-name verdict (D², inside/outside, which factors
    push a name out), and a 2D `fx`×`fy` scatter of the estimation cloud vs the held book — the literal
    version of Chris's VALUE/SIZE illustration. ~90% of the book sits inside on average; it has drifted
    from ~95% pre-2021 to ~85% since."""
    if fx not in _us.STYLE or fy not in _us.STYLE:
        raise HTTPException(400, f"fx/fy must be style factors: {_us.STYLE}")

    def run():
        if not _us.ARTIFACT.exists():
            raise HTTPException(503, "universe_span.parquet not built — run barra_universe_span.py")
        df = pd.read_parquet(_us.ARTIFACT)
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

        # live 2D scatter from the exposures frame (cloud = funnel survivors, held = the book)
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
        cloud = [pt(p) for p in cloud_pos if not (np.isnan(w.loc[p, fx]) or np.isnan(w.loc[p, fy]))]
        book = [{**pt(p), "inside": bool(inside_map.get(p, False)), "issuer": iss.get(p, "")}
                for p in held_pos if not (np.isnan(w.loc[p, fx]) or np.isnan(w.loc[p, fy]))]

        lat = {"month": sel, "n_held": int(len(gm)), "n_inside": int(gm["inside"].sum()),
               "inside_wt": _us.inside_share(gm["weight"].values, gm["inside"].values)} if len(gm) else {}
        return {
            "factors": _us.STYLE, "series": series, "selected_date": sel, "latest": lat,
            "detail": [{k: _clean(v) for k, v in row.items()} for _, row in detail.iterrows()],
            "scatter": {"fx": fx, "fy": fy, "cloud": cloud, "held": book},
            "note": ("'Inside' = squared Mahalanobis distance within the estimation cloud's own 99th "
                     "percentile — the region the estimation universe populated, where model exposures "
                     "are well-supported. Outside = extrapolation. Cloud = funnel survivors; loadings "
                     "are z-scored/winsorized, so the space is in standardized-exposure terms."),
        }
    return await run_in_threadpool(run)


# ============================================================================ style-drift attribution
# Phase 4 (docs/universe-diagnostics-plan.md). The book's net factor exposure x_k(t) over time, the
# pre/post-`split` drift per factor, and an attribution of each factor's drift into entered / exited /
# reweighted / loading_drift — making Chris's intentional-vs-not question empirical: drift dominated by
# NEW names rotating in leans intentional (→ benchmark); drift from HELD names' loadings drifting leans
# unintentional (→ hedge). Series read from data/universe_drift.parquet; attribution computed live.

@app.get("/drift")
async def drift(split: str = Query("2021-01-01", description="pre/post boundary for the drift")):
    """Style-drift attribution: per-factor net-exposure trend, the pre/post-`split` drift ranked by
    magnitude, and a decomposition of each factor's drift into entered / exited / reweighted /
    loading_drift — with a per-factor 'lean' (rotation → intentional → benchmark; re-pricing →
    unintentional → hedge). The final verdict needs desk knowledge; this lays out the evidence."""
    def run():
        if not _ud.ARTIFACT.exists():
            raise HTTPException(503, "universe_drift.parquet not built — run barra_universe_drift.py")
        df = pd.read_parquet(_ud.ARTIFACT); df["month"] = pd.to_datetime(df["month"])
        series = df.pivot_table(index="month", columns="factor", values="net_exposure").sort_index()
        sp = pd.Timestamp(split)

        exp, pos = S["frames"]["exposures"], S["frames"]["positions"]
        months = pd.DatetimeIndex(sorted(pd.to_datetime(pos["Date"].unique())))
        pre = months[months < sp]
        t0 = pre[-1] if len(pre) else months[0]
        t1 = months[-1]
        w0, l0 = _ud.book_at(exp, pos, t0)
        w1, l1 = _ud.book_at(exp, pos, t1)
        attr = _ud.decompose(w0, l0, w1, l1)
        x0, x1 = _ud.book_exposure(w0, l0), _ud.book_exposure(w1, l1)   # exposure at t0 / t1

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
            "note": ("Net book exposure x_k = Σ w·L per factor. Drift Δx_k between the pre-split book "
                     "and the latest is split into entered / exited (rotation) / reweighted (resizing) "
                     "/ loading_drift (held names' own loadings moving). Rotation-dominated drift leans "
                     "intentional (mandate shifted → update the benchmark); loading-drift-dominated "
                     "leans unintentional (re-pricing → update the hedge). The verdict is the desk's — "
                     "this is the evidence, per Chris (2026-06-23)."),
        }
    return await run_in_threadpool(run)


# ============================================================================ VaR backtest
# Constant-portfolio backtest: take the current book's daily factor-P&L series (the HistFull
# scenario vector + its date dual), roll a window to estimate VaR each day, and count exceptions
# where the realized day beat VaR. Tests the VaR METHODOLOGY against history (the 13F book has no
# live daily P&L track record). Kupiec POF test + Basel traffic-light from the binomial CDF.
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


def _backtest_result(date: str, scen: str, book: str, alpha: float, window: int,
                     method: str = "equal", lam: float = 0.94) -> dict:
    """Rolling-window VaR backtest of the book's daily factor-P&L series for one scenario set."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    have_book = "Book" in {n for _, n in cube.hierarchies}
    flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == scen)
    if have_book:
        flt = flt & (l["Book"] == book)
    pv = cube.query(m["Scenario PnL vector"], filter=flt)
    dv = cube.query(m["Scenario dates (epoch)"], filter=flt)
    base = {"set": scen, "book": book, "date": date, "alpha": alpha, "window": window,
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
async def backtest(set: str = "HistFull", date: str | None = None, book: str = "Soros",
                   alpha: float = 0.99, window: int = 250, method: str = "fhs", lam: float = 0.94):
    """Rolling-window VaR backtest (Kupiec POF + Basel traffic-light) on a book's daily factor-P&L
    series. method = fhs (default — filtered historical simulation, EWMA-vol-scaled empirical tail) |
    equal (plain historical sim) | ewma (RiskMetrics parametric-normal). `lam` is the EWMA decay.
    The fhs/lam=0.94 default was chosen by a sweep: at 99% it gives ~1.0% breaches (Kupiec-green),
    where equal HS under-covers (amber) and parametric ewma over-breaches on the fat tail (red).
    Defaults: HistFull (only set with a long daily history), latest date, 99% / 250-day."""
    if not (0.5 < alpha < 1):
        raise HTTPException(400, "alpha must be in (0.5, 1)")
    if window < 30:
        raise HTTPException(400, "window must be >= 30")
    if method not in ("equal", "ewma", "fhs"):
        raise HTTPException(400, "method must be 'equal', 'ewma', or 'fhs'")
    if not (0.0 < lam < 1.0):
        raise HTTPException(400, "lam (EWMA decay) must be in (0, 1)")
    def run():
        return _backtest_result(date or _latest_date(), set, book, alpha, window, method, lam)
    return await run_in_threadpool(run)


# ============================================================================ drawdown (path lens)
def _max_drawdown(pnl, days) -> dict | None:
    """Constant-portfolio max drawdown of the book's simulated daily P&L PATH (geometric equity
    curve), with peak/trough dates, recovery, and the longest underwater run. pnl/days are the
    cube's `Scenario PnL vector` + its `Scenario dates` dual; date-ordered here. Pure stats (no
    cube) so it unit-tests directly. Drawdown is path-dependent — the lens VaR/ES can't see."""
    pnl = np.asarray(pnl, float); days = np.asarray(days, int)
    n = min(len(pnl), len(days))
    if n == 0:
        return None
    order = np.argsort(days[:n]); pnl, days = pnl[:n][order], days[:n][order]
    eq = np.cumprod(1.0 + pnl)                       # held book compounded over the factor path
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


def _drawdown_result(date: str, scen: str, book: str) -> dict:
    """Pull the book P&L vector for one (Date, ScenarioSet) and reduce to the drawdown summary +
    path. Same vector source as /backtest and /scenario_pnl."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    have_book = "Book" in {n for _, n in cube.hierarchies}
    flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == scen)
    if have_book:
        flt = flt & (l["Book"] == book)
    pv = cube.query(m["Scenario PnL vector"], filter=flt)
    dv = cube.query(m["Scenario dates (epoch)"], filter=flt)
    base = {"set": scen, "book": book, "date": date}
    if not len(pv) or pv.iloc[0, 0] is None or not len(dv):
        return {**base, "status": "insufficient", "n": 0}
    dd = _max_drawdown(pv.iloc[0, 0], dv.iloc[0, 0])
    if dd is None or dd["n"] < 2:                      # length-1 (hypo) sets have no path
        return {**base, "status": "insufficient", "n": (dd["n"] if dd else 0)}
    return {**base, "status": "ok", **dd}


@app.get("/drawdown")
async def drawdown(set: str = "HistFull", date: str | None = None, book: str = "Soros"):
    """Constant-portfolio max drawdown: cumulate the current book's daily factor-P&L over the
    scenario set's path (geometric equity curve) and take peak-to-trough. Like /backtest this is a
    what-if on the *held* book over history, not a live track record. Drawdown is a path lens that
    VaR/ES miss. Most meaningful on HistFull (long path); event sets give the drawdown over that
    window; hypo (length-1) sets are degenerate -> status insufficient."""
    def run():
        return _drawdown_result(date or _latest_date(), set, book)
    return await run_in_threadpool(run)


# ============================================================================ stress (custom / reverse)
# A hypothetical shock's book P&L is linear: dPnL = Σ_k x_k * (sigma_k * vol_k), where x_k is the book
# net exposure to factor k and vol_k is that factor's return vol (same convention build_scenarios uses
# for the baked-in Hypo sets). So custom (user-defined sigmas) and reverse (solve the sigma that
# breaches a loss) stress are computed in the API from exposures + vols — no cube rebuild.

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


def _factor_exposures(date: str, book: str) -> dict:
    """Book net factor exposure x_k by Factor at a date (cube Net exposure — scenario-independent)."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    flt = (l["Date"] == _date(date))
    if "Book" in {n for _, n in cube.hierarchies}:
        flt = flt & (l["Book"] == book)
    df = cube.query(m["Net exposure"], levels=[l["Factor"]], filter=flt).reset_index()
    return {str(r["Factor"]): float(r["Net exposure"]) for _, r in df.iterrows()}


def _stress_result(shocks: dict, date: str, book: str) -> dict:
    """Book P&L under a user-defined set of per-factor sigma shocks (one-day hypothetical)."""
    vols = _factor_vols()
    x = _factor_exposures(date, book)
    comps, total = [], 0.0
    for f, sigma in shocks.items():
        xf, vf = x.get(f, 0.0), vols.get(f, 0.0)
        shock_ret = float(sigma) * vf
        pnl = xf * shock_ret
        total += pnl
        comps.append({"factor": f, "exposure": xf, "sigma": float(sigma), "vol": vf,
                      "shock_return": shock_ret, "pnl": pnl})
    comps.sort(key=lambda c: c["pnl"])               # worst contributor first
    return {"date": date, "book": book, "shocks": shocks,
            "total_pnl": total, "loss": -total, "components": comps}


def _conditional_shock(F: np.ndarray, idx: list[int], s: np.ndarray) -> np.ndarray:
    """E[f | f_idx = s] under the factor covariance F: F[:, idx] @ inv(F[idx, idx]) @ s. The
    correlated stress — a shocked factor drags every co-moving factor with it instead of moving
    alone. Shocking every factor returns s itself (the naive case)."""
    sub = F[np.ix_(idx, idx)]
    return F[:, idx] @ np.linalg.solve(sub, np.asarray(s, dtype=float))


def _conditional_stress_result(shocks: dict, date: str, book: str) -> dict:
    """Correlated version of _stress_result: condition the whole factor system on the shocked
    factors via the factor covariance, then dPnL = Σ x_k·E[f_k | shock]. The naive single-factor
    read understates a real event because the co-moving factors don't stay still."""
    L, w, _s, R = _book_inputs(date, book)
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
    book: str = "Soros"
    # correlated (conditional) mode: propagate the shock through the factor covariance and add a
    # `conditional` block — implied return per factor + the conditional book P&L.
    conditional: bool = False
    # correlation-stress mode (Step 15 §4): scale factor vols by vol_mult and blend correlations
    # toward 1 by rho — adds a `correlation_stress` block (base vs stressed book vol) to the result.
    vol_mult: float | None = None
    rho: float | None = None


def _corr_stress_result(date: str, book: str, vol_mult: float, rho: float) -> dict:
    """Book daily vol under a vols-and-correlations shock: F' = _stressed_cov(F). The base↔stressed
    gap on the BOOK (not any single factor) is the diversification the book leans on — where
    correlation risk lives. Normal-approx VaR99 = 2.326σ for scale."""
    L, w, s, R = _book_inputs(date, book)
    x = L.to_numpy().T @ w.to_numpy()
    F = np.cov(R, rowvar=False)
    svar = float(np.sum(w.to_numpy() ** 2 * s.to_numpy()))
    Fs = _pnl._stressed_cov(F, vol_mult, rho)
    base = float(np.sqrt(max(x @ F @ x + svar, 0.0)))
    stressed = float(np.sqrt(max(x @ Fs @ x + svar * vol_mult ** 2, 0.0)))
    return {"vol_mult": vol_mult, "rho_blend": rho,
            "base_vol_1d": base, "stressed_vol_1d": stressed,
            "base_var99_normal": _Z99 * base, "stressed_var99_normal": _Z99 * stressed}


def _corr_stress_cube(date: str, book: str, vol_mult: float, rho: float) -> dict:
    """The correlation-stress read SERVED from the cube's `Stressed model vol` (a transient
    CorrStress parameter scenario), with the numpy _corr_stress_result as the live cross-check.
    Falls back to serving the numpy numbers on any cube failure."""
    ref = _corr_stress_result(date, book, vol_mult, rho)
    scen = f"corr-{uuid.uuid4().hex[:12]}"
    sim = None
    try:
        cube = S["cube"]; l, mm = cube.levels, cube.measures
        sim = S["session"].tables["CorrStress"]
        sim.append((scen, float(vol_mult), float(rho)))
        flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == "HistFull")
        if "Book" in {n for _, n in cube.hierarchies}:
            flt &= (l["Book"] == book)
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
    """Custom one-day stress: book P&L under user-defined per-factor sigma shocks (and a per-factor
    contribution breakdown). dPnL = Σ x_k·(sigma_k·vol_k) — the same math as the baked-in Hypo sets;
    vols come from the cube's `Factor return vol` measure (via _factor_vols).
    Optional vol_mult/rho add a correlation-stress read (vols up, correlations toward 1)."""
    known = set(S["frames"]["factor_meta"]["Factor"].astype(str))
    bad = [f for f in body.shocks if f not in known]
    if bad:
        raise HTTPException(400, f"unknown factor(s): {bad}")
    if not body.shocks:
        raise HTTPException(400, "provide at least one factor shock")
    def run():
        d = body.date or _latest_date()
        # numpy reference — retained as the live cross-check
        ref = _stress_result(body.shocks, d, body.book)
        # cube-served naive shock (the StressShock parameter simulation — one transient scenario
        # per request): per-factor components from Custom stress PnL / Net exposure /
        # Factor return vol, footing to the book total. Falls back to serving the numpy numbers.
        scen = f"req-{uuid.uuid4().hex[:12]}"
        sim = None
        try:
            cube = S["cube"]; l, mm = cube.levels, cube.measures
            sim = S["session"].tables["StressShock"]
            sim.append(*[(scen, f_, float(sig)) for f_, sig in body.shocks.items()])
            flt = ((l["Date"] == _date(d)) & (l["ScenarioSet"] == "HistFull")
                   & (l["StressShock"] == scen))
            if "Book" in {n for _, n in cube.hierarchies}:
                flt &= (l["Book"] == body.book)
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
            res = {"date": d, "book": body.book, "shocks": body.shocks,
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
            res["conditional"] = _conditional_stress_result(body.shocks, d, body.book)
        if body.vol_mult is not None or body.rho is not None:
            res["correlation_stress"] = _corr_stress_cube(
                d, body.book, body.vol_mult or 1.0, body.rho or 0.0)
        return res
    return await run_in_threadpool(run)


@app.get("/reverse_stress")
async def reverse_stress(loss: float | None = None, date: str | None = None, book: str = "Soros"):
    """Reverse stress: for a target book loss `L`, the single-factor sigma move that would produce it,
    per factor, ranked by |sigma| (smallest = the book's most vulnerable factor). Default L = the
    Total VaR 99 desk limit (limits.json), else 0.05."""
    L = loss if loss is not None else (_load_limits().get("book", {})
                                       .get("Scenario VaR 99", {}).get("limit") or 0.05)
    def run():
        d = date or _latest_date()
        vols = _factor_vols(); x = _factor_exposures(d, book)
        rows = []
        for f, vf in vols.items():
            denom = x.get(f, 0.0) * vf
            sigma = (-L / denom) if abs(denom) > 1e-12 else None
            rows.append({"factor": f, "exposure": x.get(f, 0.0), "vol": vf,
                         "sigma_to_breach": sigma, "abs_sigma": (abs(sigma) if sigma is not None else None)})
        ranked = sorted((r for r in rows if r["abs_sigma"] is not None), key=lambda r: r["abs_sigma"])
        return {"date": d, "book": book, "loss": L, "factors": ranked,
                "weakest": ranked[0] if ranked else None}
    return await run_in_threadpool(run)


# ============================================================================ pre-trade / what-if
# Recompute book risk under a modified weight vector — the cube's risk math reproduced in numpy so a
# hypothetical trade (resize / add / drop) needs no cube rebuild. Factor P&L vector = R · (Lᵀ w), the
# diagonal specific block = Σ wᵢ²σᵢ², and HHI from the marginal-Total-VaR shares (self-consistent:
# the marginals sum to book Total VaR, so shares sum to 1). "Before" ≈ the cube's reported figures
# (small quantile-interpolation differences); the value is the BEFORE→AFTER delta.

_Z99 = 2.326


def _book_inputs(date: str, book: str):
    """Universe loadings L (Position×Factor, incl Market), as-of weights w, specific var s, and the
    daily factor-return panel R aligned to L's factors — the pieces the risk math needs."""
    f = S["frames"]; d = pd.Timestamp(date)
    exp_d = f["exposures"][f["exposures"]["Date"] == d]
    L = exp_d.pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first").fillna(0.0)
    wide = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
    factors = [c for c in L.columns if c in wide.columns]
    L = L[factors]
    R = wide[factors].to_numpy()
    pos = f["positions"]; asof = pos[(pos["Book"] == book) & (pos["Date"] <= d)]
    bp = asof[asof["Date"] == asof["Date"].max()] if len(asof) else asof
    held = bp.set_index("Position")["Weight"] if len(bp) else pd.Series(dtype=float)
    w = pd.Series(0.0, index=L.index)
    w.loc[w.index.intersection(held.index)] = held.reindex(w.index.intersection(held.index))
    svd = f["specific_var"][f["specific_var"]["Date"] == d].set_index("Position")["SpecificVar"]
    s = pd.Series(0.0, index=L.index)
    s.loc[s.index.intersection(svd.index)] = svd.reindex(s.index.intersection(svd.index))
    return L, w, s, R


def _risk_from_weights(w: pd.Series, L: pd.DataFrame, s: pd.Series, R: np.ndarray) -> dict:
    """Book risk for a weight vector. `model_vol_1d` (σ = √(x'Fx + w'Δw)) is the desk's REFERENCE
    risk number (2026-07-03 decision); the scenario VaR/ES quantiles are the LIMIT metrics;
    `total_var_99` is the legacy house composite, kept but demoted. Plus gross/net and the top-5
    CTR share (the ch-09 concentration idiom: the 5 largest names' share of the
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
    # top-5 share of the marginal-Total-VaR contributions (read off the book's 1% tail day)
    ti = int(np.argsort(pnl)[int(np.floor(0.01 * (n - 1)))])
    msv = -(wv * (Lv @ R[ti]))                       # marginal Scenario VaR per name
    Fro = float(np.sum(msv))                          # = -pnl[ti]; book factor-VaR read-off
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
async def contributions(date: str | None = None, book: str = "Soros"):
    """Euler risk contributions — the ch-09 standard reports, SERVED FROM THE CUBE measures
    (`Marginal Model vol` per name == CTR; `Factor variance contribution` per factor == CTV;
    `Model vol` book σ) so this endpoint and the pivot grid can never disagree. The retained
    numpy implementation (_euler_contributions) is recomputed on every call as an independent
    cross-check and reported in `verification` — the tie-out made permanent."""
    def run():
        d = date or _latest_date()
        # numpy reference — the independent implementation, kept as a live cross-check
        L, w, s, R = _book_inputs(d, book)
        if not float(np.abs(w.to_numpy()).sum()):
            raise HTTPException(404, f"no {book} positions at {d}")
        F = np.cov(R, rowvar=False)
        e = _euler_contributions(w.to_numpy(), L.to_numpy(), F, s.to_numpy())
        ref_ctv = {str(f): float(e["ctv"][i]) for i, f in enumerate(L.columns)}
        ref_ctr = {str(p): float(e["ctr"][i]) for i, p in enumerate(L.index)}
        # cube-served numbers (single source of truth with the grid), HistFull = the model σ
        cube = S["cube"]; l, m = cube.levels, cube.measures
        flt = (l["Date"] == _date(d)) & (l["ScenarioSet"] == "HistFull")
        if "Book" in {n for _, n in cube.hierarchies}:
            flt &= (l["Book"] == book)
        bk = cube.query(m["Model vol"], m["Scenario PnL vol"], m["Specific variance"], filter=flt)
        if not len(bk):
            raise HTTPException(404, f"no cube cell at {d} / HistFull")
        vol = float(bk.iloc[0]["Model vol"])
        fac_var = float(bk.iloc[0]["Scenario PnL vol"]) ** 2
        svar = float(bk.iloc[0]["Specific variance"])
        total_var = fac_var + svar
        dfF = (cube.query(m["Net exposure"], m["Factor variance contribution"],
                          levels=[l["Factor"]], filter=flt).reset_index())
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
        return {
            "date": d, "book": book, "source": "cube",
            "vol_1d": vol, "var99_normal": _Z99 * vol,
            "factor_variance": fac_var, "specific_variance": svar,
            "total_variance": total_var,
            "factor_share": (fac_var / total_var) if total_var > 0 else None,
            "sum_ctr": float(dfP["Marginal Model vol"].sum()),   # = vol_1d, Euler (all names)
            "sum_ctv": float(dfF["Factor variance contribution"].sum()),   # = factor_variance
            "factors": factors, "positions": positions,
            "verification": verification,
            "note": ("CTR (positions) is in VOL units and sums exactly to book vol; CTV (factors) "
                     "is in VARIANCE units and sums to factor variance — different unit pairings, "
                     "never compare directly. Negative CTV = the exposure hedges the book. MCR is "
                     "a rate (risk per unit weight), nothing to sum. Model vol on the full "
                     "factor-return history — distinct from the scenario-VaR views. Served from "
                     "the cube measures; `verification` is the live numpy cross-check."),
        }
    return await run_in_threadpool(run)


_CUBE_RISK_KEYS = {"model_vol_1d": "Model vol", "scenario_var_99": "Scenario VaR 99",
                   "scenario_var_975": "Scenario VaR 97.5", "es_975": "Scenario ES 97.5",
                   "es_99": "Scenario ES 99", "specific_vol": "Specific vol",
                   "total_var_99": "Total VaR 99", "top5_ctr_share": "Top-5 risk share",
                   "gross": "Gross weight", "net": "Net weight"}
_WHATIF_AUX_KEYS: tuple = ()                            # every key is cube-served now


def _cube_risk_block(date: str, book: str, scenario: str | None = None) -> dict:
    """The what-if risk keys read from the CUBE at (date, book, HistFull) — optionally on a
    transient what-if source-scenario branch. One query."""
    cube = S["cube"]; l, mm = cube.levels, cube.measures
    flt = (l["Date"] == _date(date)) & (l["ScenarioSet"] == "HistFull")
    if "Book" in {n for _, n in cube.hierarchies}:
        flt &= (l["Book"] == book)
    kw = {"scenario": scenario} if scenario is not None else {}
    q = cube.query(*[mm[v] for v in _CUBE_RISK_KEYS.values()], filter=flt, **kw)
    if not len(q):
        raise HTTPException(404, f"no cube cell at {date} / HistFull")
    row = q.iloc[0]
    return {k: float(row[v]) for k, v in _CUBE_RISK_KEYS.items()}


def _whatif_result(date: str, book: str, trades: list) -> dict:
    """Before/after book risk under a set of trades. The risk keys are SERVED FROM THE CUBE
    (base cell + a transient source-scenario branch carrying the trades), so /whatif, the grid
    and every other cube consumer share one implementation; the numpy engine
    (_risk_from_weights) is recomputed on every call as the live cross-check (`verification`)
    and still supplies the weight arithmetic (gross/net) and the mtv-based top-5 share. Falls
    back to serving the numpy numbers (source="numpy_fallback") if the cube path fails."""
    L, w, s, R = _book_inputs(date, book)
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
        cube_before = _cube_risk_block(date, book)
        if trades:
            branch = f"whatif-{uuid.uuid4().hex[:12]}"
            session.tables["Positions"].scenarios[branch].load(
                _whatif_branch_rows(date, book, trades))
            cube_after = _cube_risk_block(date, book, scenario=branch)
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
    # name) are invisible to the risk math; disclose them rather than let the book quietly
    # sum below 1. holdings + unpriced together recover the full 13F weight.
    pos = S["frames"]["positions"]
    asof = pos[(pos["Book"] == book) & (pos["Date"] <= pd.Timestamp(date))]
    bp = asof[asof["Date"] == asof["Date"].max()] if len(asof) else asof
    unpriced = [{"position": p, "ticker": tk.get(p, p), "weight": float(wt)}
                for p, wt in bp.set_index("Position")["Weight"].items() if p not in L.index]
    unpriced.sort(key=lambda u: -u["weight"])
    # the full tradeable coverage universe (every name with loadings this date) — so the UI can add
    # a name that isn't currently held, not just resize/drop holdings.
    universe = [{"position": p, "ticker": tk.get(p, p)} for p in L.index]
    universe.sort(key=lambda u: u["ticker"])
    return {"date": date, "book": book, "trades": applied, "before": before, "after": after,
            "delta": delta, "holdings": holdings, "universe": universe,
            "unpriced": unpriced, "priced_weight": float(w.sum()),
            "source": source, "verification": verification}


class WhatIfBody(BaseModel):
    trades: list[dict] = []          # [{position, weight}] — absolute target weight (0 = drop)
    date: str | None = None
    book: str = "Soros"


@app.post("/whatif")
async def whatif(body: WhatIfBody):
    """Pre-trade what-if: book VaR/ES/Total VaR/Specific vol/HHI before vs after a set of hypothetical
    trades (absolute target weight per position; 0 drops it; a universe name not currently held adds
    it), plus gross/net. Empty `trades` returns the current holdings so the UI can bootstrap the
    editor. Risk is recomputed in numpy from the same loadings/returns/specvar the cube uses."""
    for t in body.trades:
        if "position" not in t or "weight" not in t:
            raise HTTPException(400, "each trade needs {position, weight}")
    return await run_in_threadpool(_whatif_result, body.date or _latest_date(), body.book, body.trades)


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
                    book: str = Query("Soros"),
                    participation: float = Query(0.20, gt=0, le=1,
                                                 description="fraction of ADV traded per day"),
                    horizon: float = Query(5.0, gt=0, description="days to flag a name as illiquid")):
    """Days-to-liquidate for the held book: per name MV / (participation·ADV), the share of book value
    liquidatable within `horizon` days, the weighted-average days, and the worst (least-liquid) names.
    Names with no ADV are reported separately, never counted as instantly liquid."""
    def run():
        f = S["frames"]; pos = f["positions"]
        d = pd.Timestamp(date) if date else pd.Timestamp(pos["Date"].max())
        bk = pos[(pos["Book"] == book) & (pos["Date"] == d)].copy()
        if bk.empty:
            raise HTTPException(404, f"no positions for {book} on {d.date()}")
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
            "date": str(d.date()), "book": book,
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
                     "on the held book — not a live order book."),
        }
    return await run_in_threadpool(run)


# ============================================================================ PnL attribution (Step 15)
# Realized PnL split into factor + residual (docs/pnl-attribution-plan.md). The heavy daily engine
# (drifting weights, exact reconstruction) is the barra_pnl_attribution.py precompute; these
# endpoints read its artifact + the in-memory frames and add the statistics the cube can't express
# (Carino linking, the residual diagnostics, the §4 risk↔PnL linkage with the stressed band).
# The additive drill (Factor contribution / Specific PnL / Realized PnL) lives in the CUBE and is
# reached through /pivot — these endpoints are the period headline + diagnostics + reconcile.

def _attr_artifact() -> pd.DataFrame:
    """The precompute artifact, cached on S and reloaded when the file changes."""
    p = _pnl.ARTIFACT
    if not p.exists():
        raise HTTPException(404, "pnl_attribution.parquet missing — run barra_pnl_attribution.py "
                                 "after a v2 build (needs the specific_returns frame)")
    mt = p.stat().st_mtime
    if S.get("pnl_attr_mtime") != mt:
        a = pd.read_parquet(p)
        a["Date"] = pd.to_datetime(a["Date"])
        S["pnl_attr"], S["pnl_attr_mtime"] = a, mt
    return S["pnl_attr"]


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


def _name_attr(lo: pd.Timestamp, hi: pd.Timestamp, book: str,
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
    parts = []
    for d0 in d0s:
        nxt = exp_dates[np.searchsorted(exp_dates, np.datetime64(d0)) + 1] \
            if np.searchsorted(exp_dates, np.datetime64(d0)) + 1 < len(exp_dates) else None
        w_ = pos[(pos["Book"] == book) & (pos["Date"] == d0)].groupby("Position")["Weight"].sum()
        if w_.empty:
            continue
        frd = frt[(frt["Date"] > d0) & ((frt["Date"] <= nxt) if nxt is not None else True)]
        fsum = frd.groupby("Factor")["Return"].sum()
        srd = sr[(sr["Date"] > d0) & ((sr["Date"] <= nxt) if nxt is not None else True)]
        eps = srd[srd["Position"].isin(w_.index)].groupby("Position")["SpecificReturn"].sum()
        Ld = (exp[(exp["Date"] == d0) & (exp["Position"].isin(w_.index))]
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
                          book: str = "Soros",
                          by: str | None = Query(None, description="sector|name for a breakdown")):
    """Period PnL attribution headline: realized book return (geometric, Carino-linked) split into
    factor + specific, the cumulative hero series, the by-factor table (avg exposure, cumulative
    factor return, linked contribution, t-stat), coverage, and an optional sector/name breakdown.
    Default window: trailing 12 months of the artifact."""
    def run():
        art = _attr_artifact()
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
            "from": str(lo.date()), "to": str(hi.date()), "book": book,
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
            na = _name_attr(lo, hi, book)
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


def _pred_book_vols(months: list, book: str) -> tuple[dict, dict, dict]:
    """Per month-start d0: predicted DAILY book vol sqrt(x'Fx + Σw²σ²), predicted daily specific
    vol, and per-factor daily vol |x_k|·σ_k — all POINT-IN-TIME (history ≤ d0, no look-ahead).
    SERVED FROM THE CUBE's PIT:* truncated-history sets (Model vol / Specific vol / per-factor
    Scenario PnL vol at (Date=d0, ScenarioSet=PIT:d0)); the numpy F(≤t) implementation is
    recomputed alongside as the live cross-check (max diffs stashed in
    S["pred_vols_verification"], served by /calibration), and is the per-month fallback where a
    PIT set is absent (early months under the 60-obs floor)."""
    f = S["frames"]
    frw = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
    ref_book, ref_spec, ref_fac = {}, {}, {}
    for d0 in months:
        pos = f["positions"]
        w_ = pos[(pos["Book"] == book) & (pos["Date"] == d0)].groupby("Position")["Weight"].sum()
        if w_.empty:
            continue
        exp_d = f["exposures"][(f["exposures"]["Date"] == d0)
                               & (f["exposures"]["Position"].isin(w_.index))]
        L = exp_d.pivot_table(index="Position", columns="Factor", values="Loading",
                              aggfunc="first").reindex(w_.index).fillna(0.0)
        hist = frw.loc[frw.index <= d0]
        if len(hist) < 60:
            continue
        facs = [c for c in L.columns if c in hist.columns]
        x = L[facs].T @ w_
        F = hist[facs].cov().to_numpy()
        sv = f["specific_var"][f["specific_var"]["Date"] == d0].set_index("Position")["SpecificVar"]
        svar = float((w_ ** 2 * sv.reindex(w_.index).fillna(0.0)).sum())
        ref_book[d0] = float(np.sqrt(max(x.to_numpy() @ F @ x.to_numpy() + svar, 0.0)))
        ref_spec[d0] = float(np.sqrt(svar))
        ref_fac[d0] = {f_: abs(float(x[f_])) * float(hist[f_].std()) for f_ in facs}
    # cube-served PIT values, numpy as fallback + cross-check
    book_v, spec_v, fac_v = dict(ref_book), dict(ref_spec), {k: dict(v) for k, v in ref_fac.items()}
    diffs = {"book": 0.0, "specific": 0.0, "factor": 0.0, "months_from_cube": 0}
    try:
        cube = S["cube"]; l, mm = cube.levels, cube.measures
        have_book = "Book" in {n for _, n in cube.hierarchies}
        for d0 in list(ref_book):
            pit = f"PIT:{pd.Timestamp(d0).date()}"
            flt = (l["Date"] == _date(str(pd.Timestamp(d0).date()))) & (l["ScenarioSet"] == pit)
            if have_book:
                flt &= (l["Book"] == book)
            q = cube.query(mm["Model vol"], mm["Specific vol"], filter=flt)
            if not len(q) or pd.isna(q.iloc[0]["Model vol"]):
                continue                                   # no PIT set for this month — numpy stands
            qf = cube.query(mm["Scenario PnL vol"], levels=[l["Factor"]], filter=flt).reset_index()
            bv, sv_ = float(q.iloc[0]["Model vol"]), float(q.iloc[0]["Specific vol"])
            fv = {str(r["Factor"]): float(r["Scenario PnL vol"]) for _, r in qf.iterrows()
                  if not pd.isna(r["Scenario PnL vol"])}
            diffs["book"] = max(diffs["book"], abs(bv - ref_book[d0]))
            diffs["specific"] = max(diffs["specific"], abs(sv_ - ref_spec[d0]))
            diffs["factor"] = max([diffs["factor"]] + [abs(fv[k] - v) for k, v in ref_fac[d0].items()
                                                       if k in fv])
            book_v[d0], spec_v[d0] = bv, sv_
            fac_v[d0] = {k: fv.get(k, ref_fac[d0].get(k)) for k in ref_fac[d0]}
            diffs["months_from_cube"] += 1
    except Exception as e:
        diffs["error"] = f"{e.__class__.__name__}: {e}"
    S["pred_vols_verification"] = diffs
    return book_v, spec_v, fac_v


@app.get("/pnl_attribution/residual")
async def pnl_attribution_residual(frm: str | None = Query(None, alias="from"),
                                   to: str | None = None, book: str = "Soros"):
    """§2 residual diagnostics with plain RAG verdicts: is the residual LARGE (specific share, IR,
    realized-vs-predicted specific vol, explained share) and is it CORRELATED (lag-1/2
    autocorrelation, residual-vs-factor regression) — plus the Barra bias statistics (book /
    specific / per-factor) and residual concentration + hit rate. Thresholds start loose."""
    def run():
        art = _attr_artifact()
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
        book_v, spec_v, fac_v = _pred_book_vols(months, book)
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
        bias_book, bw_book = _z(r_m, book_v)
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
        na = _name_attr(lo, hi, book)
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
            st_ = "green" if abs(ac) < 0.2 else ("red" if abs(ac) > 0.35 else "amber")
            checks.append(_chk(nm, ac, st_,
                               "memoryless — independent bets" if st_ == "green" else
                               "residual trends — a persistent unhedged bet"))
        if reg["r2"] is not None:
            st_ = "green" if reg["r2"] < 0.10 else ("red" if reg["r2"] > 0.25 else "amber")
            top = reg["loadings"][0] if reg["loadings"] else None
            checks.append(_chk("Residual-vs-factor R²", reg["r2"], st_,
                               "orthogonal — clean alpha" if st_ == "green" else
                               (f"hidden beta — loads on {top['factor']}" if top else "hidden beta")))
        for nm, b, bw in (("Bias stat — book", bias_book, bw_book),
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
            "from": str(lo.date()), "to": str(hi.date()), "book": book,
            "n_months": int(len(u_m)), "status": overall, "checks": checks,
            "specific_share": spec_share, "explained_share": expl,
            "factor_regression": reg, "factor_bias": fac_bias,
            "concentration": conc,
            "hit_rate": {"names": hit_names, "months": hit_months},
            "note": ("Uncorrelated, well-sized residual = genuine diversified stock-picking. "
                     "Autocorrelation = a slow unhedged bet; factor correlation = hidden beta; "
                     "thresholds start loose (tighten once the book's distribution is seen)."),
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
                                  book: str = "Soros",
                                  vol_mult: float = Query(1.25, gt=0),
                                  rho: float = Query(0.75, ge=0, le=1),
                                  min_weight: float = Query(
                                      0.001, ge=0, le=0.1,
                                      description="materiality floor on w(T) for the position "
                                                  "surprises (z is scale-invariant, so dust "
                                                  "positions would otherwise crowd the table); "
                                                  "sub-floor breaches are disclosed, not dropped")):
    """§4 linkage: the risk decomposition at T read against the realized PnL over T→T+horizon.
    Per factor (plus Specific and the book total): the start-of-period ±2σ BASE band, a STRESSED
    band (vols ×vol_mult, correlations blended toward 1 by rho — correlations only enter the
    aggregate, so the book band widens more than any factor's), the realized contribution (dot),
    the surprise z-score, and a within/stress/investigate verdict. Plus per-position surprises
    (weight ≥ min_weight; sub-floor breaches listed in `dust_excluded`)."""
    def run():
        art = _attr_artifact()
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
        # the book actually carried (the artifact's daily drifting exposures)
        xe = (art[art["Kind"] == "exposure"]
              .pivot_table(index="Date", columns="Source", values="Value", aggfunc="first")
              .sort_index())
        xwin = xe.loc[(xe.index > t0) & (xe.index <= t1)]
        # ex-ante at T: exposures, factor covariance on history <= T, specific block
        pos = f["positions"]
        w_ = pos[(pos["Book"] == book) & (pos["Date"] == t0)].groupby("Position")["Weight"].sum()
        if w_.empty:
            raise HTTPException(404, f"no {book} positions at {t0.date()}")
        exp_d = f["exposures"][(f["exposures"]["Date"] == t0)]
        Lu = exp_d.pivot_table(index="Position", columns="Factor", values="Loading",
                               aggfunc="first")
        frw = f["factor_returns"].pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
        hist = frw.loc[frw.index <= t0]
        facs = [c_ for c_ in Lu.columns if c_ in hist.columns]
        L = Lu.reindex(w_.index)[facs].fillna(0.0)
        x = L.T @ w_
        F = hist[facs].cov().to_numpy()
        Fs = _pnl._stressed_cov(F, vol_mult, rho)
        sv = f["specific_var"][f["specific_var"]["Date"] == t0].set_index("Position")["SpecificVar"]
        svar = float((w_ ** 2 * sv.reindex(w_.index).fillna(0.0)).sum())
        sig = np.sqrt(np.diag(F));  sig_s = np.sqrt(np.diag(Fs))
        xv = x.to_numpy()
        sd_book = float(np.sqrt(max(xv @ F @ xv + svar, 0.0)) * np.sqrt(h))
        sd_book_s = float(np.sqrt(max(xv @ Fs @ xv + svar * vol_mult ** 2, 0.0)) * np.sqrt(h))

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
            # specific/book rows have no exposure to migrate — a breach there is the risk
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
        real_book = float(win["Realized"].sum()) if "Realized" in win else 0.0
        book_verdict = _verdict(real_book, sd_book, sd_book_s)
        book_row = {"name": "Book total", "kind": "book", "exposure": None, "risk_share": 1.0,
                    "realized": real_book, "sd_base": sd_book, "sd_stressed": sd_book_s,
                    "z": (real_book / sd_book) if sd_book > 0 else None,
                    "verdict": book_verdict}
        drv_bk = _agg_driver(real_book, sd_book, book_verdict, "the book bias stat and /backtest")
        if drv_bk:
            book_row["driver"] = drv_bk
        # per-position surprises: realized name PnL vs its own ex-ante sd at T
        na = _name_attr(t0, t1, book)
        tk = _ticker_map()
        Lv = L.to_numpy()
        name_var = np.einsum("ij,jk,ik->i", Lv, F, Lv) + sv.reindex(w_.index).fillna(0.0).to_numpy()
        name_sd = np.abs(w_.to_numpy()) * np.sqrt(np.clip(name_var, 0.0, None)) * np.sqrt(h)
        # in-window average as-of weight per name (the band froze w at T; the 13F re-anchor /
        # resizes inside the window are the position analogue of exposure migration)
        mwin = [d for d in exp_dates if t0 <= d < t1]
        wpath = (pos[(pos["Book"] == book) & (pos["Date"].isin(mwin))]
                 .pivot_table(index="Date", columns="Position", values="Weight", aggfunc="sum")
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
            top_s = (f"{top_f} {top_v * w_t:+.2%} of book" if top_f else "n/a")
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
            # out-rank real holdings on |z| while being unable to move the book. Below the floor
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
            srf = f.get("specific_returns")
            if srf is not None:
                sub = srf[(srf["Position"].isin(breach_ids))
                          & (srf["Date"] > t0) & (srf["Date"] <= t1)]
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
            "n_days": int(h), "book": book,
            "stress": {"vol_mult": vol_mult, "rho_blend": rho},
            "book_total": book_row, "rows": rows, "positions": positions[:15],
            "min_weight": min_weight,
            "dust_excluded": {"n": len(dust),
                              "names": [{"name": r["name"], "weight": r["weight"],
                                         "z": r["z"], "verdict": r["verdict"]}
                                        for r in dust[:10]]},
            "breach_comovement": comove,
            "surprises": [r for r in rows + [book_row] if r["verdict"] == "investigate"],
            "note": ("Bands are the start-of-period risk made visible: half-width = 2σ where σ² = "
                     "x'Fx (+ the diagonal specific block), scaled √days. The model forecasts "
                     "dispersion, not direction — bands centre at zero. Realized inside base = "
                     "risk understood; outside base but inside stressed = a stress regime; outside "
                     "stressed = a risk the decomposition missed — investigate (gain or loss "
                     "alike). Correlations only enter the aggregate rows, so the book band widens "
                     "under the correlation shock even where no single factor's does. Rows outside "
                     "the base band carry a driver read: the band freezes x at T, so a breach is "
                     "either a genuine factor move or the exposure migrating inside the window "
                     "(loading refresh / 13F re-anchor) — an ill-conditioned z, not a factor "
                     "event."),
        }
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
                      book: str = "Soros"):
    """Fit-for-purpose calibration over time: the ROLLING bias statistic b = std(realized /
    predicted vol) over a trailing window, with the 1 ± √(2/window) acceptance band — run for
    the whole book and the specific block — plus 2σ exceedance counts (expected ≈ 4.6%)."""
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
        key = ("pred_vols_full", book, str(months[-1].date()) if months else "")
        if S.get("pred_vols_key") != key:
            S["pred_vols"], S["pred_vols_key"] = _pred_book_vols(months, book), key
            # pin THIS computation's cross-check to the cache (the stash is last-writer-wins
            # and /pnl_attribution/residual also calls _pred_book_vols on its own window)
            S["pred_vols_verif_cal"] = S.get("pred_vols_verification")
        book_v, spec_v, _fv = S["pred_vols"]
        ndays = c["Realized"].resample("ME").count()

        def pred_m(pred_daily: dict) -> pd.Series:
            pv = pd.Series({(pd.Timestamp(k) + pd.offsets.MonthEnd(1)): v
                            for k, v in pred_daily.items()})
            return (pv * np.sqrt(ndays.reindex(pv.index).astype(float))).dropna()

        out = {}
        for name, realized, pred in (("book", r_m, pred_m(book_v)),
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
            "window": window, "book": book, "expected_exceedance_2s": 0.0455,
            "source": "cube",
            "pit_verification": S.get("pred_vols_verif_cal"),
            "series": out,
            "note": ("b ≈ 1 = calibrated; b > 1 = risk under-forecast (the dangerous direction); "
                     "the band is the 95% acceptance range 1 ± √(2/window). Exceedances are "
                     "months beyond ±2 predicted σ — a fat-tail read the std-based b can miss. "
                     "Realized is the attribution artifact's book return (drifting weights, "
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
                     "recent window is the diversification the book leans on decaying — the "
                     "stressed band's ρ→1 blend is the deliberate exaggeration of that."),
        }
    return await run_in_threadpool(run)


# ---- exposure profile (ch 03: what each factor IS, and where the book sits in it) ----

FACTOR_RECIPES = {
    "Market": "intercept — every name loads 1.0; carries the cross-sectional average return",
    "Beta": "252d regression beta of daily returns on the market index",
    "ResidVol": "annualized std of the 252d market-regression residual",
    "Momentum": "12-1 month price return (t−252d → t−21d)",
    "Liquidity": "log trailing-63d average dollar volume",
    "Size": "log market cap (close × PIT shares)",
    "NonLinSize": "log-mcap cubed, orthogonalized to Size on the estimation fit",
    "Value": "book-to-price: PIT equity / mcap",
    "EarnYield": "earnings yield: PIT net income / mcap",
    "Leverage": "assets / equity (PIT)",
    "Growth": "period-over-period asset growth (PIT)",
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
async def exposure_profile(factor: str, date: str | None = None, book: str = "Soros"):
    """One factor's cross-section at a date: the loading distribution (histogram + quantiles),
    the ±3 estimation winsor bounds, the uncapped tail beyond them (coverage names showing their
    true tilt), and the held book overlaid — the 'model-conditional: this is what OUR {factor}
    means' view, with the descriptor recipe attached."""
    def run():
        f = S["frames"]; exp = f["exposures"]
        d0 = _snap_exposure_date(exp, date)
        sub = (exp[(exp["Date"] == d0) & (exp["Factor"] == factor)]
               .set_index("Position")["Loading"].dropna())
        if sub.empty:
            raise HTTPException(400, f"unknown factor or no loadings: {factor}")
        pos = f["positions"]
        w_ = pos[(pos["Book"] == book) & (pos["Date"] == d0)].groupby("Position")["Weight"].sum()
        tk = _ticker_map()
        held = sorted(
            [{"ticker": tk.get(p, p), "weight": float(wt), "loading": float(sub[p])}
             for p, wt in w_.items() if p in sub.index],
            key=lambda r: -abs(r["loading"]))
        edges = np.linspace(min(float(sub.min()), -3.5), max(float(sub.max()), 3.5), 41)
        cnt, _ = np.histogram(sub, bins=edges)
        beyond = sub[sub.abs() > 3].abs().sort_values(ascending=False)
        return {
            "factor": factor, "date": _clean(d0), "book": book,
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
    """Per factor: book vol before/after NEUTRALIZING it (x_k → 0 via -x_k units of the pure
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
async def hedge(date: str | None = None, book: str = "Soros"):
    """What hedging each factor would do — SERVED FROM THE CUBE measures (`Vol ex factor` per
    factor = vol after zeroing that net exposure with the specific block kept; `Min-variance
    hedge ratio` / `Vol at min-variance hedge` = the D6 single-instrument hedge), ranked by vol
    saved. The retained numpy `_hedge_table` is recomputed on every call as an independent
    cross-check (`verification`). Specific risk is untouched by construction."""
    def run():
        d = date or _latest_date()
        # numpy reference — the independent implementation, kept as a live cross-check
        L, w, s, R = _book_inputs(d, book)
        if not float(np.abs(w.to_numpy()).sum()):
            raise HTTPException(404, f"no {book} positions at {d}")
        F = np.cov(R, rowvar=False)
        x = L.to_numpy().T @ w.to_numpy()
        svar = float(np.sum(w.to_numpy() ** 2 * s.to_numpy()))
        ref = _hedge_table(x, F, svar, list(L.columns))
        ref_after = {r["factor"]: r["vol_after"] for r in ref["rows"]}
        # cube-served numbers (HistFull = the model σ)
        cube = S["cube"]; l, m = cube.levels, cube.measures
        flt = (l["Date"] == _date(d)) & (l["ScenarioSet"] == "HistFull")
        if "Book" in {n for _, n in cube.hierarchies}:
            flt &= (l["Book"] == book)
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
            "date": d, "book": book, "source": "cube",
            "vol_base": vol_base, "specific_vol": spec_vol,
            "rows": rows, "market_hedge": mkt,
            "verification": verification,
            "note": ("hedge_units = −x_k of the pure factor-k portfolio (ch 07's f̂ = Pr dual) — "
                     "implementable in principle, but pure portfolios carry real leverage/turnover "
                     "cost. The market h* is the D6 single-instrument minimum-variance hedge "
                     "(h* = −β of the book on the Market factor). Vol is model vol σ² = x'Fx + "
                     "w'Δw; the specific block survives any factor hedge. Served from the cube "
                     "measures; `verification` is the live numpy cross-check."),
        }
    return await run_in_threadpool(run)


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
        styles = [c for c in Ld.columns if c != "Market"]
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
        if factor != "Market" and factor not in cols:
            raise HTTPException(400, f"factor not in the fit at {d0.date()}: {factor}")
        if "Size" not in cols:
            raise HTTPException(404, "Size missing from the fit — cannot form the WLS weights")
        X = np.column_stack([np.ones(len(Xd)), Xd[cols].values])
        W2 = np.exp(Xd["Size"].values / 2.0)                  # (exp(Size/4))² — the builder's W²
        names = ["Market"] + cols
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
                                book: str = "Soros", top: int = Query(12, ge=3, le=50)):
    """The specific PnL name by name over the window: top winners and losers by |specific|, each
    with sign persistence (share of consecutive same-sign months — a real edge or a stale 13F
    reads persistent; noise mean-reverts) and the share of months positive."""
    def run():
        art = _attr_artifact()
        _c, lo, hi = _attr_window(art, frm, to)
        na, panel = _name_attr(lo, hi, book, monthly=True)
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
            "from": str(lo.date()), "to": str(hi.date()), "book": book,
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
- Dry and occasionally aphoristic — one compressed line that lands ("most of this book is one
  bet on the market") beats a paragraph.
- Cite the figure next to every claim. A sentence without a number is a candidate to cut.
- If the honest read is one line, write one line. Never pad.

Doctrine — the lens for every read:
- The risk team's job is to understand ALL the risks the book is taking, not to avoid losses.
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
equity factor-risk model. The book is the Soros Fund Management 13F holdings, run as a long-only
weight overlay; monthly calendar from 2016 to the latest build.

The model has two risk blocks: a linear FACTOR P&L block and a diagonal SPECIFIC (idiosyncratic)
block. Read the measures as follows:
- Numbers are fractions of book value. 0.035 means 3.5%. VaR/ES/vol are losses, reported positive.
- Net exposure: aggregated factor loading (weight x loading). Market carries a loading of 1.0 per
  name, so a fully invested book has ~unit Market exposure.
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
  contribution to the book number (the contributions sum to the book total). "% of ..." is that
  share, summing to 100%. Incremental VaR: the risk RELEASED by removing a member — diversification-
  aware, NOT additive (it does not sum to the book total), so there is no "% of" for it.
- Marginal Model vol: the member's EULER contribution to book model vol (per name this IS the
  ch-09 CTR = w·(Σw)/σ); sums exactly to Model vol; read it in by-NAME views (by Factor the
  specific block fans out). Incremental Model vol: the vol released by removing the member —
  sub-additive like Incremental VaR, no "% of".
- VaR sensitivity: per-unit dVaR/dexposure. Risk HHI: Herfindahl index of each name's share of book
  Total VaR — 1/N for an evenly diversified book up to 1.0 for a single name; 1/HHI ~ the effective
  number of independent risk bets.
- drawdown (separate `drawdown` block, not a pivot measure): max peak-to-trough of the book's
  cumulative P&L if the *current* book had been held over the scenario set's daily path — a
  path-dependent lens VaR/ES cannot see. `max_drawdown` is a negative fraction; `longest_underwater_obs`
  is the longest run (trading days) below a prior peak; `recovered` says whether it climbed back.
- Factor contribution / Specific PnL / Realized PnL: REALIZED monthly PnL attribution (not risk).
  Additive; Realized = Σ factor contributions + Specific. FORWARD-month convention: the value at
  Date d0 is the PnL over the month AFTER d0. Specific PnL is per-name (it fans out by Factor —
  read it in by-name or book views). No ScenarioSet needed.

Scenario sets (the shock source):
- HistFull: full historical simulation. Evt:* : a past window replayed (COVID2020, Rates2022,
  Selloff2018). Hypo:* : hand-set sigma shocks (ValueRotation, RiskOff, MomentumCrash).
- KEY CAVEAT: every name shares the uniform Market loading of 1.0, so in any set that contains real
  market moves (HistFull, the Evt:* replays), Market dominates book risk (~95%) and risk is as
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
  numbers as the hypothetical book, not the held one. Limits/drawdown/attribution context blocks
  remain the BASE book.
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
    # desk-limit status + drawdown for the view's own date/set/book (so the model can lead with a
    # breach and cite the path-drawdown lens VaR/ES miss). Headline only — the dd path is dropped.
    lim = dd = None
    ldate = (fdict.get("Date") or [None])[0]
    lbook = (fdict.get("Book") or ["Soros"])[0]
    if ldate:
        lset = (fdict.get("ScenarioSet") or [_load_limits().get("scenario_set", "HistFull")])[0]
        try:
            lim = await run_in_threadpool(_limits_result, ldate, lset, lbook)
        except Exception:
            lim = None
        ddset = (fdict.get("ScenarioSet") or ["HistFull"])[0]
        try:
            d = await run_in_threadpool(_drawdown_result, ldate, ddset, lbook)
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
                model="claude-opus-4-8", max_tokens=4000,
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
You are writing the MORNING RISK SUMMARY of the whole book — the read a risk manager gives the
desk from the monitor screen. The book is the Soros Fund Management 13F holdings, run long-only
as a weight overlay; monthly calendar from 2016 to the latest build, Barra-style factor model (linear factor block
+ diagonal specific block). Numbers are fractions of book value unless marked.

The payload mirrors the daily loop — read it in this order:
1. `limits` — the hard desk limits. LEAD with any breach (value vs limit), then ambers. All
   green = one line.
2. `risk` + `variance_split` — the decomposition. `model_vol_1d` (σ = √(x'Fx + w'Δw)) is the
   REFERENCE risk number — lead the risk read with it and its factor_share split; scenario
   VaR/ES are the limit metrics; total_var_99 is a legacy house composite, quote it only
   against its limit history. top_ctv are contributions to variance (negative = hedges the
   book); top5_ctr_share is the 5 largest names' share of Total VaR.
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

Output: tight GitHub-flavoured markdown. One-line headline first (the state of the book in a
sentence). Then short sections following the loop order. 150–300 words unless a breach demands
more."""


class OverviewAnalysisBody(BaseModel):
    date: str | None = None
    book: str = "Soros"
    set: str | None = None         # scenario set for the limits read
    notes: str | None = None


@app.post("/overview/analysis")
async def overview_analysis(body: OverviewAnalysisBody):
    """Streamed morning-summary commentary on the WHOLE book — the Overview monitor narrated in
    the desk's risk-manager voice (CHRIS_VOICE). Assembles the same numbers the Overview shows
    (limits, Euler decomposition, reconcile verdicts + drivers, backtest, attribution headline,
    DQ) and hands them to the Messages API with no tools."""
    _rate_limit()
    client = _anthropic()
    d = body.date or _latest_date()
    scen = body.set or _load_limits().get("scenario_set", "HistFull")

    def collect():
        out: dict = {"as_of": d, "book": body.book, "scenario_set": scen}
        try:
            lim = _limits_result(d, scen, body.book)
            out["limits"] = {"status": lim["status"],
                             "checks": [{k: c[k] for k in ("name", "value", "warn", "limit",
                                                           "status")} for c in lim["checks"]]}
        except Exception:
            out["limits"] = None
        try:
            L, w, s, R = _book_inputs(d, body.book)
            risk = _risk_from_weights(w, L, s, R)
            out["risk"] = {k: risk[k] for k in ("model_vol_1d", "scenario_var_99", "es_975",
                                                "specific_vol", "top5_ctr_share", "gross", "net",
                                                "total_var_99")}
            F = np.cov(R, rowvar=False)
            e = _euler_contributions(w.to_numpy(), L.to_numpy(), F, s.to_numpy())
            tv = e["factor_var"] + e["specific_var"]
            order = np.argsort(-np.abs(e["ctv"]))
            out["variance_split"] = {
                "factor_share": (e["factor_var"] / tv) if tv > 0 else None,
                "vol_1d": e["sigma"],
                "top_ctv": [{"factor": str(L.columns[i]),
                             "pct_of_variance": float(e["ctv"][i] / tv)} for i in order[:6]],
            }
        except Exception:
            out["risk"] = out.setdefault("variance_split", None)
        try:
            bt = _backtest_result(d, "HistFull", body.book, 0.01, 250, "fhs", 0.94)
            out["calibration_and_backtest"] = (
                {k: bt.get(k) for k in ("kupiec_reject", "rate", "exceptions", "expected",
                                        "tested")} if bt.get("status") == "ok" else None)
        except Exception:
            out["calibration_and_backtest"] = None
        out["pnl_attribution_t12m"] = _attr_headline()
        try:
            checks = barra_dq_checks.run(S["frames"])
            summ = {k: sum(1 for c in checks if c["level"] == k) for k in ("PASS", "WARN", "FAIL")}
            out["dq"] = {"status": ("fail" if summ["FAIL"] else "warn" if summ["WARN"] else "pass"),
                         "summary": summ}
        except Exception:
            out["dq"] = None
        return out

    payload = await run_in_threadpool(collect)
    # reconcile (risk↔PnL) — reuse the linkage route's computation, trimmed to verdicts + drivers
    try:
        lk = await pnl_attribution_linkage(T=None, horizon=3, book=body.book,
                                           vol_mult=1.25, rho=0.75, min_weight=0.001)
        def trim(r):
            o = {"name": r["name"], "z": r.get("z"), "verdict": r["verdict"]}
            if r.get("driver"):
                o["driver"] = {"kind": r["driver"]["kind"], "text": r["driver"]["text"]}
            return o
        payload["reconcile"] = {
            "window": f"{lk['T']} → {lk['to']}",
            "book_total": trim(lk["book_total"]),
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
                model="claude-opus-4-8", max_tokens=3000,
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
You are writing a short read of the book's RISK TRENDS — monthly time series over the whole
calendar (2016 → the latest build) for one scenario set. The book is the Soros 13F overlay on a Barra-style
factor model. Numbers are fractions of book value; VaR/ES/vol are 1-day losses.

The payload:
- `risk_series` — monthly book measures. `Model vol` (σ = √(x'Fx + w'Δw)) is the REFERENCE
  series — lead with it; Scenario VaR 99 / ES 97.5 are the limit metrics; Total VaR 99 is the
  legacy composite. The trend matters more than the level: where each series sits NOW vs its
  own history, and when it last shifted regime.
- `exposure_series` — net factor exposures by month (quarterly-sampled) + per-factor start/end.
  Exposure paths are the mandate made visible: a persistent move is the book changing character,
  not noise. Whether drift is intentional (rotation) or re-pricing belongs to the drift
  attribution — flag the move here, don't guess the cause.
- `limits` — the standing desk limits, so a rising series can be read against its ceiling
  (headroom shrinking is the story before the breach is).

Hard rules:
- Reason ONLY from the payload; cite figures WITH their dates ("VaR peaked 2020-03 at 6.1%").
- Name the regimes you can see (a spike window, a quiet stretch, a step-change) by date range.
- Say where each headline series is now relative to its history (near its lows / median / highs)
  and its direction over the trailing year.
- Call out the factor exposures whose paths moved most since 2021, with start → end values.

Output: tight GitHub-flavoured markdown. One-line headline (what the trend history says about
today's book). Then short sections: risk trend, exposure drift, headroom. End with "**Watch:**"
— the one or two series most likely to matter next. 120–250 words."""


class TrendsAnalysisBody(BaseModel):
    set: str = "HistFull"
    notes: str | None = None


@app.post("/trends/analysis")
async def trends_analysis(body: TrendsAnalysisBody):
    """Streamed CHRIS_VOICE read of the risk-trends lens: the monthly book-measure series and the
    factor-exposure paths, narrated — regimes, current level vs history, drift, headroom vs the
    desk limits. Same no-tools Messages-API pattern and rate limit as /analysis."""
    _rate_limit()
    client = _anthropic()
    book_ts = await trends(set=body.set,
                           measures="Model vol,Scenario VaR 99,Scenario ES 97.5,"
                                    "Specific vol,Total VaR 99")
    fac_ts = await trends(set=body.set, measures="Net exposure", by="Factor")

    def rnd(v):
        return round(v, 4) if isinstance(v, (int, float)) else v
    risk_series = [{k: rnd(v) for k, v in r.items()} for r in book_ts["records"]]
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
        "risk_series": risk_series,
        "exposure_series": exposure_series,
        "limits": _load_limits().get("book", {}),
        "desk_notes": body.notes or "",
    }, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-4-8", max_tokens=3000,
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
You are writing a short read of the book's REALIZED PnL ATTRIBUTION over one window — the
Soros 13F overlay on a Barra-style factor model. Returns are fractions (0.12 = 12%). The parts
are Cariño-linked: factor contributions + specific sum to the geometric window return EXACTLY.

The payload:
- `headline` — realized geometric return, linked factor total, linked specific total. Specific is
  stock-selection money the factor block can't explain. Understand ALL of it: an unexplained GAIN
  gets investigated exactly like a loss — it is risk that happened to pay.
- `factors` — per factor: avg_exposure (book's mean net loading over the window),
  cum_factor_return (what the factor itself did — portfolio-agnostic), contribution (the money,
  linked), pct_of_total, t_stat (mean daily contribution / SE — t = IR·√T humility: a big
  contribution with |t| < 2 is one good year, not proof; only |t| > 2 is a reliable flow).
  Read exposure-without-return (a tilt that paid nothing) and return-without-exposure (a factor
  that ran while the book stood flat) as findings, not trivia.
- `residual_checks` — the RAG diagnostics on the specific stream (IR, realized/predicted specific
  vol, autocorrelation, residual-vs-factor regression, bias stats, residual HHI, hit rate).
  Correlated residuals = a missing factor. A red here outranks any contribution number.
- `linkage` — the risk↔PnL reconcile verdicts at T (factor rows + positions outside their ex-ante
  bands, with driver reads: exposure_migration is a band artifact, not an event; hidden_beta means
  suspect the loading, not the factor).
- `coverage` — priced share of the book; name the unpriced weight if material.

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
    notes: str | None = None


@app.post("/pnl_attribution/analysis")
async def pnl_attribution_analysis(body: PnlAttrAnalysisBody):
    """Streamed CHRIS_VOICE read of the PnL-attribution lens: the linked window split, factor
    table, residual RAG and linkage verdicts. Same no-tools pattern and rate limit as /analysis."""
    _rate_limit()
    client = _anthropic()          # 502 before the work if there's no key
    # NB internal calls must pass EVERY Query-defaulted param explicitly — a bare call would
    # receive the FastAPI Query objects, not their values (the /overview min_weight lesson)
    attr = await pnl_attribution(body.frm, body.to, book="Soros", by=None)
    resid = await pnl_attribution_residual(body.frm, body.to, book="Soros")
    link = await pnl_attribution_linkage(None, body.horizon, book="Soros",
                                         vol_mult=1.25, rho=0.75, min_weight=0.001)

    def rnd(v):
        return round(v, 5) if isinstance(v, (int, float)) else v
    payload = json.dumps({
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
                for r in link["rows"] + [link["book_total"]] if r["verdict"] != "within"],
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
                model="claude-opus-4-8", max_tokens=3000,
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
# attribution + the book risk delta from the what-if math (cube-consistent). /whatchanged/analysis
# hands that tidy diff to the Messages API (no tools, streamed) for a written read, like /analysis.

WHATCHANGED_SYSTEM = CHRIS_VOICE + """
You are writing a short "what changed" note between two consecutive
Soros 13F filings of a Barra-style equity factor-risk model. You receive only a tidy diff; reason
ONLY from it and cite the figures. Never invent a position, issuer, date, or value.

The payload has:
- positions: names that ENTERED (new), EXITED (dropped), or were RESIZED (weight change) between the
  `from` and `to` filings, with 13F weights (fractions of book, 0.03 = 3%).
- exposure_attribution: the book's net factor exposure (Σ weight·loading) before/after per style
  factor, and the drift Δ split into four sources that sum to Δ exactly — `src_entered`/`src_exited`
  (names rotated in/out = ROTATION), `src_reweighted` (held names resized), `src_loading_drift` (held
  names whose own loadings moved = RE-PRICING). Rotation-dominated drift is a deliberate tilt → the
  desk may update the BENCHMARK; loading-drift-dominated is market re-pricing → update the HEDGE.
- risk: book Scenario VaR 99/97.5, ES 97.5/99, Specific vol, Total VaR 99, Risk HHI, gross/net —
  before vs after vs delta, computed on the full factor-return history (HistFull-equivalent, the
  Market factor included so these read as real long-equity book risk). All are losses, positive.

Hard rules:
- LEAD with the single biggest change (a big new/dropped position, the factor that drifted most, or
  the largest risk move). Then 3-5 bullets of what's notable. Then a short "So what" for the desk.
- For the factor drift, say whether it looks intentional (rotation) or not (loading drift) and name
  the implied action (benchmark vs hedge) — but only where the attribution actually supports it.
- Cite weights and deltas. Tie risk moves back to the position/exposure changes that drove them.
- Write plainly: direct, short sentences, tight GitHub-flavoured markdown. No preamble."""


def _prior_filing_date(bpos: pd.DataFrame, d1: pd.Timestamp):
    """The latest date strictly before d1 whose held-name set differs from d1's — i.e. the previous
    distinct 13F book (the positions frame is monthly and flat between quarterly filings)."""
    dates = sorted(d for d in pd.to_datetime(bpos["Date"].unique()) if d < d1)
    if not dates:
        return None
    cur = frozenset(bpos[bpos["Date"] == d1]["Position"])
    for d in reversed(dates):
        if frozenset(bpos[bpos["Date"] == d]["Position"]) != cur:
            return d
    return dates[0]


def _whatchanged_result(date: str | None, prev: str | None, book: str = "Soros") -> dict:
    f = S["frames"]; exp, pos, sec = f["exposures"], f["positions"], f["securities"]
    bpos = pos[pos["Book"] == book]
    alldates = sorted(pd.to_datetime(bpos["Date"].unique()))
    if not alldates:
        raise HTTPException(404, f"no positions for book {book}")
    d1 = max(d for d in alldates if d <= pd.Timestamp(date)) if date else alldates[-1]
    d0 = (max(d for d in alldates if d <= pd.Timestamp(prev)) if prev
          else _prior_filing_date(bpos, d1))
    if d0 is None or d0 >= d1:
        raise HTTPException(400, "no prior filing before this date")

    issuer = dict(zip(sec["Position"], sec["Issuer"]))
    ticker = dict(zip(sec["Position"], sec["Ticker"]))
    b0 = bpos[bpos["Date"] == d0].set_index("Position")["Weight"]
    b1 = bpos[bpos["Date"] == d1].set_index("Position")["Weight"]
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

    # factor-exposure attribution (Phase 4 machinery) — delta = sum of the four sources exactly
    w0d, l0d = _ud.book_at(exp, pos, d0)
    w1d, l1d = _ud.book_at(exp, pos, d1)
    attr = _ud.decompose(w0d, l0d, w1d, l1d)
    x0, x1 = _ud.book_exposure(w0d, l0d), _ud.book_exposure(w1d, l1d)
    exposure = [{"factor": fc, "before": _clean(x0[fc]), "after": _clean(x1[fc]),
                 "delta": _clean(attr[fc]["delta"]),
                 **{f"src_{k}": _clean(attr[fc][k]) for k in _ud.SOURCES}}
                for fc in sorted(_ud.STYLE, key=lambda k: abs(attr[k]["delta"]), reverse=True)]

    # book risk delta — cube-consistent (the what-if math), full factor-return history
    L0, wv0, s0, R = _book_inputs(str(d0.date()), book)
    L1, wv1, s1, _ = _book_inputs(str(d1.date()), book)
    r0, r1 = _risk_from_weights(wv0, L0, s0, R), _risk_from_weights(wv1, L1, s1, R)
    risk = {k: {"before": _clean(r0[k]), "after": _clean(r1[k]),
                "delta": _clean(r1[k] - r0[k]) if (r0[k] is not None and r1[k] is not None) else None}
            for k in r0}

    return {
        "book": book, "from": str(d0.date()), "to": str(d1.date()),
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
                      book: str = Query("Soros")):
    """Deterministic quarter-over-quarter diff between two 13F filings: positions entered / exited /
    resized, the net factor-exposure drift attributed (rotation vs loading drift, Phase 4), and the
    book risk delta (VaR/ES/HHI/specific vol, what-if math). Grounds /whatchanged/analysis."""
    return await run_in_threadpool(_whatchanged_result, date, prev, book)


class WhatChangedBody(BaseModel):
    date: str | None = None
    prev: str | None = None
    book: str = "Soros"
    notes: str | None = None


@app.post("/whatchanged/analysis")
async def whatchanged_analysis(body: WhatChangedBody):
    """Streamed risk-manager 'what changed' read between two filings. Computes the same deterministic
    diff /whatchanged returns, then hands only those tidy numbers to the Messages API (no tools) for a
    written read. Streams markdown. The model gets the diff and nothing else."""
    _rate_limit()
    client = _anthropic()          # 502 before the work if there's no key
    diff = await run_in_threadpool(_whatchanged_result, body.date, body.prev, body.book)
    payload = json.dumps({**diff, "desk_notes": body.notes or ""}, default=str)

    def gen():
        try:
            with client.messages.stream(
                model="claude-opus-4-8", max_tokens=4000,
                thinking={"type": "adaptive"},
                system=[{"type": "text", "text": WHATCHANGED_SYSTEM,
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
ASK_MAX_RECORDS = 250       # rows handed back per query_cube call (book is ~105 names; this is slack)

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
model. The book is the Soros Fund Management 13F holdings, run as a long-only weight overlay; monthly
calendar, 2016 → the latest build.

You have ONE tool, `query_cube`, which pulls slices of the live cube (the allowed dimensions and
measures are listed in its description). You have nothing else — no filesystem, no web, no other tool,
and no figures beyond what query_cube returns. To answer, pull the slices you need, then write the read.

How to use the cube:
- Numbers are fractions of book value. 0.035 means 3.5%. VaR/ES/vol are losses, reported positive.
- Net exposure: aggregated factor loading (weight x loading); additive, no ScenarioSet needed. Market
  carries a loading of 1.0 per name, so a fully invested book has ~unit Market exposure.
- Scenario VaR 95/97.5/99 and ES 97.5/99 are losses at that confidence / tail means. Total VaR 99 /
  Total ES 97.5 fold in the diagonal SPECIFIC (idiosyncratic) block. Marginal/% measures are a member's
  additive share of the book number (they sum to the total); Incremental VaR is diversification-aware
  and does NOT sum. Risk HHI is the Herfindahl of per-name Total-VaR shares; 1/HHI ~ effective bets.
- Factor contribution / Specific PnL / Realized PnL (if listed): REALIZED monthly PnL attribution,
  additive (Realized = factor + specific). Forward-month convention: the value at Date d0 is the PnL
  over the month AFTER d0. No ScenarioSet needed; read Specific PnL in by-name or book views.
- EVERY scenario measure is blank unless you slice ScenarioSet to ONE set. Sets: HistFull (full
  historical sim), Evt:* (a past window — COVID2020, Rates2022, Selloff2018), Hypo:* (hand-set sigma
  shocks — ValueRotation, RiskOff, MomentumCrash). Slice Date to one month for a point-in-time read.
- KEY CAVEAT: every name shares the uniform Market loading of 1.0, so in any set with real market moves
  (HistFull, Evt:*) Market dominates book risk (~95%) and HHI is low. The Hypo:* shocks zero Market and
  bump only style factors, so risk collapses onto the few names with those tilts and HHI jumps. A Hypo:*
  set reading far more concentrated than HistFull is that mechanism, not a data problem.

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
                    model="claude-opus-4-8", max_tokens=4000,
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
