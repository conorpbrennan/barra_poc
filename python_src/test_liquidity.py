"""
test_liquidity.py — checks for the days-to-liquidate endpoint (Step 11; risk_api.py /liquidity).

  UNIT  — always run, no backend: the pure days-to-liquidate formula (MV / (participation·ADV);
          NaN where ADV is missing or non-positive).
  INTEG — need the live backend on :8010 AND frames rebuilt with the ADV column; SKIP if down:
          /liquidity returns a share-within-horizon in [0,1], a detail list sorted by days (with
          days reconciling to MV/(part·ADV)), and reports no-ADV names separately.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_liquidity.py
"""
from __future__ import annotations
import os

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


# ----------------------------------------------------------------------------- UNIT
@unit
def t_days_to_liquidate():
    import numpy as np
    import pandas as pd
    import risk_api
    mv = pd.Series([10e6, 10e6, 5e6, 1e6])
    adv = pd.Series([5e6, 1e6, 0.0, np.nan])      # $5M, $1M, zero, missing
    days = risk_api._days_to_liquidate(mv, adv, 0.20)
    # 10M / (0.2*5M) = 10 days ; 10M / (0.2*1M) = 50 days
    assert abs(days.iloc[0] - 10.0) < 1e-9, days.iloc[0]
    assert abs(days.iloc[1] - 50.0) < 1e-9, days.iloc[1]
    assert np.isnan(days.iloc[2]) and np.isnan(days.iloc[3])   # zero / missing ADV -> NaN


@unit
def t_participation_scales_linearly():
    import pandas as pd, risk_api
    mv, adv = pd.Series([10e6]), pd.Series([1e6])
    d10 = risk_api._days_to_liquidate(mv, adv, 0.10).iloc[0]
    d20 = risk_api._days_to_liquidate(mv, adv, 0.20).iloc[0]
    assert abs(d10 - 2 * d20) < 1e-9, (d10, d20)              # half the participation -> twice the days


# ----------------------------------------------------------------------------- INTEG
@integ
def t_liquidity_shape_and_reconcile():
    import requests
    j = requests.get(f"{API}/liquidity", params={"participation": 0.2, "horizon": 5}, timeout=60).json()
    assert j["participation"] == 0.2 and j["horizon_days"] == 5
    assert 0.0 <= j["pct_weight_within_horizon"] <= 1.0
    det = j["detail"]
    assert det, "empty detail"
    days = [r["days"] for r in det]
    assert days == sorted(days, reverse=True), "detail not sorted by days desc"
    r = det[0]
    assert abs(r["days"] - r["MV"] / (0.2 * r["ADV"])) < 1e-3, r   # formula reconciles
    assert j["n_no_adv"] >= 0


@integ
def t_higher_participation_fewer_days():
    import requests
    slow = requests.get(f"{API}/liquidity", params={"participation": 0.1, "horizon": 5}, timeout=60).json()
    fast = requests.get(f"{API}/liquidity", params={"participation": 0.4, "horizon": 5}, timeout=60).json()
    # trading a bigger share of ADV each day -> more of the book exits within the horizon
    assert fast["pct_weight_within_horizon"] >= slow["pct_weight_within_horizon"], \
        (fast["pct_weight_within_horizon"], slow["pct_weight_within_horizon"])


def main():
    p = f = 0
    print("=== unit (no backend) ===")
    for fn in UNIT:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
    print("\n=== integration (live backend) ===")
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
