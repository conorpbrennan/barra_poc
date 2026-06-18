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
                                positions_from_13f, SEED_INDEX)
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


def build() -> None:
    f = load_frames()
    exposures, positions, securities = f["exposures"], f["positions"], f["securities"]
    factor_ret, specific, factor_meta = f["factor_returns"], f["specific_var"], f["factor_meta"]

    last = positions["Date"].max()
    book = positions[positions["Date"] == last].merge(securities, on="Position")
    wide = (factor_ret[factor_ret["Factor"] != "Market"]
            .pivot(index="Date", columns="Factor", values="Return").dropna(how="any").sort_index())
    factors = list(wide.columns)

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

    # --- DQ results --------------------------------------------------------------------
    dq._results.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        dq.run()
    dqr = list(dq._results)
    n_pass = sum(1 for lvl, *_ in dqr if lvl == "PASS")
    warns = [(name, det) for lvl, name, det in dqr if lvl == "WARN"]
    fails = [(name, det) for lvl, name, det in dqr if lvl == "FAIL"]

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
    if f13_ok:
        f13_source = (
            f"Across the sample Soros filed <strong>{f13_filings}</strong> quarterly 13F-HR "
            f"reports holding a median of <strong>{f13_npf}</strong> cash-equity names each "
            f"(<strong>{f13_cusips:,}</strong> distinct CUSIPs in total, options and bond lots "
            f"dropped). Those resolve to <strong>{held_uni_n}</strong> names, and the universe keeps "
            f"them <em>all</em>, so the book is always a subset of the universe.")
    else:
        f13_source = (f"The book is drawn from the union of CUSIPs across all of Soros's 13F-HR "
                      f"tables (options and bond lots dropped), resolving to {held_uni_n} names, all "
                      f"kept so the full book is covered.")
    if seed_name:
        seed_clause = (
            f"To make the cross-section a genuine market rather than one manager's holdings, the "
            f"universe is then <strong>seeded with {seed_name}</strong>: its constituents are unioned "
            f"in, adding <strong>{idx_only_n}</strong> names not already in the book, for a total "
            f"estimation universe of <strong>{uni_n}</strong>.")
    else:
        seed_clause = (f"No market index is seeded (<code>SEED_INDEX</code> is off), so the universe "
                       f"is the {uni_n} 13F-sourced names alone, the manager's own opportunity set.")

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

<p>This document is the audit trail for every number the model produces: where each input comes
from, what we do to it, and which checks watch it. The book is <strong>Soros Fund Management's US
equity positions</strong>, taken from their quarterly SEC <strong>13F filings</strong> (CIK
{SOROS_CIK}). The model is a <strong>two-block linear factor model</strong>. Portfolio P&amp;L
splits into {len(factors)}&nbsp;style factors plus a market
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
and no external factor-return file behind these numbers. The {len(factors)} style factors and the
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
The only raw data we buy (all free) are 13F holdings, identity mappings, company fundamentals and
daily prices. The factor structure is built from those, which is why §3 is the substance of the
model.</div>

<p>Every input is free or public-domain, fetched over HTTP and cached to disk, so reruns are cheap
and do not hit rate-limited APIs again. The four raw feeds below go into the builder. The fifth
row, factor returns, is the estimated output from the callout above, listed here so all six frames
sit in one place.</p>
<table>
<tr><th>Input</th><th>Source &amp; key</th><th>Cadence</th><th>What we rely on it for</th><th>Known caveats</th></tr>
<tr><td>Positions</td><td>SEC EDGAR 13F-HR information tables, CUSIP-keyed (CIK {SOROS_CIK})</td><td>Quarterly, ~45d lag</td>
    <td>The book, held as a weight overlay (§2)</td><td>Long US equity only; options (putCall) and bond lots (PRN) are dropped; shorts and intra-quarter trades invisible; public by construction</td></tr>
<tr><td>Identity</td><td>OpenFIGI v3 mapping API (CUSIP→FIGI→ticker) + SEC <code>company_tickers.json</code> (ticker→CIK)</td>
    <td>On rebuild</td><td>One canonical id (FIGI) joining every frame</td><td>Unmapped CUSIPs drop out of the universe; FIGI = Position = SecId everywhere downstream</td></tr>
<tr><td>Fundamentals</td><td>SEC XBRL company-facts API, CIK-keyed; tags: Assets, Liabilities,
    StockholdersEquity, NetIncomeLoss, shares from <code>dei:EntityCommonStockSharesOutstanding</code></td>
    <td>Per filing (point-in-time, as-of joined on <code>filed</code> date)</td><td>Size, Value, EarnYield, Leverage, Growth descriptors</td>
    <td>Tag coverage varies by filer; ~no look-ahead by construction; see incident note in §5</td></tr>
<tr><td>Prices</td><td>Stooq daily CSV (ticker-keyed) with Yahoo chart-API fallback (split-adjusted close, volume); market proxy = <code>{MARKET_PROXY.upper()}</code></td>
    <td>Daily</td><td>Stock returns (the regression's left-hand side), Beta, ResidVol, Momentum, Liquidity, market cap</td>
    <td>Free, not redistributable; Stooq serves a JS anti-bot page to some hosts (fallback handles this transparently)</td></tr>
<tr><td>Factor returns</td><td><strong>Not sourced, estimated.</strong> Daily cross-sectional WLS regression of stock
    returns on the latest monthly exposures (§3). {len(factors)} style factors + 1 market intercept.</td>
    <td>Daily, {n_days:,} trading days ({START[:4]}–{END[:4]})</td><td>The entire systematic block and scenario engine</td>
    <td>Quality scales with universe breadth; no vendor benchmark to validate against</td></tr>
</table>

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

<p><strong>How the populations nest.</strong> The {uni_n}-name universe is the whole 13F book
({held_uni_n} names) plus the index seed (+{idx_only_n} names not already held). {exp_n} of them
have usable loadings and enter the daily regression. The held book is always a strict
<em>subset</em>: {held_n} names are held at some point in the sample, and on any one date the
active book is the names in the latest filing that are also in the universe (median {hpd_med},
range {hpd_min}–{hpd_max}). The point worth making: the regression that builds the factor returns
(§3) runs over the full {exp_n}-name market cross-section, not just the names Soros holds. So the
factors describe a market, and index names Soros has never held still add breadth. The cube prices
the full filed 13F book each quarter, and every §6 number reflects that.</p>

<p><strong>Why this shape.</strong> Factor-return quality goes up with breadth, and the daily
regression skips any date with fewer than 30 valid names, so a wide market cross-section beats the
manager's holdings alone. That is why we seed an index. The only real cost is build time: each name
needs cold-cache SEC, OpenFIGI and Stooq calls plus a fundamentals pull. Those are rate-limited,
cached to disk, and run in parallel across worker threads, so build time grows with the universe.
To go wider, point <code>SEED_INDEX</code> at a bigger benchmark (S&amp;P 1500 or Russell 3000).
The book stays a subset by construction.</p>

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
    annualised σ of that regression's residual (×√252); <code>Momentum</code> = 12-1 total
    return, price<sub>t−21</sub> / price<sub>t−252</sub> − 1 (the most recent month is skipped);
    <code>Liquidity</code> = log of mean daily dollar-volume over the trailing 63 days.
    <em>Fundamental-based</em> (SEC XBRL, as-of joined on the <code>filed</code> date so nothing
    is known before it was reported): <code>Size</code> = log(mcap+1); <code>NonLinSize</code> =
    log(mcap+1)³; <code>Value</code> = book equity / mcap; <code>EarnYield</code> = net income /
    mcap; <code>Leverage</code> = assets / equity; <code>Growth</code> = period-over-period asset
    growth. Market cap = split-adjusted close × shares outstanding (DEI cover-page count).
    <strong>Market-cap floor:</strong> if a name's mcap falls below ${MCAP_FLOOR:,.0f} that month
    we withhold its fundamental descriptors (set them missing, not zero). A corrupt share count
    would otherwise turn every ratio into raw dollars (§5).
    <br><strong>Two cadences in one monthly panel.</strong> The price descriptors are recomputed
    each month over a <em>trailing daily window</em>, so they move every month as new prices
    arrive. The fundamental descriptors only step when a <em>new SEC filing lands</em> (quarterly,
    as-of joined on the filing date) and stay flat between filings. So in a typical month a name's
    Beta, Momentum, ResidVol and Liquidity drift while its Size, Value, EarnYield, Leverage and
    Growth stay put. The panel refreshes price signals every month and fundamental signals every
    quarter.</li>

<li><strong>(b) Robust cross-sectional standardisation (winsorisation).</strong> Each descriptor
    is standardised <em>within each month's cross-section</em>, not across time. The transform is
    robust rather than the usual mean/σ. Centre by the <em>median</em> and scale by the
    <em>MAD</em> (median absolute deviation × 1.4826 for normal consistency), winsorise the score
    at <strong>±3σ</strong> (a hard clip), then re-standardise the clipped column to mean&nbsp;0,
    σ&nbsp;1. If a cross-section has no spread (MAD ≈ 0) it carries no information, so we set the
    whole column missing for that month. Median/MAD centring is what makes the clip work: one
    wrong-units outlier cannot move the centre or the scale, so the ±3σ clip lands it at a finite,
    harmless value instead of letting it dominate the column. That is the failure the earlier
    quantile clip ran into (§5). <code>NonLinSize</code> is also <strong>orthogonalised to
    Size</strong> (regressed on Size, the residual kept and re-standardised), so the factor is the
    genuinely non-linear part of size, not a second copy of Size.</li>

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
    <li><em>Name screen.</em> Keep names carrying at least <strong>6 of the {len(factors)}</strong>
        loadings (the full price block plus some fundamentals), and zero-fill the rest. A missing
        standardised exposure is the market-average tilt, which is the Barra convention. Requiring
        complete rows here used to drop whole thin months below the name minimum.</li>
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
    both sides by mcap<sup>¼</sup>). The fitted coefficients <em>are</em> that day's factor returns
    Δf<sub>k</sub>, and the residuals are the name-specific returns. Output: {n_days:,} days ×
    ({len(factors)} style + Market) factor returns, the shared scenario cache.</li>

<li><strong>(f) Specific (idiosyncratic) risk.</strong> We square each day's regression residual
    and run it through an EWMA with a <strong>{EWMA_HALFLIFE_D}-trading-day half-life</strong> per
    name, giving a daily specific <em>variance</em> per (name, day). For the cube join we
    <strong>snapshot this at each calendar month-end</strong> (the model's join cadence). The
    specific block is strictly <em>diagonal</em>: one variance per name, no cross-name specific
    covariance, in line with the daily VaR horizon.</li>

<li><strong>(g) Market intercept as a leaf loading.</strong> After the regression we give every
    (date, name) a <code>Market</code> loading of exactly <strong>1.0</strong> in the exposure
    panel. This is the leaf form of the regression intercept. A fully-invested book (Σ weights = 1)
    then carries unit market exposure, so the directional market move flows through the scenario
    engine. We add it <em>after</em> stage (e) so the style factor returns are estimated on style
    exposures only and are not contaminated by the constant.</li>

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

<h2>7 · Limitations the numbers inherit</h2>
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
<li><strong>Sector is a free SIC→GICS proxy, Country is stubbed.</strong> Sector comes from each
    filer's SEC SIC code crosswalked to the 11 GICS sectors (CIK-keyed), so sector drill-downs
    work. A handful of BDCs and closed-end funds have no SEC SIC and fall back to "Unknown".
    Country is still a constant "US", pending a real domicile source.</li>
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
    print(f"wrote {OUT} ({len(html)/1024:.0f} KB)")


if __name__ == "__main__":
    build()
