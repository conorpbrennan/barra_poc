"""
test_backtest.py — checks for the VaR backtest (Step 3).

  * UNIT  — always run, no backend: the pure Kupiec POF and Basel-zone statistics on known inputs.
  * INTEG — need the live backend on :8010; SKIP if down. /backtest returns a well-formed result on
            HistFull, exception count is in a sane range, and a bad-window request is rejected.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_backtest.py
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


# --------------------------------------------------------------------------- UNIT
@unit
def t_basel_zones_250_99():
    """The classic 250-day / 99% Basel traffic-light: green 0-4, amber 5-9, red 10+."""
    import risk_api
    for n in range(0, 5):
        assert risk_api._basel_zone(n, 250, 0.01)[0] == "green", n
    for n in range(5, 10):
        assert risk_api._basel_zone(n, 250, 0.01)[0] == "amber", n
    for n in (10, 12, 20):
        assert risk_api._basel_zone(n, 250, 0.01)[0] == "red", n


@unit
def t_kupiec_well_calibrated_passes():
    """Exceptions near the expected rate -> low LR, below the 3.841 critical value (don't reject)."""
    import risk_api
    assert risk_api._kupiec_lr(2, 250, 0.01) < 3.841          # ~expected (2.5)
    assert risk_api._kupiec_lr(3, 250, 0.01) < 3.841


@unit
def t_kupiec_too_many_rejects():
    """Far too many exceptions -> high LR, above critical (reject the model)."""
    import risk_api
    assert risk_api._kupiec_lr(10, 250, 0.01) > 3.841


@unit
def t_kupiec_zero_exceptions_finite():
    """Zero exceptions is a valid (over-conservative) case — LR finite and non-negative."""
    import risk_api
    lr = risk_api._kupiec_lr(0, 250, 0.01)
    assert lr >= 0 and lr != float("inf"), lr


@unit
def t_var_thresholds_react_to_vol():
    """EWMA/FHS thresholds widen after a volatility jump faster than equal-weight HS — the whole
    point of the reactive methods. Synthetic series: calm, then a 10x-vol regime."""
    import numpy as np, risk_api
    rng = np.random.default_rng(0)
    pnl = np.concatenate([rng.normal(0, 0.01, 300), rng.normal(0, 0.10, 60)])
    just_after = 305                                    # a few days into the high-vol regime
    for method in ("ewma", "fhs"):
        t_eq = risk_api._var_thresholds(pnl, 250, 0.99, "equal", 0.94)
        t_rx = risk_api._var_thresholds(pnl, 250, 0.99, method, 0.94)
        # reactive method's loss threshold is more negative (wider) right after the vol jump
        assert t_rx[just_after] < t_eq[just_after], (method, t_rx[just_after], t_eq[just_after])


# --------------------------------------------------------------------------- INTEG
@integ
def t_backtest_endpoint_shape():
    """/backtest on HistFull returns a complete result with a valid Basel zone and Kupiec fields."""
    import requests
    d = requests.get(f"{API}/backtest", timeout=120); d.raise_for_status()
    j = d.json()
    assert j["status"] == "ok", j
    assert j["tested"] > 0 and j["exceptions"] >= 0, j
    assert j["basel_zone"] in ("green", "amber", "red"), j["basel_zone"]
    for k in ("kupiec_LR", "kupiec_reject", "expected", "rate"):
        assert k in j, (k, j)


@integ
def t_backtest_exception_rate_sane():
    """At 99%, the realized exception rate should be in a believable band (0%–5%), not wildly off."""
    import requests
    j = requests.get(f"{API}/backtest", params={"alpha": 0.99, "window": 250}, timeout=120).json()
    if j["status"] == "ok" and j["rate"] is not None:
        assert 0.0 <= j["rate"] < 0.05, j["rate"]


@integ
def t_backtest_rejects_bad_window():
    """window < 30 -> 400."""
    import requests
    r = requests.get(f"{API}/backtest", params={"window": 5}, timeout=30)
    assert r.status_code == 400, r.status_code


@integ
def t_backtest_rejects_bad_method():
    """An unknown method -> 400."""
    import requests
    r = requests.get(f"{API}/backtest", params={"method": "nope"}, timeout=30)
    assert r.status_code == 400, r.status_code


@integ
def t_backtest_default_is_fhs():
    """The chosen production default is FHS λ=0.94."""
    import requests
    j = requests.get(f"{API}/backtest", timeout=120).json()
    assert j["method"] == "fhs" and j["lam"] == 0.94, (j["method"], j["lam"])


@integ
def t_fhs_better_calibrated_than_equal():
    """At 99%, FHS λ=0.94 lands closer to the 1% target breach rate than equal-weight HS — the
    reason it was picked as the default."""
    import requests
    fhs = requests.get(f"{API}/backtest", params={"method": "fhs", "lam": 0.94}, timeout=120).json()
    eq = requests.get(f"{API}/backtest", params={"method": "equal"}, timeout=120).json()
    if fhs["rate"] is not None and eq["rate"] is not None:
        assert abs(fhs["rate"] - 0.01) <= abs(eq["rate"] - 0.01), (fhs["rate"], eq["rate"])
        assert not fhs["kupiec_reject"], fhs["kupiec_LR"]      # well-calibrated -> doesn't reject


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
