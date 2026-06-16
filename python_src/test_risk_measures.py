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
def t_scenario_day_unpacks_vector_via_pivot():
    """The synthetic ScenarioDay dimension turns the scenario P&L vector into a normal pivot query:
    rows=[ScenarioDay] yields one row per REAL array element (each with its date dual + the gated
    book markers), the surplus members of the full-size dimension are trimmed (every measure is
    ScenarioDay-gated -> NON EMPTY), and the worst day reconstructs the book worst-loss marker."""
    import requests, urllib.parse
    flt = {"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["Evt:COVID2020"]}
    q = {"rows": "ScenarioDay",
         "measures": ("Scenario PnL at day,Scenario date at day (epoch),"
                      "Scenario VaR line at day,Scenario worst pnl at day"),
         "filters": json.dumps(flt), "totals": "false"}
    recs = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()["records"]
    pnls = [r["Scenario PnL at day"] for r in recs]
    # trimmed to the set's real length (COVID ≈ 82 days), NOT the full-size dimension (~2200)
    assert 1 < len(pnls) < 500, f"expected the set's real days, got {len(pnls)}"
    # all measures scalar (no out-of-range null cells survive) and every day carries its date dual
    assert all(isinstance(p, (int, float)) for p in pnls), pnls[:3]
    assert all(isinstance(r.get("Scenario date at day (epoch)"), int) for r in recs), recs[:1]
    # the worst single day equals the (negative, book) worst-P&L marker carried on every row
    worst_marker = min(r["Scenario worst pnl at day"] for r in recs)
    assert abs(min(pnls) - worst_marker) < 1e-9, (min(pnls), worst_marker)


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
