"""
test_span.py — checks for the span / high-confidence check (Phase 3; risk_api.py /span +
barra_universe_span.py).

  UNIT  — always run, no backend, no network: the pure geometry — Mahalanobis distance, the cloud
          centre/box, the per-factor extreme flag, and the weight-inside share.
  INTEG — need the live backend on :8010 AND the built artifact; SKIP if down: /span returns a
          per-month inside-share series in [0,1], a latest verdict, a detail list with D²/inside/extreme,
          a 2D scatter (cloud + book), and rejects a non-style factor pair.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_span.py
"""
from __future__ import annotations
import os

import numpy as np

import barra_universe_span as us

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
def t_mahalanobis_identity_is_euclidean():
    mu = np.zeros(2); Cinv = np.eye(2)
    d2 = us.mahalanobis2(np.array([[3.0, 0.0], [0.0, 4.0]]), mu, Cinv)
    assert abs(d2[0] - 9.0) < 1e-9 and abs(d2[1] - 16.0) < 1e-9, d2


@unit
def t_cloud_stats_centre():
    E = np.array([[0.0, 0.0], [2.0, 0.0], [0.0, 2.0], [2.0, 2.0]])
    mu, Cinv, lo, hi = us.cloud_stats(E)
    assert np.allclose(mu, [1.0, 1.0]), mu
    assert Cinv.shape == (2, 2) and lo.shape == (2,) and hi.shape == (2,)


@unit
def t_extreme_factors():
    lo = np.array([-1.0, -1.0]); hi = np.array([1.0, 1.0])
    assert us.extreme_factors(np.array([5.0, 0.0]), lo, hi, ["A", "B"]) == ["A"]
    assert us.extreme_factors(np.array([0.0, -3.0]), lo, hi, ["A", "B"]) == ["B"]
    assert us.extreme_factors(np.array([np.nan, 0.0]), lo, hi, ["A", "B"]) == []   # nan skipped


@unit
def t_inside_share():
    w = np.array([0.5, 0.3, 0.2]); inside = np.array([True, False, True])
    assert abs(us.inside_share(w, inside) - 0.7) < 1e-9


# ----------------------------------------------------------------------------- INTEG
@integ
def t_span_series_and_latest():
    import requests
    j = requests.get(f"{API}/span", timeout=60).json()
    assert j["factors"] == us.STYLE, j["factors"]
    assert j["series"], "empty series"
    for r in j["series"]:
        assert 0.0 <= r["inside_wt"] <= 1.0 and r["n_inside"] <= r["n_held"], r
    assert 0.0 <= j["latest"]["inside_wt"] <= 1.0


@integ
def t_span_detail_and_scatter():
    import requests
    j = requests.get(f"{API}/span", params={"fx": "Size", "fy": "Value"}, timeout=60).json()
    for d in j["detail"]:
        assert "d2" in d and "inside" in d and "extreme" in d
    sc = j["scatter"]
    assert sc["fx"] == "Size" and sc["fy"] == "Value"
    assert sc["cloud"] and all("x" in p and "y" in p for p in sc["cloud"][:3])
    assert all("inside" in p for p in sc["held"][:3]) if sc["held"] else True


@integ
def t_span_book_guard():
    """Multi-manager Phase 3: the artifact + live scatter are single-book. Default book (Soros)
    is UNCHANGED; any other book comes back as a clean book_mismatch status."""
    import requests
    base = requests.get(f"{API}/span", timeout=60).json()
    assert "status" not in base and base["series"], base
    mism = requests.get(f"{API}/span", params={"book": "Citadel"}, timeout=60).json()
    assert mism["status"] == "book_mismatch", mism
    assert mism["requested_book"] == "Citadel" and mism["artifact_book"] == "Soros", mism


@integ
def t_span_rejects_bad_factor():
    import requests
    r = requests.get(f"{API}/span", params={"fx": "NotAFactor"}, timeout=30)
    assert r.status_code == 400, r.status_code


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
