"""
test_stress.py — checks for custom & reverse stress (Step 5).

  * UNIT  — always run, no backend: _conditional_shock (covariance propagation, all-factor
            identity, diagonal-F no-op).
  * INTEG — need the live backend on :8010; SKIP if down. Custom stress reproduces the cube's
            built-in Hypo set exactly (same linear math), components sum to the total, the
            conditional block propagates by covariance, reverse stress round-trips (its implied
            sigma reproduces the target loss), and bad inputs 400.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_stress.py
"""
from __future__ import annotations
import os
import json
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
UNIT, INTEG = [], []


def unit(fn):
    UNIT.append(fn); return fn


def integ(fn):
    INTEG.append(fn); return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------- UNIT
@unit
def t_conditional_shock_propagates_by_covariance():
    """E[f|f_0=s] on a 2-factor F: the unshocked factor moves by F10/F00·s (the beta of f1 on f0)."""
    import numpy as np
    import risk_api
    F = np.array([[1.0, 0.5], [0.5, 4.0]])            # vols 1 & 2, corr 0.25
    f = risk_api._conditional_shock(F, [0], np.array([-1.0]))
    assert abs(f[0] - (-1.0)) < 1e-12                 # the shocked factor IS the shock
    assert abs(f[1] - (-0.5)) < 1e-12                 # beta = F10/F00 = 0.5


@unit
def t_conditional_shock_all_factors_is_identity():
    """Conditioning on every factor returns the shock vector itself — the naive case."""
    import numpy as np
    import risk_api
    rng = np.random.default_rng(7)
    A = rng.normal(size=(5, 3)); F = A.T @ A + np.eye(3) * 0.1
    s = np.array([0.01, -0.02, 0.005])
    f = risk_api._conditional_shock(F, [0, 1, 2], s)
    assert np.allclose(f, s)


@unit
def t_conditional_shock_uncorrelated_stays_naive():
    """With a diagonal F the co-moving factors don't move — conditional == naive."""
    import numpy as np
    import risk_api
    F = np.diag([1.0, 2.0, 3.0])
    f = risk_api._conditional_shock(F, [1], np.array([0.5]))
    assert abs(f[1] - 0.5) < 1e-12 and abs(f[0]) < 1e-12 and abs(f[2]) < 1e-12


@integ
def t_stress_conditional_block():
    """conditional:true adds the correlated read: components sum to its total, the shocked factor's
    implied return equals sigma·vol, and unshocked co-moving factors carry non-zero implied moves."""
    import requests
    s = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -2}, "conditional": True},
                      timeout=60).json()
    c = s["conditional"]
    assert abs(sum(r["pnl"] for r in c["components"]) - c["total_pnl"]) < 1e-12
    mom = next(r for r in c["components"] if r["factor"] == "Momentum")
    assert mom["shocked"] and abs(mom["implied_sigma"] - (-2)) < 1e-6, mom
    others = [r for r in c["components"] if not r["shocked"]]
    assert any(abs(r["implied_return"]) > 1e-9 for r in others)   # covariance propagated


@integ
def t_meta_serves_hypo_shocks():
    """/meta serves the cube's HYPO_SHOCKS definitions (the Stress-lens presets' single source):
    every set present, every entry {Factor: sigma} with factors the cube knows."""
    import requests
    j = requests.get(f"{API}/meta", timeout=30).json()
    hs = j.get("hypo_shocks")
    assert hs and set(hs) == {"Hypo:ValueRotation", "Hypo:RiskOff", "Hypo:MomentumCrash"}, hs
    facs = set(j["factors"])
    for name, shocks in hs.items():
        assert shocks, name
        for f_, sig in shocks.items():
            assert f_ in facs and isinstance(sig, (int, float)), (name, f_, sig)
    assert hs["Hypo:MomentumCrash"] == {"Momentum": -3.0}


@integ
def t_stress_cube_prototype_ties_and_cleans_up():
    """The Tier-2 parameter-simulation prototype: /stress returns a cube_prototype block whose
    total ties the naive API number at float precision, and the transient scenario is dropped —
    a second call gets a fresh branch and the same answer (no residue)."""
    import requests
    a = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -2, "Value": 1.5}},
                      timeout=60).json()
    cp = a.get("cube_prototype", {})
    assert "error" not in cp, cp
    assert cp["total_pnl"] is not None
    assert cp["abs_diff_vs_naive"] < 1e-12, cp
    b = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -2, "Value": 1.5}},
                      timeout=60).json()
    assert abs(b["cube_prototype"]["total_pnl"] - cp["total_pnl"]) < 1e-15


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
    print("=== unit ===")
    for fn in UNIT:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
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
