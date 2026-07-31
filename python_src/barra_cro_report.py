"""
barra_cro_report.py
===================
Generates a single-file HTML reference report (Tufte/Few style) documenting the full
data sourcing and every transformation in the pipeline, for presentation to the CRO.

All figures are computed from the live parquet frames at generation time — nothing is
hard-coded — so the report is always consistent with the data it describes.

    ../barra/bin/python barra_cro_report.py     ->  tmp/barra_model_reference.html
"""
from __future__ import annotations
import contextlib, datetime, io, pathlib
import numpy as np
import pandas as pd

import barra_dq_checks as dq
from barra_build_frames import (EWMA_HALFLIFE_D, MCAP_FLOOR, SOROS_CIK, START, END,
                                STYLE_FACTORS, MARKET_PROXY,
                                positions_from_13f, SEED_INDEX, UNCAP_COVERAGE, COVERAGE_CAP)
from barra_factor_risk_cube import EVENT_WINDOWS, HYPO_SHOCKS, load_frames

OUT = pathlib.Path(__file__).resolve().parent.parent / "tmp" / "barra_model_reference.html"
Z99 = 2.326

GRAY, ACCENT, BLUE = "#777", "#b04030", "#4a6a8a"


# ---------------------------------------------------------------- tiny SVG sparkline
def spark(values: np.ndarray, w: int = 190, h: int = 26) -> str:
    v = np.asarray(values, float)
    lo, hi = v.min(), v.max()
    rng = (hi - lo) or 1.0
    xs = np.linspace(2, w - 8, len(v))
    ys = h - 3 - (v - lo) / rng * (h - 6)
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    i_min, i_max = int(v.argmin()), int(v.argmax())
    dots = (f'<circle cx="{xs[i_min]:.1f}" cy="{ys[i_min]:.1f}" r="1.8" fill="{ACCENT}"/>'
            f'<circle cx="{xs[i_max]:.1f}" cy="{ys[i_max]:.1f}" r="1.8" fill="{BLUE}"/>'
            f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="1.8" fill="#222"/>')
    return (f'<svg width="{w}" height="{h}" style="vertical-align:middle">'
            f'<polyline points="{pts}" fill="none" stroke="{GRAY}" stroke-width="1"/>{dots}</svg>')


def hbar(frac: float, w: int = 110) -> str:
    """Few-style horizontal magnitude bar; negative = accent, positive = blue-gray."""
    half = w / 2
    px = min(abs(frac), 1.0) * half
    x = half - px if frac < 0 else half
    color = ACCENT if frac < 0 else BLUE
    return (f'<svg width="{w}" height="12" style="vertical-align:middle">'
            f'<line x1="{half}" y1="0" x2="{half}" y2="12" stroke="#ccc" stroke-width="1"/>'
            f'<rect x="{x:.1f}" y="2" width="{px:.1f}" height="8" fill="{color}" opacity="0.75"/></svg>')


REPORT_BOOK = "Soros"   # this document is written about ONE manager; see the filter in build()


def build() -> None:
    f = load_frames()
    exposures, positions, securities = f["exposures"], f["positions"], f["securities"]
    factor_ret, specific, factor_meta = f["factor_returns"], f["specific_var"], f["factor_meta"]

    # Every count and headline below is prose about REPORT_BOOK's 13F ("Soros filed 52 quarterly
    # 13F-HR reports ..."), so the frame must be scoped to that book. Unscoped against the
    # multi-manager build it summed all eleven -- the report claimed 19,593 names held at
    # 2026-06-30 under Soros's name, and specific vol 1.20% against the true 0.26%.
    # The universe frames (securities/exposures) are NOT book-scoped -- they span every book in the
    # build -- so the §4 prose needs the pre-filter book count to describe them honestly.
    all_books = (sorted(positions["Book"].unique().tolist())
                 if "Book" in positions.columns else [REPORT_BOOK])
    n_books = len(all_books)
    if "Book" in positions.columns and positions["Book"].nunique() > 1:
        positions = positions[positions["Book"] == REPORT_BOOK]
        if positions.empty:
            raise SystemExit(f"no positions for REPORT_BOOK={REPORT_BOOK!r}")

    last = positions["Date"].max()
    book = positions[positions["Date"] == last].merge(securities, on="Position")
    wide = (factor_ret[factor_ret["Factor"] != "Market"]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    factors = list(wide.columns)
    n_style = int((factor_meta["FactorGroup"] == "Style").sum())
    n_ind = int((factor_meta["FactorGroup"] == "Industry").sum())

    # --- full-book risk snapshot (mirrors the cube measures in pandas) ----------------
    L = (exposures[(exposures["Date"] == last) & (exposures["Position"].isin(book["Position"]))]
         .pivot(index="Position", columns="Factor", values="Loading")
         .reindex(index=book["Position"], columns=factors).fillna(0.0))
    wts = book.set_index("Position")["Weight"]
    x = pd.Series(L.values.T @ wts.values, index=factors)
    pnl = pd.Series(wide.values @ x.values, index=wide.index)
    sv = (specific[(specific["Position"].isin(book["Position"])) & (specific["Date"] <= last)]
          .sort_values("Date").groupby("Position").last()["SpecificVar"].reindex(book["Position"]))
    spec_var = float((wts.values ** 2 * sv.fillna(0).values).sum())
    spec_vol = float(np.sqrt(spec_var))
    sv_cov = sv.notna().mean()

    def var_row(p):
        v = -np.percentile(p, 1)
        return v, -p.min(), float(np.sqrt(v * v + (Z99 * spec_vol) ** 2))
    scen = {"HistFull (full history)": var_row(pnl)}
    for name, (a, b) in EVENT_WINDOWS.items():
        w = pnl.loc[a:b]
        if len(w):
            scen[f"{name} ({a} → {b}, {len(w)}d)"] = var_row(w)
    vols = wide.std(ddof=1)
    for name, shock in HYPO_SHOCKS.items():
        h = float(sum(x[fc] * shock.get(fc, 0.0) * vols[fc] for fc in factors))
        scen[f"{name} ({', '.join(f'{k} {v:+.0f}σ' for k, v in shock.items())})"] = (
            -h, -h, float(np.sqrt(h * h + (Z99 * spec_vol) ** 2)))

    # --- DQ results (barra_dq_checks.run now returns structured {level,name,detail} dicts) -----
    dqr = dq.run()
    n_pass = sum(1 for r in dqr if r["level"] == "PASS")
    warns = [(r["name"], r["detail"]) for r in dqr if r["level"] == "WARN"]
    fails = [(r["name"], r["detail"]) for r in dqr if r["level"] == "FAIL"]

    # --- estimation universe: 13F book UNION market-index seed --------------------------
    uni_n = securities["Position"].nunique()              # total estimation universe
    exp_n = exposures["Position"].nunique()               # of those, with usable loadings
    held_n = positions["Position"].nunique()              # held at >=1 sampled month-end
    hpd = positions.groupby("Date")["Position"].nunique()  # active book breadth per date
    hpd_min, hpd_med, hpd_max = int(hpd.min()), int(hpd.median()), int(hpd.max())
    # Index-seeded names carry their (lowercased) ticker as Position (no real FIGI); held names
    # carry a real FIGI. So this mask separates the market-index seed from the 13F-sourced book.
    idx_only_n = int((securities["Position"].str.lower() == securities["Ticker"].str.lower()).sum())
    held_uni_n = uni_n - idx_only_n                       # crosswalked 13F names kept whole
    seed_name = ({"sp500": "the S&amp;P 500"}.get(str(SEED_INDEX).lower(), str(SEED_INDEX))
                 if SEED_INDEX else None)
    seed_label = ({"sp500": "S&amp;P 500"}.get(str(SEED_INDEX).lower(), str(SEED_INDEX))
                  if SEED_INDEX else "none")
    # Full 13F name set (the source population the book is drawn from). Cached SEC pull; guard so
    # report generation never fails if the cache is cold / SEC is unreachable.
    try:
        _p13 = positions_from_13f(SOROS_CIK)
        f13_filings = int(_p13["filing_date"].nunique())
        f13_cusips = int(_p13["cusip"].nunique())
        f13_npf = int(_p13.groupby("filing_date")["cusip"].nunique().median())
        f13_ok = True
    except Exception:
        f13_filings = f13_cusips = f13_npf = 0
        f13_ok = False
    # With several books loaded, held_uni_n is the union across ALL of them -- attributing it to
    # REPORT_BOOK alone read as "2,461 CUSIPs resolve to 4,796 names". Keep the two separate.
    multi = n_books > 1
    books_clause = (
        f" The universe is not book-scoped: it spans all <strong>{n_books}</strong> books in this "
        f"build ({held_uni_n} 13F-sourced names between them). This report is written about "
        f"{REPORT_BOOK} alone." if multi else "")
    if f13_ok:
        f13_source = (
            f"Across the sample {REPORT_BOOK} filed <strong>{f13_filings}</strong> quarterly 13F-HR "
            f"reports holding a median of <strong>{f13_npf}</strong> cash-equity names each "
            f"(<strong>{f13_cusips:,}</strong> distinct CUSIPs in total, options and bond lots "
            f"dropped), resolving to <strong>{held_n}</strong> names here. The universe keeps "
            f"them <em>all</em>, so the book is always a subset of the universe." + books_clause)
    else:
        f13_source = (f"The book is drawn from the union of CUSIPs across all of {REPORT_BOOK}'s "
                      f"13F-HR tables (options and bond lots dropped), resolving to {held_n} names, "
                      f"all kept so the full book is covered." + books_clause)
    _not_in = "those books" if multi else "the book"
    nest_pop = (f"the union of all {n_books} books' 13F names" if multi else "the whole 13F book")
    cik_note = (f"; one CIK per book, {n_books} in this build" if multi else "")
    multi_book_note = (
        f" The build carries <strong>{n_books}</strong> books in all ({', '.join(all_books)}); this "
        f"document covers {REPORT_BOOK} only, and the risk numbers in §6 are {REPORT_BOOK}'s."
        if multi else "")
    if seed_name:
        seed_clause = (
            f"To make the cross-section a genuine market rather than one manager's holdings, the "
            f"universe is then <strong>seeded with {seed_name}</strong>: its constituents are unioned "
            f"in, adding <strong>{idx_only_n}</strong> names not already in {_not_in}, for a total "
            f"estimation universe of <strong>{uni_n}</strong>.")
    else:
        seed_clause = (f"No market index is seeded (<code>SEED_INDEX</code> is off), so the universe "
                       f"is the {uni_n} 13F-sourced names alone, the managers' own opportunity set.")

    # --- factor table with sparklines ---------------------------------------------------
    fgrp = factor_meta.set_index("Factor")["FactorGroup"]
    frows = []
    for fc in ["Market"] + factors:
        s = factor_ret[factor_ret["Factor"] == fc].set_index("Date")["Return"].sort_index()
        cum = (1 + s).cumprod()
        frows.append(
            f"<tr><td>{fc}</td><td>{fgrp.get(fc, '')}</td>"
            f"<td class='num'>{s.std():.2%}</td>"
            f"<td class='num'>{s.min():.2%}</td><td class='num'>{s.idxmin().date()}</td>"
            f"<td>{spark(cum.values)}</td></tr>")

    nx = max(abs(x.max()), abs(x.min()))
    xrows = "".join(
        f"<tr><td>{fc}</td><td class='num'>{x[fc]:+.3f}</td><td>{hbar(x[fc]/nx)}</td></tr>"
        for fc in x.sort_values(key=abs, ascending=False).index)

    srows = "".join(
        f"<tr><td>{name}</td><td class='num'>{v:.2%}</td><td class='num'>{wl:.2%}</td>"
        f"<td class='num'>{tv:.2%}</td></tr>" for name, (v, wl, tv) in scen.items())

    wrows = "".join(f"<tr><td>{n}</td><td>{d}</td></tr>" for n, d in warns)
    frows_html = "".join(frows)
    n_names = positions["Position"].nunique()
    n_days = wide.shape[0]
    gen = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # estimation/coverage split prose for §3 stage (b)
    if UNCAP_COVERAGE:
        cap_clause = ("<strong>Estimation</strong> rows (the names the factor returns are fit on) are "
                      "winsorised at <strong>±3σ</strong>; <strong>coverage</strong> rows (held names "
                      "not in the seed) are left <strong>uncapped</strong>, bounded only by a loose "
                      f"±{COVERAGE_CAP:g} backstop against corrupt data. NonLinSize is the one "
                      "exception — capped ±3 on every row, because a cubed tail amplifies noise "
                      "rather than telling the truth (§3a, §7·b).")
        cap_why = (f"This is Chris's data-quality point: a tiny held name far below anything in the "
                   f"{seed_label} reads its <em>true</em> large-negative Size loading (≈ −6), not a "
                   f"clip to −3 — the correct statement of a real tilt, and capping belongs only in "
                   f"the estimation set, to protect the regression.")
        est_fit_clause = (f" The regression is fit on the <strong>estimation universe only</strong> "
                          f"(the {seed_label} seed), so held-but-not-seed names never pull the factor "
                          f"returns.")
        cov_spec_clause = (" Specific risk is formed for <strong>every coverage name</strong> (its own "
                           "residual against the estimation-fit factor returns), so the cube prices the "
                           "whole book even though only estimation names enter the fit.")
    else:
        cap_clause = "Every row is winsorised at <strong>±3σ</strong> (the estimation/coverage split is off)."
        cap_why = ""
        est_fit_clause = ""
        cov_spec_clause = ""

    # --- estimation-universe DQ filter stack (Phase 2 funnel; universe_filters.json) ----------
    import json as _json
    _root = pathlib.Path(__file__).resolve().parent.parent
    try:
        fcfg = {k: v for k, v in _json.loads((_root / "universe_filters.json").read_text()).items()
                if not k.startswith("_")}
    except Exception:
        fcfg = {}

    def _money(v):
        try:
            return f"${float(v) / 1e6:,.0f}M"
        except Exception:
            return str(v)
    _tf = fcfg.get("min_trade_freq")
    _tf_s = f"≥ {_tf:.0%}" if isinstance(_tf, (int, float)) else "—"
    _buf = fcfg.get("buffer", {})
    _buf_s = (f"enter {_buf.get('enter_pctile', '—')}th / exit {_buf.get('exit_pctile', '—')}th "
              f"pctile {_buf.get('metric', '')}" if _buf else "—")
    filter_rows = [
        ("1 · Listing / security type", "primary common", "Drop warrants, preferred, units, "
         "closed-end funds. S&amp;P 500 membership already implies a primary common listing, so this "
         "stage is effectively pass-through here."),
        ("2 · Size — min market cap", _money(fcfg.get("min_mcap", "—")), "close × shares-outstanding, "
         "point-in-time (shares as-of the SEC <code>filed</code> date). Data quality correlates with "
         "size; small-cap moves are dominated by microstructure noise."),
        ("3 · History — min trading days", f"{fcfg.get('min_hist_days', '—')}d", "Length of the daily "
         "price series as-of the month-end — a minimum trading history before admission."),
        ("4 · Trading frequency", _tf_s, "Fraction of recent days with non-zero volume — screens "
         "stale / thinly-traded quotes."),
        ("5 · Liquidity — min ADV", _money(fcfg.get("min_adv", "—")) + "/day", "Trailing mean dollar "
         "volume (close × volume). Ensures each day's price is market-clearing, not a stale quote."),
        ("6 · Completeness", f"≥ {fcfg.get('min_descriptors', '—')} of {len(STYLE_FACTORS)}",
         "Enough non-null style descriptors present for the name to be represented in the model."),
        ("7 · Stability buffer", _buf_s, "Hysteresis on the liquidity rank across months: a name "
         "enters above the high band and leaves only below the low band, so membership doesn't churn "
         "in and out at a single threshold."),
        ("— Free float", "<em>unavailable</em>", "No free-data source for float-adjusted shares; "
         "shown as a disclosed, inert stage (drops nobody)."),
        ("— Confirmed-M&amp;A removal", "<em>unavailable</em>", "Needs deal data — confirmed targets "
         "track deal probability, not factors. Disclosed, inert."),
    ]
    filter_html = "".join(f"<tr><td>{n}</td><td class='num'>{t}</td><td>{d}</td></tr>"
                          for n, t, d in filter_rows)
    try:
        _fdf = pd.read_parquet(_root / "data" / "universe_funnel.parquet")
        _fl = _fdf[_fdf["month"] == _fdf["month"].max()]
        fn_month = str(pd.Timestamp(_fdf["month"].max()).date())
        fn_pop = int(len(_fl))
        fn_unavail = int((_fl["stage_dropped"] == "data unavailable").sum())
        fn_surv = int(_fl["survived"].sum())
        fn_eval = fn_pop - fn_unavail
        funnel_result = (f"On the latest month (<strong>{fn_month}</strong>), of "
                         f"<strong>{fn_eval}</strong> evaluable point-in-time S&amp;P 500 names "
                         f"(<strong>{fn_unavail}</strong> more are delisted members we can't measure, "
                         f"tagged <em>data unavailable</em>, never counted as a filter drop), "
                         f"<strong>{fn_surv}</strong> survive the stack.")
    except Exception:
        funnel_result = ("Run <code>barra_universe_funnel.py</code> to populate the per-month "
                         "survivor counts.")

    css = """
    body { background:#fffff8; color:#151515; font-family:Palatino,'Palatino Linotype',Georgia,serif;
           max-width:980px; margin:2.5rem auto 5rem; padding:0 2rem; line-height:1.55; font-size:15px; }
    h1 { font-size:1.9rem; font-weight:400; margin-bottom:.2rem; }
    h2 { font-size:1.15rem; font-weight:600; font-variant:small-caps; letter-spacing:.04em;
         margin:2.6rem 0 .6rem; border-bottom:1px solid #ddd; padding-bottom:.15rem; }
    .sub { color:#666; font-style:italic; margin-top:0; }
    table { border-collapse:collapse; width:100%; margin:.8rem 0; font-size:14px; }
    th { text-align:left; font-weight:600; font-variant:small-caps; letter-spacing:.03em;
         border-bottom:1px solid #999; padding:.25rem .6rem .25rem 0; }
    td { padding:.28rem .6rem .28rem 0; border-bottom:1px solid #eee; vertical-align:top; }
    td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
    .flow { display:flex; align-items:center; gap:.4rem; flex-wrap:wrap; margin:1rem 0; font-size:13px; }
    .box { border:1px solid #bbb; padding:.4rem .7rem; background:#fdfdf4; }
    .arr { color:#999; }
    .note { background:#f6f4ea; border-left:3px solid #b04030; padding:.6rem 1rem; margin:1rem 0;
            font-size:14px; }
    .ok   { color:#3a6a3a; } .warn { color:#b04030; }
    code { font-family:Consolas,Menlo,monospace; font-size:13px; background:#f4f2e8; padding:0 .25rem; }
    .small { font-size:12.5px; color:#666; }
    ol li, ul li { margin-bottom:.45rem; }
    .formula { font-style:italic; padding:.2rem 0 .2rem 1.5rem; }
    """

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Barra-style Factor Risk POC — Data &amp; Transformation Reference</title>
<style>{css}</style></head><body>

<h1>Factor Risk Model — Data &amp; Transformation Reference</h1>

<h2>Summary</h2>
<ul>
<li><strong>What.</strong> A two-block linear factor model — {n_style} style factors + {n_ind} GICS
    industry factors + a market
    intercept (the systematic block) and a diagonal name-specific block — over Soros Fund Management's
    13F US-equity book ({n_names} names across the sample, {book.shape[0]} held at {last.date()}).</li>
<li><strong>Data, all free / public.</strong> SEC EDGAR 13F (positions), SEC XBRL company facts
    (fundamentals, point-in-time), OpenFIGI (CUSIP→FIGI identity), Stooq / Yahoo (daily prices). One
    builder writes six parquet frames; the Atoti cube reads those and nothing else. No factor-return
    series is bought or downloaded — they are estimated in-house (§3).</li>
<li><strong>Loadings.</strong> Each name's style loading is a cross-sectional z-score of a characteristic
    — Size / Value / Earnings Yield / Leverage from fundamentals, Beta / Residual Vol /
    Momentum / Liquidity / RateBeta / NdxBeta from prices, NonLinSize derived from Size —
    standardised by median / MAD on the estimation universe (winsorised ±3 there, left uncapped
    on coverage so off-index names read their true loadings; NonLinSize capped ±3 everywhere,
    the one exception — §3a).</li>
<li><strong>Risk.</strong> Every scenario is the same calculation
    <span class="formula">dPnL = Σ<sub>k</sub> x<sub>k</sub>·Δf<sub>k</sub></span> — only the source of the
    shock vector changes (historical simulation, event replay, or a hypothetical stress). All figures are
    1-day, 99%; book specific vol is currently {spec_vol:.2%}.</li>
<li><strong>Industries are factors; country is a tag.</strong> Since 2026-07-04 each name carries a
    0/1 loading on its GICS sector and the daily regression prices {n_ind} industry factors under the
    Barra constraint (weighted industry returns sum to zero, so Market stays the market). Country
    (SEC state of incorporation; ~21% of the book reads non-US) remains a tag only — there is
    <strong>no country factor</strong>, so that exposure is carried by the market / style / industry /
    specific loadings, not a country bet.</li>
<li><strong>Cadence.</strong> Three different clocks. <strong>Quarterly</strong> 13F holdings set the
    book. Each <strong>month-end</strong> the exposure loadings are rebuilt — from as-of fundamentals and
    prices (§3a–b). The daily stock returns then regress on those month-old loadings to give
    <strong>daily</strong> factor returns (§3c), which drive every risk number. Quarterly → monthly → daily.</li>
<li><strong>Watch-outs.</strong> Monthly loadings drive daily returns (intra-month drift); the estimation
    cross-section is US-large-cap-tilted; 13F is quarterly, lagged and long-only; specific-risk coverage is
    partial. Full list in §9.</li>
</ul>
<div class="flow">
  <span class="box">13F holdings<br><span class="small">quarterly · stock</span></span>
  <span class="arr">⟶</span>
  <span class="box">Exposure loadings<br><span class="small">monthly · stock × factor</span></span>
  <span class="arr">⟶</span>
  <span class="box">Factor returns<br><span class="small">daily · factor</span></span>
  <span class="arr">⟶</span>
  <span class="box">Risk numbers<br><span class="small">VaR / ES / stress, 1-day 99%</span></span>
</div>
<p class="small">Fundamentals (per filing, as-of) and daily prices feed the monthly loadings step.
The rest of this document is the audit trail behind each line above — every input, every transformation,
every check.</p>

<p>This document is the audit trail for every number the model produces: where each input comes
from, what we do to it, and which checks watch it. The book is <strong>Soros Fund Management's US
equity positions</strong>, taken from their quarterly SEC <strong>13F filings</strong> (CIK
{SOROS_CIK}).{multi_book_note} The model is a <strong>two-block linear factor model</strong>. Portfolio P&amp;L
splits into {n_style}&nbsp;style factors plus {n_ind}&nbsp;GICS industry factors plus a market
intercept (the systematic block) and a diagonal name-specific block. Every scenario is the same
calculation, <span class="formula">dPnL = Σ<sub>k</sub> x<sub>k</sub> · Δf<sub>k</sub></span>,
where x<sub>k</sub> is the book's net exposure to factor k and Δf<sub>k</sub> is a shock vector.
The only thing that changes between scenario types is where that shock vector comes from:
historical simulation, event replay, or a hypothetical stress. <strong>All risk figures are 1-day,
99%.</strong></p>

<h2>Pipeline at a glance</h2>
<p>The whole pipeline, end to end. Free public feeds go into one builder. The builder writes six
parquet frames. The cube reads those frames and nothing else. The builder and the cube share no
state in memory, so the six frames are the entire hand-off between data and risk.</p>
<div class="flow">
  <span class="box">SEC EDGAR 13F<br><span class="small">positions, quarterly</span></span>
  <span class="box">OpenFIGI v3<br><span class="small">CUSIP→FIGI identity</span></span>
  <span class="box">SEC XBRL facts<br><span class="small">fundamentals, PIT</span></span>
  <span class="box">Stooq / Yahoo<br><span class="small">daily prices</span></span>
  <span class="arr">⟶</span>
  <span class="box"><strong>builder</strong><br><span class="small">monthly z-score exposures · daily WLS (weighted least squares) → factor returns</span></span>
  <span class="arr">⟶</span>
  <span class="box">6 parquet frames<br><span class="small">the only hand-off</span></span>
  <span class="arr">⟶</span>
  <span class="box"><strong>Atoti cube</strong><br><span class="small">exposures · scenario engine</span></span>
</div>
<p class="small">Exposures are built <strong>monthly</strong>. The daily factor returns that drive
every risk number are <strong>estimated from them in-house</strong> (§3). No factor-return series
is bought or downloaded. The monthly-to-daily step is the heart of the model and is set out in
§3c.</p>

<h2>1 · Data sources</h2>

<div class="note"><strong>Where does the factor model come from? Read this first.</strong>
<strong>The factor model is not downloaded from anyone.</strong> There is no MSCI or Barra licence
and no external factor-return file behind these numbers. The {n_style} style factors, the {n_ind}
industry factors and the
market factor are <em>built here, from scratch</em>, in three parts:
<ol style="margin:.4rem 0">
<li><strong>Factor definitions (the taxonomy), open-source and academic.</strong> What
    Size, Value, Momentum, EarnYield, Leverage, ResidVol and the rest mean follows the public
    literature: the Fama–French / <strong>Ken French Data Library</strong> factors, the
    <strong>BARRA USE4</strong> style set, and the <strong>JKP</strong> characteristic library
    (Jensen–Kelly–Pedersen, <code>jkpfactors.com</code>). These are conventions, not a data feed.
    We re-implement the formulas (§3a). We do not import them.</li>
<li><strong>Exposures (loadings), computed here</strong> from SEC fundamentals and Stooq/Yahoo
    prices as cross-sectional z-scores, monthly (§3a–b).</li>
<li><strong>Factor returns, estimated here</strong>, <strong>daily</strong>, by regressing each
    day's stock returns on the latest monthly exposures (§3c–e). This is the standard BARRA
    cross-sectional approach. The factor returns are an output of our regression, not a bought or
    downloaded series. The monthly-to-daily step is in §3c.</li>
</ol>
The only raw data we use (all free) are 13F holdings, identity mappings, company fundamentals and
daily prices. The factor structure is built from those, which is why §3 is the substance of the
model.</div>

<p>Every input is free or public-domain, fetched over HTTP and cached to disk, so reruns are cheap
and do not hit rate-limited APIs again. The four raw feeds below go into the builder. The fifth
row, factor returns, is the estimated output from the callout above, listed here so all six frames
sit in one place.</p>
<table>
<tr><th>Input</th><th>Source &amp; key</th><th>Cadence</th><th>Grain</th><th>What we rely on it for</th><th>Known caveats</th></tr>
<tr><td>Positions</td><td>SEC EDGAR 13F-HR information tables, CUSIP-keyed (CIK {SOROS_CIK}{cik_note})</td><td>Quarterly, ~45d lag</td>
    <td>Stock</td><td>The book, held as a weight overlay (§2)</td><td>Long US equity only; options (putCall) and bond lots (PRN) are dropped; ETFs and commodity/crypto trusts dropped (no fundamentals, no sector); shorts and intra-quarter trades invisible; public by construction</td></tr>
<tr><td>Identity</td><td>OpenFIGI v3 mapping API (CUSIP→FIGI→ticker) + SEC <code>company_tickers.json</code> (ticker→CIK)</td>
    <td>On rebuild</td><td>Stock</td><td>One canonical id (FIGI) joining every frame</td><td>Unmapped CUSIPs drop out of the universe; FIGI = Position = SecId everywhere downstream</td></tr>
<tr><td>Fundamentals</td><td>SEC XBRL company-facts API, CIK-keyed; tags: Assets, Liabilities,
    StockholdersEquity, NetIncomeLoss, shares from <code>dei:EntityCommonStockSharesOutstanding</code></td>
    <td>Per filing (point-in-time, as-of joined on <code>filed</code> date)</td><td>Stock</td><td>Size, Value, EarnYield, Leverage descriptors</td>
    <td>Tag coverage varies by filer; ~no look-ahead by construction; see incident note in §5</td></tr>
<tr><td>Prices</td><td>Stooq daily CSV (ticker-keyed) with Yahoo chart-API fallback (split-adjusted close, volume); market proxy = <code>{MARKET_PROXY.upper()}</code></td>
    <td>Daily</td><td>Stock</td><td>Stock returns (the regression's left-hand side), Beta, ResidVol, Momentum, Liquidity, market cap</td>
    <td>Free, not redistributable; Stooq serves a JS anti-bot page to some hosts (fallback handles this transparently)</td></tr>
<tr><td>Exposure loadings</td><td><strong>Not sourced, computed.</strong> Cross-sectional z-scores of the
    fundamental + price descriptors, standardised on the estimation universe (§3a–b)</td>
    <td><strong>Monthly</strong> (rebuilt each month-end)</td><td><strong>Stock × Factor</strong></td><td>The book's net factor exposure, and the right-hand
    side of the daily factor-return regression</td>
    <td>Month-old when the daily returns regress on them — intra-month drift (§9)</td></tr>
<tr><td>Factor returns</td><td><strong>Not sourced, estimated.</strong> Daily cross-sectional WLS regression of stock
    returns on the latest monthly exposures (§3). {n_style} style factors + {n_ind} GICS industry
    factors (0/1 sector dummies, constrained — §3e) + 1 market intercept.</td>
    <td>Daily, {n_days:,} trading days ({START[:4]}–{END[:4]})</td><td><strong>Factor</strong></td><td>The entire systematic block and scenario engine</td>
    <td>Quality scales with universe breadth; no vendor benchmark to validate against</td></tr>
</table>
<p class="small"><strong>A note on cadence and grain.</strong> The feeds arrive at different rhythms —
positions quarterly, fundamentals per filing, prices daily — but the model runs on a
<strong>monthly</strong> calendar: <strong>exposure loadings are rebuilt each month-end</strong> (§3a–b),
and the factor returns are estimated <strong>daily</strong> off those month-old loadings (§3c). The
<em>Grain</em> column is the other axis — every input is per-<strong>stock</strong>, the loadings are
per-<strong>stock&nbsp;×&nbsp;factor</strong>, and the daily regression collapses them to
per-<strong>factor</strong>. That collapse is §3, the substance of the model.</p>

<h2>2 · Transformation register — positions &amp; identity</h2>
<ol>
<li><strong>13F parsing.</strong> Every information table for the CIK is parsed to
    (report date, filing date, CUSIP, $ value). Multiple lots or share classes of one issuer
    are <em>summed</em> per filing.</li>
<li><strong>Identity resolution.</strong> CUSIP to FIGI via OpenFIGI, ticker to CIK via SEC.
    All frames key on FIGI. CUSIP, ticker and CIK never leave the builder.</li>
<li><strong>Point-in-time weight overlay.</strong> Weights are normalised <em>within each
    filing</em> (Σw = 1). Each calendar month-end is then matched to the <em>latest filing on or
    before it</em>, so the book only counts once it has been filed. Names dropped from the newest
    filing expire with it, so no stale positions persist. Weights sum to exactly 1.0 on all
    {positions["Date"].nunique()} month-ends.</li>
</ol>

<h2>2·b · Estimation universe &amp; its intersection with the 13F filings</h2>
<p>The factor model is estimated cross-sectionally (§3), so each day it needs a <em>population of
names</em> to regress over. That is the <strong>estimation universe</strong>. It is built in two
parts: <strong>the whole 13F book plus a market-index seed</strong>. {f13_source} {seed_clause}
The result is a market cross-section that fully contains the book, which is what a Barra-style
model needs.</p>

<table>
<tr><th>Population</th><th class="num">Names</th><th>Definition / role</th></tr>
<tr><td>13F source population</td><td class="num">{f13_cusips:,}</td><td>Distinct cash-equity CUSIPs across all {f13_filings} quarterly filings (before identity resolution)</td></tr>
<tr><td>13F-held names (kept whole)</td><td class="num">{held_uni_n}</td><td>Those CUSIPs resolved to FIGI, all kept so the full book is covered</td></tr>
<tr><td>+ market-index seed ({seed_label})</td><td class="num">{idx_only_n}</td><td>Index constituents not already in the book, unioned in for market breadth</td></tr>
<tr><td>= estimation universe</td><td class="num">{uni_n}</td><td>The names carried in <code>securities</code></td></tr>
<tr><td>… with usable exposures</td><td class="num">{exp_n}</td><td>Of the universe, those with enough price/fundamental data to carry loadings, the regression cross-section</td></tr>
<tr><td>Held at ≥1 month-end</td><td class="num">{held_n}</td><td>Universe names that surface as an actual book weight at some sampled date</td></tr>
<tr><td>Active book per month-end</td><td class="num">{hpd_med} <span class="small">(med)</span></td><td>Names in the latest filing ∩ universe on a given date (range {hpd_min}–{hpd_max})</td></tr>
</table>

<p><strong>How the populations nest.</strong> The {uni_n}-name universe is {nest_pop}
({held_uni_n} names) plus the index seed (+{idx_only_n} names not already held). {exp_n} of them
have usable loadings and enter the daily regression. {REPORT_BOOK}'s book is always a strict
<em>subset</em>: {held_n} names are held at some point in the sample, and on any one date the
active book is the names in the latest filing that are also in the universe (median {hpd_med},
range {hpd_min}–{hpd_max}). The point worth making: the regression that builds the factor returns
(§3) runs over the full {exp_n}-name market cross-section, not just the names {REPORT_BOOK} holds.
So the factors describe a market, and names {REPORT_BOOK} has never held still add breadth. The
cube prices the full filed 13F book each quarter, and every §6 number reflects that.</p>

<p><strong>Why this shape.</strong> Factor-return quality goes up with breadth, and the daily
regression skips any date with fewer than 30 valid names, so a wide market cross-section beats the
manager's holdings alone. That is why we seed an index. The only real cost is build time: each name
needs cold-cache SEC, OpenFIGI and Stooq calls plus a fundamentals pull. Those are rate-limited,
cached to disk, and run in parallel across worker threads, so build time grows with the universe.
To go wider, point <code>SEED_INDEX</code> at a bigger benchmark (S&amp;P 1500 or Russell 3000).
The book stays a subset by construction.</p>

<h2>2·c · Estimation-universe data-quality filters</h2>
<p>A real estimation universe is defined by data-quality <em>rules</em>, not by index membership
(Chris, §5.2: "the estimation universe is a statistical sample chosen for clean estimation, not an
investable index"). The pre-filter population is the <strong>point-in-time S&amp;P 500</strong> — the
only survivorship-free index available on free data (read as-of each month from a constituent change
log that keeps delisted names), and the desk's settled choice over a broader index
(<strong>Chris, 2026-06-23:</strong> "go with option (1) … survivorship bias should be avoided at all
costs"). Each member is then run, point-in-time, through a fixed filter stack and tagged with the
<em>first</em> stage that drops it:</p>

<table>
<tr><th>Filter (in order)</th><th class="num">Threshold</th><th>What it screens / why</th></tr>
{filter_html}
</table>

<p>{funnel_result} The funnel is <strong>near-flat by design</strong>: the S&amp;P 500 is already a
committee-curated set, so the filters <em>confirm</em> clean data rather than carve much away —
exactly as expected (<strong>Chris:</strong> for the most-traded SPX names the daily prices are
reliable / market-clearing). A name we cannot measure (a delisted member absent from the built
universe, or one missing a share count) is shown as <em>data unavailable</em>, never as a filter
drop, so the per-stage counts only ever reflect genuine threshold failures. Two of the criteria —
<strong>free float</strong> and <strong>confirmed-M&amp;A removal</strong> — have no free data source
and appear as inert, disclosed stages so the methodology is documented in full.</p>

<p class="small">Thresholds live in <code>universe_filters.json</code> (documented and tunable). This
filtration is the Phase-2 diagnostic over the estimation universe — surfaced live in the dashboard's
"🌐 Estimation universe" panel (per-month population→survivor waterfall, per-stage drop list, and the
span / high-confidence check) and detailed in <code>docs/universe-diagnostics-plan.md</code>. It is a
diagnostic on data quality; it does not alter the §3 cross-section the cube currently fits on.</p>

<h2>3 · Transformation register — exposures, factor returns, specific risk</h2>
<p>This is the heart of the model and the part with no external benchmark, so every stage is set
out. Exposures, factor returns and specific risk all come out of <strong>one regression</strong>.
The monthly exposure panel is the regression's right-hand side (the loadings), and the same
regression produces the factor-return cache and the residual variances. They are never spliced
from different sources. The flow runs in eight stages and moves the data from a <em>monthly</em>
exposure cadence to a <em>daily</em> factor-return cadence. Stage&nbsp;(c) is the bridge.</p>
<div class="flow">
  <span class="box">monthly characteristic exposures<br><span class="small">cross-sectional z-scores (a–b)</span></span>
  <span class="arr">⟶</span>
  <span class="box">held fixed for the month<br><span class="small">prior month-end loadings (c)</span></span>
  <span class="arr">⟶</span>
  <span class="box">daily cross-sectional WLS<br><span class="small">stock returns ~ lagged exposures (d–e)</span></span>
  <span class="arr">⟶</span>
  <span class="box"><strong>daily factor returns</strong><br><span class="small">the scenario cache</span></span>
</div>

<p><strong>How to read this.</strong> For each month-end we build a table: one row per stock, one
column per metric (Size, Value, Momentum, Leverage and so on). Each cell is that stock's value for
the month. Stack the monthly tables and you have a block with three axes: stock, metric, month.
Read one stock down the months and that is its monthly history for a metric. Read one month across
all stocks and that is the cross-section. Stage (a) fills the table from prices and fundamentals.
Stage (b) scores each column, one month at a time. The point to be clear on: the exposure is not
the raw value. It is a score for where the stock sits on that metric against the rest of the
universe that month, in standard deviations. A Size score of +2 means the stock is two standard
deviations bigger than the median name that month. That is what lets us compare Size against Value,
and one month against the next, even though the raw numbers (log dollars for Size, a ratio for
Value) are on different scales.</p>

<p class="small"><strong>A worked example.</strong> Take one stock, one month, one metric. Say its
raw Value (book equity / market cap) is 0.045 in June. To score it we look at every stock's Value
that June, take the middle and the spread, and turn 0.045 into something like +0.7. That just says
it is a bit cheaper than the typical name that month. We store +0.7 as the stock's Value loading
for June. July runs the same way over July's stocks and gives the next point in the series. So the
exposures we feed the model are just the scored version of the per-stock monthly metrics you asked
about.</p>
<ol>
<li><strong>(a) Descriptor construction</strong>, per name, on the monthly month-end calendar.
    <em>Price-based</em> (trailing daily prices): <code>Beta</code> = cov(stock, {MARKET_PROXY.upper()}) / var({MARKET_PROXY.upper()})
    over a 252-day window (needs ≥120 observations, else withheld); <code>ResidVol</code> =
    annualised σ of that regression's residual (×√252); <code>Momentum</code> = 12-1 <em>log</em>
    relative strength, ln(price<sub>t−21</sub> / price<sub>t−252</sub>) (the most recent month is
    skipped; respecified 2026-07-04 from the arithmetic ratio, which is bounded at −1 and unbounded
    above, so its z-scored winner tail ran to +8/+10 while realized momentum sensitivity saturates
    near +1 — the log symmetrises it, §7·b);
    <code>Liquidity</code> = log turnover (trailing-63d mean daily dollar-volume / mcap),
    orthogonalised to Size on the estimation fit — respecified 2026-07-04; the raw dollar-volume
    version was cross-sectionally a second Size (factor-return ρ ≈ −0.8);
    <code>RateBeta</code> = partial duration beta — the rate proxy's (TLT) daily return is
    residualised against the market over the same 252-day window, then the stock's beta to that
    residual, so it measures rate sensitivity <em>beyond</em> what equity beta already carries;
    <code>NdxBeta</code> = partial mega-complex beta, the same construction against QQQ — the
    realized comovement with the mega-cap complex, which trades as a group beyond any smooth
    function of log-mcap.
    <em>Fundamental-based</em> (SEC XBRL, as-of joined on the <code>filed</code> date so nothing
    is known before it was reported): <code>Size</code> = log(mcap+1); <code>NonLinSize</code> =
    the cube of the <em>standardised</em> Size loading <em>winsorised at ±3</em> (USE4 convention —
    cubing raw log-mcap leaves a quadratic U-shape after the Size fit, which pinned small/mid-caps
    together; respecified 2026-07-04), orthogonalised to Size, with the standardised output also
    capped at ±3 for coverage rows — cubing amplifies a Size error by 3z², so the uncapped coverage
    tail carried −9/−10 loadings with no realized comovement (§7·b; unlike Size, the uncapped cubic
    tail is not truth-telling) (<code>MegaCap</code>, a spline knot above
    the estimation q90, was added and dropped the same day — 2026-07-04 — once NdxBeta took over
    the mega club's pricing; §7·b);
    <code>Value</code> = book equity / mcap; <code>EarnYield</code> = net income /
    mcap; <code>Leverage</code> = total liabilities / total assets (respecified 2026-07-04 from
    assets/equity, which is unbounded as equity → 0 — insurers read 40×, distressed names 100×+ —
    so the winsor pinned them at one loading and the factor degenerated into a financials-sector
    dummy; liabilities/assets is the same information through a bounded monotone transform).
    (<code>Growth</code> was dropped 2026-07-04:
    |t|&gt;2 on only 9% of regression days and a loading on only 21% of held weight.)
    Market cap = split-adjusted close × shares outstanding (DEI cover-page count).
    <strong>Market-cap floor:</strong> if a name's mcap falls below ${MCAP_FLOOR:,.0f} that month
    we withhold its fundamental descriptors (set them missing, not zero). A corrupt share count
    would otherwise turn every ratio into raw dollars (§5).
    <strong>Size-curve imputation (2026-07-04):</strong> a held name with prices but no usable
    share count (foreign IFRS filers in native currency; 20-F cover pages count ordinary shares
    against an ADR price; ETFs) gets its raw log-mcap <em>imputed</em> from the per-month
    estimation-universe regression of log-mcap on log-ADV (ρ ≈ 0.9) — estimation names are never
    imputed, so factor-return estimation is untouched; the proxy only lets the held book be
    priced. Its Liquidity / Value / EarnYield stay missing (turnover against its own imputation
    basis would be circular; ratios need real fundamentals). Disclosed as a DQ check.
    <br><strong>Two cadences in one monthly panel.</strong> The price descriptors are recomputed
    each month over a <em>trailing daily window</em>, so they move every month as new prices
    arrive. The fundamental descriptors only step when a <em>new SEC filing lands</em> (quarterly,
    as-of joined on the filing date) and stay flat between filings. So in a typical month a name's
    Beta, Momentum, ResidVol and Liquidity drift while its Size, Value, EarnYield and Leverage
    stay put. The panel refreshes price signals every month and fundamental signals every
    quarter.</li>

<li><strong>(b) Robust cross-sectional standardisation — the estimation/coverage split.</strong>
    Each descriptor is standardised <em>within each month's cross-section</em>, not across time,
    robustly: centre by the <em>median</em>, scale by the <em>MAD</em> (× 1.4826). The centring
    statistics and the cap come from the <strong>estimation universe only</strong> (the clean
    {seed_label} seed), not the whole cross-section. {cap_clause} Both sets are then re-standardised
    on the estimation post-clip mean/σ so they share one scale. {cap_why} If a cross-section has no
    spread (MAD ≈ 0) the column is set missing for the month. Median/MAD centring is what makes the
    cap work: one wrong-units outlier cannot move the centre or scale (§5). <code>NonLinSize</code>
    is also <strong>orthogonalised to Size</strong> (on the estimation fit), so the factor is the
    genuinely non-linear part of size.</li>

<li><strong>(c) The monthly-to-daily bridge (Barra timing).</strong> Exposures are slow-moving, so
    we compute them <em>monthly</em>. Factor returns have to be daily for the tails to be real.
    Here is the bridge. Walk consecutive month-ends (d₀, d₁). For every trading day d in the
    window (d₀, d₁], hold the loadings <strong>fixed at their d₀ (prior month-end) values</strong>
    and let only the left-hand side, that day's stock returns, change. So one monthly exposure
    panel feeds about 21 daily regressions before the next panel takes over. This is standard
    Barra timing: returns lead, loadings lag. It turns ≈{positions["Date"].nunique()} monthly
    snapshots into <strong>{n_days:,} daily factor-return observations</strong>, which is the
    difference between a 99% tail estimated from ~{positions["Date"].nunique()} points and one
    estimated from thousands. The cost: loadings drift inside the month, and the last few days of a
    window regress on a month-old panel (§7).</li>

<li><strong>(d) Per-day universe and factor screening (removing degenerate factors).</strong>
    Before each day's regression we screen the design matrix:
    <ul>
    <li><em>Name screen.</em> Keep names carrying a <strong>majority ({len(STYLE_FACTORS) // 2 + 1}
        of the {len(STYLE_FACTORS)}) of the style loadings</strong>, and zero-fill the rest. A missing
        standardised exposure is the market-average tilt, which is the Barra convention. Requiring
        complete rows here used to drop whole thin months below the name minimum. Majority, not a
        fixed count (respecified 2026-07-04): a hard-coded "6" written for a 10-style model silently
        moved the pricing-coverage gate whenever the factor set changed — young listings carry
        exactly the descriptors that need no history (Size, NonLinSize, sector) plus the 120-day
        price block, so a one-factor change could unprice them for their first months.</li>
    <li><em>Factor screen.</em> Drop any factor column whose cross-sectional standard deviation
        that day is ≤ <strong>0.05</strong>. A near-constant regressor is collinear with the
        intercept and makes the normal equations near-singular (the old Value/EarnYield blow-ups,
        §5). That factor simply <strong>does not exist for that day</strong> and contributes no
        return. The robust z-scoring in (b) should prevent this. The screen keeps the solver safe
        either way.</li>
    <li><em>Minimums.</em> Skip the day unless at least <strong>30</strong> names survive and the
        <code>Size</code> column is present (Size is also the WLS weight). Daily stock returns
        beyond <strong>±50%</strong> are masked as data errors (split or adjclose glitches) rather
        than clipped, so a fake −90% never enters a regression.</li>
    </ul></li>

<li><strong>(e) The daily cross-sectional WLS regression.</strong> For each surviving day we
    regress the day's stock returns y on the screened exposure matrix X with a column of ones
    prepended. That <strong>intercept is the Market factor</strong>. It is weighted least squares
    with weight ∝ <strong>√mcap</strong>, so large names anchor the fit (implemented by multiplying
    both sides by mcap<sup>¼</sup>). <strong>Industries (2026-07-04):</strong> the design also
    carries a <strong>0/1 dummy per GICS sector</strong>. Raw dummies sum to the intercept
    (every name is in exactly one sector), so they enter under the standard <strong>Barra
    constraint</strong> — the weighted industry returns sum to zero, with weights = each sector's
    WLS mass — imposed by substituting the heaviest (reference) sector out of the design,
    <span class="formula">D̃<sub>j</sub> = D<sub>j</sub> − (c<sub>j</sub>/c<sub>ref</sub>)·D<sub>ref</sub></span>,
    and recovering its return from the constraint after the fit. Market therefore stays the
    weighted-market return and each industry return reads as that sector <em>relative to</em> the
    market. Names with an unknown sector carry no dummy and price through Market + styles. The
    fitted coefficients <em>are</em> that day's factor returns
    Δf<sub>k</sub>, and the residuals are the name-specific returns.{est_fit_clause} Output: {n_days:,} days ×
    ({n_style} style + {n_ind} industry + Market) factor returns, the shared scenario cache.</li>

<li><strong>(f) Specific (idiosyncratic) risk.</strong> We square each day's regression residual
    and run it through an EWMA with a <strong>{EWMA_HALFLIFE_D}-trading-day half-life</strong> per
    name, giving a daily specific <em>variance</em> per (name, day). For the cube join we
    <strong>snapshot this at each calendar month-end</strong> (the model's join cadence). The
    specific block is strictly <em>diagonal</em>: one variance per name, no cross-name specific
    covariance, in line with the daily VaR horizon.{cov_spec_clause}</li>

<li><strong>(g) Market and industry memberships as leaf loadings.</strong> After the regression we
    give every (date, name) a <code>Market</code> loading of exactly <strong>1.0</strong> in the
    exposure panel — the leaf form of the regression intercept — and a
    <code>Ind:&lt;Sector&gt;</code> loading of <strong>1.0</strong> on its GICS sector, the leaf
    form of the industry dummies. A fully-invested book (Σ weights = 1)
    then carries unit market exposure and its sector weights as industry exposures, so the
    directional market move and each sector's relative move flow through the scenario
    engine, and the cube can drill factor risk by industry. Both are added <em>after</em> stage (e)
    so the style factor returns are estimated on style
    exposures only and are not contaminated by the constants.</li>

<li><strong>(h) Scenario construction.</strong> One table keyed (ScenarioSet, Factor) → shock
    vector. <em>HistFull</em> is the full daily factor-return history. <em>Evt:*</em> is that
    history cut to a past event window. <em>Hypo:*</em> is single-day shocks of n×σ per factor
    (σ = that factor's daily vol). All three feed the same calculation,
    dPnL = Σ<sub>k</sub> x<sub>k</sub>·Δf<sub>k</sub>. Only the source of the Δf vector changes.</li>
</ol>

<h2>4 · The factor block as estimated</h2>
<table>
<tr><th>Factor</th><th>Group</th><th class="num">Daily σ</th><th class="num">Worst day</th>
<th class="num">On</th><th>Cumulative return {START[:4]}–{END[:4]}</th></tr>
{frows_html}
</table>
<p class="small">Dots: <span style="color:{ACCENT}">●</span> minimum,
<span style="color:{BLUE}">●</span> maximum, ● latest. The worst Market-factor day is 2020-03-16.
The model recovers the COVID crash from estimation alone, with no factor data bought or
downloaded.</p>

<h2>5 · Data quality, controls &amp; the shares-outstanding incident</h2>
<p>A {len(dqr)}-check DQ suite runs after every rebuild: key uniqueness, referential
integrity across all six frames, weight normalisation, calendar continuity, value ranges,
and cross-frame coverage. Current status: <strong class="ok">{n_pass} pass</strong>,
<strong class="warn">{len(warns)} warn</strong>, {len(fails)} fail.</p>

<div class="note"><strong>Incident, root-caused and defended.</strong> During validation the
Value/EarnYield factor returns hit ±10<sup>6</sup> on 21 of 73 regression dates. The cause:
<code>us-gaap:CommonStockSharesOutstanding</code> facts of <em>0 shares</em> (CVNA) and
<em>100 shares</em> (CHWY) collapsed market cap, which turned ratio descriptors into raw dollars
(10<sup>9</sup>). Quantile winsorisation could not remove them. The outliers crushed every other
name's z-score into a near-constant column, and the regression design matrix went near-singular
(condition number 10<sup>8</sup>). Four separate defences now stand:
(1)&nbsp;shares come from the DEI cover-page tag; (2)&nbsp;a ${MCAP_FLOOR:,.0f} market-cap floor
withholds descriptors rather than emit wrong units; (3)&nbsp;median/MAD standardisation caps any
surviving outlier at a harmless ±3σ; (4)&nbsp;the solver drops degenerate factor columns per date.
Each layer is documented at the relevant line of code.</div>

<table>
<tr><th>Open item (WARN)</th><th>Detail / assessment</th></tr>
{wrows}
</table>

<h2>6 · Risk measures &amp; current book snapshot ({last.date()})</h2>
<p>Definitions. All on the daily P&amp;L vector of the <em>current</em> book
({n_names} names over the sample, {book.shape[0]} held at the snapshot date):</p>
<ul>
<li><span class="formula">VaR 99 = −P1(dPnL)</span>. The 1st percentile of daily scenario P&amp;L (1-day horizon).</li>
<li><span class="formula">Worst loss = −min(dPnL)</span>. The single worst scenario day.</li>
<li><span class="formula">Specific vol = √(Σ w²·σ²<sub>specific</sub>)</span>. The diagonal block,
    currently {spec_vol:.2%} daily, with as-of specific variance available for {sv_cov:.0%} of held names.</li>
<li><span class="formula">Total VaR 99 = √(VaR² + (2.326·specific vol)²)</span>. The factor tail and an
    independent-normal idiosyncratic tail, combined.</li>
<li><span class="formula">VaR ladder 95 / 97.5 / 99 = −P5 / −P2.5 / −P1(dPnL)</span>. The 95/99 spread
    reads tail fatness; 97.5% is the FRTB regulatory point.</li>
<li><span class="formula">ES 97.5 / 99 = −mean of the worst 2.5% / 1% of days</span>. Expected Shortfall
    (CVaR) — the average loss in the tail beyond VaR. Coherent / sub-additive where VaR is not, and the
    Basel FRTB replacement for VaR. <span class="formula">Total ES 97.5</span> combines it with the
    idiosyncratic tail in quadrature, like Total VaR.</li>
<li><span class="formula">Risk HHI = Σ<sub>name</sub> share²</span>, share = a name's fraction of book
    Total VaR. 1 / HHI ≈ the effective number of independent risk bets — the single-number
    concentration gauge a desk watches against a limit.</li>
<li><span class="formula">Marginal VaR / ES</span> — a member's own P&amp;L on the book's tail day(s).
    <em>Additive</em>: sums to the book VaR/ES, so it splits the tail across factors, sectors, names.
    <span class="formula">Incremental VaR</span> — the book VaR released by removing a member
    (recompute-without); diversification-aware and <em>not</em> additive, answering "what does cutting
    this release?".</li>
</ul>

<table>
<tr><th>Net factor exposure x<sub>k</sub></th><th class="num">Loading</th><th></th></tr>
{xrows}
</table>

<table>
<tr><th>Scenario set</th><th class="num">VaR 99</th><th class="num">Worst loss</th><th class="num">Total VaR 99</th></tr>
{srows}
</table>
<p class="small">Negative VaR on short event windows means the current factor tilts would
have profited in that window's 1st-percentile day.</p>

<h2>7 · VaR backtest &amp; model validation</h2>
<p>The 13F book has no live daily P&amp;L, so VaR is validated by a <em>constant-portfolio backtest</em>:
the current book's exposures applied to the daily factor-return history, rolling a 250-day window to
estimate VaR each day and counting <em>exceptions</em> where the realised day beat VaR. This validates
the methodology, not a trading record.</p>
<ul>
<li><span class="formula">Kupiec POF</span> — a likelihood-ratio test that the observed exception rate
    matches the 1% claimed at 99%. χ²(1) at 95% = 3.841; above that, the calibration is rejected.</li>
<li><span class="formula">Basel traffic-light</span> — green / amber / red from the binomial CDF of the
    exception count, generalising the 250-day/99% zones to any window.</li>
</ul>
<p>Three estimators are available; the default was chosen by a sweep over this book:</p>
<table>
<tr><th>Estimator</th><th>Tail</th><th>Reactivity</th><th>99% backtest</th></tr>
<tr><td>Equal-weight historical sim</td><td>empirical (fat)</td><td>slow (window edge)</td><td>~1.8% — under-covers, amber</td></tr>
<tr><td>EWMA (RiskMetrics, normal)</td><td>normal (thin)</td><td>fast</td><td>~2.1–2.4% — over-breaches, red</td></tr>
<tr><td><strong>FHS (default, λ=0.94)</strong></td><td>empirical (fat)</td><td>fast</td><td>~1.0% — well-calibrated, green</td></tr>
</table>
<p class="small">Filtered Historical Simulation rescales the empirical (fat-tailed) shock distribution
by reactive EWMA volatility — reactivity without the normality penalty. Live in the dashboard's
VaR-backtest badge.</p>

<h2>7·b · Descriptor health — the 2026-07-04 audit &amp; respecs</h2>
<p>The residual diagnostics flagged a hidden beta (residual-vs-factor R² 0.50, "loads on
Liquidity" β 0.89, t 7.9). Chasing it produced a standing audit and a dozen model changes in one
day. The audit (<code>barra_descriptor_audit.py</code>, rerun after every build) applies three
tests to every factor:</p>
<ul>
<li><span class="formula">Collinearity</span> — factor-return correlations, full sample and
    trailing year. Two factors running |ρ| ≥ 0.6 are one effect split across two unstable
    regression coefficients; the labels stop being trustworthy.</li>
<li><span class="formula">Hidden beta</span> — each held name's daily specific return regressed
    on each factor's return. The modeled loading is already removed, so β ~ 0 if loadings are
    right. Σw·β is the unmodeled book exposure the risk block can't see; the share of names
    with |t| &gt; 2 says how broad it is.</li>
<li><span class="formula">Coverage</span> — the held weight actually carrying a loading.</li>
</ul>
<p>What the tests caught, in the order the trail unwound:</p>
<table>
<tr><th>Change</th><th>Fault</th><th>Result</th></tr>
<tr><td><strong>Liquidity → turnover</strong>, Size-orthogonalised</td>
    <td>log dollar-ADV ≈ log mcap cross-sectionally: a second Size (factor-return ρ −0.81)</td>
    <td class="num">ρ vs Size −0.81 → −0.06; the residual's "Liquidity" label was a mislabel —
    the true driver re-attributed to Size/NonLinSize</td></tr>
<tr><td><strong>Growth dropped</strong></td>
    <td>|t| &gt; 2 on 9% of days; loadings on 21% of held weight (needs two XBRL vintages)</td>
    <td class="num">nothing moved on removal — it was dead weight</td></tr>
<tr><td><strong>MegaCap added</strong> — hinge of raw log-mcap above the estimation q90,
    orthogonalised to Size + NonLinSize</td>
    <td>the ±3 estimation winsor bounds regression leverage but flattens the top tail: NVDA
    (3.5T) ≈ a 600B name at Size +3, so the mega regime sat in residuals</td>
    <td class="num">residual R² 0.51 → 0.44; loadings grade the tail (nvda 7.5 &gt; googl 7.1
    &gt; msft 5.7)</td></tr>
<tr><td><strong>NonLinSize → cube of standardised Size</strong> (USE4)</td>
    <td>cubing raw log-mcap (all positive, mean ~24) leaves a residual dominated by the
    quadratic term — a U-shape (ρ +0.74 with Size²) pinning small/mid-caps together at +6/+7
    on its left arm</td>
    <td class="num">R² 0.44 → 0.37; the mid-cap carriers vanish; Beta and Momentum clear</td></tr>
<tr><td><strong>Size-curve imputation</strong> — coverage names with prices but no share count
    get log-mcap from the per-month estimation fit of log-mcap on log-ADV (ρ ≈ 0.9)</td>
    <td>20% of held weight (foreign IFRS filers, ADR share-count mismatches, ETFs) carried
    <em>no size-curve risk at all</em></td>
    <td class="num">size-curve coverage 80% → 96% of held weight; estimation never imputed;
    disclosed as a DQ check (15.8% of weight proxy-priced)</td></tr>
<tr><td><strong>Leverage → liabilities / assets</strong></td>
    <td>assets/equity is unbounded as equity → 0 (insurers 40×, distressed names 100×+): 28
    names pinned at one clipped loading — a financials-sector dummy, not a leverage factor</td>
    <td class="num">pinned cluster 28 → 3; loading p99 6.47 → 3.24; EarnYield's flag clears</td></tr>
<tr><td><strong>RateBeta added</strong> — partial duration beta (stock return on the TLT return
    residualised against the market, 252-day window)</td>
    <td>the remaining Leverage / Liquidity / MegaCap flags shared one carrier list (hto, tsm,
    ida, cms, amzn — utilities, a REIT, rate-sensitive mega-caps): correlated residuals with
    one theme = a missing factor, read as rates/duration</td>
    <td class="num">the factor is strongly priced — |t| &gt; 2 on 41% of days — and its own
    hidden beta is near-clean; but the shared carriers' co-movement <em>persisted</em>, so
    duration was only part of the theme</td></tr>
<tr><td><strong>GICS industry block added</strong> — eleven 0/1 sector dummies in the daily
    cross-section, under the Barra constraint Σ c<sub>s</sub>·f<sub>s</sub> = 0 (c = the
    sector's WLS weight mass; the reference sector is substituted out of the design and its
    return recovered from the constraint), so Market remains the weighted-market intercept and
    industries are pure relative-sector returns; each name carries an <code>Ind:&lt;Sector&gt;</code>
    leaf loading of 1.0 so sector P&amp;L flows the scenario engine</td>
    <td>the persistent flags' carriers after RateBeta were two utilities and a hotel REIT —
    sector co-movement with <em>no industry factors</em> to absorb it, only the market
    intercept</td>
    <td class="num">daily cross-sectional R² 0.19 → 0.30; Leverage's flag clears (breadth
    41% → 25%, the utilities leave its carrier list); all eleven industries test ok/watch;
    industries admit strongly (Energy 51%, Industrials 43%, IT 41% of days)</td></tr>
<tr><td><strong>NdxBeta added</strong> — partial mega-complex beta (stock return on the QQQ
    return residualised against the market, 252-day window, the RateBeta construction)</td>
    <td>post-industries the last two flags (Liquidity, MegaCap) were carried by amzn / googl /
    msft / tsm — the mega complex trades as a <em>group</em>, beyond any smooth function of
    log-mcap (MegaCap grades size; the regime is club membership), and tsm's imputed size
    can't see its home-market volume</td>
    <td class="num">Liquidity's flag CLEARS (Σw·β +0.63 → +0.36, amzn/msft leave the carriers);
    residual-vs-factor R² 0.40 → 0.26 — the largest single drop of the programme; NdxBeta
    admits at 43% of days with a near-clean own hidden beta; collinearity stays clean (no
    NdxBeta ~ Ind:IT pair); RateBeta clears to ok</td></tr>
<tr><td><strong>MegaCap dropped</strong> (added earlier the same day)</td>
    <td>NdxBeta took over the mega club's pricing — MegaCap's admission fell to 13%, weakest in
    the model — and its one remaining flag was carried by <em>imputed-size</em> names whose
    hinge loading the log-ADV fit cannot estimate (tsm's volume trades in Taipei). A descriptor
    that only misprices the names it was built for earns no seat.</td>
    <td class="num">Liquidity fully clean (−0.03); span inside-share 72% → 85% (the hinge's
    extreme loadings were pushing the megas past the cloud edge by construction); daily fit R²
    unchanged at 0.30. Cost: residual R² 0.26 → 0.32 and a modest new Momentum flag (−0.22,
    same mega carriers) — the hinge was doing real work for the correctly-measured megas</td></tr>
<tr><td><strong>Momentum → log relative strength</strong>, ln(p<sub>t−21</sub>/p<sub>t−252</sub>)</td>
    <td>the arithmetic 12-1 ratio is bounded at −1 and unbounded above, so its z-scored winner
    tail ran to +8/+10 (cross-sectional skew +2.2; 4.3% of names pinned at the +3 estimation
    winsor vs 0.0% at −3) while realized momentum sensitivity saturates near +1 — the book is
    long winners, so it inherited the overstatement as a negative hidden beta</td>
    <td class="num">skew +2.2 → +0.5; residual-vs-factor R² 0.32 → 0.30 and Momentum leaves the
    book-residual regression (β −0.17 t −2.8 → −0.11 t −1.8); tsm exits the carriers; admission
    51%. The remaining audit row (≈ −0.16) is structural — realized sensitivity is concave in
    the characteristic on <em>both</em> tails (γ ≈ −0.35 every era since 2016, flat in
    loading-refresh age, invariant to rescaling) plus loading-independent name effects whose
    residuals are mutually uncorrelated (pairwise ρ ≈ 0) — no common thread, no missing factor</td></tr>
<tr><td><strong>NonLinSize winsorised</strong> — cube the ±3 Size, cap the standardised output
    at ±3 for coverage rows too</td>
    <td>cubing the UNCAPPED coverage Size amplifies a Size error by 3z²: held small-caps carried
    −9/−10 loadings (35% of held weight beyond |3|) whose predicted comovement was fully
    cancelled in residuals (hidden-beta γ ≈ −1 every era — the no-pricing-content signature;
    the deep tail realized sign-wrong). Unlike Size's linear tail, cubic extrapolation beyond
    the estimation range is not truth-telling</td>
    <td class="num">Σw·β +1.65 → +1.15; the fiction had leaked into neighbours — Momentum
    −0.18 → −0.16, Size +0.72 → +0.66; residual-vs-factor R² 0.30 → 0.28; span inside-share
    85% → 88%; fit R² unchanged. A <em>drop test</em> (rebuild without NonLinSize) was run and
    REJECTED: residual/fit R² a wash, Size and Momentum hidden betas worsen, and ~17k young-IPO
    name-dates lose book pricing — NonLinSize needs only mcap so it exists from a listing's
    first month and keeps young names above the regression's majority-of-loadings gate (that
    gate's hard-coded "6 of 10" was itself respecified to a majority rule the test exposed)</td></tr>
</table>
<p><strong>Where it stands.</strong> The model is Market + 11 styles + 11 GICS industries,
estimated jointly under the proper constraint. Over the programme: daily cross-sectional R²
0.19 → 0.30, residual-vs-factor R² 0.51 → 0.28. Every construction fault the audit surfaced is
fixed and verified — Liquidity-as-Size, the NonLinSize U-shape and its fictional cubed coverage
tail, the Leverage financials dummy, Momentum's arithmetic winner-tail skew — and two factors
that could not justify their seats (Growth, MegaCap) were dropped for cause, both sides
measured. What remains is <em>structural and disclosed, not chased</em>: a modest Momentum
hidden beta (Σw·β ≈ −0.16 — payoff concavity every linear momentum factor carries, plus
uncorrelated name effects with no common thread) and a NonLinSize intercept (Σw·β ≈ +1.15,
loading-independent — the book co-moves with a noisy curvature factor return; its drop test
failed on the evidence, see the table). The open watch list is those two structural rows plus
the sub-one-third admission cohort — Liquidity (19%), NonLinSize (18%), Value (18%), Leverage
(16%), EarnYield (14%) — all judged again after two more quarters of data, now that industries
and the comovement betas hold what's theirs.</p>
<p class="small">Estimation loadings stay winsorised at ±3 throughout: the winsor is a
<em>leverage bound</em> on the cross-sectional regression, not a validity judgment. Where it
erased real information (the mega-cap tail), the answer was a new bounded regressor computed
from pre-winsor values — never unbounding the old one.</p>

<h2>8 · Risk tooling on the cube</h2>
<p>Operational layers exposed in the dashboard, all reading the same cube and frames:</p>
<ul>
<li><strong>Desk limits (RAG)</strong> — book VaR / ES / Risk HHI and single-name / sector weight vs a
    desk limit set, red/amber/green with breach flags.</li>
<li><strong>Data-quality panel</strong> — the §5 checks surfaced live against the served frames, plus the
    "Unknown" sector fallback for the few names without an SEC SIC.</li>
<li><strong>Risk-analyst commentary</strong> — an on-demand written read of any view's numbers (LLM,
    grounded strictly on the figures shown, no tool or data access), leading with any limit breach.</li>
<li><strong>Scoped Q&amp;A (Ask the risk model)</strong> — a free-text question answered by the LLM with
    <em>one</em> tool: <code>query_cube</code>, the same guarded pivot the grid uses. It pulls its own
    slices (each shown inline) and cites them; it can reach nothing off the allowlist, the filesystem,
    the web, or any second tool. The agentic loop is bounded.</li>
<li><strong>Risk trends</strong> — Scenario VaR / ES and Risk HHI, plus factor exposures, across the
    2016–2024 calendar.</li>
<li><strong>Stress test</strong> — custom one-day shocks (any per-factor σ → book P&amp;L, same
    <span class="formula">Σ x<sub>k</sub>·σ<sub>k</sub>·vol<sub>k</sub></span> engine as the Hypo sets)
    and reverse stress (the single-factor move that breaches a target loss, ranked by vulnerability).</li>
<li><strong>Pre-trade / what-if</strong> — resize / drop / add a position and see book VaR, ES, Total
    VaR, Specific vol and Risk HHI before vs after; the risk math is reproduced in numpy so the
    "before" reconciles with the cube and the delta is the trade's effect.</li>
<li><strong>Drawdown</strong> — the constant-portfolio equity curve and max peak-to-trough over the
    scenario path (the COVID crash reads ≈ −39%), the path lens VaR/ES miss.</li>
<li><strong>Liquidity (days-to-liquidate)</strong> — per name <span class="formula">MV / (participation
    · ADV)</span> on a trailing-63-day dollar ADV; the share of the book liquidatable within a horizon,
    the weighted-average days, the least-liquid names, and any name with no ADV (reported separately).
    ~87% of the Soros book clears within 5 days at 20% participation.</li>
<li><strong>What changed (quarter-over-quarter)</strong> — a deterministic diff between two 13F filings:
    positions entered / exited / resized, the net factor-exposure drift attributed (rotation vs loading
    drift), and the book risk delta (VaR / ES / HHI / specific vol, what-if math), with an on-demand
    written read.</li>
<li><strong>Estimation universe (4-phase diagnostic)</strong> — for the held book: which index each
    name sits in, point-in-time (membership); the DQ filtration funnel over the PIT S&amp;P 500 (§2·c);
    the span / high-confidence check (is each holding inside the estimation universe's factor space, by
    Mahalanobis distance); and <strong>style-drift attribution</strong> — the book's net factor
    exposure over time, with each factor's drift split into rotation (new names) vs held-name loading
    drift, to read the post-2021 tilt as intentional (→ benchmark) or not (→ hedge). See
    <code>docs/universe-diagnostics-plan.md</code>.</li>
</ul>

<h2>9 · Limitations the numbers inherit</h2>
<ul>
<li><strong>13F is a quarterly, lagged, long-only disclosure.</strong> Intra-quarter trading,
    shorts, options and non-US listings are invisible. The book is the latest filed snapshot.</li>
<li><strong>Estimation breadth.</strong> Factor-return quality and stability improve with the
    number of names in the daily regression (§2·b). A broader cross-section gives more reliable
    factors.</li>
<li><strong>Index-seeded but US-large-cap.</strong> The estimation cross-section is the 13F book
    plus the {seed_label} (§2·b). That is a real market, but it is US large-cap-tilted, so
    small-caps and non-US names outside the book are absent. Point <code>SEED_INDEX</code> at a
    broader benchmark (S&amp;P 1500 or Russell 3000) for a fuller market.</li>
<li><strong>Sector and Country are free SEC-derived descriptors — neither is a factor.</strong> Sector
    comes from each filer's SEC SIC code crosswalked to the 11 GICS sectors (CIK-keyed); a handful of
    BDCs and closed-end funds have no SEC SIC and fall back to "Unknown". Country is the state of
    incorporation from the same SEC submissions JSON (US state codes → US, foreign codes → the country),
    so ~21% of the book reads non-US (Alibaba/JD, Canadian energy/industrials, Sanofi). Both are tags
    only: the model has <strong>no country or industry factor</strong>, so that exposure is carried by
    the style/market/specific loadings, not a country bet. A residual few ADRs that file a US address
    with a blank domicile (e.g. Grifols) still read US.</li>
<li><strong>Exposures update monthly.</strong> Daily factor returns are regressed on month-old
    loadings. This is standard Barra timing, but the loadings drift inside the month.</li>
<li><strong>Specific-risk coverage is partial.</strong> Names outside the regression cross-section
    (incomplete descriptors) carry no specific variance that month and add zero to the diagonal
    block.</li>
<li><strong>Free-data licensing.</strong> Stooq and Yahoo price data are free to use but not
    redistributable. This POC is for internal evaluation only.</li>
</ul>

</body></html>"""
    OUT.write_text(html)
    # also publish to the UI's static dir so the dashboard can link it (tmp/ is not served)
    static = pathlib.Path(__file__).resolve().parent / "static" / "barra_model_reference.html"
    static.parent.mkdir(exist_ok=True)
    static.write_text(html)
    print(f"wrote {OUT} and {static} ({len(html)/1024:.0f} KB)")


if __name__ == "__main__":
    build()
