"""
test_stress.py — checks for custom & reverse stress (Step 5).

  * INTEG — need the live backend on :8010; SKIP if down. Custom stress reproduces the cube's
            built-in Hypo set exactly (same linear math), components sum to the total, reverse
            stress round-trips (its implied sigma reproduces the target loss), and bad inputs 400.

The stress helpers read the cube + frames, so there are no pure-unit cases.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_stress.py
"""
from __future__ import annotations
import os
import json
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
INTEG = []


def integ(fn):
    INTEG.append(fn); return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


@integ
def t_stress_matches_cube_hypo():
    """Custom /stress {Momentum:-3} == the cube's Hypo:MomentumCrash mean P&L (a length-1 vector),
    since both are Σ x_k·(σ_k·vol_k) with the same vols — validates the linear stress math."""
    import requests
    s = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -3}}, timeout=30).json()
    # match /stress's date (it defaults to the latest) — without it the cube aggregates all dates
    q = {"rows": "ScenarioSet", "measures": "Scenario mean PnL",
         "filters": json.dumps({"Book": ["Soros"], "Date": [s["date"]],
                                "ScenarioSet": ["Hypo:MomentumCrash"]})}
    cube = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=30).json()
    cube_pnl = cube["records"][0]["Scenario mean PnL"]
    assert abs(s["total_pnl"] - cube_pnl) < 1e-9, (s["total_pnl"], cube_pnl)


@integ
def t_stress_components_sum_to_total():
    """The per-factor P&L components sum to the reported total."""
    import requests
    s = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -2, "Value": 1.5, "Beta": -1}},
                      timeout=30).json()
    comp_sum = sum(c["pnl"] for c in s["components"])
    assert abs(comp_sum - s["total_pnl"]) < 1e-12, (comp_sum, s["total_pnl"])
    assert abs(s["loss"] + s["total_pnl"]) < 1e-12, (s["loss"], s["total_pnl"])   # loss = -total


@integ
def t_stress_rejects_unknown_factor():
    import requests
    r = requests.post(f"{API}/stress", json={"shocks": {"NotAFactor": 1.0}}, timeout=30)
    assert r.status_code == 400, r.status_code


@integ
def t_stress_rejects_empty():
    import requests
    r = requests.post(f"{API}/stress", json={"shocks": {}}, timeout=30)
    assert r.status_code == 400, r.status_code


@integ
def t_reverse_stress_ranked_and_shaped():
    """/reverse_stress returns factors ranked ascending by |sigma|, with the weakest first."""
    import requests
    j = requests.get(f"{API}/reverse_stress", params={"loss": 0.05}, timeout=30).json()
    facs = j["factors"]
    assert facs and j["weakest"]["factor"] == facs[0]["factor"], j["weakest"]
    abss = [abs(f["sigma_to_breach"]) for f in facs]
    assert abss == sorted(abss), abss


@integ
def t_reverse_stress_roundtrips():
    """The weakest factor's sigma-to-breach, fed back through /stress, reproduces the target loss."""
    import requests
    L = 0.05
    j = requests.get(f"{API}/reverse_stress", params={"loss": L}, timeout=30).json()
    w = j["weakest"]
    s = requests.post(f"{API}/stress", json={"shocks": {w["factor"]: w["sigma_to_breach"]}}, timeout=30).json()
    assert abs(s["loss"] - L) < 1e-6, (s["loss"], L)


def main():
    p = f = 0
    print("=== integration (live backend) ===")
    if _backend_up():
        for fn in INTEG:
            try:
                fn(); print(f"PASS  {fn.__name__}"); p += 1
            except Exception as e:
                import traceback
                print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
