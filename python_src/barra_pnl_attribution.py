"""
barra_pnl_attribution.py
========================
Step 15 precompute: realized PnL attribution (factor + residual) for the 13F book, plus the
pure statistics the residual diagnostics and the risk↔PnL linkage need.

Realized engine (docs/pnl-attribution-plan.md §1):
  * Each name's daily return is RECONSTRUCTED from the model's own frames:
        R_i(t) = Σ_k L_ik(d0)·f_k(t) + ε_i(t)
    where d0 is the latest exposure month-end strictly before t (the regression's own timing),
    f is the daily factor-return frame and ε the daily specific_returns frame (the 7th frame).
    ε is the WLS residual, so this is EXACT — the same price-only return the regression saw
    (±50% masked days drop out on both sides), and realized = factor + specific is an identity
    at machine precision, per day. That identity is the tie-out the tests assert.
  * Drifting (buy-and-hold) weights: anchored at each 13F filing change, each name compounding
    by its own unit NAV until the next filing re-anchors — what actually happened to the book
    between filings, not the constant-portfolio assumption.
  * Coverage is disclosed, never silent: a held name with no reconstructed return in a month
    keeps its weight (flat NAV) but is excluded from realized PnL; the priced share of book
    weight and the unpriced names are written to the artifact.

Writes data/pnl_attribution.parquet — one tidy frame (Date, Kind, Source, Value):
  Kind=contribution  Source ∈ {Market, <style factors>, Specific, Realized}
                     daily arithmetic contribution to book return (Realized = the book return)
  Kind=exposure      Source = factor, Value = x_k(t) on the drifting weights (masked to the
                     names priced that day, so c_k = x_k·f_k holds row by row)
  Kind=coverage      Source = "priced_share", share of start-of-day weight priced that day
  Kind=unpriced      Source = ticker (or FIGI), Value = weight — per month-start d0, the names
                     with no usable series that month

Run from python_src/ after a v2 build (needs the specific_returns frame):
    ../barra/bin/python barra_pnl_attribution.py
"""
from __future__ import annotations
import pathlib

import numpy as np
import pandas as pd

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
ARTIFACT = OUT / "pnl_attribution.parquet"


# --------------------------------------------------------------------------- pure statistics
def _carino_link(contrib: pd.DataFrame, realized: pd.Series) -> tuple[dict, float]:
    """Carino-link daily arithmetic contributions to the geometric period return.

    contrib: (day x source) daily contributions with Σ_source = realized per day.
    Returns ({source: linked contribution}, geometric total). Linked contributions sum to the
    compounded total EXACTLY (no plug): scale day t by k_t = ln(1+R_t)/R_t, divide by
    K = ln(1+R_G)/R_G.
    """
    r = np.nan_to_num(realized.to_numpy(dtype=float))
    rg = float(np.prod(1.0 + r) - 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        kt = np.where(np.abs(r) > 1e-12, np.log1p(r) / r, 1.0)
    K = (np.log1p(rg) / rg) if abs(rg) > 1e-12 else 1.0
    linked = {c: float(np.nansum(contrib[c].to_numpy(dtype=float) * kt) / K)
              for c in contrib.columns}
    return linked, rg


def _info_ratio(u: pd.Series, periods_per_year: int = 12) -> float | None:
    """Annualized IR of the residual series: mean/std * sqrt(periods). None if degenerate."""
    u = u.dropna()
    if len(u) < 3 or float(u.std(ddof=1)) == 0.0:
        return None
    return float(u.mean() / u.std(ddof=1) * np.sqrt(periods_per_year))


def _autocorr(u: pd.Series, lag: int) -> float | None:
    """Lag-k autocorrelation of the residual series."""
    u = u.dropna()
    if len(u) <= lag + 2:
        return None
    a, b = u.iloc[lag:].to_numpy(), u.iloc[:-lag].to_numpy()
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def _bias_stat(realized: pd.Series, predicted_vol: pd.Series) -> tuple[float | None, float | None]:
    """Barra bias statistic B = std(realized/predicted_vol) and its significance half-width
    sqrt(2/N). Calibrated forecasts give B ≈ 1 within ±sqrt(2/N)."""
    z = (realized / predicted_vol).replace([np.inf, -np.inf], np.nan).dropna()
    if len(z) < 6:
        return None, None
    return float(z.std(ddof=1)), float(np.sqrt(2.0 / len(z)))


def _concentration_hhi(values: pd.Series) -> dict:
    """Concentration of a signed PnL split across names: HHI of |value| shares + top-5 share."""
    a = values.dropna().abs()
    tot = float(a.sum())
    if tot <= 0:
        return {"hhi": None, "top5_share": None, "n": int(len(a))}
    s = (a / tot).sort_values(ascending=False)
    return {"hhi": float((s ** 2).sum()), "top5_share": float(s.head(5).sum()), "n": int(len(a))}


def _hit_rate(values: pd.Series) -> float | None:
    v = values.dropna()
    return float((v > 0).mean()) if len(v) else None


def _resid_factor_regression(u: pd.Series, fac: pd.DataFrame) -> dict:
    """Regress the book residual u_p(t) on the factor returns (Chris's test #2): R², and the
    per-factor loading + t-stat, largest |t| first. Near-zero R² = clean, orthogonal alpha."""
    df = pd.concat([u.rename("_u"), fac], axis=1).dropna()
    if len(df) < len(fac.columns) + 4:
        return {"r2": None, "loadings": []}
    y = df["_u"].to_numpy()
    X = np.column_stack([np.ones(len(df)), df[fac.columns].to_numpy()])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = len(df) - X.shape[1]
    ss_res, ss_tot = float(resid @ resid), float(((y - y.mean()) ** 2).sum())
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    se = np.sqrt(np.maximum(np.diag(np.linalg.pinv(X.T @ X)) * ss_res / max(dof, 1), 1e-30))
    rows = [{"factor": f, "beta": float(b), "t_stat": float(b / s)}
            for f, b, s in zip(fac.columns, beta[1:], se[1:])]
    rows.sort(key=lambda r: -abs(r["t_stat"]))
    return {"r2": r2, "loadings": rows}


def _stressed_cov(F: np.ndarray, vol_mult: float = 1.25, rho_blend: float = 0.75) -> np.ndarray:
    """Correlation-stress a factor covariance: scale vols by vol_mult and blend the correlation
    matrix toward all-ρ=1 (C' = (1-b)·C + b·J). Chris's "shock the vols and correlations", used
    for the stressed band in the reconcile chart. b=0, m=1 returns F unchanged."""
    v = np.sqrt(np.clip(np.diag(F), 0.0, None))
    with np.errstate(divide="ignore", invalid="ignore"):
        C = F / np.outer(v, v)
    C = np.nan_to_num(C, nan=0.0)
    np.fill_diagonal(C, 1.0)
    Cs = (1.0 - rho_blend) * C + rho_blend * np.ones_like(C)
    vs = v * vol_mult
    return np.outer(vs, vs) * Cs


def _rolling_bias(realized: pd.Series, predicted_vol: pd.Series, window: int = 24) -> list[dict]:
    """Rolling bias statistic: b_t = std of the trailing `window` standardized returns
    z = realized/predicted-vol, with the 95% acceptance band 1 ± √(2/window). b > 1 = risk
    under-forecast (the dangerous direction), b < 1 = over-forecast. The time-series version of
    _bias_stat — where in the calendar the model drifted out of calibration."""
    z = (realized / predicted_vol).replace([np.inf, -np.inf], np.nan).dropna()
    band = float(np.sqrt(2.0 / window))
    out = []
    for i in range(window - 1, len(z)):
        w = z.iloc[i - window + 1: i + 1]
        out.append({"date": z.index[i], "b": float(w.std(ddof=1)), "band": band})
    return out


def _linkage_driver(x_t: float | None, x_win: float | None, realized: float,
                    sig_daily: float, h: float, cum_f: float | None) -> dict | None:
    """Classify what drove a reconcile-band breach. The band freezes the exposure at T
    (sd = |x_T|·σ·√h), so a breach can be a genuine factor move OR an artifact of the exposure
    migrating inside the window (a near-zero x_T makes z ill-conditioned). Returns:
      z_window      — z rebuilt on the average in-window exposure (the honest denominator),
      factor_sigma  — the factor's cumulative window move in σs of the window,
      ratio         — |x_win| / |x_T|, migrated when ≥ 3 (loose, like the residual thresholds),
      kind          — exposure_migration (within band on the in-window exposure — a timing
                      artifact, not a factor event) / factor_move (exposure stable, factor
                      moved ≥ 1.5σ) / mixed.
    None when the row has no exposure/vol to reason about (specific / book rows)."""
    if x_t is None or x_win is None or sig_daily <= 0 or h <= 0:
        return None
    rt = float(np.sqrt(h))
    fs = float(cum_f / (sig_daily * rt)) if cum_f is not None else None
    sd_win = abs(x_win) * sig_daily * rt
    z_win = float(realized / sd_win) if sd_win > 0 else None
    ratio = float(abs(x_win) / max(abs(x_t), 1e-9))
    migrated = ratio >= 3.0
    if migrated and z_win is not None and abs(z_win) <= 2.0:
        kind = "exposure_migration"
    elif not migrated and fs is not None and abs(fs) >= 1.5:
        kind = "factor_move"
    else:
        kind = "mixed"
    return {"kind": kind, "migrated": migrated, "ratio": ratio,
            "z_window": z_win, "factor_sigma": fs}


def _position_driver(realized: float, factor_pnl: float, specific_pnl: float,
                     w_t: float | None, w_win: float | None, sd_base: float) -> dict | None:
    """Classify a per-NAME reconcile-band breach — the stock-level analogue of _linkage_driver.
    The band freezes the weight at T (sd = |w_T|·σ_i·√h) while realized PnL runs on the as-of
    monthly weights, so a breach decomposes into:
      weight_migration — the 13F re-anchor / resize made w(T) unrepresentative: z rebuilt on the
                         in-window average weight sits within ±2 (a band artifact, not an event);
      specific_move    — the name's own residual dominates the PnL (earnings/M&A-type move the
                         factor block can't see; cross-check the specific-vol calibration);
      factor_move      — the name's loadings carried a factor move (systematic, not stock news);
      mixed            — no dominant component.
    Returns None when there is no band to reason about."""
    if sd_base is None or sd_base <= 0 or w_t is None:
        return None
    ratio = float(abs(w_win) / max(abs(w_t), 1e-9)) if w_win is not None else None
    sd_win = sd_base * ratio if ratio is not None else None
    z_win = float(realized / sd_win) if sd_win and sd_win > 0 else None
    migrated = ratio is not None and (ratio >= 3.0 or ratio <= 1.0 / 3.0)
    tot = abs(factor_pnl) + abs(specific_pnl)
    spec_share = float(abs(specific_pnl) / tot) if tot > 0 else None
    if migrated and z_win is not None and abs(z_win) <= 2.0:
        kind = "weight_migration"
    elif spec_share is not None and spec_share >= 0.65:
        kind = "specific_move"
    elif spec_share is not None and spec_share <= 0.35:
        kind = "factor_move"
    else:
        kind = "mixed"
    return {"kind": kind, "migrated": migrated, "ratio": ratio, "z_window": z_win,
            "specific_share": spec_share}


def _pairwise_mean_corr(panel: pd.DataFrame, min_obs: int = 20) -> dict | None:
    """Mean pairwise correlation across a days×names residual panel — the cheap version of the
    missing-factor test (Chris: correlated residuals across names are most likely a missing
    factor; PCA deferred). Returns mean/max pairwise corr and the max pair, or None with < 2
    usable columns. Independent stock events read ≈ 0; a common thread reads well above it."""
    panel = panel.dropna(axis=1, thresh=min_obs)
    if panel.shape[1] < 2:
        return None
    C = panel.corr().to_numpy()
    names = list(panel.columns)
    iu = np.triu_indices(len(names), 1)
    vals = C[iu]
    ok = ~np.isnan(vals)
    if not ok.any():
        return None
    vals, ii, jj = vals[ok], iu[0][ok], iu[1][ok]
    k = int(np.argmax(np.abs(vals)))
    return {"mean_corr": float(np.mean(vals)), "max_corr": float(vals[k]),
            "max_pair": [str(names[ii[k]]), str(names[jj[k]])],
            "n_names": int(len(names)), "n_pairs": int(len(vals)),
            "n_obs": int(len(panel))}


# --------------------------------------------------------------------------- realized engine
def compute_attribution(frames: dict[str, pd.DataFrame], book: str = "Soros") -> pd.DataFrame:
    """The daily tidy artifact (see module docstring) from the seven frames."""
    if "specific_returns" not in frames:
        raise ValueError("specific_returns frame missing — rebuild with the v2 builder first")
    exp, pos = frames["exposures"], frames["positions"]
    fr, sr = frames["factor_returns"], frames["specific_returns"]
    sec = frames["securities"]
    tick = dict(zip(sec["Position"], sec.get("Ticker", sec["Position"])))

    pos = pos[pos["Book"] == book]
    held_ever = sorted(pos["Position"].unique())
    fr_w = fr.pivot(index="Date", columns="Factor", values="Return").sort_index()
    sr_held = sr[sr["Position"].isin(held_ever)]
    eps_w = (sr_held.pivot_table(index="Date", columns="Position", values="SpecificReturn")
             .reindex(columns=held_ever))
    exp_dates = np.sort(exp["Date"].unique())
    pos_dates = np.sort(pos["Date"].unique())

    # weight regimes: a new anchor whenever the (Position -> Weight) map changes (a new filing).
    wmaps, anchors = {}, []
    prev = None
    for d in pos_dates:
        g = pos[pos["Date"] == d].groupby("Position")["Weight"].sum()
        cur = {p: round(float(v), 12) for p, v in g.items()}
        if cur != prev:
            anchors.append(d)
            prev = cur
        wmaps[d] = g

    # reconstructed daily returns for held names, month by month (L at d0 explains (d0, d1]);
    # kept as a (day x name) frame. NaN = the name had no return that day (excluded everywhere).
    exp_held = exp[exp["Position"].isin(held_ever)]
    days_all = fr_w.index
    R = pd.DataFrame(np.nan, index=days_all, columns=held_ever)
    L_by_d0 = {}
    for i, d0 in enumerate(exp_dates):
        d1 = exp_dates[i + 1] if i + 1 < len(exp_dates) else None
        days = days_all[(days_all > d0) & ((days_all <= d1) if d1 is not None
                                           else np.ones(len(days_all), bool))]
        if not len(days):
            continue
        Ld = (exp_held[exp_held["Date"] == d0]
              .pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first"))
        if Ld.empty:
            continue
        L_by_d0[pd.Timestamp(d0)] = Ld
        f = fr_w.loc[days, fr_w.columns.intersection(Ld.columns)].fillna(0.0)
        Lm = Ld[f.columns].fillna(0.0)
        fac_part = pd.DataFrame(f.to_numpy() @ Lm.to_numpy().T, index=days, columns=Lm.index)
        e = eps_w.reindex(index=days, columns=Lm.index)
        R.loc[days, Lm.index] = (fac_part + e).to_numpy()   # NaN eps -> NaN return (no obs)

    nav = (1.0 + R.fillna(0.0)).cumprod()

    rows = []
    for a_i, anchor in enumerate(anchors):
        nxt = anchors[a_i + 1] if a_i + 1 < len(anchors) else None
        days = days_all[(days_all > anchor) & ((days_all <= nxt) if nxt is not None
                                               else np.ones(len(days_all), bool))]
        if not len(days):
            continue
        w0 = wmaps[anchor]
        names = [p for p in w0.index if p in nav.columns]
        w0v = w0.reindex(names).to_numpy()
        base_i = nav.index.searchsorted(anchor, side="right") - 1
        base = nav[names].iloc[base_i].to_numpy() if base_i >= 0 else np.ones(len(names))
        navrel = nav.loc[days, names].to_numpy() / base
        # start-of-day drifting weights: yesterday's compounded value, renormalized
        prev_val = np.vstack([w0v, (w0v * navrel)[:-1]])
        W = prev_val / prev_val.sum(axis=1, keepdims=True)
        Rm = R.loc[days, names]
        M = Rm.notna().to_numpy()
        Rv = np.nan_to_num(Rm.to_numpy())
        eps = np.nan_to_num(eps_w.reindex(index=days, columns=names).to_numpy())
        realized = (W * Rv).sum(axis=1)
        u_p = (W * eps).sum(axis=1)
        # group the regime's days by their owning exposure month d0: one loading matrix per month
        d0_idx = np.searchsorted(exp_dates, days.values, side="left") - 1
        for d0i in np.unique(d0_idx):
            Ld = L_by_d0.get(pd.Timestamp(exp_dates[d0i])) if d0i >= 0 else None
            if Ld is None:
                continue
            sel = np.where(d0_idx == d0i)[0]
            facs = [c for c in fr_w.columns if c in Ld.columns]
            Lm = Ld.reindex(index=names, columns=facs).fillna(0.0).to_numpy()
            X = (W[sel] * M[sel]) @ Lm                  # (day x factor), masked to names priced
            Fm = fr_w.loc[days[sel], facs].fillna(0.0).to_numpy()
            C = X * Fm
            for jj, j in enumerate(sel):
                d = days[j]
                for fi, f_ in enumerate(facs):
                    if Fm[jj, fi] == 0.0 and np.isnan(fr_w.loc[d, f_]):
                        continue                        # factor not fit that day
                    rows.append({"Date": d, "Kind": "contribution", "Source": f_,
                                 "Value": float(C[jj, fi])})
                    rows.append({"Date": d, "Kind": "exposure", "Source": f_,
                                 "Value": float(X[jj, fi])})
                rows.append({"Date": d, "Kind": "contribution", "Source": "Specific",
                             "Value": float(u_p[j])})
                rows.append({"Date": d, "Kind": "contribution", "Source": "Realized",
                             "Value": float(realized[j])})
                rows.append({"Date": d, "Kind": "coverage", "Source": "priced_share",
                             "Value": float((W[j] * M[j]).sum())})
        # unpriced disclosure per anchor month: held names with no return anywhere in the regime
        priced_any = R.loc[days, names].notna().any()
        for p in w0.index:
            if p not in nav.columns or not bool(priced_any.get(p, False)):
                rows.append({"Date": anchor, "Kind": "unpriced", "Source": tick.get(p, p),
                             "Value": float(w0[p])})
    art = pd.DataFrame(rows)
    art["Date"] = pd.to_datetime(art["Date"])
    return art


def run(frames: dict[str, pd.DataFrame] | None = None, book: str = "Soros") -> pd.DataFrame:
    if frames is None:
        from barra_factor_risk_cube import load_frames
        frames = load_frames()
    art = compute_attribution(frames, book=book)
    return art


if __name__ == "__main__":
    art = run()
    art.to_parquet(ARTIFACT, index=False)
    c = art[art["Kind"] == "contribution"].pivot(index="Date", columns="Source", values="Value")
    fac = c.drop(columns=["Specific", "Realized"]).sum(axis=1)
    gap = (c["Realized"] - fac - c["Specific"]).abs().max()
    linked, rg = _carino_link(c.drop(columns=["Realized"]), c["Realized"])
    print(f"wrote {ARTIFACT}  ({len(art):,} rows, {c.index.min().date()} → {c.index.max().date()})")
    print(f"tie-out |realized - factor - specific| max = {gap:.2e} (identity, must be ~0)")
    print(f"since-inception geometric return {rg:+.1%}; linked: "
          f"Market {linked.get('Market', 0):+.1%}, Specific {linked.get('Specific', 0):+.1%}, "
          f"styles {sum(v for k, v in linked.items() if k not in ('Market', 'Specific')):+.1%}")
