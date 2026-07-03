"""
barra_universe_drift.py
=======================
Phase 4 of the universe diagnostics (see docs/universe-diagnostics-plan.md): STYLE-DRIFT ATTRIBUTION.
The span check (Phase 3) showed the book drifting out of the S&P 500's factor space since ~2021. Chris
(2026-06-23) framed the question: was that shift **intentional** (a deliberate style tilt / a new PM
covering smaller names) or **unintentional** (a re-pricing of risk that made those names more
attractive)? The action differs — **update the benchmark** if intentional, **update the hedging** if
not — and the desk needs the evidence to decide.

This makes the question empirical. For the book's net factor exposure x_k(t) = Σ_i w_i(t)·L_ik(t):

  * the per-factor **trend** over time shows WHICH factors drifted (e.g. Size falling = book moving
    smaller, ResidVol rising = more volatile names);
  * a between-period **attribution** decomposes each factor's drift Δx_k into four sources —
      entered      new names rotated INTO the book              ─┐ a deliberate rotation:
      exited       names rotated OUT                            ─┘ leans INTENTIONAL → benchmark
      reweighted   held names resized (weight change)           ── an active sizing decision
      loading_drift held names whose own loadings drifted        ── re-pricing / characteristic drift:
                                                                    leans UNINTENTIONAL → hedge

So if a factor's drift is dominated by `entered`, the book rotated into new names with that tilt
(intentional); if it's dominated by `loading_drift`, the names already held drifted there on their own
(unintentional). The split (uncapped coverage loadings) matters here — the book's true off-index tilts
now show, where the old ±3 clip masked them.

We present the evidence and point to the action; the final intentional/not VERDICT needs desk knowledge
(Soros's intent, PM changes) we don't have. Pure attribution math is unit-tested. Writes
data/universe_drift.parquet (the net-exposure series); the /drift endpoint reads it and computes the
attribution for a chosen split date live from the exposures frame.

CLI:  cd python_src && ../barra/bin/python barra_universe_drift.py
"""
from __future__ import annotations
import pathlib

import numpy as np
import pandas as pd

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
ARTIFACT = OUT / "universe_drift.parquet"

STYLE = ["Beta", "Momentum", "Size", "Value", "MegaCap",
         "Leverage", "Liquidity", "ResidVol", "EarnYield", "NonLinSize"]
SOURCES = ["entered", "exited", "reweighted", "loading_drift"]


# --------------------------------------------------------------------------- pure attribution (unit-tested)
def book_exposure(weights: dict, loadings: dict, factors=STYLE) -> dict:
    """Net book exposure per factor: x_k = Σ_i w_i · L_ik."""
    return {f: float(sum(weights[p] * loadings.get(p, {}).get(f, 0.0) for p in weights))
            for f in factors}


def decompose(w0: dict, l0: dict, w1: dict, l1: dict, factors=STYLE) -> dict:
    """Attribute Δx_k = x_k(t1) − x_k(t0) into entered / exited / reweighted / loading_drift.
    Retained names split as Δ(wL) = (w1−w0)·L0 (reweight) + w1·(L1−L0) (loading drift); the four
    sources sum to the total Δ exactly."""
    entered, exited, retained = set(w1) - set(w0), set(w0) - set(w1), set(w0) & set(w1)
    out = {}
    for f in factors:
        ent = sum(w1[p] * l1.get(p, {}).get(f, 0.0) for p in entered)
        exi = -sum(w0[p] * l0.get(p, {}).get(f, 0.0) for p in exited)
        rw = sum((w1[p] - w0[p]) * l0.get(p, {}).get(f, 0.0) for p in retained)
        ld = sum(w1[p] * (l1.get(p, {}).get(f, 0.0) - l0.get(p, {}).get(f, 0.0)) for p in retained)
        out[f] = {"entered": ent, "exited": exi, "reweighted": rw, "loading_drift": ld,
                  "delta": ent + exi + rw + ld}
    return out


def drift_summary(series: pd.DataFrame, split: pd.Timestamp) -> pd.DataFrame:
    """Per-factor early (< split) vs late (>= split) mean net exposure + the delta, ranked by |delta|.
    `series` is wide: index=month, columns=factors."""
    early = series[series.index < split].mean()
    late = series[series.index >= split].mean()
    d = pd.DataFrame({"early": early, "late": late, "delta": late - early})
    return d.reindex(d["delta"].abs().sort_values(ascending=False).index)


# --------------------------------------------------------------------------- build
def _wide_loadings(exp: pd.DataFrame, D: pd.Timestamp) -> pd.DataFrame:
    return (exp[(exp["Date"] == D) & (exp["Factor"].isin(STYLE))]
            .pivot_table(index="Position", columns="Factor", values="Loading").reindex(columns=STYLE))


def book_at(exp: pd.DataFrame, pos: pd.DataFrame, D: pd.Timestamp):
    """(weights dict, loadings dict) for the book held at month-end D."""
    bk = pos[pos["Date"] == D][["Position", "Weight"]]
    w = dict(zip(bk["Position"], bk["Weight"]))
    L = _wide_loadings(exp, D)
    L = L[L.index.isin(w)]
    loadings = {p: {f: (0.0 if pd.isna(v) else float(v)) for f, v in row.items()}
                for p, row in L.iterrows()}
    return w, loadings


def run(write: bool = True) -> dict:
    print("[drift] loading frames ...", flush=True)
    exp = pd.read_parquet(OUT / "exposures.parquet")
    pos = pd.read_parquet(OUT / "positions.parquet")
    months = pd.DatetimeIndex(sorted(pd.to_datetime(pos["Date"].unique())))

    rows = []
    for D in months:
        w, L = book_at(exp, pos, D)
        if not w:
            continue
        x = book_exposure(w, L)
        for f, v in x.items():
            rows.append({"month": D, "factor": f, "net_exposure": v})
    detail = pd.DataFrame(rows)
    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        detail.to_parquet(ARTIFACT, index=False)
        print(f"[drift] wrote {ARTIFACT}  ({len(detail)} rows)", flush=True)

    series = detail.pivot_table(index="month", columns="factor", values="net_exposure")
    summ = drift_summary(series, pd.Timestamp("2021-01-01"))
    print("\n[drift] book net-exposure drift, pre-2021 vs 2021+ (top movers):")
    for f, r in summ.head(5).iterrows():
        print(f"    {f:11s} {r['early']:+.3f} -> {r['late']:+.3f}   (Δ {r['delta']:+.3f})")
    # attribution: 2020-12-31 vs latest
    t0 = months[months < pd.Timestamp("2021-01-01")][-1]
    t1 = months[-1]
    w0, l0 = book_at(exp, pos, t0); w1, l1 = book_at(exp, pos, t1)
    attr = decompose(w0, l0, w1, l1)
    print(f"\n[drift] attribution {t0.date()} -> {t1.date()} (top movers):")
    for f, _ in summ.head(4).iterrows():
        a = attr[f]
        print(f"    {f:11s} Δ{a['delta']:+.3f} = enter {a['entered']:+.3f}  exit {a['exited']:+.3f}  "
              f"reweight {a['reweighted']:+.3f}  loading-drift {a['loading_drift']:+.3f}")
    return {"detail": detail, "summary": summ, "attr": attr}


if __name__ == "__main__":
    run()
