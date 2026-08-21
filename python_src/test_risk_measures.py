"""
test_risk_measures.py — backend (cube) checks for the VaR decomposition measures, hit through the
live /pivot API so they exercise exactly what the UI sees.

The defining contrast (Flex Agg convention):
  * MARGINAL    (component)        is ADDITIVE -> Σ_member = book measure exactly.
  * INCREMENTAL (remove-recompute) answers "how much risk does removing this member release".

Σ_member Incremental < book is a property of a COHERENT (sub-additive) measure, and the suite
pins it on `Model vol` — a standard deviation — over member dimensions, where it holds on both
books. It is NOT a property of the VaR pair: a 99% quantile is the textbook example of a measure
that is not sub-additive, and the remove-recompute re-reads the reduced book at ITS OWN tail day,
so each member gets a credit for shifting the tail as well as for its own risk. Measured
2026-08-21 on Soros/HistFull, Σ Incremental vs book: Scenario VaR by Issuer 0.0428 vs 0.0353,
Total VaR by Issuer 0.0427 vs 0.0358 — both over. The old `t_incremental_total_is_subadditive`
asserted the textbook line against the quantile measure and had failed ever since it was written;
see `t_incremental_var_bounds` for what actually holds.

Requires the FastAPI backend on http://127.0.0.1:8010; SKIPS (exit 0) if unreachable. Run:
    BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_risk_measures.py
"""
from __future__ import annotations
import os
import json
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
RESULTS = []
DATE = None   # latest cube date, filled by _backend_up


def test(fn):
    RESULTS.append(fn)
    return fn


def _backend_up():
    global DATE
    try:
        import requests
        d = requests.get(f"{API}/dims", timeout=5)
        if d.status_code != 200:
            return False
        DATE = d.json()["dates"][-1]
        return True
    except Exception:
        return False


def _pivot(rows, measures, book="Soros", scen="HistFull"):
    import requests
    filters = {"Book": [book], "Date": [DATE], "ScenarioSet": [scen]}
    q = {"rows": rows, "measures": ",".join(measures),
         "filters": json.dumps(filters), "totals": "true"}
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60)
    r.raise_for_status()
    return r.json()


def _col_sum(recs, name):
    return sum((r.get(name) or 0.0) for r in recs)


@test
def t_marginal_scenario_is_additive():
    """Σ Marginal Scenario VaR 99 over Factor == book Scenario VaR (the grand corner)."""
    d = _pivot("Factor", ["Marginal Scenario VaR 99"])
    s = _col_sum(d["records"], "Marginal Scenario VaR 99")
    book = d["grand"]["Marginal Scenario VaR 99"]
    assert abs(s - book) < 1e-9, f"marginal not additive: Σ={s} book={book}"
    assert book > 0, book


@test
def t_marginal_total_is_additive():
    """Σ Marginal Total VaR 99 over Issuer == book Total VaR (Euler split sums exactly)."""
    d = _pivot("Issuer", ["Marginal Total VaR 99"])
    s = _col_sum(d["records"], "Marginal Total VaR 99")
    book = d["grand"]["Marginal Total VaR 99"]
    assert abs(s - book) < 1e-9, f"marginal total not additive: Σ={s} book={book}"


@test
def t_incremental_total_row_reconciles_with_marginal():
    """The col-TOTAL (grand) row must read the SAME book VaR under Marginal and Incremental —
    both reference the read-off book VaR, so they agree to the last digit (the off-by-0.001 bug
    was Incremental referencing the interpolated quantile instead)."""
    for rows, pair in (("Factor", ("Marginal Scenario VaR 99", "Incremental Scenario VaR 99")),
                       ("Issuer", ("Marginal Total VaR 99", "Incremental Total VaR 99"))):
        g = _pivot(rows, list(pair))["grand"]
        assert abs(g[pair[0]] - g[pair[1]]) < 1e-9, f"{rows} total mismatch: {g[pair[0]]} vs {g[pair[1]]}"


@test
def t_incremental_scenario_is_subadditive():
    """Σ Incremental Scenario VaR 99 < book VaR, and each member's incremental ≤ its marginal.

    NB this holds over FACTOR members and is not a general property — the same measure runs OVER
    the book by Issuer/Position/Sector (see the module docstring). Kept as a regression pin on the
    factor decomposition, not as evidence that quantile incrementals are sub-additive."""
    d = _pivot("Factor", ["Marginal Scenario VaR 99", "Incremental Scenario VaR 99"])
    recs = d["records"]
    book = d["grand"]["Marginal Scenario VaR 99"]
    inc = _col_sum(recs, "Incremental Scenario VaR 99")
    assert inc < book - 1e-6, f"incremental should be sub-additive: Σincr={inc} !< book={book}"
    # the dominant factor's incremental must be strictly less than its marginal (diversification)
    top = max(recs, key=lambda r: r.get("Marginal Scenario VaR 99") or 0)
    mar, im = top["Marginal Scenario VaR 99"], top["Incremental Scenario VaR 99"]
    assert 0 < im <= mar + 1e-9, f"top factor {top.get('Factor')}: incr={im} marg={mar}"
    assert im < mar, f"top factor incr {im} should be < marg {mar} (diversified book)"


@test
def t_incremental_model_vol_is_subadditive():
    """Σ Incremental Model vol < book σ over MEMBER dimensions, on the reference book and the
    largest one — the diversification property, pinned on the measure that actually has it.

    σ = √(x'Fx + w'Δw) is a standard deviation, so it is sub-additive; removing a member releases
    less than that member's Euler share, and the releases sum to less than the book. Holds by
    Issuer, Position and Sector, on Soros (181 names) and Vanguard (3,617).

    NB by FACTOR it does NOT, and that is documented, not a defect: `Incremental Model vol` strips
    the member's own specific variance — right for a name, wrong for a factor, where the whole
    specific block is then subtracted once per factor. barra_factor_risk_cube.py flags the same
    fan-out where it defines `Vol ex factor`, which is the factor-correct twin."""
    for book_ in ("Soros", "Vanguard"):
        for rows in ("Issuer", "Sector"):
            d = _pivot(rows, ["Marginal Model vol", "Incremental Model vol"], book=book_)
            recs = [r for r in d["records"] if (r.get("Marginal Model vol") or 0) != 0]
            book = d["grand"]["Marginal Model vol"]
            inc = _col_sum(recs, "Incremental Model vol")
            ctx = (book_, rows, len(recs))
            assert inc < book - 1e-9, f"{ctx}: Σincr={inc} !< book={book}"
            assert all((r.get("Incremental Model vol") or 0) >= 0 for r in recs), ctx
            top = max(recs, key=lambda r: r.get("Marginal Model vol") or 0)
            assert 0 < top["Incremental Model vol"] < top["Marginal Model vol"], (ctx, top)


@test
def t_incremental_var_bounds():
    """What the VaR incrementals DO satisfy: every member releases something, and no member
    releases more than the whole book.

    Deliberately no assertion on Σ_member vs book. VaR is a quantile and is not sub-additive, and
    the remove-recompute reads the reduced book at its own tail day, so the sum runs OVER the book
    on this market-dominated book (measured above). Asserting the violation would be worse than
    asserting the textbook claim: it would pin today's incoherence as a requirement, and a future
    switch to a coherent basis (ES, or a common tail day) would then read as a regression."""
    for meas in ("Scenario VaR 99", "Total VaR 99"):
        d = _pivot("Issuer", [f"Marginal {meas}", f"Incremental {meas}"])
        recs = [r for r in d["records"] if (r.get(f"Marginal {meas}") or 0) != 0]
        book = d["grand"][f"Marginal {meas}"]
        assert recs and book > 0, (meas, book)
        for r in recs:
            v = r.get(f"Incremental {meas}") or 0.0
            assert v <= book + 1e-9, (meas, r)          # nobody releases more than the book holds
        top = max(recs, key=lambda r: r.get(f"Marginal {meas}") or 0)
        assert top[f"Incremental {meas}"] > 0, (meas, top)


@test
def t_incremental_measures_are_whitelisted():
    """The /pivot guard accepts the new measures (no 400) and returns a ScenarioSet warning=None."""
    d = _pivot("Factor", ["Incremental Scenario VaR 99", "Incremental Total VaR 99"])
    assert d["warning"] is None, d["warning"]        # ScenarioSet is in the filter -> no warning
    assert d["measures"] == ["Incremental Scenario VaR 99", "Incremental Total VaR 99"]


def _scenario_pnl(filters, sset="Evt:COVID2020"):
    import requests, urllib.parse
    q = {"date": DATE, "set": sset, "filters": json.dumps(filters)}
    r = requests.get(f"{API}/scenario_pnl?{urllib.parse.urlencode(q)}", timeout=60)
    r.raise_for_status()
    return r.json()


@test
def t_scenario_pnl_filters_scope_the_path():
    """/scenario_pnl honors the generic `filters` JSON: a Book scope returns the full path; an
    Issuer drill is a non-empty subset on the SAME date axis; Date/ScenarioSet inside `filters`
    are ignored (they're the fixed path axis, taken from date=/set=)."""
    book = _scenario_pnl({"Book": ["Soros"]})
    assert book["n"] > 0 and book["var99"] > 0, book

    # a held issuer -> non-empty path, same length (date axis) as the book
    import requests, urllib.parse
    q = {"rows": "Issuer", "measures": "Net exposure",
         "filters": json.dumps({"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})}
    recs = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()["records"]
    issuer = next(r["Issuer"] for r in recs if r.get("Net exposure"))
    iss = _scenario_pnl({"Book": ["Soros"], "Issuer": [issuer]})
    assert iss["n"] == book["n"], (iss["n"], book["n"])
    assert iss["var99"] > 0, iss

    # Date/ScenarioSet in `filters` must be stripped (path axis comes from the date/set params)
    bogus = _scenario_pnl({"Book": ["Soros"], "Date": ["1999-01-01"], "ScenarioSet": ["HistFull"]})
    assert abs(bogus["var99"] - book["var99"]) < 1e-12, (bogus["var99"], book["var99"])


@test
def t_scenario_pnl_returns_chart_datasets():
    """/scenario_pnl exposes ready-to-bind `datasets` for a JSON chart spec: a `points` feed
    (with a READABLE ISO scenario date + pnl) and a one-row `stat` feed (var / worst markers)."""
    import datetime as _dt
    d = _scenario_pnl({"Book": ["Soros"]})
    ds = d["datasets"]
    assert ds["points"] and {"date", "pnl"} <= set(ds["points"][0]), ds["points"][:1]
    _dt.date.fromisoformat(ds["points"][0]["date"])          # readable calendar date, not epoch int
    assert len(ds["stat"]) == 1, ds["stat"]
    assert {"var", "worst_pnl", "worst_date"} <= set(ds["stat"][0]), ds["stat"][0]
    # distribution is the CUBE-SORTED loss curve (tt.array.sort), not a client/numpy histogram:
    # one (percentile, pnl) per scenario day, sorted ascending, worst at p=0.
    assert ds["dist"] and {"p", "pnl"} <= set(ds["dist"][0]), ds["dist"][:1]
    assert len(ds["dist"]) == d["n"], (len(ds["dist"]), d["n"])
    pnls = [pt["pnl"] for pt in ds["dist"]]
    assert pnls == sorted(pnls), "dist must be ascending (cube-sorted)"
    assert abs(ds["dist"][0]["pnl"] - ds["stat"][0]["worst_pnl"]) < 1e-9   # p=0 is the worst loss


@test
def t_scenario_pnl_stats_come_from_cube():
    """var99 / worst / mean in /scenario_pnl are the CUBE measures (no numpy), so they equal the
    cube's Scenario VaR 99 / worst loss / mean PnL queried via /pivot to the last digit."""
    import requests, urllib.parse
    d = _scenario_pnl({"Book": ["Soros"]}, sset="Evt:COVID2020")
    q = {"rows": "Book", "measures": "Scenario VaR 99,Scenario worst loss,Scenario mean PnL",
         "filters": json.dumps({"Book": ["Soros"], "Date": [DATE],
                                "ScenarioSet": ["Evt:COVID2020"]})}
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()["records"][0]
    assert abs(d["var99"] - r["Scenario VaR 99"]) < 1e-12, (d["var99"], r["Scenario VaR 99"])
    assert abs(-d["worst"]["pnl"] - r["Scenario worst loss"]) < 1e-12
    assert abs(d["mean"] - r["Scenario mean PnL"]) < 1e-12


@test
def t_scenario_pnl_sector_breakout_stacks_to_book():
    """breakout=Sector returns the loss curve decomposed by sector (each sector's per-day P&L is a
    CUBE aggregation); at the worst scenario the sectors sum to the book worst loss (they stack)."""
    import requests, urllib.parse
    q = {"date": DATE, "set": "Evt:COVID2020", "filters": json.dumps({"Book": ["Soros"]}),
         "breakout": "Sector"}
    d = requests.get(f"{API}/scenario_pnl?{urllib.parse.urlencode(q)}", timeout=60).json()
    st = d["datasets"]["dist_stacked"]
    assert st and {"date", "Sector", "pnl", "rank"} <= set(st[0]), st[:1]
    # on the worst-loss DATE the sector contributions sum to the book worst loss (they stack)
    worst_rows = [r for r in st if r["date"] == d["worst"]["date"]]
    assert worst_rows, "no rows on worst date"
    assert abs(sum(r["pnl"] for r in worst_rows) - d["worst"]["pnl"]) < 1e-9, \
        (sum(r["pnl"] for r in worst_rows), d["worst"]["pnl"])
    # sorted worst→best: the worst date is rank 0 (leftmost on the date-labelled axis)
    assert {r["rank"] for r in worst_rows} == {0}, {r["rank"] for r in worst_rows}


@test
def t_day_path_markers_tie_book_cells_and_legacy_is_pruned():
    """The per-day path (2026-08-15): rows=[Day, DayDate] + a DaySet slice. Its markers are the
    BOOK cells made chart-ready: `VaR line at day` == -`Scenario VaR 99`, `Worst pnl at day` ==
    -`Scenario worst loss` (and == the minimum of the path itself), `Worst date at day (epoch)` ==
    `Scenario worst date (epoch)` and names a real day of the path; the day x Sector breakout foots
    to the book day; a query with no DaySet context carries the DaySet warning (the ScenarioSet one
    does not cover it); and the legacy ScenarioDay names are OFF the allowlist (400), round 3."""
    import requests, urllib.parse
    import pandas as pd
    base = {"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["Evt:COVID2020"]}
    new_f = {**base, "DaySet": ["Evt:COVID2020"]}
    def piv(rows, measures, flt, **extra):
        q = {"rows": rows, "measures": measures, "filters": json.dumps(flt), "totals": "false", **extra}
        return requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=120)
    new = piv("Day,DayDate", "PnL at day,VaR line at day,Worst pnl at day,Worst date at day (epoch)", new_f).json()
    assert new["warning"] is None, new["warning"]
    nr = new["records"]
    assert 1 < len(nr) < 500, len(nr)
    book = piv("ScenarioSet", "Scenario VaR 99,Scenario worst loss,Scenario worst date (epoch)", base).json()["records"][0]
    epoch = lambda d: (pd.Timestamp(d) - pd.Timestamp("1970-01-01")).days
    for r in nr:
        assert abs(r["VaR line at day"] + book["Scenario VaR 99"]) < 1e-12, (r, book)
        assert abs(r["Worst pnl at day"] + book["Scenario worst loss"]) < 1e-12, (r, book)
        assert r["Worst date at day (epoch)"] == book["Scenario worst date (epoch)"], (r, book)
    pnls = [r["PnL at day"] for r in nr]
    assert abs(min(pnls) - nr[0]["Worst pnl at day"]) < 1e-12, (min(pnls), nr[0]["Worst pnl at day"])
    worst_day = nr[pnls.index(min(pnls))]
    assert epoch(worst_day["DayDate"]) == nr[0]["Worst date at day (epoch)"], worst_day
    assert [r["Day"] for r in nr] == list(range(len(nr))), "Day is the set's 0..n-1 index"
    # the breakout: sector rows of a day sum to that day's book P&L
    sec = piv("Day,DayDate,Sector", "PnL at day", new_f).json()["records"]
    for day in (0, 1, len(nr) - 1):
        s_ = sum(r["PnL at day"] for r in sec if r["Day"] == day)
        assert abs(s_ - nr[day]["PnL at day"]) < 1e-12, (day, s_, nr[day]["PnL at day"])
    # no DaySet context -> the DaySet warning (its own, not the ScenarioSet one)
    w = piv("Day,DayDate", "PnL at day", base).json()["warning"]
    assert w and "DaySet" in w, w
    # the legacy path is pruned from the allowlist: dimension AND measures 400
    r = piv("ScenarioDay", "PnL at day", base)
    assert r.status_code == 400 and "ScenarioDay" in r.text, (r.status_code, r.text[:200])
    r = piv("Day", "Scenario PnL at day", new_f)
    assert r.status_code == 400 and "Scenario PnL at day" in r.text, (r.status_code, r.text[:200])
    d = requests.get(f"{API}/dims", timeout=60).json()
    assert "ScenarioDay" not in d["dimensions"] and not any("Scenario" in m and "at day" in m for m in d["measures"]), d["dimensions"]


@test
def t_day_vector_plan_ties_level_plan():
    """The Day-shape VECTOR PLAN (follow-up 2, 2026-08-15): /pivot answers the Day shape from the
    cube's `Scenario PnL vector` (+ its dates dual, + one single-cell markers query) instead of
    the 2,618-member level scan. It must be indistinguishable in output: record-for-record equal
    (same keys, same order, |diff| < 1e-12) to the LEVEL plan (`plan=levels`) -- the fact-joined
    Day level -- on Soros and Vanguard, HistFull and Evt:COVID2020, book path and the
    day x Sector breakout (which must still foot). Since 2026-08-21 (TODO item 5) it also takes
    TWO breakouts and a Day/DayDate window, both pinned here against `plan=levels`; the shape it
    still does not take (no DaySet) falls through to the level plan and its warning."""
    import requests, urllib.parse
    import pandas as pd
    def piv(rows, measures, flt, **extra):
        q = {"rows": rows, "measures": measures, "filters": json.dumps(flt), "totals": "false", **extra}
        return requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=300).json()
    epoch = lambda d: (pd.Timestamp(d) - pd.Timestamp("1970-01-01")).days
    def same(a, b, ctx):
        assert list(a.keys()) == list(b.keys()), (ctx, list(a.keys()), list(b.keys()))
        for k in a:
            x, y = a[k], b[k]
            if isinstance(x, float) or isinstance(y, float):
                assert x is not None and y is not None and abs(x - y) < 1e-12, (ctx, k, x, y)
            else:
                assert x == y, (ctx, k, x, y)
    MEAS = "PnL at day,VaR line at day,Worst pnl at day,Worst date at day (epoch)"
    for book, date in (("Soros", DATE), ("Vanguard", "2026-06-30")):
        for st in ("HistFull", "Evt:COVID2020"):
            ctx = (book, st)
            f = {"Book": [book], "Date": [date], "ScenarioSet": [st], "DaySet": [st]}
            vec = piv("Day,DayDate", MEAS, f)
            lev = piv("Day,DayDate", MEAS, f, plan="levels")
            assert vec.get("plan") == "vector" and lev.get("plan") == "levels", (ctx, vec.get("plan"), lev.get("plan"))
            assert vec["warning"] is None, (ctx, vec["warning"])
            vr, lr = vec["records"], lev["records"]
            assert len(vr) == len(lr) > 1, (ctx, len(vr), len(lr))
            for a, b in zip(vr, lr):
                same(a, b, ctx)
            # DaySet-only filter (no ScenarioSet) is the same shape
            v2 = piv("Day,DayDate", "PnL at day", {"Book": [book], "Date": [date], "DaySet": [st]})
            assert v2.get("plan") == "vector" and len(v2["records"]) == len(vr), ctx
            for a, b in zip(v2["records"], vr):
                assert abs(a["PnL at day"] - b["PnL at day"]) < 1e-12, (ctx, a, b)
            # day x Sector: vector == levels record-for-record (same hierarchy path columns) + foots
            if st == "Evt:COVID2020" or book == "Soros":      # keep the Vanguard HistFull level scan out of the gate
                vs = piv("Day,DayDate,Sector", "PnL at day,VaR line at day", f)
                ls = piv("Day,DayDate,Sector", "PnL at day,VaR line at day", f, plan="levels")
                assert vs.get("plan") == "vector" and len(vs["records"]) == len(ls["records"]) > 0, (ctx, len(vs["records"]), len(ls["records"]))
                key = lambda r: (r["Day"], r.get("Country"), r.get("Sector"))
                vs_s, ls_s = sorted(vs["records"], key=key), sorted(ls["records"], key=key)
                for a, b in zip(vs_s, ls_s):
                    same(a, b, ctx)
                for day in (0, len(vr) - 1):
                    tot = sum(r["PnL at day"] for r in vs["records"] if r["Day"] == day)
                    assert abs(tot - vr[day]["PnL at day"]) < 1e-12, (ctx, day, tot, vr[day]["PnL at day"])
    # no DaySet is the one Day shape the vector plan still declines: level plan + its warning
    f = {"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["Evt:COVID2020"]}
    r = piv("Day,DayDate", "PnL at day", f)
    assert r.get("plan") == "levels" and r["warning"] and "DaySet" in r["warning"], (r.get("plan"), r["warning"])
    # TWO breakouts (2026-08-21): vector, and record-for-record equal to the level plan
    fd = {**f, "DaySet": ["Evt:COVID2020"]}
    MEAS2 = "PnL at day,VaR line at day"
    v2b = piv("Day,DayDate,Sector,Issuer", MEAS2, fd)
    l2b = piv("Day,DayDate,Sector,Issuer", MEAS2, fd, plan="levels")
    assert v2b.get("plan") == "vector" and l2b.get("plan") == "levels", (v2b.get("plan"), l2b.get("plan"))
    assert len(v2b["records"]) == len(l2b["records"]) > 0, (len(v2b["records"]), len(l2b["records"]))
    k2 = lambda r: (r["Day"], r.get("Country"), r.get("Sector"), r.get("Issuer"))
    for a, b in zip(sorted(v2b["records"], key=k2), sorted(l2b["records"], key=k2)):
        same(a, b, "two-breakout")
    # a Day / DayDate WINDOW (2026-08-21): vector, same rows the level plan returns, and the day
    # index is NOT re-based (a filtered row keeps its real Day member)
    for slicer in ({"Day": [0, 1]}, {"DayDate": [piv("Day,DayDate", "PnL at day", fd)["records"][2]["DayDate"]]}):
        vw = piv("Day,DayDate", MEAS, {**fd, **slicer})
        lw = piv("Day,DayDate", MEAS, {**fd, **slicer}, plan="levels")
        assert vw.get("plan") == "vector" and lw.get("plan") == "levels", (slicer, vw.get("plan"))
        assert len(vw["records"]) == len(lw["records"]) == len(list(slicer.values())[0]), (
            slicer, len(vw["records"]), len(lw["records"]))
        for a, b in zip(vw["records"], lw["records"]):
            same(a, b, f"day-window {slicer}")
    # ... and the windowed rows are the same cells the unwindowed query returns
    full = piv("Day,DayDate", MEAS, fd)["records"]
    win = piv("Day,DayDate", MEAS, {**fd, "Day": [0, 1]})["records"]
    for a, b in zip(win, full[:2]):
        same(a, b, "day-window vs full")


def main():
    if not _backend_up():
        print(f"SKIP: backend not reachable at {API} (start risk_api on :8010 to run measure tests)")
        raise SystemExit(0)
    passed = failed = 0
    for fn in RESULTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
