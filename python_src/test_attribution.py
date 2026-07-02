"""
test_attribution.py — checks for PnL attribution & factor-model validation (Step 15).

  * UNIT  — always run, no backend: the pure stats (_carino_link exactness, _info_ratio,
            _autocorr, _bias_stat, _concentration_hhi, _hit_rate, _resid_factor_regression,
            _stressed_cov) and the §1 three-way tie-out (realized = Σ factor contribution +
            specific, machine precision) on a tiny synthetic seven-frame set.
  * INTEG — need the live backend on :8010; SKIP if down. /pnl_attribution linked contributions
            sum to the geometric return exactly; /residual returns RAG checks; /linkage bands
            behave (stressed ≥ base, verdicts consistent); the cube's attribution measures foot
            (Σ factor rows = grand) and Realized = Factor contribution + Specific PnL at book
            level; /stress correlation-stress widens book vol.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_attribution.py
"""
from __future__ import annotations
import os
import json
import urllib.parse

import numpy as np
import pandas as pd

from barra_pnl_attribution import (
    _carino_link, _info_ratio, _autocorr, _bias_stat, _concentration_hhi, _hit_rate,
    _resid_factor_regression, _stressed_cov, compute_attribution,
)

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


# --------------------------------------------------------------------------- UNIT: pure stats
@unit
def t_carino_links_exactly():
    """Linked contributions sum to the compounded period return with NO plug, including a
    zero-return day (the k_t = 1 branch)."""
    r = pd.Series([0.01, -0.02, 0.0, 0.03])
    contrib = pd.DataFrame({"A": [0.004, -0.01, 0.0, 0.02], "B": [0.006, -0.01, 0.0, 0.01]})
    assert np.allclose(contrib.sum(axis=1), r)          # daily identity holds by construction
    linked, rg = _carino_link(contrib, r)
    assert abs(rg - (float(np.prod(1 + r)) - 1)) < 1e-15
    assert abs(sum(linked.values()) - rg) < 1e-12, (sum(linked.values()), rg)


@unit
def t_info_ratio():
    u = pd.Series([0.01, 0.02, 0.0, 0.015, 0.005, 0.01])
    ir = _info_ratio(u)
    assert ir is not None and abs(ir - float(u.mean() / u.std(ddof=1) * np.sqrt(12))) < 1e-12
    assert _info_ratio(pd.Series([0.01, 0.01, 0.01])) is None      # zero std -> None
    assert _info_ratio(pd.Series([0.01])) is None                  # too short


@unit
def t_autocorr():
    alt = pd.Series([1.0, -1.0] * 10)                   # perfectly alternating
    assert _autocorr(alt, 1) is not None and _autocorr(alt, 1) < -0.9
    assert _autocorr(alt, 2) > 0.9
    assert _autocorr(pd.Series([1.0, 2.0]), 1) is None  # too short


@unit
def t_bias_stat():
    z = [1.0, -1.0, 2.0, -2.0, 1.5, -1.5, 0.5, -0.5]
    realized = pd.Series(z, dtype=float)                # predicted vol 1 -> B = std(z)
    b, band = _bias_stat(realized, pd.Series(1.0, index=realized.index))
    assert abs(b - float(pd.Series(z).std(ddof=1))) < 1e-12
    assert abs(band - np.sqrt(2 / len(z))) < 1e-12
    assert _bias_stat(pd.Series([1.0, 2.0]), pd.Series([1.0, 1.0])) == (None, None)  # too short


@unit
def t_concentration_and_hit_rate():
    one = _concentration_hhi(pd.Series([0.05]))
    assert one["hhi"] == 1.0 and one["top5_share"] == 1.0
    ten = _concentration_hhi(pd.Series([0.01] * 10))
    assert abs(ten["hhi"] - 0.1) < 1e-12 and abs(ten["top5_share"] - 0.5) < 1e-12
    assert _hit_rate(pd.Series([1.0, -1.0, 2.0, 3.0])) == 0.75
    assert _hit_rate(pd.Series(dtype=float)) is None


@unit
def t_resid_factor_regression():
    rng = np.random.default_rng(7)
    f = pd.DataFrame({"F1": rng.normal(0, 0.01, 120), "F2": rng.normal(0, 0.01, 120)})
    u = pd.Series(0.5 * f["F1"].to_numpy())             # pure F1 beta, no noise
    reg = _resid_factor_regression(u, f)
    assert reg["r2"] > 0.999
    top = reg["loadings"][0]
    assert top["factor"] == "F1" and abs(top["beta"] - 0.5) < 1e-9
    noise = pd.Series(rng.normal(0, 0.01, 120))          # orthogonal residual
    assert _resid_factor_regression(noise, f)["r2"] < 0.1
    assert _resid_factor_regression(pd.Series([0.01, 0.02]), f.head(2))["r2"] is None


@unit
def t_stressed_cov():
    v = np.array([0.01, 0.02]); C = np.array([[1.0, 0.3], [0.3, 1.0]])
    F = np.outer(v, v) * C
    assert np.allclose(_stressed_cov(F, 1.0, 0.0), F)                       # no-op
    Fs = _stressed_cov(F, 1.25, 1.0)                                        # correlations -> 1
    vs = np.sqrt(np.diag(Fs))
    assert np.allclose(vs, v * 1.25)
    assert np.allclose(Fs / np.outer(vs, vs), np.ones((2, 2)))
    half = _stressed_cov(F, 1.0, 0.5)                                       # blend halves the gap
    assert abs(half[0, 1] / (v[0] * v[1]) - 0.65) < 1e-12


# --------------------------------------------------------------------------- UNIT: the tie-out
def _toy_frames():
    """Two names, one style factor + Market, two exposure months, one 13F filing. Returns are
    RECONSTRUCTED by the engine as L·f + eps, so realized = factor + specific is testable."""
    m1, m2 = pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-29")
    days1 = pd.to_datetime(["2024-02-05", "2024-02-12"])
    days2 = pd.to_datetime(["2024-03-04", "2024-03-11"])
    exposures = pd.DataFrame(
        [(d, p, f, l) for d in (m1, m2)
         for p, f, l in [("AAA", "Size", 0.5), ("AAA", "Market", 1.0),
                         ("BBB", "Size", -1.0), ("BBB", "Market", 1.0)]],
        columns=["Date", "Position", "Factor", "Loading"])
    factor_returns = pd.DataFrame(
        [(d, f, r) for d in list(days1) + list(days2)
         for f, r in [("Market", 0.01), ("Size", 0.002)]],
        columns=["Date", "Factor", "Return"])
    specific_returns = pd.DataFrame(
        [(d, p, e) for d in list(days1) + list(days2)
         for p, e in [("AAA", 0.003), ("BBB", -0.001)]],
        columns=["Date", "Position", "SpecificReturn"])
    positions = pd.DataFrame(
        [(d, "Soros", "AAA", 0.6, 60.0, None) for d in (m1, m2)] +
        [(d, "Soros", "BBB", 0.4, 40.0, None) for d in (m1, m2)],
        columns=["Date", "Book", "Position", "Weight", "MV", "ADV"])
    securities = pd.DataFrame({"Position": ["AAA", "BBB"], "Ticker": ["aaa", "bbb"]})
    return {"exposures": exposures, "factor_returns": factor_returns,
            "specific_returns": specific_returns, "positions": positions,
            "securities": securities}


@unit
def t_tieout_realized_equals_factor_plus_specific():
    """§1 three-way tie-out on the toy frames: every day, Realized = Σ factor contribution +
    Specific to machine precision, and the artifact carries every Kind."""
    art = compute_attribution(_toy_frames())
    assert set(art["Kind"]) >= {"contribution", "exposure", "coverage"}
    c = art[art["Kind"] == "contribution"].pivot(index="Date", columns="Source", values="Value")
    fac = c.drop(columns=["Specific", "Realized"]).sum(axis=1)
    gap = (c["Realized"] - fac - c["Specific"]).abs().max()
    assert gap < 1e-15, gap
    # day 1: weights are the filing weights -> x_Market=1, x_Size=0.6*0.5-0.4=−0.1;
    # realized = 1*.01 + (−0.1)*.002 + (0.6*.003 − 0.4*.001) = .0112
    first = c.iloc[0]
    assert abs(first["Realized"] - 0.0112) < 1e-15, first["Realized"]
    assert abs(first["Market"] - 0.01) < 1e-15
    # coverage: both names priced every day
    cov = art[art["Kind"] == "coverage"]["Value"]
    assert np.allclose(cov, 1.0)


@unit
def t_requires_seventh_frame():
    fr = _toy_frames(); fr.pop("specific_returns")
    try:
        compute_attribution(fr)
        assert False, "should have raised"
    except ValueError as e:
        assert "specific_returns" in str(e)


# --------------------------------------------------------------------------- INTEG
@integ
def t_attribution_linked_sums_to_geometric():
    """Carino linking is exact end-to-end: Σ linked contributions == realized geometric return."""
    import requests
    a = requests.get(f"{API}/pnl_attribution", timeout=60).json()
    assert abs(sum(a["linked"].values()) - a["headline"]["realized_geometric"]) < 1e-9
    h = a["headline"]
    assert abs(h["factor"] + h["specific"] - h["realized_geometric"]) < 1e-9
    assert a["n_days"] > 200 and len(a["series"]) == a["n_days"]
    assert a["factors"] and all("t_stat" in r for r in a["factors"])


@integ
def t_attribution_series_identity():
    """Every point of the hero series: market + style + specific == realized (arithmetic cum)."""
    import requests
    a = requests.get(f"{API}/pnl_attribution", timeout=60).json()
    for p in a["series"][:: max(1, len(a["series"]) // 20)]:
        assert abs(p["market"] + p["style"] + p["specific"] - p["realized"]) < 1e-9, p


@integ
def t_residual_diagnostics_shape():
    import requests
    r = requests.get(f"{API}/pnl_attribution/residual", timeout=60).json()
    assert r["status"] in ("green", "amber", "red")
    names = {c["name"] for c in r["checks"]}
    assert any("Information ratio" in n for n in names)
    assert any("autocorrelation" in n for n in names)
    assert all(c["status"] in ("green", "amber", "red") for c in r["checks"])
    assert r["factor_regression"]["r2"] is not None      # daily regression always has enough obs
    assert r["concentration"]["n"] > 0


@integ
def t_linkage_bands_behave():
    import requests
    lk = requests.get(f"{API}/pnl_attribution/linkage", timeout=60).json()
    rows = lk["rows"] + [lk["book_total"]]
    for r in rows:
        assert r["sd_stressed"] >= r["sd_base"] - 1e-15, r
        assert r["verdict"] in ("within", "stress", "investigate"), r
        if r["z"] is not None and abs(r["z"]) <= 2:      # inside base band -> never "investigate"
            assert r["verdict"] == "within", r
    # book stressed band must widen MORE than vol_mult alone (the correlation blend)
    b = lk["book_total"]
    assert b["sd_stressed"] / b["sd_base"] > lk["stress"]["vol_mult"] - 0.05
    zs = [abs(p["z"]) for p in lk["positions"]]
    assert zs == sorted(zs, reverse=True)


@integ
def t_cube_measures_foot():
    """Σ Factor contribution over Factor rows == the grand total (additive), and at book level
    Realized PnL == Factor contribution + Specific PnL."""
    import requests
    q = {"rows": "Factor", "measures": "Factor contribution", "totals": "true",
         "filters": json.dumps({"Book": ["Soros"], "Date": ["2024-11-30"]})}
    j = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=60).json()
    s = sum(r["Factor contribution"] for r in j["records"] if r.get("Factor contribution") is not None)
    assert abs(s - j["grand"]["Factor contribution"]) < 1e-9
    q2 = {"rows": "Book", "measures": "Factor contribution,Specific PnL,Realized PnL",
          "filters": json.dumps({"Book": ["Soros"], "Date": ["2024-11-30"]})}
    j2 = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q2)}", timeout=60).json()
    r = j2["records"][0]
    assert abs(r["Realized PnL"] - r["Factor contribution"] - r["Specific PnL"]) < 1e-12, r


@integ
def t_stress_correlation_mode():
    import requests
    s = requests.post(f"{API}/stress", json={"shocks": {"Momentum": -1},
                                             "vol_mult": 1.25, "rho": 0.75}, timeout=60).json()
    cs = s["correlation_stress"]
    assert cs["stressed_vol_1d"] > cs["base_vol_1d"] > 0
    assert abs(cs["base_var99_normal"] - 2.326 * cs["base_vol_1d"]) < 1e-12


def main():
    p = f = 0

    def _run(fns):
        nonlocal p, f
        for fn in fns:
            try:
                fn(); print(f"PASS  {fn.__name__}"); p += 1
            except Exception as e:
                import traceback
                print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
                traceback.print_exc(); f += 1
    print("=== unit (no backend) ===")
    _run(UNIT)
    print("=== integration (live backend) ===")
    if _backend_up():
        _run(INTEG)
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
