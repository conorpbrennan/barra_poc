"""
test_contributions.py — checks for the Euler risk-contribution report (/contributions).

  * UNIT  — always run, no backend: _euler_contributions identities on synthetic data —
            Σ CTR = σ exactly (Euler), Σ CTV = factor variance, negative CTV for a hedging
            exposure, MCR·w = CTR.
  * INTEG — need the live backend on :8010; SKIP if down. /contributions sums tie out, positions
            are held names ranked by CTR, factor rows include Market.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_contributions.py
"""
from __future__ import annotations
import os

import numpy as np

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


def _toy():
    """3 names, 2 factors; one factor exposure is a hedge (negative x against positive corr)."""
    rng = np.random.default_rng(3)
    Lv = np.array([[1.0, 0.8], [1.0, -0.4], [1.0, -2.0]])
    w = np.array([0.5, 0.3, 0.2])
    A = rng.normal(size=(300, 2)) @ np.array([[0.01, 0.004], [0.0, 0.02]])
    F = np.cov(A, rowvar=False)
    sv = np.array([0.0004, 0.0002, 0.0009])
    return w, Lv, F, sv


# --------------------------------------------------------------------------- UNIT
@unit
def t_euler_ctr_sums_to_vol():
    import risk_api
    w, Lv, F, sv = _toy()
    e = risk_api._euler_contributions(w, Lv, F, sv)
    assert abs(float(np.sum(e["ctr"])) - e["sigma"]) < 1e-12
    assert np.allclose(e["ctr"], w * e["mcr"])


@unit
def t_euler_ctv_sums_to_factor_variance():
    import risk_api
    w, Lv, F, sv = _toy()
    e = risk_api._euler_contributions(w, Lv, F, sv)
    assert abs(float(np.sum(e["ctv"])) - e["factor_var"]) < 1e-15
    x = Lv.T @ w
    assert abs(e["factor_var"] - float(x @ F @ x)) < 1e-15
    assert abs(e["sigma"] ** 2 - (e["factor_var"] + e["specific_var"])) < 1e-15


@unit
def t_euler_negative_ctv_for_hedge():
    """An exposure negatively co-varying with the book carries a negative CTV."""
    import risk_api
    F = np.array([[1.0, -0.3], [-0.3, 1.0]]) * 1e-4
    Lv = np.array([[1.0, 0.05], [1.0, 0.1]])
    w = np.array([0.5, 0.5])                          # x = (1.0, 0.075): tiny f1 vs big f0
    e = risk_api._euler_contributions(w, Lv, F, np.zeros(2))
    assert e["ctv"][1] < 0, e["ctv"]                  # f1's covariance with the book is negative
    assert abs(float(np.sum(e["ctv"])) - e["factor_var"]) < 1e-18


# --------------------------------------------------------------------------- INTEG
@integ
def t_contributions_tie_out():
    import requests
    j = requests.get(f"{API}/contributions", timeout=60).json()
    assert abs(j["sum_ctr"] - j["vol_1d"]) < 1e-10, (j["sum_ctr"], j["vol_1d"])
    assert abs(j["sum_ctv"] - j["factor_variance"]) < 1e-14
    assert abs(j["total_variance"] - (j["factor_variance"] + j["specific_variance"])) < 1e-18
    assert abs(j["vol_1d"] ** 2 - j["total_variance"]) < 1e-14


@integ
def t_contributions_cube_numpy_crosscheck():
    """/contributions serves the CUBE measures with the numpy Euler recomputed as a live
    cross-check — the `verification` diffs must sit at float precision. This preserves the
    independent-implementation guarantee after the endpoint moved to the cube."""
    import requests
    j = requests.get(f"{API}/contributions", timeout=120).json()
    assert j.get("source") == "cube"
    v = j["verification"]
    assert v["vol_abs_diff"] < 5e-10, v
    assert v["max_ctv_abs_diff"] < 5e-10, v
    assert v["max_ctr_abs_diff"] < 5e-10, v


@integ
def t_contributions_shape():
    import requests
    j = requests.get(f"{API}/contributions", timeout=60).json()
    assert any(r["factor"] == "Market" for r in j["factors"])
    ps = j["positions"]
    assert ps and all(p["weight"] != 0 for p in ps)
    ctrs = [p["ctr"] for p in ps]
    assert ctrs == sorted(ctrs, reverse=True)
    # position CTRs sum to vol too (unheld names contribute 0)
    assert abs(sum(ctrs) - j["vol_1d"]) < 1e-9


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
