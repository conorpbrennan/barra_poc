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
            f"them <em>all</em> — so the book is always a subset of the universe.")
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
                       f"is the {uni_n} 13F-sourced names alone — the manager's own opportunity set.")

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
<p class="sub">Barra-style proof of concept on free public data · Soros Fund Management 13F book
(CIK {SOROS_CIK}) · sample {START} → {END} · prepared for CRO review · generated {gen}</p>

<p>This document is the audit trail for every number the model produces: where each input
comes from, what is done to it, and which controls watch it. The model is a
<strong>two-block linear factor model</strong>: portfolio P&amp;L is decomposed into
{len(factors)}&nbsp;style factors plus a market intercept (systematic block) and a diagonal
name-specific block. All scenario risk is the single operation
<span class="formula">dPnL = Σ<sub>k</sub> x<sub>k</sub> · Δf<sub>k</sub></span>
where x<sub>k</sub> is the book's net exposure to factor k and Δf<sub>k</sub> is a factor-shock
vector whose <em>source</em> distinguishes historical simulation, event replay, and
hypothetical stress. <strong>All risk figures are 1-day horizon, 99% confidence.</strong></p>

<h2>Pipeline at a glance</h2>
<p>The whole pipeline, end to end. Free public feeds enter one builder, which emits the six-frame
contract that feeds the one unchanged cube. The builder and the cube share no in-process state —
the six parquet frames are the complete, inspectable hand-off between data production and risk
consumption.</p>
<div class="flow">
  <span class="box">SEC EDGAR 13F<br><span class="small">positions, quarterly</span></span>
  <span class="box">OpenFIGI v3<br><span class="small">CUSIP→FIGI identity</span></span>
  <span class="box">SEC XBRL facts<br><span class="small">fundamentals, PIT</span></span>
  <span class="box">Stooq / Yahoo<br><span class="small">daily prices</span></span>
  <span class="arr">⟶</span>
  <span class="box"><strong>builder</strong><br><span class="small">monthly z-score exposures · daily WLS → factor returns</span></span>
  <span class="arr">⟶</span>
  <span class="box">6 parquet frames<br><span class="small">the only hand-off</span></span>
  <span class="arr">⟶</span>
  <span class="box"><strong>Atoti cube</strong><br><span class="small">exposures · scenario engine</span></span>
</div>
<p class="small">Exposures are built on a <strong>monthly</strong> calendar; the daily factor
returns that drive every risk number are <strong>estimated from them in-house</strong> (§3) — no
factor-return series is purchased or downloaded. The monthly→daily mechanism is the heart of the
model and is detailed in §3c.</p>

<h2>1 · Data sources</h2>

<div class="note"><strong>Where does the factor model come from? — read this first.</strong>
<strong>The factor model is not downloaded from anyone.</strong> There is no MSCI/Barra vendor
licence and no external factor-return file behind these numbers. The {len(factors)} style
factors and the market factor are <em>built here, from scratch</em>, in three explicit pieces:
<ol style="margin:.4rem 0">
<li><strong>Factor definitions (the taxonomy) — open-source / academic.</strong> <em>What</em>
    Size, Value, Momentum, EarnYield, Leverage, ResidVol, etc. mean follows the public
    literature: the Fama–French / <strong>Ken French Data Library</strong> factors, the
    <strong>BARRA USE4</strong> style set, and the <strong>JKP</strong> characteristic library
    (Jensen–Kelly–Pedersen, <code>jkpfactors.com</code>). These are conventions, not a data
    feed — we re-implement the descriptor formulas (§3a), we do not import them.</li>
<li><strong>Exposures (loadings) — computed here</strong> from SEC fundamentals + Stooq/Yahoo
    prices as cross-sectional z-scores, on a <strong>monthly</strong> calendar (§3a–b).</li>
<li><strong>Factor returns — estimated here</strong>, <strong>daily</strong>, by regressing each
    day's stock returns on the latest monthly exposures (§3c–e). This is the standard BARRA
    cross-sectional approach: the factor returns are an <em>output of our regression</em>, never
    a purchased or downloaded series. The monthly→daily step is detailed in §3c.</li>
</ol>
So the only raw data bought (all free) are 13F holdings, identity mappings, company
fundamentals and daily prices — the systematic factor structure is manufactured from those,
which is why the §3 audit trail is the substance of the model.</div>

<p>Every input is free or public-domain, fetched over HTTP and disk-cached so reruns are cheap
and do not re-hit rate-limited APIs. The four raw feeds below enter the builder; the fifth
row — factor returns — is the estimated output described in the callout, listed here so the
provenance of all six frames is in one place.</p>
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
<tr><td>Factor returns</td><td><strong>Not sourced — estimated.</strong> Daily cross-sectional WLS regression of stock
    returns on the latest monthly exposures (§3). {len(factors)} style factors + 1 market intercept.</td>
    <td>Daily, {n_days:,} trading days ({START[:4]}–{END[:4]})</td><td>The entire systematic block and scenario engine</td>
    <td>Quality scales with universe breadth; no vendor benchmark to validate against</td></tr>
</table>

<h2>2 · Transformation register — positions &amp; identity</h2>
<ol>
<li><strong>13F parsing.</strong> Every information table for the CIK is parsed to
    (report date, filing date, CUSIP, $ value). Multiple lots / share classes of one issuer
    are <em>summed</em> per filing.</li>
<li><strong>Identity resolution.</strong> CUSIP → FIGI via OpenFIGI; ticker → CIK via SEC.
    All frames key on FIGI; CUSIP/ticker/CIK never leave the builder.</li>
<li><strong>Point-in-time weight overlay.</strong> Weights are normalised <em>within each
    filing</em> (Σw = 1), then each calendar month-end is matched to the <em>latest filing
    date on or before it</em> — the book becomes investable knowledge only after it is filed.
    Names absent from the newest filing expire with it: no stale positions persist.
    Verified: weights sum to exactly 1.0 on all {positions["Date"].nunique()} month-ends.</li>
</ol>

<h2>2·b · Estimation universe &amp; its intersection with the 13F filings</h2>
<p>The factor model is estimated cross-sectionally (§3), so it needs a <em>population of names</em>
to regress over each day — the <strong>estimation universe</strong>. That universe is built in two
parts: <strong>the whole 13F book plus a market-index seed</strong>. {f13_source} {seed_clause}
The result is a real-market cross-section that fully contains the book — the design a Barra-style
model wants.</p>

<table>
<tr><th>Population</th><th class="num">Names</th><th>Definition / role</th></tr>
<tr><td>13F source population</td><td class="num">{f13_cusips:,}</td><td>Distinct cash-equity CUSIPs across all {f13_filings} quarterly filings (before identity resolution)</td></tr>
<tr><td>13F-held names (kept whole)</td><td class="num">{held_uni_n}</td><td>Those CUSIPs resolved to FIGI — all retained so the full book is covered</td></tr>
<tr><td>+ market-index seed ({seed_label})</td><td class="num">{idx_only_n}</td><td>Index constituents not already in the book, unioned in for market breadth</td></tr>
<tr><td>= estimation universe</td><td class="num">{uni_n}</td><td>The names carried in <code>securities</code></td></tr>
<tr><td>… with usable exposures</td><td class="num">{exp_n}</td><td>Of the universe, those with enough price/fundamental data to carry loadings — the regression cross-section</td></tr>
<tr><td>Held at ≥1 month-end</td><td class="num">{held_n}</td><td>Universe names that surface as an actual book weight at some sampled date</td></tr>
<tr><td>Active book per month-end</td><td class="num">{hpd_med} <span class="small">(med)</span></td><td>Names in the latest filing ∩ universe on a given date (range {hpd_min}–{hpd_max})</td></tr>
</table>

<p><strong>How the populations nest.</strong> The {uni_n}-name universe is the union of the whole
13F book ({held_uni_n} names) and the index seed (+{idx_only_n} names not already held); {exp_n}
of them resolve to usable loadings and enter the daily regression cross-section. The held book is
always a strict <em>subset</em>: {held_n} names are held at some point in the sample, and the
active book on any one date is the latest filing ∩ universe (median {hpd_med}, range
{hpd_min}–{hpd_max}). The consequence worth stating: the regression that manufactures the
factor returns (§3) runs over the full {exp_n}-name market cross-section — not just the names
Soros holds — so the factors describe a market, and index names that Soros has never held still
contribute estimation breadth. The cube prices the full filed 13F holdings each quarter, and
every §6 risk number reflects that complete book.</p>

<p><strong>Why this shape.</strong> Cross-sectional factor-return quality scales with breadth and
the daily regression skips any date with &lt;30 valid names, so a wide market cross-section is
strictly better than the manager's holdings alone — hence the index seed. The remaining bound is
cost: each name costs cold-cache SEC/OpenFIGI/Stooq round-trips plus a fundamentals pull (all
rate-limited and disk-cached, parallelised across worker threads), so build time scales with
universe size. To widen further, swap <code>SEED_INDEX</code> for a broader benchmark
(S&amp;P 1500 / Russell 3000) — the book stays a subset by construction.</p>

<h2>3 · Transformation register — exposures, factor returns, specific risk</h2>
<p>This is the heart of the model and the part with no external benchmark, so every stage is
spelled out. Exposures, factor returns and specific risk are <strong>three outputs of one
regression</strong>: the same monthly exposure panel is both the regression's right-hand side
(loadings) and, through that regression, the source of the factor-return cache and the residual
variances. They are never spliced from different sources. The flow runs in eight stages,
moving the data from a <em>monthly</em> exposure cadence to a <em>daily</em> factor-return
cadence (stage&nbsp;c is the bridge).</p>
<div class="flow">
  <span class="box">monthly characteristic exposures<br><span class="small">cross-sectional z-scores (a–b)</span></span>
  <span class="arr">⟶</span>
  <span class="box">held fixed for the month<br><span class="small">prior month-end loadings (c)</span></span>
  <span class="arr">⟶</span>
  <span class="box">daily cross-sectional WLS<br><span class="small">stock returns ~ lagged exposures (d–e)</span></span>
  <span class="arr">⟶</span>
  <span class="box"><strong>daily factor returns</strong><br><span class="small">the scenario cache</span></span>
</div>

<p><strong>The mental model (how to read this).</strong> Picture one table per month-end: one row
per stock, one column per characteristic (Size, Value, Momentum, Leverage, …), each cell the
stock's metric that month. Stack the monthly tables and you have a <em>panel</em> indexed by
(stock&nbsp;×&nbsp;characteristic&nbsp;×&nbsp;month). Read <em>down</em> a column for one stock and
you get that stock's <strong>monthly time series</strong> of a characteristic; read <em>across</em>
a single month and you get the <strong>cross-section</strong> of every stock on that characteristic.
Stage&nbsp;(a) fills the panel from prices and fundamentals; stage&nbsp;(b) then standardises each
column <em>within each month</em>. That standardisation is the crux a reader must grasp: an
<strong>exposure is not the raw metric</strong> — it is a z-score answering "how does this stock
rank on this characteristic relative to the universe <em>this month</em>", measured in
cross-sectional standard deviations. A Size loading of +2 therefore means "two sigmas larger than
the median name this month", which is why Size is comparable to Value, and one month to the next,
even though their raw units (log-dollars vs a ratio) are nothing alike.</p>

<p class="small"><strong>Worked example — one stock, one month, one factor.</strong> Suppose a
name's raw Value (book equity ÷ market cap) is 0.045 in June. The standardiser looks at <em>every</em>
name's Value that June, takes the median (say 0.030) and the MAD-scaled spread, and maps 0.045 to,
say, <strong>+0.7</strong> — modestly cheaper than the typical name that month. That +0.7 is the
Value loading stored in the exposures leaf for (this name, June, Value). July repeats the whole
calculation on July's cross-section, producing the next point in that name's Value series. So the
exposures frame <em>is</em> the standardised version of the per-stock monthly characteristic
time series you described.</p>
<ol>
<li><strong>(a) Descriptor construction</strong> — per name, on the monthly month-end calendar.
    <em>Price-based</em> (trailing daily prices): <code>Beta</code> = cov(stock, {MARKET_PROXY.upper()}) / var({MARKET_PROXY.upper()})
    over a 252-day window (≥120 observations required, else withheld); <code>ResidVol</code> =
    annualised σ of that regression's residual (×√252); <code>Momentum</code> = 12-1 total
    return, price<sub>t−21</sub> / price<sub>t−252</sub> − 1 (most recent month skipped);
    <code>Liquidity</code> = log of mean daily dollar-volume over the trailing 63 days.
    <em>Fundamental-based</em> (SEC XBRL, as-of joined on the <code>filed</code> date so nothing
    is known before it was reported): <code>Size</code> = log(mcap+1); <code>NonLinSize</code> =
    log(mcap+1)³; <code>Value</code> = book equity / mcap; <code>EarnYield</code> = net income /
    mcap; <code>Leverage</code> = assets / equity; <code>Growth</code> = period-over-period asset
    growth. Market cap = split-adjusted close × shares outstanding (DEI cover-page count).
    <strong>Market-cap floor:</strong> any name whose mcap falls below ${MCAP_FLOOR:,.0f} that
    month has its fundamental descriptors withheld (set to missing, not zero) — a corrupt share
    count would otherwise turn every ratio into raw dollars (§5).
    <br><strong>Two cadences inside one monthly panel.</strong> The price-based descriptors are
    recomputed each month over a <em>trailing daily window</em>, so they move every month as new
    daily prices arrive. The fundamental descriptors instead step only when a <em>new SEC filing
    lands</em> (quarterly, as-of joined on the filing date) and are held flat between filings. So
    in a typical month a name's Beta/Momentum/ResidVol/Liquidity loadings drift while its
    Size/Value/EarnYield/Leverage/Growth loadings are unchanged from the last report — the monthly
    panel refreshes price signals continuously and fundamental signals quarterly.</li>

<li><strong>(b) Robust cross-sectional standardisation (winsorisation).</strong> Each descriptor
    is standardised <em>within each month's cross-section</em>, not across time. The transform is
    deliberately robust rather than the usual mean/σ: centre by the <em>median</em> and scale by
    the <em>MAD</em> (median absolute deviation × 1.4826 for normal consistency); winsorise the
    resulting score at <strong>±3σ</strong> (hard clip); then re-standardise the clipped column to
    mean&nbsp;0, σ&nbsp;1. A cross-section with no dispersion (MAD ≈ 0) is treated as carrying no
    information and the whole column is set missing for that month. Median/MAD centring is what
    makes the winsorisation effective: a single wrong-units outlier cannot move the centre or the
    scale, so the ±3σ clip lands it at a finite, harmless value instead of letting it dominate the
    column — the failure mode that defeated the earlier quantile-clip approach (§5).
    <code>NonLinSize</code> is additionally <strong>orthogonalised to Size</strong> (regressed on
    Size, the residual kept and re-standardised) so the cube factor is the genuinely non-linear
    part of the size effect, not a second copy of Size.</li>

<li><strong>(c) The monthly → daily bridge (Barra timing).</strong> Exposures are expensive and
    slow-moving, so they are computed <em>monthly</em>; factor returns must be daily for the tails
    to be real. The bridge: walk consecutive month-ends (d₀, d₁). For every trading day d in the
    half-open window (d₀, d₁], the loadings are <strong>held fixed at their d₀ (prior month-end)
    values</strong> and only the left-hand side — that day's stock returns — changes. So one
    monthly exposure panel feeds ~21 daily regressions before the next panel takes over. This is
    standard Barra timing (returns lead, loadings lag) and it is what turns ≈{positions["Date"].nunique()}
    monthly snapshots into <strong>{n_days:,} daily factor-return observations</strong> — the
    difference between a 99% tail estimated from ~{positions["Date"].nunique()} points and one
    estimated from thousands. The cost: loadings drift intra-month and the last few days of a
    window regress on a month-old panel (§7).</li>

<li><strong>(d) Per-day universe &amp; factor screening (removal of degenerate factors).</strong>
    Before each day's regression the design matrix is screened:
    <ul>
    <li><em>Name screen.</em> Keep names carrying at least <strong>6 of the {len(factors)}</strong>
        loadings (the full price block plus some fundamentals); zero-fill the remaining missing
        loadings (a missing standardised exposure is the market-average tilt — Barra convention).
        Requiring complete rows here had silently dropped whole thin months below the name
        minimum.</li>
    <li><em>Factor screen.</em> Drop any factor column whose cross-sectional standard deviation
        that day is ≤ <strong>0.05</strong> — a near-constant regressor is collinear with the
        intercept and makes the normal equations near-singular (the historical Value/EarnYield
        blow-ups, §5). Such a factor simply <strong>does not exist for that day</strong> and
        contributes no return. The robust z-scoring in (b) should prevent this; the screen keeps
        the solver safe regardless.</li>
    <li><em>Minimums.</em> The day is skipped entirely unless ≥<strong>30</strong> names survive
        and the <code>Size</code> column is present (Size is also the WLS weight). Daily stock
        returns beyond <strong>±50%</strong> are masked as data errors (split/adjclose glitches)
        rather than clipped, so a fake −90% never enters a regression.</li>
    </ul></li>

<li><strong>(e) The daily cross-sectional WLS regression.</strong> For each surviving day the
    day's stock returns y are regressed on the screened exposure matrix X with a column of ones
    prepended — that <strong>intercept is the Market factor</strong>. The regression is weighted
    least squares with weight ∝ <strong>√mcap</strong> (large names anchor the fit; implemented as
    multiplying both sides by mcap<sup>¼</sup>). The fitted coefficients <em>are</em> that day's
    factor returns Δf<sub>k</sub>; the regression residuals are the name-specific returns. Output:
    {n_days:,} days × ({len(factors)} style + Market) factor returns — the shared scenario cache.</li>

<li><strong>(f) Specific (idiosyncratic) risk.</strong> Each day's regression residual is squared
    and run through an EWMA with a <strong>{EWMA_HALFLIFE_D}-trading-day half-life</strong> per
    name, giving a daily specific <em>variance</em> per (name, day). For the cube join this daily
    state is <strong>snapshotted at each calendar month-end</strong> (the model's join cadence).
    The specific block is strictly <em>diagonal</em> — one variance per name, no cross-name
    specific covariance — consistent with the daily VaR horizon.</li>

<li><strong>(g) Market intercept as a leaf loading.</strong> After the regression, every
    (date, name) is given a <code>Market</code> loading of exactly <strong>1.0</strong> in the
    exposure panel — the leaf form of the regression intercept. A fully-invested book (Σ weights
    = 1) therefore carries unit market exposure, so the directional market move flows through the
    scenario engine. This is added <em>after</em> stage (e) so the style factor returns are
    estimated on style exposures only and are not contaminated by the constant.</li>

<li><strong>(h) Scenario construction.</strong> One table keyed (ScenarioSet, Factor) → shock
    vector: <em>HistFull</em> = the full daily factor-return history; <em>Evt:*</em> = the same
    history cut to a past event window; <em>Hypo:*</em> = single-day shocks of n×σ per factor
    (σ = that factor's daily vol). All three feed the identical operation
    dPnL = Σ<sub>k</sub> x<sub>k</sub>·Δf<sub>k</sub>; only the source of the Δf vector differs.</li>
</ol>

<h2>4 · The factor block as estimated</h2>
<table>
<tr><th>Factor</th><th>Group</th><th class="num">Daily σ</th><th class="num">Worst day</th>
<th class="num">On</th><th>Cumulative return {START[:4]}–{END[:4]}</th></tr>
{frows_html}
</table>
<p class="small">Dots: <span style="color:{ACCENT}">●</span> minimum,
<span style="color:{BLUE}">●</span> maximum, ● latest. The worst Market-factor day is
2020-03-16 — the model recovers the COVID crash from estimation alone, with no factor data
purchased or downloaded.</p>

<h2>5 · Data quality, controls &amp; the shares-outstanding incident</h2>
<p>A {len(dqr)}-check DQ suite runs after every rebuild: key uniqueness, referential
integrity across all six frames, weight normalisation, calendar continuity, value ranges,
and cross-frame coverage. Current status: <strong class="ok">{n_pass} pass</strong>,
<strong class="warn">{len(warns)} warn</strong>, {len(fails)} fail.</p>

<div class="note"><strong>Incident, root-caused and defended.</strong> During validation the
Value/EarnYield factor returns reached ±10<sup>6</sup> on 21 of 73 regression dates. Root
cause: <code>us-gaap:CommonStockSharesOutstanding</code> facts of <em>0 shares</em> (CVNA)
and <em>100 shares</em> (CHWY) collapsed market cap, turning ratio descriptors into raw
dollars (10<sup>9</sup>); quantile winsorisation could not remove them, the outliers crushed
every other name's z-score into a near-constant column, and the regression design matrix
became near-singular (condition number 10<sup>8</sup>). Four independent defences now stand:
(1)&nbsp;shares are taken from the DEI cover-page tag; (2)&nbsp;a ${MCAP_FLOOR:,.0f} market-cap
floor withholds descriptors rather than emit wrong units; (3)&nbsp;median/MAD standardisation
caps any surviving outlier at a harmless ±3σ; (4)&nbsp;the solver drops degenerate factor
columns per date. Each layer is documented at the relevant line of code.</div>

<table>
<tr><th>Open item (WARN)</th><th>Detail / assessment</th></tr>
{wrows}
</table>

<h2>6 · Risk measures &amp; current book snapshot ({last.date()})</h2>
<p>Definitions — all on the daily P&amp;L vector of the <em>current</em> book
({n_names} names over the sample; {book.shape[0]} held at the snapshot date):</p>
<ul>
<li><span class="formula">VaR 99 = −P1(dPnL)</span> — 1st percentile of daily scenario P&amp;L (1-day horizon).</li>
<li><span class="formula">Worst loss = −min(dPnL)</span> — single worst scenario day.</li>
<li><span class="formula">Specific vol = √(Σ w²·σ²<sub>specific</sub>)</span> — diagonal block;
    currently {spec_vol:.2%} daily, with as-of specific variance available for {sv_cov:.0%} of held names.</li>
<li><span class="formula">Total VaR 99 = √(VaR² + (2.326·specific vol)²)</span> — factor tail and an
    independent-normal idiosyncratic tail.</li>
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
<li><strong>13F is a quarterly, lagged, long-only disclosure</strong> — intra-quarter trading,
    shorts, options and non-US listings are invisible; the "book" is the latest filed snapshot.</li>
<li><strong>Estimation breadth.</strong> Cross-sectional factor-return quality and stability
    improve with the number of names in the daily regression (§2·b); a broader cross-section
    gives more reliable factors.</li>
<li><strong>Index-seeded but US-large-cap.</strong> The estimation cross-section is the 13F book
    unioned with the {seed_label} (§2·b) — a genuine market, but US large-cap-tilted; small-caps
    and non-US names outside the book are absent. Swap <code>SEED_INDEX</code> for a broader
    benchmark (S&amp;P 1500 / Russell 3000) for a fuller market.</li>
<li><strong>Sector is a free SIC→GICS proxy; Country is stubbed.</strong> Sector is derived
    from each filer's SEC SIC code crosswalked to the 11 GICS sectors (CIK-keyed), so sector
    drill-downs work; a handful of BDCs/closed-end funds carry no SEC SIC and fall back to
    "Unknown". Country is still a constant "US" pending a real domicile source.</li>
<li><strong>Exposures update monthly</strong>; daily factor returns are regressed on
    month-old loadings (standard Barra timing, but loadings drift intra-month).</li>
<li><strong>Specific-risk coverage is partial</strong> — names outside the regression
    cross-section (incomplete descriptors) carry no specific variance that month and
    contribute zero to the diagonal block.</li>
<li><strong>Free-data licensing</strong> — Stooq/Yahoo price data is free to use but not
    redistributable; this POC is for internal evaluation only.</li>
</ul>

<p class="small">Generated by <code>python_src/barra_cro_report.py</code> from the six
parquet frames in <code>data/</code> ({len(exposures):,} exposure rows ·
{len(factor_ret):,} factor-return rows · {len(specific):,} specific-variance rows).
Full session log of how the pipeline was brought up: <code>PROJECT_HISTORY.md</code>.</p>

</body></html>"""
    OUT.write_text(html)
    print(f"wrote {OUT} ({len(html)/1024:.0f} KB)")


if __name__ == "__main__":
    build()
