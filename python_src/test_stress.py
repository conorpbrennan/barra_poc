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
def t_meta_serves_managers():
    """Multi-manager Phase 3: /meta serves the available books/managers -- Phase 4's UI context
    bar's single source. Today's data has no managers.parquet, so entries are book-name-only
    (entity attributes null) but the shape is identical to a build that HAS the frame."""
    import requests
    j = requests.get(f"{API}/meta", timeout=30).json()
    mgrs = j.get("managers")
    assert mgrs and isinstance(mgrs, list), mgrs
    books = {m["book"] for m in mgrs}
    # Asserted against the cube's own Book members rather than a hardcoded {"Soros"}: production
    # was single-book when this test was written and is 11-book since the multi-manager build, so
    # a literal expectation just goes stale again on the next scope change. ("N/A" is atoti's
    # default member for exposure rows no book holds — not a manager.)
    dims = requests.get(f"{API}/dims", timeout=60).json()
    assert books == {b for b in dims["members"]["Book"] if b != "N/A"}, (books, dims["members"]["Book"])
    for m in mgrs:
        for k in ("book", "entity_name", "firm_type", "cik", "n_positions_distinct"):
            assert k in m, (k, m)
    # entity attributes are populated exactly when managers.parquet is present; both shapes are
    # legal, so assert the invariant (same keys either way), not one build's contents.
    assert len({frozenset(m) for m in mgrs}) == 1, mgrs


@unit
def t_managers_meta_degrades_without_managers_frame():
    """UNIT (no backend): _managers_meta reads straight off S['frames'] -- with a positions frame
    but no 'managers' key (today's production shape) every entry is book-name-only, same key set
    as the with-managers case, never a different response shape the UI has to branch on."""
    import risk_api
    import pandas as pd
    saved = risk_api.S.get("frames")
    risk_api.S["frames"] = {"positions": pd.DataFrame({
        "Date": pd.to_datetime(["2024-01-31", "2024-01-31"]), "Book": ["Soros", "TigerGlobal"],
        "Position": ["p1", "p2"], "Weight": [1.0, 1.0],
    })}   # no "managers" key at all
    try:
        out = risk_api._managers_meta()
        assert {m["book"] for m in out} == {"Soros", "TigerGlobal"}, out
        for m in out:
            assert m["entity_name"] is None and m["firm_type"] is None and m["cik"] is None, m
    finally:
        if saved is not None:
            risk_api.S["frames"] = saved
        else:
            risk_api.S.pop("frames", None)


@unit
def t_managers_meta_uses_managers_frame_when_present():
    """UNIT: with a managers frame present, entity attributes are pulled in per book; a book in
    `positions` but ABSENT from `managers` (a partial/stale managers.parquet) still gets a row,
    just with null attributes -- holdings, not the managers frame, drive which books are listed."""
    import risk_api
    import pandas as pd
    saved = risk_api.S.get("frames")
    risk_api.S["frames"] = {
        "positions": pd.DataFrame({
            "Date": pd.to_datetime(["2024-01-31", "2024-01-31"]), "Book": ["Soros", "Elliott"],
            "Position": ["p1", "p2"], "Weight": [1.0, 1.0],
        }),
        "managers": pd.DataFrame({
            "Book": ["Soros"], "CIK": [1029160], "EntityName": ["SOROS FUND MANAGEMENT LLC"],
            "FirmType": ["hedge_fund"], "n_positions_distinct": [42],
        }),
    }
    try:
        out = {m["book"]: m for m in risk_api._managers_meta()}
        assert out["Soros"]["entity_name"] == "SOROS FUND MANAGEMENT LLC", out["Soros"]
        assert out["Soros"]["cik"] == 1029160, out["Soros"]
        assert out["Elliott"]["entity_name"] is None, out["Elliott"]   # held, not in managers frame
    finally:
        if saved is not None:
            risk_api.S["frames"] = saved
        else:
            risk_api.S.pop("frames", None)


@integ
def t_stress_served_from_cube():
    """/stress naive numbers are SERVED from the StressShock parameter simulation, with the
    numpy engine retained as the live cross-check: verification diffs at float precision, the
    transient scenario dropped (a second call is identical), components still sum to the total."""
    import requests
    a = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -2, "Value": 1.5}},
                      timeout=60).json()
    assert a.get("source") == "cube", a.get("source")
    v = a["verification"]
    assert v["total_abs_diff"] < 1e-12, v
    assert v["max_component_abs_diff"] < 1e-12, v
    b = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -2, "Value": 1.5}},
                      timeout=60).json()
    assert abs(b["total_pnl"] - a["total_pnl"]) < 1e-15


@integ
def t_pivot_shocks_param():
    """/pivot?shocks= runs the SAME guarded pivot under a transient custom stress: the by-Factor
    Custom stress PnL rows sum to /stress's total; an unknown factor 400s."""
    import requests
    body = {"shocks": {"Momentum": -3.0}}
    s = requests.post(f"{API}/stress", json=body, timeout=60).json()
    q = {"rows": "Factor", "measures": "Custom stress PnL",
         "filters": json.dumps({"Book": ["Soros"], "Date": [s["date"]],
                                "ScenarioSet": ["HistFull"]}),
         "shocks": json.dumps(body["shocks"])}
    j = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()
    tot = sum(r["Custom stress PnL"] for r in j["records"]
              if r.get("Custom stress PnL") is not None)
    assert abs(tot - s["total_pnl"]) < 1e-12, (tot, s["total_pnl"])
    q["shocks"] = json.dumps({"NotAFactor": 1.0})
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=30)
    assert r.status_code == 400, r.status_code


@integ
def t_corr_stress_served_from_cube():
    """The correlation-stress block is served from the cube's Stressed model vol (closed-form
    D J D expansion, no matrix algebra) on a transient CorrStress scenario: numpy _stressed_cov
    cross-check at float precision, the Base-scenario identity (Stressed model vol == Model vol
    at mult 1 / blend 0), and per-sector drill positive."""
    import requests
    s = requests.post(f"{API}/stress", json={"shocks": {"Value": -1},
                                             "vol_mult": 1.25, "rho": 0.75}, timeout=60).json()
    cs = s["correlation_stress"]
    assert cs.get("source") == "cube", cs
    assert cs["verification"]["base_abs_diff"] < 5e-10, cs["verification"]
    assert cs["verification"]["stressed_abs_diff"] < 5e-10, cs["verification"]
    assert cs["stressed_vol_1d"] > cs["base_vol_1d"]
    d = s["date"]
    q = {"rows": "ScenarioSet", "measures": "Stressed model vol,Model vol",
         "filters": json.dumps({"Book": ["Soros"], "Date": [d], "ScenarioSet": ["HistFull"]})}
    j = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()
    r = j["records"][0]                      # Base scenario: mult 1, blend 0
    assert abs(r["Stressed model vol"] - r["Model vol"]) < 1e-15, r
    q2 = dict(q); q2["rows"] = "Sector"; q2["measures"] = "Stressed model vol"
    j2 = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q2)}", timeout=60).json()
    cells = [x["Stressed model vol"] for x in j2["records"]
             if x.get("Stressed model vol") is not None]
    assert len(cells) >= 5 and all(c > 0 for c in cells)


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
