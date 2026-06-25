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
import barra_universe_membership as _um
import barra_universe_funnel as _uf
import barra_universe_span as _us
import barra_universe_drift as _ud
from barra_factor_risk_cube import load_frames, build_cube, EVENT_WINDOWS, HYPO_SHOCKS

CUBE_PORT = int(os.environ.get("BARRA_CUBE_PORT", "9091"))   # own port, distinct from the 9090 UI cube
TS_MEASURES = ["Total VaR 99", "Scenario VaR 99", "Scenario worst loss", "Specific vol"]
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
                 # ES contribution split + risk-concentration HHI:
                 "Marginal Scenario ES 97.5", "% of Scenario ES 97.5", "Risk HHI",
                 # per-day unpacked scenario series (read with ScenarioDay on an axis):
                 "Scenario PnL at day", "Scenario date at day (epoch)",
                 "Scenario VaR line at day", "Scenario worst pnl at day",
                 "Scenario worst date at day (epoch)",
                 "Scenario worst date (epoch)", "Scenario n"]
SCEN_DEP = {"Scenario VaR 99", "Scenario worst loss", "Scenario mean PnL", "Total VaR 99",
            "Marginal Scenario VaR 99", "Marginal Total VaR 99", "VaR sensitivity",
            "% of Scenario VaR 99", "% of Total VaR 99",
            "Incremental Scenario VaR 99", "Incremental Total VaR 99",
            "Scenario VaR 95", "Scenario VaR 97.5", "Scenario ES 97.5", "Scenario ES 99",
            "Scenario PnL vol", "Total ES 97.5",
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
    print(f"[risk_api] cube ready on :{CUBE_PORT}; UI at {session.url}")
    yield
    session.close()


app = FastAPI(title="Barra Factor Risk API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ----------------------------------------------------------------------------- endpoints
@app.get("/meta")
async def meta():
    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        dates = sorted({str(pd.Timestamp(d).date()) for d in
                        cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index})
        sets = sorted({str(s) for s in cube.query(m["contributors.COUNT"], levels=[l["ScenarioSet"]]).index})
        factors = sorted(S["frames"]["factor_meta"]["Factor"].tolist())
        return {"dates": dates, "scenario_sets": sets, "factors": factors,
                "ts_measures": TS_MEASURES, "by_levels": list(BY_LEVELS)}
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


def _pivot_result(rlist: list, clist: list, mlist: list, fdict: dict, totals: bool) -> dict:
    """The tidy pivot result (records [+ per_row/per_col/grand margins when totals]). Extracted
    from /pivot so /analysis feeds the model the EXACT numbers the view renders. Synchronous —
    call via run_in_threadpool. Assumes _validate_pivot has already run."""
    cube = S["cube"]; l, m = cube.levels, cube.measures
    seen, axis = set(), []
    for name in rlist + clist:          # dedupe, preserve order
        if name not in seen:
            seen.add(name); axis.append(name)
    filt = _build_filter(l, fdict)

    scen_ctx = ("ScenarioSet" in axis) or ("ScenarioSet" in fdict)
    warning = None
    if any(x in SCEN_DEP for x in mlist) and not scen_ctx:
        warning = ("Scenario measures need a ScenarioSet context — put ScenarioSet on an "
                   "axis or pick a single scenario; otherwise those cells are blank.")
    meas_objs = [m[x] for x in mlist]
    df = cube.query(*meas_objs, levels=[l[a] for a in axis], filter=filt)
    out = {"rows": rlist, "cols": clist, "measures": mlist, "totals": bool(totals),
           "warning": warning, "records": _records(df)}
    if totals:
        per_row = cube.query(*meas_objs, levels=[l[a] for a in rlist], filter=filt)
        out["per_row"] = _records(per_row)                              # Total column
        if clist:
            per_col = cube.query(*meas_objs, levels=[l[a] for a in clist], filter=filt)
            out["per_col"] = _records(per_col)                          # Total row
        grand = cube.query(*meas_objs, filter=filt)                     # corner
        def _scalar(v):                                                 # null array-measures -> None
            try:
                f = float(v); return None if math.isnan(f) else f
            except (TypeError, ValueError):
                return None
        out["grand"] = {x: _scalar(grand.iloc[0][x]) for x in mlist} if len(grand) else {}
    return out


@app.get("/pivot")
async def pivot(rows: str = "", cols: str = "", measures: str = "",
                date: str | None = None, set: str | None = None,
                filters: str | None = None, totals: bool = False):
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
    """
    rlist, clist, mlist = _csv(rows), _csv(cols), _csv(measures)
    fdict = _parse_filters(filters, date, set)
    _validate_pivot(rlist, clist, mlist, fdict)
    return await run_in_threadpool(_pivot_result, rlist, clist, mlist, fdict, bool(totals))


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

    # book-level scenario measures (VaR/ES/HHI need a single ScenarioSet)
    bspec = cfg.get("book", {})
    if bspec:
        df = cube.query(*[m[x] for x in bspec], filter=base & (l["ScenarioSet"] == scen))
        row = df.iloc[0] if len(df) else None
        for name, spec in bspec.items():
            val = _clean(row[name]) if row is not None else None
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
    """Per-factor return vol, matching build_scenarios: std over the dropna'd daily factor-return
    panel. Cached on S."""
    if "factor_vols" not in S:
        fr = S["frames"]["factor_returns"]
        wide = fr.pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
        S["factor_vols"] = {str(f): float(wide[f].std()) for f in wide.columns}
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


class StressBody(BaseModel):
    shocks: dict[str, float]                          # {Factor: sigma}
    date: str | None = None
    book: str = "Soros"


@app.post("/stress")
async def stress(body: StressBody):
    """Custom one-day stress: book P&L under user-defined per-factor sigma shocks (and a per-factor
    contribution breakdown). dPnL = Σ x_k·(sigma_k·vol_k) — the same math as the baked-in Hypo sets."""
    vols = _factor_vols()
    bad = [f for f in body.shocks if f not in vols]
    if bad:
        raise HTTPException(400, f"unknown factor(s): {bad}")
    if not body.shocks:
        raise HTTPException(400, "provide at least one factor shock")
    return await run_in_threadpool(_stress_result, body.shocks, body.date or _latest_date(), body.book)


@app.get("/reverse_stress")
async def reverse_stress(loss: float | None = None, date: str | None = None, book: str = "Soros"):
    """Reverse stress: for a target book loss `L`, the single-factor sigma move that would produce it,
    per factor, ranked by |sigma| (smallest = the book's most vulnerable factor). Default L = the
    Total VaR 99 desk limit (limits.json), else 0.05."""
    L = loss if loss is not None else (_load_limits().get("book", {})
                                       .get("Total VaR 99", {}).get("limit") or 0.05)
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
    """Book risk for a weight vector — mirrors the cube measures (Scenario VaR ladder, ES, Specific
    vol, Total VaR 99, Risk HHI) plus gross/net."""
    wv, Lv, sv = w.to_numpy(), L.to_numpy(), s.to_numpy()
    x = Lv.T @ wv
    pnl = R @ x
    n = len(pnl)
    svar = float(np.sum(wv * wv * sv))
    specvol = svar ** 0.5
    var99 = float(-np.quantile(pnl, 0.01))
    var975 = float(-np.quantile(pnl, 0.025))
    es = lambda a: float(-np.mean(np.sort(pnl)[:max(1, int(np.ceil((1 - a) * n)))]))
    total99 = (var99 * var99 + (_Z99 * specvol) ** 2) ** 0.5
    # HHI from marginal Total VaR shares (read off the book's 1% tail day, lower interpolation)
    ti = int(np.argsort(pnl)[int(np.floor(0.01 * (n - 1)))])
    msv = -(wv * (Lv @ R[ti]))                       # marginal Scenario VaR per name
    Fro = float(np.sum(msv))                          # = -pnl[ti]; book factor-VaR read-off
    T = (Fro * Fro + _Z99 * _Z99 * svar) ** 0.5
    hhi = None
    if T > 0:
        mtv = msv * (Fro / T) + (_Z99 * _Z99) * (wv * wv * sv) / T
        tot = float(np.sum(mtv))
        if tot:
            hhi = float(np.sum((mtv / tot) ** 2))
    return {"scenario_var_99": var99, "scenario_var_975": var975,
            "es_975": es(0.975), "es_99": es(0.99), "specific_vol": specvol,
            "total_var_99": total99, "risk_hhi": hhi,
            "gross": float(np.sum(np.abs(wv))), "net": float(np.sum(wv))}


def _whatif_result(date: str, book: str, trades: list) -> dict:
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
    before, after = _risk_from_weights(w, L, s, R), _risk_from_weights(w2, L, s, R)
    delta = {k: ((after[k] - before[k]) if isinstance(before[k], (int, float)) and before[k] is not None
                 and after[k] is not None else None) for k in before}
    holdings = [{"position": p, "ticker": tk.get(p, p), "weight": float(wt)}
                for p, wt in w[w != 0].sort_values(ascending=False).items()]
    # the full tradeable coverage universe (every name with loadings this date) — so the UI can add
    # a name that isn't currently held, not just resize/drop holdings.
    universe = [{"position": p, "ticker": tk.get(p, p)} for p in L.index]
    universe.sort(key=lambda u: u["ticker"])
    return {"date": date, "book": book, "trades": applied, "before": before, "after": after,
            "delta": delta, "holdings": holdings, "universe": universe}


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


# ============================================================================ analysis (LLM)
# A written risk-manager read of ONE view. The model is the plain Anthropic Messages API with
# NO tools: it receives the view's tidy numbers as text and returns prose. It has no access to
# the cube, the filesystem, or any tool, and cannot re-query — the only thing it can do is read
# the figures we hand it. All domain grounding lives in ANALYST_SYSTEM below.

ANALYST_SYSTEM = """\
You are a buy-side market-risk manager writing a short commentary on one view from a Barra-style
equity factor-risk model. The book is the Soros Fund Management 13F holdings, run as a long-only
weight overlay; monthly calendar, 2016–2024.

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
  factor risk combined in quadrature with the idiosyncratic tail.
- Marginal Scenario VaR 99 / Marginal Scenario ES 97.5 / Marginal Total VaR 99: a member's ADDITIVE
  contribution to the book number (the contributions sum to the book total). "% of ..." is that
  share, summing to 100%. Incremental VaR: the risk RELEASED by removing a member — diversification-
  aware, NOT additive (it does not sum to the book total), so there is no "% of" for it.
- VaR sensitivity: per-unit dVaR/dexposure. Risk HHI: Herfindahl index of each name's share of book
  Total VaR — 1/N for an evenly diversified book up to 1.0 for a single name; 1/HHI ~ the effective
  number of independent risk bets.
- drawdown (separate `drawdown` block, not a pivot measure): max peak-to-trough of the book's
  cumulative P&L if the *current* book had been held over the scenario set's daily path — a
  path-dependent lens VaR/ES cannot see. `max_drawdown` is a negative fraction; `longest_underwater_obs`
  is the longest run (trading days) below a prior peak; `recovered` says whether it climbed back.

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
    client = _anthropic()          # raise the 502 BEFORE the (slow) cube query if there's no key
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
    payload = json.dumps({
        "view": body.name or "(unnamed view)",
        "filters": fdict, "rows": rlist, "cols": clist, "measures": mlist,
        "warning": data.get("warning"),
        "limits": lim,
        "drawdown": dd,
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
