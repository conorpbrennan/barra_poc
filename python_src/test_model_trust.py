"""
test_model_trust.py — checks for the fit-for-purpose + alignment-view endpoints
(/calibration, /regression, /factor_cov, /hedge, /exposure_profile, /factor_portfolio,
/pnl_attribution/names).

  * UNIT  — always run, no backend: _hedge_table identities (neutralize-the-only-exposure
            kills factor vol; the market h* is the min-variance hedge; ranking).
            (_rolling_bias is unit-tested in test_attribution.py.)
  * INTEG — need the live backend on :8010; SKIP if down.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_model_trust.py
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


# --------------------------------------------------------------------------- UNIT
@unit
def t_hedge_neutralize_only_exposure():
    """One nonzero exposure: zeroing it leaves exactly the specific vol."""
    import risk_api
    F = np.array([[4.0, 0.0], [0.0, 1.0]]) * 1e-4
    x = np.array([1.0, 0.0]); svar = 1e-4
    h = risk_api._hedge_table(x, F, svar, ["Market", "Value"])
    row = next(r for r in h["rows"] if r["factor"] == "Market")
    assert abs(row["vol_after"] - np.sqrt(svar)) < 1e-15
    assert abs(row["hedge_units"] + 1.0) < 1e-15
    assert abs(h["vol_base"] - np.sqrt(4e-4 + svar)) < 1e-15


@unit
def t_hedge_market_hstar_is_min_variance():
    """With correlated factors, h* = −(Fx)_m/F_mm beats naive full neutralization of Market
    alone when the cross-covariance matters — and vol_after at h* is a true minimum (perturbing
    h* in either direction increases vol)."""
    import risk_api
    F = np.array([[4.0, 1.0], [1.0, 2.0]]) * 1e-4
    x = np.array([1.0, 0.5]); svar = 0.0
    h = risk_api._hedge_table(x, F, svar, ["Market", "Value"])
    m = h["market_hedge"]
    hs = m["h_star"]
    def vol(hh):
        xh = x.copy(); xh[0] += hh
        return float(np.sqrt(xh @ F @ xh))
    assert abs(vol(hs) - m["vol_after"]) < 1e-15
    assert vol(hs) < vol(hs + 0.05) and vol(hs) < vol(hs - 0.05)
    assert abs(hs + (F @ x)[0] / F[0, 0]) < 1e-12


@unit
def t_hedge_rows_ranked_by_reduction():
    import risk_api
    rng = np.random.default_rng(5)
    A = rng.normal(size=(200, 4)); F = np.cov(A, rowvar=False) * 1e-4
    x = np.array([1.0, -0.4, 0.2, 0.05])
    h = risk_api._hedge_table(x, F, 1e-5, ["a", "b", "c", "d"])
    reds = [r["vol_reduction"] for r in h["rows"]]
    assert reds == sorted(reds, reverse=True)


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


@integ
def t_calibration_shape():
    """Rolling bias series for book + specific, band = sqrt(2/window), b > 0 throughout."""
    import math
    import requests
    j = requests.get(f"{API}/calibration", params={"window": 24}, timeout=120).json()
    for k in ("book", "specific"):
        s = j["series"][k]
        assert len(s["bias"]) > 12, (k, len(s["bias"]))
        assert abs(s["band"] - math.sqrt(2 / 24)) < 1e-9
        assert all(p["b"] > 0 for p in s["bias"])
        assert s["exceedance_2s"] is None or 0 <= s["exceedance_2s"] <= 1


@integ
def t_calibration_pit_served_from_cube():
    """/calibration's predicted vols now come from the cube's PIT:* sets, with the numpy F(≤t)
    engine as the live cross-check: most months cube-served, diffs at float precision."""
    import requests
    j = requests.get(f"{API}/calibration", params={"window": 24}, timeout=180).json()
    v = j.get("pit_verification") or {}
    assert "error" not in v, v
    assert v.get("months_from_cube", 0) >= 90, v          # ~105 of 108 months carry a PIT set
    assert v["book"] < 5e-10 and v["specific"] < 5e-10 and v["factor"] < 5e-10, v


@integ
def t_calibration_window_shrinks_series():
    """A longer window means fewer rolling points."""
    import requests
    a = requests.get(f"{API}/calibration", params={"window": 12}, timeout=120).json()
    b = requests.get(f"{API}/calibration", params={"window": 36}, timeout=120).json()
    assert len(a["series"]["book"]["bias"]) > len(b["series"]["book"]["bias"])


@integ
def t_regression_shape():
    """R² monthly trend + per-factor |t|>2 shares, Market the most significant factor."""
    import requests
    j = requests.get(f"{API}/regression", timeout=60).json()
    assert 0 < j["r2_mean"] < 1
    assert len(j["r2_monthly"]) > 24
    assert all(0 <= f["pct_days_t_gt2"] <= 1 for f in j["factors"])
    shares = [f["pct_days_t_gt2"] for f in j["factors"]]
    assert shares == sorted(shares, reverse=True)
    assert j["factors"][0]["factor"] == "Market"          # the intercept dominates daily fits
    assert j["n_names"]["min"] >= 30                      # the builder's own floor


@integ
def t_factor_cov_shape():
    """Correlation matrix is symmetric with unit diagonal; vols positive; recent ⊂ full."""
    import requests
    j = requests.get(f"{API}/factor_cov", timeout=60).json()
    n = len(j["factors"])
    C = j["corr"]
    for i in range(n):
        assert abs(C[i][i] - 1.0) < 1e-9
        for k in range(n):
            assert abs(C[i][k] - C[k][i]) < 1e-9
            assert -1.000001 <= C[i][k] <= 1.000001
    assert all(v > 0 for v in j["vol_full"].values())
    assert j["n_days_recent"] < j["n_days"]


@integ
def t_hedge_consistent_with_contributions():
    """/hedge base vol (numpy) equals /contributions vol (now cube-served); rows ranked; market
    h* set. Tolerance is the cube↔numpy float-precision bound, not 1e-12 identity."""
    import requests
    h = requests.get(f"{API}/hedge", timeout=60).json()
    c = requests.get(f"{API}/contributions", timeout=60).json()
    assert abs(h["vol_base"] - c["vol_1d"]) < 5e-10
    reds = [r["vol_reduction"] for r in h["rows"]]
    assert reds == sorted(reds, reverse=True)
    assert h["market_hedge"] and h["market_hedge"]["vol_after"] < h["vol_base"]
    # no factor hedge can beat the specific floor
    assert all(r["vol_after"] >= h["specific_vol"] - 1e-12 for r in h["rows"])


@integ
def t_exposure_profile_shape():
    import requests
    j = requests.get(f"{API}/exposure_profile", params={"factor": "Size"}, timeout=60).json()
    assert sum(b["n"] for b in j["hist"]) == j["n_names"]
    assert j["held"] and all("ticker" in r for r in j["held"])
    assert 0 <= j["beyond3"]["share"] < 0.5
    assert j["quantiles"]["p01"] < j["quantiles"]["p50"] < j["quantiles"]["p99"]
    assert requests.get(f"{API}/exposure_profile",
                        params={"factor": "NotAFactor"}, timeout=30).status_code == 400


@integ
def t_factor_portfolio_purity():
    """PX = I: unit self exposure, ~zero cross exposures. A style portfolio is dollar-neutral
    (net ≈ 0, long-short); the Market portfolio is fully invested (net ≈ 1). NB gross can sit
    near 1× on a broad cross-section — the mini-example's 11.9× came from a 10-stock universe
    with industry dummies, so don't assert gross > 1."""
    import requests
    j = requests.get(f"{API}/factor_portfolio", params={"factor": "Value"}, timeout=60).json()
    assert abs(j["self_exposure"] - 1.0) < 1e-8, j["self_exposure"]
    assert j["max_cross_exposure"] < 1e-8
    assert abs(j["net"]) < 1e-8, j["net"]                      # style: dollar-neutral
    assert j["gross_leverage"] >= abs(j["net"])
    assert j["longs"] and j["shorts"] and j["shorts"][0]["weight"] < 0
    m = requests.get(f"{API}/factor_portfolio", params={"factor": "Market"}, timeout=60).json()
    assert abs(m["net"] - 1.0) < 1e-8, m["net"]                # market: fully invested


@integ
def t_pnl_names_shape():
    import requests
    j = requests.get(f"{API}/pnl_attribution/names", timeout=120).json()
    assert all(r["specific_pnl"] > 0 for r in j["winners"])
    assert all(r["specific_pnl"] < 0 for r in j["losers"])
    for r in j["winners"] + j["losers"]:
        assert abs(r["realized"] - r["factor_pnl"] - r["specific_pnl"]) < 1e-12
        if r["sign_persistence"] is not None:
            assert 0 <= r["sign_persistence"] <= 1


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
