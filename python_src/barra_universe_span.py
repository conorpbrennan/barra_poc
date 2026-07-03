"""
barra_universe_span.py
======================
Phase 3 of the universe diagnostics (see docs/universe-diagnostics-plan.md): the SPAN / high-confidence
check — Chris's VALUE/SIZE picture, generalized. Does each Soros holding sit INSIDE the factor-space
spanned by the estimation universe, or out beyond it (extrapolation, lower confidence)?

For each month:
  * estimation cloud = the funnel survivors (Phase 2) at that month — the names the model is actually
    fit on — read from data/universe_funnel.parquet (falls back to the point-in-time S&P 500 ∩ the
    exposures frame if the funnel artifact is absent).
  * each held name gets a squared **Mahalanobis distance** D² from the cloud's centre, measured in the
    cloud's own covariance. "Inside the space" = D² within the cloud's own 99th-percentile edge; beyond
    that the book is in a region the estimation universe didn't populate, so the model extrapolates.
  * per-factor, we also flag which descriptors push a name out (loading beyond the cloud's 1–99th range)
    — so "high confidence vs extrapolation" is explainable, not just a number.

Aggregated BY 13F WEIGHT: what fraction of the book sits inside the span each month. The prototype run
showed ~90% inside on average, ~95% pre-2021 falling to ~85% since — a real drift of the book out of
the S&P 500's span, toward smaller / higher-vol names. That drift is the Phase-4 question Chris raised.

Pure geometry (Mahalanobis, edge, extremes, weight-share) is split out for unit tests. Like the other
phases this is a precompute step; it writes data/universe_span.parquet and /span only reads it. The
2D factor-pair scatter (the literal version of Chris's picture) is built by the endpoint at request
time from the live exposures frame, so any factor pair can be picked without rebuilding.

CLI:  cd python_src && ../barra/bin/python barra_universe_span.py
"""
from __future__ import annotations
import pathlib

import numpy as np
import pandas as pd

import barra_universe_membership as um

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
ARTIFACT = OUT / "universe_span.parquet"
FUNNEL = OUT / "universe_funnel.parquet"

STYLE = ["Beta", "Momentum", "Size", "Value", "MegaCap",
         "Leverage", "Liquidity", "ResidVol", "EarnYield", "NonLinSize"]
EDGE_Q = 0.99            # the cloud's own 99th-pct D² is the edge of "the space"
MIN_CLOUD = 30           # need a real cross-section to estimate the covariance


# --------------------------------------------------------------------------- pure geometry (unit-tested)
def cloud_stats(E: np.ndarray):
    """Centre, inverse-covariance (pseudo-inverse for stability), and 1st/99th-pct box of a cloud
    of loading vectors E (n × k)."""
    mu = E.mean(axis=0)
    Cinv = np.linalg.pinv(np.cov(E, rowvar=False))
    lo = np.quantile(E, 0.01, axis=0)
    hi = np.quantile(E, 0.99, axis=0)
    return mu, Cinv, lo, hi


def mahalanobis2(X: np.ndarray, mu: np.ndarray, Cinv: np.ndarray) -> np.ndarray:
    """Squared Mahalanobis distance of each row of X from mu under Cinv."""
    Xc = X - mu
    return np.einsum("ij,jk,ik->i", Xc, Cinv, Xc)


def extreme_factors(vec: np.ndarray, lo: np.ndarray, hi: np.ndarray, factors: list[str]) -> list[str]:
    """Factors whose loading is outside the cloud's 1–99th-pct range (what pushes a name out)."""
    return [f for f, v, a, b in zip(factors, vec, lo, hi)
            if not np.isnan(v) and (v < a or v > b)]


def inside_share(weights: np.ndarray, inside: np.ndarray) -> float:
    """Weight-fraction of the book that sits inside the span (rebased on covered weight)."""
    tot = float(weights.sum())
    return float(weights[inside].sum() / tot) if tot > 0 else float("nan")


# --------------------------------------------------------------------------- build
def _cloud_positions_by_month(months) -> dict:
    """{month -> set of estimation-cloud positions}. Funnel survivors if available, else None
    (caller falls back to PIT S&P 500)."""
    if not FUNNEL.exists():
        return {}
    fn = pd.read_parquet(FUNNEL)
    fn["month"] = pd.to_datetime(fn["month"])
    surv = fn[fn["survived"] == True]                                    # noqa: E712 (parquet bool)
    return {pd.Timestamp(m): set(g["position"].dropna())
            for m, g in surv.groupby("month")}


def run(write: bool = True) -> dict:
    print("[span] loading frames ...", flush=True)
    exp = pd.read_parquet(OUT / "exposures.parquet")
    pos = pd.read_parquet(OUT / "positions.parquet")
    sec = pd.read_parquet(OUT / "securities.parquet")
    exp = exp[exp["Factor"].isin(STYLE)]
    months = pd.DatetimeIndex(sorted(pd.to_datetime(exp["Date"].unique())))

    cloud_by_month = _cloud_positions_by_month(months)
    if cloud_by_month:
        print(f"[span] estimation cloud = funnel survivors ({FUNNEL.name})", flush=True)
    else:
        print("[span] funnel artifact absent — cloud = PIT S&P 500 ∩ exposures", flush=True)
        hist = um.load_sp500_history()
        tick = {p: um.canon(t) for p, t in zip(sec["Position"], sec["Ticker"])}

    issuer = dict(zip(sec["Position"], sec["Issuer"]))
    ticker = dict(zip(sec["Position"], sec["Ticker"]))
    rows = []
    for D in months:
        wide = (exp[exp["Date"] == D].pivot_table(index="Position", columns="Factor",
                                                  values="Loading").reindex(columns=STYLE))
        if cloud_by_month:
            cloud_pos = cloud_by_month.get(D, set()) & set(wide.index)
        else:
            members = next((mm for d, mm in reversed(hist) if d <= D), frozenset())
            cloud_pos = {p for p in wide.index if tick.get(p, "") in members}
        if len(cloud_pos) < MIN_CLOUD:
            continue
        E = wide.loc[sorted(cloud_pos)].fillna(0.0).values
        mu, Cinv, lo, hi = cloud_stats(E)
        edge = float(np.quantile(mahalanobis2(E, mu, Cinv), EDGE_Q))

        book = pos[pos["Date"] == D][["Position", "Weight"]]
        held = book[book["Position"].isin(wide.index)]
        if held.empty:
            continue
        H = wide.loc[held["Position"]].fillna(0.0).values
        d2 = mahalanobis2(H, mu, Cinv)
        for (P, w), dd, vec in zip(held.itertuples(index=False), d2, wide.loc[held["Position"]].values):
            ex = extreme_factors(vec, lo, hi, STYLE)
            rows.append({"month": D, "position": P, "ticker": ticker.get(P, ""),
                         "issuer": issuer.get(P, ""), "weight": float(w), "d2": float(dd),
                         "edge": edge, "inside": bool(dd <= edge),
                         "n_extreme": len(ex), "extreme": ", ".join(ex)})

    detail = pd.DataFrame(rows).sort_values(["month", "d2"], ascending=[True, False])
    if write:
        OUT.mkdir(parents=True, exist_ok=True)
        detail.to_parquet(ARTIFACT, index=False)
        print(f"[span] wrote {ARTIFACT}  ({len(detail)} name-month rows)", flush=True)

    # yearly inside-share summary
    detail["yr"] = detail["month"].dt.year
    yr = detail.groupby("yr").apply(
        lambda g: inside_share(g["weight"].values, g["inside"].values), include_groups=False)
    print("\n[span] book weight INSIDE the estimation-universe span, by year:")
    for y, v in yr.items():
        print(f"    {y}  {v:.1%}")
    last = detail["month"].max()
    lg = detail[detail["month"] == last]
    print(f"\n[span] latest {last.date()}: {inside_share(lg['weight'].values, lg['inside'].values):.1%} "
          f"of book inside ({int(lg['inside'].sum())}/{len(lg)} names)")
    return {"detail": detail, "latest": str(last.date())}


if __name__ == "__main__":
    run()
