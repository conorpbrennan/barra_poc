"""
test_drawdown.py — checks for the /drawdown path lens (risk_api.py).

  UNIT  — always run, no backend: _max_drawdown on a hand-built path with a known answer, and that
          it re-orders an out-of-order date axis before cumulating.
  INTEG — need the live backend on :8010; SKIP if down: /drawdown on HistFull is sane (max_dd ≤ 0,
          |max_dd| ≥ the worst single-day loss since drawdown accumulates, path length = n), and a
          length-1 hypothetical set returns status "insufficient".

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_drawdown.py
"""
from __future__ import annotations
import os
import json
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
UNIT, INTEG = [], []
DATE = None


def unit(fn):
    UNIT.append(fn); return fn


def integ(fn):
    INTEG.append(fn); return fn


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


# --------------------------------------------------------------------------- UNIT
@unit
def t_max_drawdown_known_path():
    """equity = cumprod(1+pnl): 1.10, 1.21, 0.847, 0.889, 1.245 -> deepest dd at obs 2 = 0.847/1.21-1
    = -0.30, peak at obs 1, recovers at obs 4."""
    import risk_api
    pnl = [0.10, 0.10, -0.30, 0.05, 0.40]
    days = [0, 1, 2, 3, 4]
    d = risk_api._max_drawdown(pnl, days)
    assert d is not None and d["n"] == 5, d
    assert abs(d["max_drawdown"] - (-0.30)) < 1e-9, d["max_drawdown"]
    assert d["drawdown_obs"] == 1, d["drawdown_obs"]          # trough obs 2 - peak obs 1
    assert d["recovered"] is True, d
    assert len(d["path"]) == 5, d


@unit
def t_max_drawdown_reorders_dates():
    """Same path fed with a reversed date axis must give the same answer (it sorts by date first)."""
    import risk_api
    pnl = [0.10, 0.10, -0.30, 0.05, 0.40][::-1]
    days = [4, 3, 2, 1, 0]
    d = risk_api._max_drawdown(pnl, days)
    assert abs(d["max_drawdown"] - (-0.30)) < 1e-9, d["max_drawdown"]
    assert d["recovered"] is True, d


@unit
def t_max_drawdown_empty():
    import risk_api
    assert risk_api._max_drawdown([], []) is None


# --------------------------------------------------------------------------- INTEG
@integ
def t_drawdown_histfull_sane():
    import requests
    d = requests.get(f"{API}/drawdown", params={"set": "HistFull"}, timeout=60).json()
    assert d["status"] == "ok", d
    assert d["n"] > 100, d["n"]
    assert d["max_drawdown"] <= 0, d["max_drawdown"]
    assert isinstance(d["recovered"], bool)
    assert len(d["path"]) == d["n"], (len(d["path"]), d["n"])
    # drawdown accumulates -> |max drawdown| >= the worst single-day loss
    q = {"rows": "ScenarioSet", "measures": "Scenario worst loss",
         "filters": json.dumps({"Book": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})}
    wl = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()
    worst = wl["records"][0]["Scenario worst loss"]
    assert abs(d["max_drawdown"]) >= worst - 1e-9, (d["max_drawdown"], worst)


@integ
def t_drawdown_hypo_insufficient():
    import requests
    d = requests.get(f"{API}/drawdown", params={"set": "Hypo:MomentumCrash"}, timeout=30).json()
    assert d["status"] == "insufficient", d


def _run(group):
    p = f = 0
    for fn in group:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
    return p, f


def main():
    p = f = 0
    print("=== unit (no backend) ===")
    a, b = _run(UNIT); p += a; f += b
    print("\n=== integration (live backend) ===")
    if _backend_up():
        a, b = _run(INTEG); p += a; f += b
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
