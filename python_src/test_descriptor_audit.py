"""
test_descriptor_audit.py — unit tests for the pure descriptor-health helpers (no backend,
no artifacts): _collinear_pairs, _residual_betas, _coverage.

Run:  ../barra/bin/python test_descriptor_audit.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from barra_descriptor_audit import _collinear_pairs, _coverage, _residual_betas

TESTS = []


def t(fn):
    TESTS.append(fn); return fn


@t
def t_collinear_pairs_finds_planted_pair():
    rng = np.random.default_rng(7)
    n = 500
    a = rng.normal(size=n)
    wide = pd.DataFrame({
        "A": a,
        "B": -0.9 * a + 0.3 * rng.normal(size=n),     # strongly anti-correlated with A
        "C": rng.normal(size=n),                       # independent
    })
    pairs = _collinear_pairs(wide, thresh=0.6)
    assert len(pairs) == 1 and {pairs[0]["a"], pairs[0]["b"]} == {"A", "B"}
    assert pairs[0]["rho"] < -0.8
    assert _collinear_pairs(wide, thresh=0.99) == []


@t
def t_residual_betas_recovers_planted_beta():
    """A residual built as 1.5*f + noise must come back with beta ~ 1.5 and |t| > 2;
    a pure-noise residual with beta ~ 0. Book beta = Sum(w*b)."""
    rng = np.random.default_rng(11)
    n = 250
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    f = pd.Series(rng.normal(0, 0.01, n), index=idx)
    panel = pd.DataFrame({
        "HID": 1.5 * f + rng.normal(0, 0.002, n),
        "CLEAN": pd.Series(rng.normal(0, 0.01, n), index=idx),
    })
    w = pd.Series({"HID": 0.10, "CLEAN": 0.20})
    r = _residual_betas(panel, f, w, min_obs=120)
    by = {x["position"]: x for x in r["names"]}
    assert abs(by["HID"]["beta"] - 1.5) < 0.1 and abs(by["HID"]["t"]) > 2
    assert abs(by["CLEAN"]["beta"]) < 0.5 and abs(by["CLEAN"]["t"]) < 2
    expect = 0.10 * by["HID"]["beta"] + 0.20 * by["CLEAN"]["beta"]
    assert abs(r["book_beta"] - expect) < 1e-12
    assert r["n_names"] == 2 and abs(r["weight_tested"] - 0.30) < 1e-12


@t
def t_residual_betas_gates():
    """Zero-weight names and short histories are excluded; empty input degrades cleanly."""
    idx = pd.date_range("2025-01-01", periods=200, freq="B")
    f = pd.Series(np.random.default_rng(3).normal(0, 0.01, 200), index=idx)
    panel = pd.DataFrame({
        "SHORT": pd.Series(np.random.default_rng(4).normal(size=50), index=idx[:50]),
        "UNHELD": pd.Series(np.random.default_rng(5).normal(size=200), index=idx),
    })
    r = _residual_betas(panel, f, pd.Series({"SHORT": 0.1}), min_obs=120)
    assert r["n_names"] == 0 and r["book_beta"] is None


@t
def t_coverage_splits_weight():
    exp_d = pd.DataFrame({"Position": ["P1", "P2"], "Factor": ["Liquidity", "Liquidity"],
                          "Loading": [0.5, -1.0]})
    held = pd.Series({"P1": 0.5, "P2": 0.3, "P3": 0.2})
    c = _coverage(exp_d, held, "Liquidity")
    assert abs(c["weight_covered"] - 0.8) < 1e-12
    assert abs(c["weight_missing"] - 0.2) < 1e-12
    assert c["n_missing"] == 1 and list(c["missing"].index) == ["P3"]
    z = _coverage(exp_d, held, "Momentum")
    assert z["weight_covered"] == 0.0 and z["n_missing"] == 3


def main():
    p = f = 0
    for fn in TESTS:
        try:
            fn(); print(f"PASS  {fn.__name__}"); p += 1
        except Exception as e:
            import traceback
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
