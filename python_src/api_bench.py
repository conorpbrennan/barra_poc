"""
api_bench.py — the API-layer timing harness (2026-08-15, cube optimization round 3, item 5).

`cube_bench.py` times cube QUERIES in-process. The Vite UI never sees a cube query — it waits on
HTTP endpoints, several of which loop over dates, fan out over other endpoints, or do numpy work on
top of the cube. This harness times exactly those: every endpoint the Overview / Pivot / Trends /
Stress / What-if / Attribution / Model / Universe / Changes lenses call, over HTTP against a RUNNING
risk_api (default the :8010 service; set BARRA_API), for two managers (the reference manager and
the largest one), each request cold then warm (H5 — the cube's aggregate cache + any API-side cache).

Every row records wall time (cold + warm), HTTP status, payload size, and any error text (a 4xx/5xx
is a data point, never a crash); `meta` records the loadavg at start/end so a busy-box run can be
told apart from a quiet one (sibling cube builds move these numbers up to 5x — always compare two
runs taken under the same load, or quote the same-run control).

Run:   cd python_src && BARRA_API=http://127.0.0.1:8010 ../barra/bin/python api_bench.py [out.json]
Diff:  ../barra/bin/python api_bench.py --diff before.json after.json
Also:  --payloads DIR   dump every response body to DIR/<id>.json (the before/after identity gate:
                        `api_bench.py --same DIR_A DIR_B` asserts byte-identical payloads per id).
"""
from __future__ import annotations
import json
import os
import pathlib
import sys
import time

import requests

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
MANAGERS = [os.environ.get("BENCH_SMALL_MANAGER", "Soros"), os.environ.get("BENCH_MANAGER", "Vanguard")]
TIMEOUT = float(os.environ.get("BENCH_TIMEOUT", "600"))


def _latest_date(manager: str) -> str:
    """The manager's latest date via /meta (dates are cube-wide) — the date the UI opens on."""
    d = requests.get(f"{API}/meta", timeout=60).json()
    return d["dates"][-1]


def suite(manager: str, date: str) -> list[tuple[str, str, str, dict | None]]:
    """(id, method, path, body) — the UI's request set for one manager at one date."""
    f = lambda **kw: json.dumps(kw)                                                # noqa: E731
    return [
        # ---- global / context bar
        ("meta",                       "GET",  "/meta", None),
        ("dims",                       "GET",  "/dims", None),
        # ---- Overview (hero + RAG strip + top contributions + QoQ)
        ("risk_histfull",              "GET",  f"/risk?date={date}&set=HistFull&book={manager}", None),
        ("limits",                     "GET",  f"/limits?date={date}&book={manager}", None),
        ("dq",                         "GET",  "/dq", None),
        ("backtest",                   "GET",  f"/backtest?date={date}&book={manager}", None),
        ("contributions",              "GET",  f"/contributions?date={date}&book={manager}", None),
        ("whatchanged",                "GET",  f"/whatchanged?date={date}&book={manager}", None),
        ("pnl_attribution",            "GET",  f"/pnl_attribution?book={manager}", None),
        ("pnl_attribution_linkage",    "GET",  f"/pnl_attribution/linkage?book={manager}", None),
        ("pnl_attribution_residual",   "GET",  f"/pnl_attribution/residual?book={manager}", None),
        ("pnl_attribution_names",      "GET",  f"/pnl_attribution/names?book={manager}", None),
        ("pnl_attribution_by_sector",  "GET",  f"/pnl_attribution?book={manager}&by=sector", None),
        # ---- Trends lens
        ("trends_book",                "GET",  f"/trends?set=HistFull&book={manager}", None),
        ("trends_book_modelvol",       "GET",  f"/trends?set=HistFull&book={manager}&measures=Model%20vol", None),
        ("trends_by_factor",           "GET",  f"/trends?set=HistFull&book={manager}&measures=Net%20exposure&by=Factor", None),
        # ---- Stress / What-if lenses
        ("stress_naive",               "POST", "/stress", {"shocks": {"Value": -2.0}, "date": date, "book": manager}),
        ("stress_conditional",         "POST", "/stress", {"shocks": {"Value": -2.0}, "date": date, "book": manager, "conditional": True}),
        ("reverse_stress",             "GET",  f"/reverse_stress?date={date}&book={manager}", None),
        ("hedge",                      "GET",  f"/hedge?date={date}&book={manager}", None),
        ("whatif_empty",               "POST", "/whatif", {"trades": [], "date": date, "book": manager}),
        # ---- Universe / Drift lenses (artifact-backed; may be manager_mismatch for the large manager)
        ("universe",                   "GET",  f"/universe?book={manager}", None),
        ("funnel",                     "GET",  f"/funnel?book={manager}", None),
        ("span",                       "GET",  f"/span?book={manager}", None),
        ("drift",                      "GET",  f"/drift?book={manager}", None),
        # ---- Model lens
        ("calibration",                "GET",  f"/calibration?book={manager}", None),
        ("regression",                 "GET",  "/regression", None),
        ("factor_cov",                 "GET",  f"/factor_cov?date={date}", None),
        # ---- Pivot lens: the field-list default shapes
        ("pivot_var_by_sector",        "GET",  "/pivot?rows=Sector&measures=Scenario%20VaR%2099,Net%20exposure&filters="
                                                + f(Manager=[manager], Date=[date], ScenarioSet=["HistFull"]), None),
        ("pivot_marginal_by_position", "GET",  "/pivot?rows=Position&measures=Marginal%20Model%20vol,%25%20of%20Model%20vol&filters="
                                                + f(Manager=[manager], Date=[date], ScenarioSet=["HistFull"]), None),
        ("pivot_by_factor_totals",     "GET",  "/pivot?rows=Factor&measures=Net%20exposure,Factor%20variance%20contribution&totals=true&filters="
                                                + f(Manager=[manager], Date=[date], ScenarioSet=["HistFull"]), None),
        ("pivot_var_trend_by_date",    "GET",  "/pivot?rows=Date&measures=Scenario%20VaR%2099&filters="
                                                + f(Manager=[manager], ScenarioSet=["HistFull"]), None),
        ("pivot_stress_board",         "GET",  "/pivot?rows=ScenarioSet&measures=Scenario%20VaR%2099,Scenario%20worst%20loss&filters="
                                                + f(Manager=[manager], Date=[date]), None),
        ("pivot_day_path_covid",       "GET",  "/pivot?rows=Day,DayDate&measures=PnL%20at%20day&filters="
                                                + f(Manager=[manager], Date=[date], DaySet=["Evt:COVID2020"]), None),
    ]


def call(method: str, path: str, body: dict | None):
    t0 = time.perf_counter()
    try:
        if method == "GET":
            r = requests.get(API + path, timeout=TIMEOUT)
        else:
            r = requests.post(API + path, json=body, timeout=TIMEOUT)
        dt = time.perf_counter() - t0
        return {"s": round(dt, 3), "status": r.status_code, "bytes": len(r.content),
                "error": (r.text[:200] if r.status_code >= 400 else None)}, r.content
    except Exception as e:                                       # timeouts are data points
        return {"s": round(time.perf_counter() - t0, 3), "status": None, "bytes": 0,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}, b""


def run(out: pathlib.Path | None, payload_dir: pathlib.Path | None = None) -> dict:
    load0 = os.getloadavg()
    rows = []
    for manager in MANAGERS:
        date = _latest_date(manager)
        for qid, method, path, body in suite(manager, date):
            rid = f"{qid}@{manager}"
            cold, content = call(method, path, body)
            warm, _ = call(method, path, body)
            row = {"id": rid, "endpoint": qid, "manager": manager, "method": method, "path": path,
                   "cold_s": cold["s"], "warm_s": warm["s"], "status": cold["status"],
                   "bytes": cold["bytes"], "error": cold["error"]}
            rows.append(row)
            print(f"{rid:44s} {method:4s} cold={cold['s']:7.2f}s warm={warm['s']:7.2f}s "
                  f"status={cold['status']} bytes={cold['bytes']:>9}"
                  + (f"  ERR {cold['error'][:60]}" if cold["error"] else ""), flush=True)
            if payload_dir is not None:
                payload_dir.mkdir(parents=True, exist_ok=True)
                (payload_dir / f"{rid.replace('/', '_')}.json").write_bytes(content)
    res = {"meta": {"api": API, "managers": MANAGERS, "loadavg_start": load0, "loadavg_end": os.getloadavg(),
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
           "requests": rows}
    if out is not None:
        out.write_text(json.dumps(res, indent=1))
        print(f"\nwrote {out}")
    slow = sorted(rows, key=lambda r: -(r["cold_s"] or 0))[:12]
    print("\nslowest (cold):")
    for r in slow:
        print(f"  {r['id']:44s} {r['cold_s']:7.2f}s  warm {r['warm_s']:7.2f}s")
    return res


def diff(a: pathlib.Path, b: pathlib.Path) -> None:
    ja, jb = json.loads(a.read_text()), json.loads(b.read_text())
    ra = {r["id"]: r for r in ja["requests"]}
    rb = {r["id"]: r for r in jb["requests"]}
    print(f"{'id':44s} {'cold A':>8} {'cold B':>8} {'ratio':>6}   {'warm A':>8} {'warm B':>8} {'ratio':>6}")
    for k in ra:
        if k not in rb:
            continue
        ca, cb, wa, wb = ra[k]["cold_s"], rb[k]["cold_s"], ra[k]["warm_s"], rb[k]["warm_s"]
        rc = (cb / ca) if ca else float("nan"); rw = (wb / wa) if wa else float("nan")
        print(f"{k:44s} {ca:8.2f} {cb:8.2f} {rc:6.2f}   {wa:8.2f} {wb:8.2f} {rw:6.2f}")
    print(f"\nload A {ja['meta']['loadavg_start']} -> {ja['meta']['loadavg_end']}; "
          f"load B {jb['meta']['loadavg_start']} -> {jb['meta']['loadavg_end']}")


def same(a: pathlib.Path, b: pathlib.Path) -> None:
    """The before/after identity gate: every payload byte-identical (after JSON re-serialisation,
    which normalises whitespace/key order but not values)."""
    bad = []
    for fa in sorted(a.glob("*.json")):
        fb = b / fa.name
        if not fb.exists():
            bad.append((fa.name, "missing in B")); continue
        try:
            xa, xb = json.loads(fa.read_bytes() or b"null"), json.loads(fb.read_bytes() or b"null")
        except json.JSONDecodeError:
            xa, xb = fa.read_bytes(), fb.read_bytes()
        if xa != xb:
            bad.append((fa.name, "differs"))
    for name, why in bad:
        print(f"DIFF  {name}: {why}")
    print(f"{len(list(a.glob('*.json'))) - len(bad)} identical, {len(bad)} differ")
    if bad:
        raise SystemExit(1)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--diff"]:
        diff(pathlib.Path(args[1]), pathlib.Path(args[2]))
    elif args[:1] == ["--same"]:
        same(pathlib.Path(args[1]), pathlib.Path(args[2]))
    else:
        out = None; pdir = None
        if "--payloads" in args:
            i = args.index("--payloads"); pdir = pathlib.Path(args[i + 1]); del args[i:i + 2]
        if args:
            out = pathlib.Path(args[0])
        run(out, pdir)
