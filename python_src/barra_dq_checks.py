"""
barra_dq_checks.py
==================
Basic data-quality checks on the six parquet frames (the builder<->cube contract).
Read-only; prints a PASS/WARN/FAIL report. Run after any frame rebuild:

    ../barra/bin/python barra_dq_checks.py
"""
from __future__ import annotations
import pathlib
import numpy as np
import pandas as pd

OUT = pathlib.Path(__file__).resolve().parent.parent / "data"
KEYS = {
    "exposures":      ["Date", "Position", "Factor"],
    "positions":      ["Date", "Book", "Position"],
    "securities":     ["Position"],
    "factor_meta":    ["Factor"],
    "factor_returns": ["Date", "Factor"],
    "specific_var":   ["Date", "Position"],
}

def run(frames: dict | None = None) -> list[dict]:
    """Run every check and RETURN the results as a list of {level, name, detail} dicts (no printing
    — the CLI block below prints). Pass `frames` (the cube's in-memory six) to check exactly what is
    served; omit to read the parquet frames from disk. Reusable by risk_api's /dq endpoint."""
    f = frames if frames is not None else {n: pd.read_parquet(OUT / f"{n}.parquet") for n in KEYS}
    _results: list[dict] = []

    def check(level: str, name: str, detail: str = "") -> None:
        _results.append({"level": level, "name": name, "detail": detail})

    # --- 1. key integrity: uniqueness + no nulls in key columns -------------
    for name, keys in KEYS.items():
        df = f[name]
        dup = df.duplicated(subset=keys).sum()
        check("FAIL" if dup else "PASS", f"{name}: key {keys} unique",
              f"{dup} duplicate rows" if dup else f"{len(df):,} rows")
        nullkey = df[keys].isna().any(axis=1).sum()
        check("FAIL" if nullkey else "PASS", f"{name}: no null keys",
              f"{nullkey} rows with null key" if nullkey else "")

    # --- 2. referential integrity (everything joins on Position / Factor) ---
    sec_pos = set(f["securities"]["Position"])
    fm_fac  = set(f["factor_meta"]["Factor"])
    for name, col, universe, label in [
        ("exposures", "Position", sec_pos, "securities"),
        ("positions", "Position", sec_pos, "securities"),
        ("specific_var", "Position", sec_pos, "securities"),
        ("exposures", "Factor", fm_fac, "factor_meta"),
        ("factor_returns", "Factor", fm_fac, "factor_meta"),
    ]:
        orphans = set(f[name][col]) - universe
        check("FAIL" if orphans else "PASS", f"{name}.{col} ⊆ {label}",
              f"{len(orphans)} orphans e.g. {sorted(orphans)[:3]}" if orphans else "")

    # --- 3. value ranges -----------------------------------------------------
    pos = f["positions"]
    wsum = pos.groupby("Date")["Weight"].sum()
    bad = wsum[(wsum - 1.0).abs() > 1e-6]
    check("FAIL" if len(bad) else "PASS", "positions: weights sum to 1.0 per date",
          f"{len(bad)} bad dates" if len(bad) else f"{len(wsum)} dates, max |err| {(wsum-1).abs().max():.1e}")
    neg = (pos["Weight"] < 0).sum()
    check("WARN" if neg else "PASS", "positions: no negative weights (13F is long-only)",
          f"{neg} rows" if neg else "")

    sv = f["specific_var"]
    nsv = (sv["SpecificVar"] < 0).sum()
    check("FAIL" if nsv else "PASS", "specific_var: non-negative", f"{nsv} rows" if nsv else "")

    exp = f["exposures"]
    nan_load = exp["Loading"].isna().sum()
    check("FAIL" if nan_load else "PASS", "exposures: no null loadings", f"{nan_load}" if nan_load else "")
    # Loading-magnitude guard. With the estimation/coverage split (UNCAP_COVERAGE), coverage loadings
    # are INTENTIONALLY uncapped up to ±COVERAGE_CAP (estimation stays winsorised at ±3), so the old
    # flat ±6 bound false-warns by design. Flag only loadings beyond the cap (+ a re-standardisation
    # margin) — that range catches genuine corruption (the inf / 1e4 blowups) but not the designed tilt.
    try:
        from barra_build_frames import UNCAP_COVERAGE, COVERAGE_CAP
    except Exception:
        UNCAP_COVERAGE, COVERAGE_CAP = False, 6.0
    lim = COVERAGE_CAP * 1.5 if UNCAP_COVERAGE else 6.0
    label = (f"exposures: |loading| within coverage cap (±{COVERAGE_CAP:g}; estimation winsorised ±3)"
             if UNCAP_COVERAGE else "exposures: |z| <= 6 (winsorised z-scores)")
    big = exp[exp["Loading"].abs() > lim]
    check("WARN" if len(big) else "PASS", label,
          f"{len(big)} rows beyond ±{lim:g}, max |loading| {exp['Loading'].abs().max():.1f}"
          if len(big) else f"max |loading| {exp['Loading'].abs().max():.1f}")

    fr = f["factor_returns"]
    wild = fr[fr["Return"].abs() > 0.5]
    check("WARN" if len(wild) else "PASS", "factor_returns: |monthly return| <= 50%",
          f"{len(wild)}/{len(fr)} rows, max |r| {fr['Return'].abs().max():.3g} "
          f"(worst: {', '.join(wild.nlargest(3, 'Return', keep='all')['Factor'].unique()[:3])})" if len(wild) else "")

    # --- 4. calendar continuity ------------------------------------------------
    for name in ("exposures", "positions"):                        # monthly frames
        d = pd.DatetimeIndex(sorted(f[name]["Date"].unique()))
        expect = pd.date_range(d.min(), d.max(), freq="ME")
        missing = expect.difference(d)
        check("WARN" if len(missing) else "PASS",
              f"{name}: monthly calendar {d.min().date()} -> {d.max().date()} gap-free",
              f"{len(missing)} missing months e.g. {[str(x.date()) for x in missing[:3]]}" if len(missing) else f"{len(d)} months")
    d = pd.DatetimeIndex(sorted(fr["Date"].unique()))              # daily frame
    gap = d.to_series().diff().dt.days.max()
    check("WARN" if gap > 7 else "PASS",                           # 7d allows holiday weekends
          f"factor_returns: daily calendar {d.min().date()} -> {d.max().date()}",
          f"{len(d)} trading days, max gap {gap:.0f}d")

    # --- 5. cross-frame coverage ---------------------------------------------
    held = pos[["Date", "Position"]].drop_duplicates()
    cov = held.merge(exp[["Date", "Position"]].drop_duplicates(), on=["Date", "Position"], how="left", indicator=True)
    miss = (cov["_merge"] == "left_only").mean()
    check("WARN" if miss > 0.05 else "PASS", "held positions have exposures on the same date",
          f"{miss:.1%} of (date, position) uncovered")
    covsv = held.merge(sv[["Date", "Position"]].drop_duplicates(), on=["Date", "Position"], how="left", indicator=True)
    misv = (covsv["_merge"] == "left_only").mean()
    check("WARN" if misv > 0.05 else "PASS", "held positions have specific_var on the same date",
          f"{misv:.1%} of (date, position) uncovered")

    # Style loadings only (Market/industries are memberships, not descriptors), against the
    # regression's own pricing gate: a MAJORITY of the style set (the old ">=9 of 10" was a
    # fixed count written for a 10-style model — same fault as the regression's hard-coded 6).
    styles = set(f["factor_meta"].loc[f["factor_meta"]["FactorGroup"] == "Style", "Factor"])
    n_sty = len(styles)
    gate = n_sty // 2 + 1
    facs_per_date = exp[exp["Factor"].isin(styles)].groupby(["Date", "Position"]).Factor.nunique()
    check("WARN" if (facs_per_date < gate).any() else "PASS",
          f"exposures: >={gate} of {n_sty} style loadings per (date, position)",
          f"min {facs_per_date.min()}, {(facs_per_date < gate).mean():.1%} below {gate}")

    sec = f["securities"]
    stub = (sec["Sector"] == "Unknown").mean()
    check("WARN" if stub > 0 else "PASS", "securities: Sector populated", f"{stub:.0%} 'Unknown' (known stub)")

    # --- 6. model gates (theory-derived, the ch-16 pipeline checks) -----------
    # Each gate validates a property the construction guarantees — a failure indicts the inputs,
    # not the math. (a) standardization: every style factor's median ≈ 0 on the latest date, on
    # the ESTIMATION cross-section (z-scores are centred there; the uncapped coverage tail is
    # smaller/less liquid BY DESIGN, so full-cross-section medians legitimately sit off 0 —
    # Size ≈ −1, NonLinSize ≈ +1.5). Estimation proxy = funnel survivors when the artifact
    # exists; otherwise the gate is skipped rather than false-warned.
    dlast = exp["Date"].max()
    funnel_p = OUT / "universe_funnel.parquet"
    est_pos: set = set()
    if funnel_p.exists():
        fn = pd.read_parquet(funnel_p, columns=["month", "position", "survived"])
        est_pos = set(fn[(pd.to_datetime(fn["month"]) == dlast) & (fn["survived"] == True)]  # noqa: E712
                      ["position"].dropna())
    if len(est_pos) >= 30:
        # STYLE loadings only — Market (1.0) and Ind:* (0/1 memberships, median 1 among their
        # own members by definition) are not standardized descriptors and would always "fail".
        med = (exp[(exp["Date"] == dlast) & (exp["Position"].isin(est_pos))
                   & (exp["Factor"].isin(styles))]
               .groupby("Factor")["Loading"].median())
        worst = med.abs().idxmax()
        check("WARN" if med.abs().max() > 0.5 else "PASS",
              "exposures: estimation-universe style medians ≈ 0 (standardization gate)",
              f"{len(est_pos)} funnel survivors; worst {worst} {med[worst]:+.2f}")
    else:
        check("WARN", "exposures: estimation-universe style medians ≈ 0 (standardization gate)",
              "skipped — universe_funnel.parquet absent (run barra_universe_funnel.py)")
    # (b) the factor covariance must be PSD — the optimizer/risk-math gate.
    widefr = fr.pivot(index="Date", columns="Factor", values="Return").dropna(how="any")
    if len(widefr) > 30:
        eig = float(np.linalg.eigvalsh(widefr.cov().to_numpy()).min())
        check("FAIL" if eig < -1e-12 else "PASS", "factor covariance PSD",
              f"min eigenvalue {eig:.2e}")
    # (c) the regression fit-health artifact (v2 builder): breadth floor + sane R².
    reg_p = OUT / "regression_stats.parquet"
    if reg_p.exists():
        rs = pd.read_parquet(reg_p)
        day = rs.groupby("Date")[["R2", "N"]].first()
        bad_r2 = int(((day["R2"] < 0) | (day["R2"] > 1)).sum())
        nmin = int(day["N"].min())
        check("WARN" if nmin < 30 or bad_r2 else "PASS",
              "regression_stats: N >= 30 every day, R² in [0,1]",
              f"min N {nmin}, {bad_r2} bad-R² days, mean R² {day['R2'].mean():.2f}")
    else:
        check("WARN", "regression_stats artifact present",
              "missing — rebuild with the v2 builder to enable /regression")

    # (d) size-curve imputation disclosure: a held name with a Size loading but no Value loading
    # has no share count (foreign filer / ETF) — its Size/NonLinSize/MegaCap are the builder's
    # log-ADV proxy, not close × shares. Disclosed, never silent; Liquidity/Value/EarnYield are
    # deliberately NaN for these names.
    exp_l, pos_l = f["exposures"], f["positions"]
    d_last = pos_l["Date"].max()
    held = pos_l[pos_l["Date"] == d_last].groupby("Position")["Weight"].sum()
    ed = exp_l[exp_l["Date"] == exp_l["Date"].max()]
    have = ed.pivot_table(index="Position", columns="Factor", values="Loading", aggfunc="first")
    if {"Size", "Value"}.issubset(have.columns):
        prox = have.index[have["Size"].notna() & have["Value"].isna()]
        w = float(held[held.index.isin(prox)].sum())
        n = int(held.index.isin(prox).sum())
        check("WARN" if w > 0.25 else "PASS",
              "size-curve proxy loadings (imputed log-mcap, held book)",
              f"{n} held names, {w:.1%} of weight priced via the estimation log-ADV fit")

    return _results


def _print(results: list[dict]) -> None:
    order = {"FAIL": 0, "WARN": 1, "PASS": 2}
    width = max(len(r["name"]) for r in results)
    for r in sorted(results, key=lambda r: order[r["level"]]):
        print(f"[{r['level']:4s}] {r['name']:<{width}}  {r['detail']}")
    n = {k: sum(1 for r in results if r["level"] == k) for k in order}
    print(f"\n{n['PASS']} pass, {n['WARN']} warn, {n['FAIL']} fail")


if __name__ == "__main__":
    _print(run())
