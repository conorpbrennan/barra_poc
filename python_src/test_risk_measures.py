"""
test_risk_measures.py — backend (cube) checks for the VaR decomposition measures, hit through the
live /pivot API so they exercise exactly what the UI sees.

The defining contrast (Flex Agg convention):
  * MARGINAL    (component)         is ADDITIVE     -> Σ_member = book VaR exactly.
  * INCREMENTAL (remove-recompute)  is SUB-ADDITIVE -> Σ_member < book VaR (diversification),
    and 0 < member-incremental ≤ member-marginal for a long, diversifying book.

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
    """Σ Incremental Scenario VaR 99 < book VaR, and each member's incremental ≤ its marginal."""
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
def t_incremental_total_is_subadditive():
    """Σ Incremental Total VaR 99 < book Total VaR; positive for the largest issuer."""
    d = _pivot("Issuer", ["Marginal Total VaR 99", "Incremental Total VaR 99"])
    recs = [r for r in d["records"] if (r.get("Marginal Total VaR 99") or 0) != 0]
    book = d["grand"]["Marginal Total VaR 99"]
    inc = _col_sum(recs, "Incremental Total VaR 99")
    assert inc < book - 1e-6, f"incremental total not sub-additive: Σincr={inc} !< book={book}"
    top = max(recs, key=lambda r: r.get("Marginal Total VaR 99") or 0)
    assert top["Incremental Total VaR 99"] > 0, top
    assert top["Incremental Total VaR 99"] <= top["Marginal Total VaR 99"] + 1e-9, top


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
    day x Sector breakout (which must still foot). Shapes the vector plan does not take (no
    DaySet, a Day filter, two breakouts) fall through to the level plan and its warning."""
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
    # non-vector shapes fall through to the level plan
    f = {"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["Evt:COVID2020"]}
    r = piv("Day,DayDate", "PnL at day", f)                        # no DaySet -> level plan + warning
    assert r.get("plan") == "levels" and r["warning"] and "DaySet" in r["warning"], (r.get("plan"), r["warning"])
    r = piv("Day,DayDate,Sector,Issuer", "PnL at day", {**f, "DaySet": ["Evt:COVID2020"]})
    assert r.get("plan") == "levels", r.get("plan")               # two breakouts -> level plan
    r = piv("Day,DayDate", "PnL at day", {**f, "DaySet": ["Evt:COVID2020"], "Day": [0, 1]})
    assert r.get("plan") == "levels" and len(r["records"]) == 2, (r.get("plan"), len(r["records"]))


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
