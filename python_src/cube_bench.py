"""
cube_bench.py — the cube performance harness (2026-08-14, the 124-book optimization program).

Stands up the FULL cube from data/ in-process (all managers), instruments every stage of the
build, then times a fixed suite of queries covering every measure family and the specific
hypothesized pathologies from the optimization analysis:

  H1  PIT-set multiplier      — same measure grouped over all 130 ScenarioSets vs the 7 real
                                sets vs sliced to HistFull.
  H2  expression duplication  — baseline only here; variant cubes are compared by re-running
                                this harness against a modified build_cube (the harness IS the
                                A/B gate).
  H3  dead columns            — positions load timed full-width vs slim (micro-bench, own
                                throwaway session).
  H4  cross-book scaling      — vector measures by Manager at N = 1..123 books; where the
                                intermediate limit / time limit actually bites.
  H5  aggregate cache         — every query runs twice; warm/cold ratio.

Every query records wall time (cold + warm), the JVM's RSS before/after, and the PEAK RSS
observed during the call (200ms sampler thread). Failures (timeout / retrieval limit / OOM)
are captured as data points, not crashes.

Run:  cd python_src && BARRA_CUBE_XMX=32g ../barra/bin/python cube_bench.py [out.json]
The suite is deterministic; compare two runs with `python cube_bench.py --diff a.json b.json`.
"""
from __future__ import annotations
import json
import pathlib
import sys
import threading
import time

import pandas as pd
import psutil

BENCH_BOOK = "Vanguard"          # the largest manager — worst realistic single-book case
SMALL_BOOK = "ThirdPoint"        # a small book for contrast
REAL_SETS = ["HistFull", "Evt:COVID2020", "Evt:Rates2022", "Evt:Selloff2018",
             "Hypo:ValueRotation", "Hypo:RiskOff", "Hypo:MomentumCrash"]
SCALE_N = [1, 2, 4, 8, 16, 32, 64, 123]


def _jvm_proc() -> psutil.Process | None:
    me = psutil.Process()
    for c in me.children(recursive=True):
        try:
            if "java" in c.name().lower():
                return c
        except psutil.Error:
            pass
    return None


class RssSampler(threading.Thread):
    """Peak-RSS sampler for one process, 200ms cadence."""
    def __init__(self, proc: psutil.Process):
        super().__init__(daemon=True)
        self.proc, self.peak, self._halt = proc, 0, threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                self.peak = max(self.peak, self.proc.memory_info().rss)
            except psutil.Error:
                break
            self._halt.wait(0.2)

    def stop(self) -> int:
        self._halt.set()
        self.join(timeout=1)
        return self.peak


def _rss(proc) -> int:
    try:
        return proc.memory_info().rss if proc else 0
    except psutil.Error:
        return 0


def timed(fn, jvm):
    """(seconds, jvm_rss_before, jvm_rss_after, jvm_rss_peak, error|None, n_rows|None)"""
    before = _rss(jvm)
    sampler = RssSampler(jvm) if jvm else None
    if sampler:
        sampler.start()
    t0 = time.perf_counter()
    err = rows = None
    try:
        out = fn()
        rows = len(out) if hasattr(out, "__len__") else None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:200]}"
    dt = time.perf_counter() - t0
    peak = sampler.stop() if sampler else 0
    return dt, before, _rss(jvm), peak, err, rows


def build_suite(cube, ctx):
    """The fixed query suite: (id, family, callable). Order is the execution order."""
    l, m, h = ctx["l"], ctx["m"], ctx["h"]
    D, B = ctx["date"], BENCH_BOOK
    HF = l["ScenarioSet"] == "HistFull"
    BK = l["Manager"] == B
    DT = l["Date"] == D
    base = DT & BK & HF
    DAY_HF = l["DaySet"] == "HistFull"      # the day-facts table's OWN set key (day_path_* entries)
    q = cube.query
    books = ctx["books"]

    suite = [
        # A — additive family
        ("net_exposure_scalar",      "additive", lambda: q(m["Net exposure"], filter=DT & BK)),
        ("net_exposure_by_factor",   "additive", lambda: q(m["Net exposure"], levels=[l["Factor"]], filter=DT & BK)),
        ("net_exposure_by_sector",   "additive", lambda: q(m["Net exposure"], levels=[l["Sector"]], filter=DT & BK)),
        ("net_exposure_by_position", "additive", lambda: q(m["Net exposure"], levels=[l["Position"]], filter=DT & BK)),
        ("net_exposure_by_date",     "additive", lambda: q(m["Net exposure"], levels=[l["Date"]], filter=BK)),
        # B — scenario scalars (HistFull-sliced)
        ("var99_scalar",             "scenario", lambda: q(m["Scenario VaR 99"], filter=base)),
        ("tail_board_scalar",        "scenario", lambda: q(m["Scenario VaR 95"], m["Scenario VaR 97.5"],
                                                           m["Scenario VaR 99"], m["Scenario ES 97.5"],
                                                           m["Scenario ES 99"], m["Scenario PnL vol"], filter=base)),
        ("var99_by_factor",          "scenario", lambda: q(m["Scenario VaR 99"], levels=[l["Factor"]], filter=base)),
        ("var99_by_sector",          "scenario", lambda: q(m["Scenario VaR 99"], levels=[l["Sector"]], filter=base)),
        ("model_vol_scalar",         "scenario", lambda: q(m["Model vol"], filter=base)),
        ("model_vol_by_sector",      "scenario", lambda: q(m["Model vol"], levels=[l["Sector"]], filter=base)),
        ("total_var_scalar",         "scenario", lambda: q(m["Total VaR 99"], filter=base)),
        # C — vector family
        ("pnl_vector_book",          "vector",   lambda: q(m["Scenario PnL vector"], filter=base)),
        ("pnl_sorted_book",          "vector",   lambda: q(m["Scenario PnL sorted"], filter=base)),
        # legacy per-day path (ScenarioDay parameter hierarchy) — kept for the A/B against the twins below
        ("scenario_day_path",        "vector",   lambda: q(m["Scenario PnL at day"], levels=[l["ScenarioDay"]], filter=base)),
        ("scenario_day_by_sector",   "vector",   lambda: q(m["Scenario PnL at day"], levels=[l["ScenarioDay"], l["Sector"]], filter=base)),
        # the FAST per-day path (2026-08-15): days as facts on ScenarioDays, read off Day/DayDate with a
        # DaySet slice (`PnL at day` reads DaySet; the lifted markers read ScenarioSet, so both are set)
        ("day_path",                 "vector",   lambda: q(m["PnL at day"], levels=[l["Day"], l["DayDate"]],
                                                           filter=base & DAY_HF)),
        ("day_path_markers",         "vector",   lambda: q(m["PnL at day"], m["VaR line at day"], m["Worst pnl at day"],
                                                           m["Worst date at day (epoch)"],
                                                           levels=[l["Day"], l["DayDate"]], filter=base & DAY_HF)),
        ("day_path_by_sector",       "vector",   lambda: q(m["PnL at day"], levels=[l["Day"], l["DayDate"], l["Sector"]],
                                                           filter=base & DAY_HF)),
        # D — decomposition family
        ("marginal_var_by_factor",   "decomp",   lambda: q(m["Marginal Scenario VaR 99"], m["% of Scenario VaR 99"],
                                                           levels=[l["Factor"]], filter=base)),
        ("marginal_tvar_by_position", "decomp",  lambda: q(m["Marginal Total VaR 99"], levels=[l["Position"]], filter=base)),
        ("pct_model_vol_by_position", "decomp",  lambda: q(m["% of Model vol"], levels=[l["Position"]], filter=base)),
        ("incremental_var_by_factor", "decomp",  lambda: q(m["Incremental Scenario VaR 99"], levels=[l["Factor"]], filter=base)),
        ("top5_share_scalar",        "decomp",   lambda: q(m["Top-5 risk share"], filter=base)),
        ("hhi_scalar_histfull",      "decomp",   lambda: q(m["Risk HHI"], filter=base)),
        # E — H1: set enumeration (the PIT multiplier)
        ("var99_by_set_real7",       "sets",     lambda: q(m["Scenario VaR 99"], levels=[l["ScenarioSet"]],
                                                           filter=DT & BK & l["ScenarioSet"].isin(*REAL_SETS))),
        ("var99_by_set_all130",      "sets",     lambda: q(m["Scenario VaR 99"], levels=[l["ScenarioSet"]], filter=DT & BK)),
        ("model_vol_by_set_real7",   "sets",     lambda: q(m["Model vol"], levels=[l["ScenarioSet"]],
                                                           filter=DT & BK & l["ScenarioSet"].isin(*REAL_SETS))),
        ("model_vol_by_set_all130",  "sets",     lambda: q(m["Model vol"], levels=[l["ScenarioSet"]], filter=DT & BK)),
        ("hhi_by_set_real7",         "sets",     lambda: q(m["Risk HHI"], levels=[l["ScenarioSet"]],
                                                           filter=DT & BK & l["ScenarioSet"].isin(*REAL_SETS))),
        ("hhi_by_set_all130",        "sets",     lambda: q(m["Risk HHI"], levels=[l["ScenarioSet"]], filter=DT & BK)),
        ("hhi_small_book_all130",    "sets",     lambda: q(m["Risk HHI"], levels=[l["ScenarioSet"]],
                                                           filter=DT & (l["Manager"] == SMALL_BOOK))),
    ]
    # F — H4: cross-book scaling of a vector measure vs an additive one
    for n in SCALE_N:
        picks = books[:n]
        suite.append((f"model_vol_by_book_{n:03d}", "xbook",
                      (lambda p: lambda: q(m["Model vol"], levels=[l["Manager"]],
                                           filter=DT & HF & l["Manager"].isin(*p)))(picks)))
    suite.append(("net_exposure_by_book_all", "xbook",
                  lambda: q(m["Net exposure"], levels=[l["Manager"]], filter=DT & HF)))
    # G — attribution + Tier-1 families
    if "Factor contribution" in {n for n in cube.measures}:
        suite += [
            ("factor_contrib_by_factor", "attrib", lambda: q(m["Factor contribution"], levels=[l["Factor"]], filter=DT)),
            ("realized_pnl_by_date",     "attrib", lambda: q(m["Realized PnL"], levels=[l["Date"]])),
        ]
    suite += [
        ("factor_ret_vol_by_factor", "tier1", lambda: q(m["Factor return vol"], levels=[l["Factor"]], filter=DT & HF)),
        ("vol_ex_factor_by_factor",  "tier1", lambda: q(m["Vol ex factor"], levels=[l["Factor"]], filter=base)),
        ("hedge_ratio_by_factor",    "tier1", lambda: q(m["Min-variance hedge ratio"], levels=[l["Factor"]], filter=base)),
        ("stressed_vol_by_sector",   "tier1", lambda: q(m["Stressed model vol"], levels=[l["Sector"]], filter=base)),
    ]
    return suite


def micro_bench_load(frames) -> dict:
    """H3: positions load, full-width vs slim, into a throwaway session."""
    import atoti as tt
    out = {}
    pos = frames["positions"]
    slim = pos[["Date", "Manager", "Position", "Weight"]]
    s = tt.Session.start(tt.SessionConfig(port=9098, java_options=["-Xmx6g"]))
    try:
        t0 = time.perf_counter()
        s.read_pandas(pos, keys={"Date", "Manager", "Position"}, table_name="PosFull")
        out["positions_load_full_s"] = round(time.perf_counter() - t0, 2)
        t0 = time.perf_counter()
        s.read_pandas(slim, keys={"Date", "Manager", "Position"}, table_name="PosSlim")
        out["positions_load_slim_s"] = round(time.perf_counter() - t0, 2)
    finally:
        s.close()
    return out


def main(out_path: str):
    from barra_factor_risk_cube import load_frames, build_cube, BUILD_TIMINGS
    me = psutil.Process()
    stages, t0 = {}, time.perf_counter()

    t = time.perf_counter()
    frames = load_frames()
    stages["load_frames_s"] = round(time.perf_counter() - t, 2)
    stages["py_rss_after_frames_gb"] = round(me.memory_info().rss / 1e9, 2)

    t = time.perf_counter()
    session, cube = build_cube(frames, port=9097)
    stages["build_cube_s"] = round(time.perf_counter() - t, 2)
    # per-stage attribution of that number, when BARRA_CUBE_TIMINGS=1 was set (empty otherwise)
    if BUILD_TIMINGS:
        stages["build_stages_s"] = {k: round(v, 2) for k, v in BUILD_TIMINGS.items()}
    jvm = _jvm_proc()
    stages["jvm_rss_after_build_gb"] = round(_rss(jvm) / 1e9, 2)
    stages["py_rss_after_build_gb"] = round(me.memory_info().rss / 1e9, 2)

    l, m, h = cube.levels, cube.measures, cube.hierarchies
    dates = sorted(cube.query(m["contributors.COUNT"], levels=[l["Date"]]).index)
    D = pd.Timestamp(dates[-1]).date()
    pos = frames["positions"]
    latest = pos[pos["Date"] == pos["Date"].max()]
    books = (latest.groupby("Manager")["MV"].sum().sort_values(ascending=False).index.tolist())
    ctx = {"l": l, "m": m, "h": h, "date": D, "books": books}

    results = []
    for qid, family, fn in build_suite(cube, ctx):
        rec = {"id": qid, "family": family}
        for run in ("cold", "warm"):
            dt, b, a, p, err, rows = timed(fn, jvm)
            rec[f"{run}_s"] = round(dt, 3)
            rec[f"{run}_jvm_gb"] = round(a / 1e9, 2)
            rec[f"{run}_peak_gb"] = round(p / 1e9, 2)
            if err:
                rec[f"{run}_error"] = err
            if run == "cold":
                rec["rows"] = rows
        results.append(rec)
        e = rec.get("cold_error", "")
        print(f"  {qid:28s} {family:8s} cold {rec['cold_s']:8.2f}s  warm {rec['warm_s']:8.2f}s  "
              f"jvm {rec['warm_jvm_gb']:5.1f}G peak {rec['cold_peak_gb']:5.1f}G  "
              f"{'rows=' + str(rec.get('rows')) if not e else 'ERR ' + e[:60]}", flush=True)

    stages["jvm_rss_after_suite_gb"] = round(_rss(jvm) / 1e9, 2)
    stages["total_s"] = round(time.perf_counter() - t0, 1)
    session.close()

    print("micro-bench: positions load width (H3)...", flush=True)
    micro = micro_bench_load(frames)

    out = {"stages": stages, "micro": micro, "queries": results,
           "meta": {"bench_book": BENCH_BOOK, "date": str(D), "n_books": len(books),
                    "xmx": __import__("os").environ.get("BARRA_CUBE_XMX", "32g (default)"),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")}}
    pathlib.Path(out_path).write_text(json.dumps(out, indent=1))
    print(f"\nwrote {out_path}")
    print(json.dumps(stages, indent=1))
    print(json.dumps(micro, indent=1))


def diff(a_path: str, b_path: str):
    a = json.loads(pathlib.Path(a_path).read_text())
    b = json.loads(pathlib.Path(b_path).read_text())
    qa = {r["id"]: r for r in a["queries"]}
    qb = {r["id"]: r for r in b["queries"]}
    print(f"{'query':30s} {'A cold':>8s} {'B cold':>8s} {'ratio':>7s}   {'A warm':>8s} {'B warm':>8s} {'ratio':>7s}")
    for k in qa:
        if k in qb:
            ra, rb = qa[k], qb[k]
            rc = rb["cold_s"] / ra["cold_s"] if ra["cold_s"] else float("nan")
            rw = rb["warm_s"] / ra["warm_s"] if ra["warm_s"] else float("nan")
            print(f"{k:30s} {ra['cold_s']:8.2f} {rb['cold_s']:8.2f} {rc:6.2f}x   "
                  f"{ra['warm_s']:8.2f} {rb['warm_s']:8.2f} {rw:6.2f}x")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--diff":
        diff(sys.argv[2], sys.argv[3])
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "cube_bench_baseline.json")
