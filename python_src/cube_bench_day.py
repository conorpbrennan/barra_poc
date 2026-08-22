"""cube_bench_day.py — the micro-bench + correctness gate for the per-day drill (2026-08-15).

Round 2 of the cube optimization program targets ONE hotspot: "Scenario PnL at day" over the
ScenarioDay parameter hierarchy (~10 s warm / 25-50 s cold, +14 G heap; x Sector fails). This
script stands up a FRESH cube and, on it:

  * times the NEW day-facts path (`PnL at day` over the `Day` level) cold + warm, manager and
    manager x Sector, sampling the JVM's RSS across each call;
  * CHECKS CORRECTNESS: for HistFull and Evt:COVID2020, on two managers, the per-day series must
    equal the elements of `Scenario PnL vector` to 1e-12, in order, with the set's true length;
  * optionally (--with-old) times the OLD parameter-hierarchy path in the same process, for an
    apples-to-apples A/B.

It is the cheap gate (~2 min) that every attempt runs; the full 44-query suite (cube_bench.py)
runs once at the end to prove nothing else regressed.

Run:  cd python_src && BARRA_CUBE_XMX=32g ../barra/bin/python cube_bench_day.py [out.json] [--with-old]
NB acquire the global build lock first (any cube build over 8 G of heap).
"""
from __future__ import annotations
import json
import pathlib
import sys
import time

import numpy as np
import pandas as pd
import psutil

from cube_bench import RssSampler, _jvm_proc, _rss, BENCH_MANAGER, SMALL_MANAGER

TOL = 1e-12
CHECK_SETS = ["HistFull", "Evt:COVID2020"]
CHECK_MANAGERS = [BENCH_MANAGER, SMALL_MANAGER]


def timed(fn, jvm):
    before = _rss(jvm)
    sampler = RssSampler(jvm) if jvm else None
    if sampler:
        sampler.start()
    t0 = time.perf_counter()
    err = out = None
    try:
        out = fn()
    except Exception as e:                                    # failures are data points
        err = f"{type(e).__name__}: {str(e)[:200]}"
    dt = time.perf_counter() - t0
    peak = sampler.stop() if sampler else 0
    return {"s": round(dt, 3), "jvm_before_gb": round(before / 1e9, 2),
            "jvm_after_gb": round(_rss(jvm) / 1e9, 2), "jvm_peak_gb": round(peak / 1e9, 2),
            "jvm_delta_gb": round((_rss(jvm) - before) / 1e9, 2),
            "rows": (len(out) if hasattr(out, "__len__") else None), "error": err}, out


def _day_series(cube, l, m, date, manager, dayset, measures=("PnL at day",), with_date=True):
    """The gated shape: the manager's per-day P&L, labelled by the DayDate LEVEL (not the epoch
    measure — a level is read off the axis, a measure is aggregated per member)."""
    levels = [l["Day"], l["DayDate"]] if with_date else [l["Day"]]
    return cube.query(*[m[x] for x in measures], levels=levels,
                      filter=(l["Date"] == date) & (l["Manager"] == manager) & (l["DaySet"] == dayset))


def sector_footing(cube, l, m, date, manager, dayset, days=(0, 1, 41)) -> dict:
    """The drill must FOOT: the sector rows of a day sum to that day's portfolio P&L."""
    d = cube.query(m["PnL at day"], levels=[l["Day"], l["Sector"]],
                   filter=(l["Date"] == date) & (l["Manager"] == manager) & (l["DaySet"] == dayset)
                          & l["Day"].isin(*days))
    manager_rows = _day_series(cube, l, m, date, manager, dayset, with_date=False)
    worst = 0.0
    for day in days:
        s = float(d.xs(day, level="Day")["PnL at day"].astype(float).sum())
        b = float(manager_rows.loc[day, "PnL at day"])
        worst = max(worst, abs(s - b))
    rec = {"check": "sector_footing", "manager": manager, "set": dayset, "days": list(days),
           "max_abs_diff": worst, "pass": worst <= 1e-12}
    print(f"  sector footing {manager:12s} {dayset:14s} maxdiff={worst:.3e} "
          f"-> {'PASS' if rec['pass'] else 'FAIL'}", flush=True)
    return rec


def correctness(cube, l, m, date) -> list[dict]:
    """The gate: per-day values == the elements of the P&L vector, elementwise, in order."""
    out = []
    for manager in CHECK_MANAGERS:
        for s in CHECK_SETS:
            rec = {"manager": manager, "set": s}
            df = _day_series(cube, l, m, date, manager, s,
                             ("PnL at day", "Date at day (epoch)"), with_date=False).sort_index()
            vec = cube.query(m["Scenario PnL vector"], m["Scenario dates (epoch)"],
                             filter=(l["Date"] == date) & (l["Manager"] == manager)
                                    & (l["ScenarioSet"] == s))
            v = np.asarray(vec["Scenario PnL vector"].iloc[0], dtype="float64")
            dates_v = np.asarray(vec["Scenario dates (epoch)"].iloc[0], dtype="int64")
            d = np.asarray(df["PnL at day"], dtype="float64")
            rec.update(n_days=len(d), n_vector=len(v),
                       len_match=bool(len(d) == len(v)),
                       index_ordered=bool(list(df.index) == list(range(len(d)))))
            if rec["len_match"]:
                rec["max_abs_diff"] = float(np.max(np.abs(d - v))) if len(v) else 0.0
                rec["date_max_abs_diff"] = int(np.max(np.abs(
                    np.asarray(df["Date at day (epoch)"], dtype="int64") - dates_v))) if len(v) else 0
                rec["pass"] = bool(rec["len_match"] and rec["index_ordered"]
                                   and rec["max_abs_diff"] <= TOL and rec["date_max_abs_diff"] == 0)
            else:
                rec["pass"] = False
            out.append(rec)
            print(f"  correctness {manager:12s} {s:14s} n={rec['n_days']:5d}/{rec['n_vector']:5d} "
                  f"maxdiff={rec.get('max_abs_diff', float('nan')):.3e} "
                  f"dates={rec.get('date_max_abs_diff')} -> {'PASS' if rec['pass'] else 'FAIL'}",
                  flush=True)
    return out


def main(out_path: str, with_old: bool):
    from barra_factor_risk_cube import load_frames, build_cube
    me = psutil.Process()
    stages = {}
    t = time.perf_counter()
    frames = load_frames()
    stages["load_frames_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    session, cube = build_cube(frames, port=9096)
    stages["build_cube_s"] = round(time.perf_counter() - t, 2)
    jvm = _jvm_proc()
    stages["jvm_rss_after_build_gb"] = round(_rss(jvm) / 1e9, 2)
    stages["py_rss_after_build_gb"] = round(me.memory_info().rss / 1e9, 2)

    l, m = cube.levels, cube.measures
    D = pd.Timestamp(sorted(cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index)[-1]).date()
    B, HF = BENCH_MANAGER, "HistFull"
    base = (l["Date"] == D) & (l["Manager"] == B)

    queries = {
        # the gated shape: the manager's per-day series, both measures, as a caller would ask for it
        "new_day_path": lambda: _day_series(cube, l, m, D, B, HF),
        # the same without the date label, and the OLD way of getting the date (a measure)
        "new_day_no_date": lambda: _day_series(cube, l, m, D, B, HF, with_date=False),
        "new_day_date_measure": lambda: _day_series(cube, l, m, D, B, HF,
                                                    ("PnL at day", "Date at day (epoch)"),
                                                    with_date=False),
        "new_day_by_sector": lambda: cube.query(m["PnL at day"], levels=[l["Day"], l["Sector"]],
                                                filter=base & (l["DaySet"] == HF)),
        "new_day_path_covid": lambda: _day_series(cube, l, m, D, B, "Evt:COVID2020"),
        "new_day_path_small_book": lambda: _day_series(cube, l, m, D, SMALL_MANAGER, HF),
        # the shape the UI actually uses today (one query, whole vector) — the yardstick
        "pnl_vector_book": lambda: cube.query(m["Scenario PnL vector"], filter=base
                                              & (l["ScenarioSet"] == HF)),
    }
    if with_old:
        queries["old_day_path"] = lambda: cube.query(m["Scenario PnL at day"],
                                                     levels=[l["ScenarioDay"]],
                                                     filter=base & (l["ScenarioSet"] == HF))
        queries["old_day_by_sector"] = lambda: cube.query(m["Scenario PnL at day"],
                                                          levels=[l["ScenarioDay"], l["Sector"]],
                                                          filter=base & (l["ScenarioSet"] == HF))

    results = {}
    for qid, fn in queries.items():
        rec = {}
        for run in ("cold", "warm"):
            r, _ = timed(fn, jvm)
            rec[run] = r
        results[qid] = rec
        c, w = rec["cold"], rec["warm"]
        print(f"  {qid:24s} cold {c['s']:8.2f}s warm {w['s']:8.2f}s  rows={c['rows']}  "
              f"jvm {c['jvm_before_gb']:.1f}->{c['jvm_after_gb']:.1f}G (peak {c['jvm_peak_gb']:.1f}G, "
              f"delta {c['jvm_delta_gb']:+.1f}G)" + (f"  ERR {c['error'][:70]}" if c["error"] else ""),
              flush=True)

    checks = correctness(cube, l, m, D)
    checks.append(sector_footing(cube, l, m, D, B, HF))
    stages["jvm_rss_after_gb"] = round(_rss(jvm) / 1e9, 2)

    verdict = {
        "day_path_cold_lt_2s": results["new_day_path"]["cold"]["s"] < 2.0,
        "day_path_warm_lt_0p5s": results["new_day_path"]["warm"]["s"] < 0.5,
        "day_by_sector_ok": (results["new_day_by_sector"]["cold"]["error"] is None
                             and results["new_day_by_sector"]["cold"]["s"] < 5.0),
        "jvm_delta_lt_2g": max(results[q][r]["jvm_delta_gb"] for q in
                               ("new_day_path", "new_day_by_sector") for r in ("cold", "warm")) < 2.0,
        "correctness": all(c["pass"] for c in checks),
    }
    verdict["ALL"] = all(verdict.values())
    out = {"stages": stages, "queries": results, "checks": checks, "verdict": verdict,
           "meta": {"manager": B, "small_manager": SMALL_MANAGER, "date": str(D),
                    # ambient load MOVES these numbers by up to 5x (measured across attempts:
                    # the same query read 2.5 s on a quiet box and 13.7 s with a sibling cube
                    # running) — so every run records it, and runs are only compared like for like.
                    "loadavg": [round(x, 1) for x in __import__("os").getloadavg()],
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")}}
    pathlib.Path(out_path).write_text(json.dumps(out, indent=1))
    session.close()
    print("\n" + json.dumps(stages, indent=1))
    print(json.dumps(verdict, indent=1))
    print(f"wrote {out_path}")
    return 0 if verdict["ALL"] else 1


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args[0] if args else "cube_bench_day.json", "--with-old" in sys.argv))
