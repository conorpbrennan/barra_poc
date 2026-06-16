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
"""
from __future__ import annotations
import os
import math
import json
import datetime as _dt
from contextlib import asynccontextmanager

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

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
                 # per-day unpacked scenario series (read with ScenarioDay on an axis):
                 "Scenario PnL at day", "Scenario date at day (epoch)",
                 "Scenario VaR line at day", "Scenario worst pnl at day",
                 "Scenario worst date at day (epoch)",
                 "Scenario worst date (epoch)", "Scenario n"]
SCEN_DEP = {"Scenario VaR 99", "Scenario worst loss", "Scenario mean PnL", "Total VaR 99",
            "Marginal Scenario VaR 99", "Marginal Total VaR 99", "VaR sensitivity",
            "% of Scenario VaR 99", "% of Total VaR 99",
            "Incremental Scenario VaR 99", "Incremental Total VaR 99",
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

    def run():
        cube = S["cube"]; l, m = cube.levels, cube.measures
        seen, axis = set_(), []
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
    return await run_in_threadpool(run)


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
