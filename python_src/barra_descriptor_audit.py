"""
barra_descriptor_audit.py — the three descriptor-health tests that caught the Liquidity/Size
problem, generalized to every factor. Run from python_src/ after a build:

    ../barra/bin/python barra_descriptor_audit.py [--window-start YYYY-MM-DD]

Tests, per factor:
  1. COLLINEARITY — factor-return correlations (full sample + trailing window). Two factors
     whose returns run |rho| >~ 0.6 are one effect split across two unstable regression
     coefficients; the residual regression's factor LABELS stop being trustworthy.
  2. HIDDEN BETA — regress each held name's daily specific return on the factor's return over
     the window (univariate). The modeled loading's contribution is already removed from the
     residual, so beta ~ 0 if loadings are right. Book-level Sum(w*beta) is the unmodeled
     factor exposure the risk block can't see; the share of names with |t|>2 says how broad
     it is. Read alongside test 1: a hidden beta on one of a collinear pair may belong to
     either factor.
  3. COVERAGE — held-book weight carrying a loading at the latest date; the missing names.

Pure helpers are importable for tests (test_descriptor_audit.py): _residual_betas,
_collinear_pairs, _coverage.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent.parent / "data"


# --------------------------------------------------------------------------- pure
def _collinear_pairs(wide: pd.DataFrame, thresh: float = 0.6) -> list[dict]:
    """Factor pairs whose return correlation exceeds |thresh|, sorted by |rho| desc."""
    c = wide.corr()
    out = []
    cols = list(c.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            rho = float(c.loc[a, b])
            if abs(rho) >= thresh:
                out.append({"a": a, "b": b, "rho": rho})
    return sorted(out, key=lambda r: -abs(r["rho"]))


def _residual_betas(resid_panel: pd.DataFrame, f: pd.Series,
                    weights: pd.Series, min_obs: int = 120) -> dict:
    """Univariate OLS of each name's residual on one factor's daily return.

    resid_panel: Date x Position specific returns; f: the factor's daily return series;
    weights: latest-book weights (only weighted names are tested).
    Returns per-name betas/t-stats plus the book-level aggregates."""
    rows = []
    x_all = f.dropna()
    for p in resid_panel.columns:
        w = float(weights.get(p, 0.0))
        if w == 0.0:
            continue
        u = resid_panel[p].dropna()
        ix = u.index.intersection(x_all.index)
        if len(ix) < min_obs:
            continue
        u, x = u.loc[ix], x_all.loc[ix]
        vx = float(np.var(x, ddof=1))
        if vx <= 0:
            continue
        b = float(np.cov(u, x, ddof=1)[0, 1] / vx)
        se = float(np.sqrt(np.var(u - b * x, ddof=1) / (len(ix) * vx)))
        rows.append({"position": p, "weight": w, "beta": b,
                     "t": b / se if se > 0 else np.nan, "n": len(ix)})
    if not rows:
        return {"names": [], "book_beta": None, "wavg_beta": None,
                "share_sig": None, "n_names": 0, "weight_tested": 0.0}
    df = pd.DataFrame(rows)
    wsum = float(df["weight"].sum())
    return {
        "names": df.sort_values("weight", ascending=False).to_dict("records"),
        "book_beta": float((df["weight"] * df["beta"]).sum()),   # Sum(w*b): unmodeled exposure
        "wavg_beta": float(np.average(df["beta"], weights=df["weight"])),
        "share_sig": float((df["t"].abs() > 2).mean()),
        "n_names": int(len(df)), "weight_tested": wsum,
    }


def _coverage(exp_d: pd.DataFrame, held: pd.Series, factor: str) -> dict:
    """Held-book coverage of one factor's loading on one date."""
    have = set(exp_d[exp_d["Factor"] == factor]["Position"])
    miss = held[~held.index.isin(have)]
    return {"weight_covered": float(held[held.index.isin(have)].sum()),
            "weight_missing": float(miss.sum()), "n_missing": int(len(miss)),
            "missing": miss.sort_values(ascending=False)}


# --------------------------------------------------------------------------- report
def run(window_start: str = "2025-06-30", corr_thresh: float = 0.6) -> None:
    fr = pd.read_parquet(OUT / "factor_returns.parquet")
    sr = pd.read_parquet(OUT / "specific_returns.parquet")
    exp = pd.read_parquet(OUT / "exposures.parquet")
    pos = pd.read_parquet(OUT / "positions.parquet")
    sec = pd.read_parquet(OUT / "securities.parquet").set_index("Position")
    tk = sec["Ticker"].to_dict()

    d = pos["Date"].max()
    held = pos[pos["Date"] == d].groupby("Position")["Weight"].sum()
    wide = fr.pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
    w12 = wide.loc[wide.index >= window_start]
    factors = [c for c in wide.columns if c != "Market"]

    print(f"descriptor audit — held book {d.date()}, window {window_start} → {wide.index.max().date()}")

    print("\n== 1. collinearity (factor-return correlations) ==")
    full = _collinear_pairs(wide, corr_thresh)
    recent = _collinear_pairs(w12, corr_thresh)
    if not full and not recent:
        print(f"   no pair beyond |rho| >= {corr_thresh} (full sample or window) — labels separable")
    for tag, pairs, ref in (("full", full, wide), ("window", recent, w12)):
        for pr in pairs:
            print(f"   [{tag:6}] {pr['a']:<10} ~ {pr['b']:<10} rho {pr['rho']:+.2f}")

    print("\n== 2. hidden beta (residual-vs-factor, daily, univariate) ==")
    srw = sr[sr["Date"] >= window_start]
    panel = srw.pivot_table(index="Date", columns="Position", values="SpecificReturn")
    print(f"   {'factor':<10} {'Sum(w*b)':>9} {'wavg b':>8} {'|t|>2':>6}  read")
    summary = []
    for f_ in factors:
        r = _residual_betas(panel, w12[f_], held)
        if r["book_beta"] is None:
            continue
        flag = ("HIDDEN BETA" if abs(r["book_beta"]) >= 0.10 and r["share_sig"] >= 0.4 else
                "watch" if abs(r["book_beta"]) >= 0.05 else "ok")
        summary.append((f_, r, flag))
        print(f"   {f_:<10} {r['book_beta']:>+9.3f} {r['wavg_beta']:>+8.2f} "
              f"{r['share_sig']:>6.0%}  {flag}")
    for f_, r, flag in summary:
        if flag != "HIDDEN BETA":
            continue
        worst = sorted((n for n in r["names"] if abs(n.get("t", 0)) > 2),
                       key=lambda n: -n["weight"])[:5]
        who = ", ".join(f"{tk.get(n['position'], n['position'])} b={n['beta']:+.2f}" for n in worst)
        print(f"   -> {f_}: largest significant carriers: {who}")

    print("\n== 3. loading coverage (held book, latest date) ==")
    exp_d = exp[exp["Date"] == d]
    print(f"   {'factor':<10} {'covered':>8} {'missing':>8}  names")
    for f_ in wide.columns:
        c = _coverage(exp_d, held, f_)
        names = ", ".join(tk.get(p, p) for p in c["missing"].index[:6])
        more = f" +{c['n_missing'] - 6}" if c["n_missing"] > 6 else ""
        print(f"   {f_:<10} {c['weight_covered']:>8.1%} {c['weight_missing']:>8.2%}  "
              f"{names}{more}" if c["n_missing"] else
              f"   {f_:<10} {c['weight_covered']:>8.1%} {'—':>8}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-start", default="2025-06-30")
    ap.add_argument("--corr-thresh", type=float, default=0.6)
    a = ap.parse_args()
    run(a.window_start, a.corr_thresh)
