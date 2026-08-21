"""
test_notebook.py — guard the direct-Atoti demo notebook's query path.

Builds the cube ONCE and re-runs the same explicit `cube.query(...)` calls the notebook uses,
asserting each returns sensible data — so the notebook can't silently rot if a measure/level is
renamed in the cube. No Jupyter, no HTTP, no pytest (matches the repo's script-style tests):

    cd python_src && PYTHONPATH=python_src ../barra/bin/python test_notebook.py
"""
from __future__ import annotations
import datetime as dt
import json
import pathlib
import re
import pandas as pd
import notebook_helpers as N

NOTEBOOK = pathlib.Path(__file__).resolve().parent.parent / "notebooks" / "soros_13f_risk.ipynb"


def _notebook_D() -> dt.date:
    """The as-of date READ OUT OF THE NOTEBOOK, not restated here. This file mirrors the
    notebook's queries by hand, so a duplicated constant is a second thing to keep in sync and
    the one that rotted: it said 2024-12-31 long after the build's calendar reached 2026-06-30,
    and `t_latest_cob_is_D` failed as a stale-copy artifact rather than as a real signal about
    the notebook. Parsed from the source cell so the notebook stays the single source of truth."""
    nb = json.loads(NOTEBOOK.read_text())
    for cell in nb.get("cells", []):
        for line in cell.get("source", []):
            m = re.match(r"\s*D\s*=\s*dt\.date\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", line)
            if m:
                return dt.date(*(int(g) for g in m.groups()))
    raise AssertionError(f"no `D = dt.date(...)` line found in {NOTEBOOK}")


D = _notebook_D()
RESULTS = []
def _test(fn):                       # collect like test_risk_measures.py does
    RESULTS.append(fn)
    return fn


@_test
def t_latest_cob_is_D(cube):
    """The notebook's `D` must still be the latest COB in the build — its own comment calls it
    "latest monthly COB in the sample", and every view in the notebook is as-of D. When the
    builder's END moves, this fails until the notebook is re-pointed (and re-executed)."""
    l, m = cube.levels, cube.measures
    last = pd.Timestamp(sorted(cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index)[-1]).date()
    assert last == D, (f"{NOTEBOOK.name} pins D = {D}, but the build's latest COB is {last} — "
                       f"re-point the notebook's D (and re-run it) or rebuild the frames")


@_test
def t_l1_book_summary(cube):
    l, m = cube.levels, cube.measures
    df = cube.query(m["Total VaR 99"], m["Scenario VaR 99"], m["Specific vol"], levels=[l["Book"]],
                    filter=(l["Book"] == "Soros") & (l["Date"] == D) & (l["ScenarioSet"] == "HistFull"))
    assert len(df) == 1, len(df)
    row = df.iloc[0].astype(float)                          # long-equity book: ~3.5% daily 99% VaR,
    assert 0.02 < row["Total VaR 99"] < 0.06, row["Total VaR 99"]
    assert row["Scenario VaR 99"] > row["Specific vol"]     # factor tail dominates the specific tail


@_test
def t_var_trend_series(cube):
    l, m = cube.levels, cube.measures
    df = cube.query(m["Scenario VaR 99"], m["Total VaR 99"], m["Specific vol"], levels=[l["Date"]],
                    filter=(l["Book"] == "Soros") & (l["ScenarioSet"] == "HistFull"))
    assert len(df) > 50, len(df)                            # the full monthly calendar, 2016 -> D
    assert {"Scenario VaR 99", "Total VaR 99", "Specific vol"} <= set(df.columns)


@_test
def t_stress_board_all_sets(cube):
    l, m = cube.levels, cube.measures
    df = cube.query(m["Scenario VaR 99"], m["Scenario worst loss"], m["Total VaR 99"],
                    levels=[l["ScenarioSet"]], filter=(l["Book"] == "Soros") & (l["Date"] == D))
    assert {"HistFull", "Evt:COVID2020"} <= set(df.index), set(df.index)


@_test
def t_covid_path_unpacks_vector(cube):
    """The Day/DayDate levels read the COVID P&L vector as a per-day series (the graph-1 query):
    `PnL at day` sliced by DaySet, the calendar date off the DayDate LEVEL, the book VaR marker
    (lifted, reads ScenarioSet) constant along the rows."""
    l, m = cube.levels, cube.measures
    covid = ((l["Book"] == "Soros") & (l["Date"] == D) & (l["ScenarioSet"] == "Evt:COVID2020")
             & (l["DaySet"] == "Evt:COVID2020"))
    df = cube.query(m["PnL at day"], m["VaR line at day"],
                    levels=[l["Day"], l["DayDate"]], filter=covid).reset_index()
    assert 60 < len(df) < 120, len(df)                      # ~80 trading days, << HistFull's length
    dates = pd.to_datetime(df["DayDate"])
    assert dates.dt.year.eq(2020).all()
    assert df["VaR line at day"].astype(float).nunique() == 1   # one book-level rule


@_test
def t_covid_sector_rank_is_monotonic(cube):
    """The graph-2 loss-curve ordering computed in the DataFrame is monotonic in book-total P&L."""
    l, m = cube.levels, cube.measures
    covid = (l["Book"] == "Soros") & (l["Date"] == D) & (l["DaySet"] == "Evt:COVID2020")
    sector = cube.query(m["PnL at day"],
                        levels=[l["Day"], l["DayDate"], l["Sector"]], filter=covid).reset_index()
    sector["PnL at day"] = sector["PnL at day"].astype(float)
    order = (sector.groupby("Day", as_index=False)["PnL at day"]
                    .sum().sort_values("PnL at day"))
    assert order["PnL at day"].is_monotonic_increasing    # worst-first -> non-decreasing
    assert sector["Sector"].nunique() > 5, sector["Sector"].nunique()


def main():
    print("building cube (once) ...")
    _session, cube = N.build()
    passed = failed = 0
    for fn in RESULTS:
        try:
            fn(cube)
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
