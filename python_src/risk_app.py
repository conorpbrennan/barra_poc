"""
risk_app.py
===========
Streamlit frontend for the Barra factor-risk cube. PURE PRESENTATION — it owns no cube;
it calls the FastAPI backend (risk_api.py) over HTTP and renders the JSON.

Visual design follows Tufte + Stephen Few:
  * maximal data-ink — no gridlines, borders, backgrounds, or chartjunk;
  * muted grayscale, with colour used ONLY to encode (red = loss/short, slate = long);
  * sparkline small-multiples (one row per scenario set, independent y, last point dotted);
  * horizontal sorted bars with DIRECT value labels (no legends), a deviation reference
    line at the HistFull baseline; diverging bars for signed factor tilts;
  * serif type, generous whitespace, dense but quiet.

Two global slicers (As-of Date, Scenario) drive every panel; three tabs are three lenses
on the same guarded slice (Risk / Exposure / Validation).

Run (separate process from the backend):
    cd python_src
    BARRA_API=http://127.0.0.1:8010 ../barra/bin/streamlit run risk_app.py
"""
from __future__ import annotations
import os
import requests
import pandas as pd
import altair as alt
import streamlit as st

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")

# --- palette (muted; colour encodes, never decorates) ----------------------------------
INK, MUTE, GRID = "#1a1a1a", "#8a8a8a", "#d9d6cf"
BAR = "#6f6f6f"          # neutral magnitude
LOSS = "#9c3a2e"         # muted red — loss / short
LONG = "#3b5e8c"         # muted slate — long / positive
REF = "#9c3a2e"          # reference line

st.set_page_config(page_title="Barra Factor Risk — Soros 13F", layout="wide")

st.markdown("""
<style>
  /* force the Tufte/Few light ground regardless of any stored dark preference */
  .stApp, section.main, [data-testid="stAppViewContainer"] { background-color: #fdfdfb !important; }
  [data-testid="stSidebar"] { background-color: #f1efe9 !important; }
  .stApp, .stMarkdown, p, span, label, li, td, th { color: #1a1a1a; }
  html, body, [class*="css"] { font-family: Georgia, 'Iowan Old Style', 'Times New Roman', serif; }
  .block-container { max-width: 1080px; padding-top: 2.2rem; }
  #MainMenu, footer, header { visibility: hidden; }
  h1 { font-weight: 500; font-size: 1.55rem; letter-spacing: .01em; }
  h2, h3 { font-weight: 500; color: #2a2a2a; }
  .kpi-l { font-size:.70rem; letter-spacing:.06em; text-transform:uppercase; color:#8a8a8a; }
  .kpi-v { font-size:1.7rem; color:#1a1a1a; line-height:1.1; }
  .kpi-w { border-top:1px solid #1a1a1a; padding-top:.3rem; margin-right:1.2rem; }
  .cap   { color:#8a8a8a; font-size:.78rem; font-style:italic; }
  [data-testid="stDataFrame"] { font-family: Georgia, serif; }
</style>
""", unsafe_allow_html=True)


# --- Tufte/Few Altair theme ------------------------------------------------------------
def _theme():
    return {"config": {
        "view": {"stroke": "transparent", "continuousWidth": 380, "continuousHeight": 200},
        "background": "#fdfdfb",
        "font": "Georgia, 'Times New Roman', serif",
        "axis": {"grid": False, "domainColor": MUTE, "tickColor": MUTE, "labelColor": "#444",
                 "titleColor": "#444", "labelFontSize": 11, "titleFontSize": 11,
                 "titleFontWeight": "normal", "titlePadding": 6},
        "axisY": {"domain": False, "ticks": False, "labelPadding": 4},
        "axisX": {"domain": True, "ticks": True, "tickSize": 4},
        "bar": {"color": BAR},
        "line": {"color": INK, "strokeWidth": 1.1},
        "point": {"filled": True},
    }}
alt.themes.register("tufte_few", _theme)
alt.themes.enable("tufte_few")


# --- data access -----------------------------------------------------------------------
@st.cache_data(ttl=300)
def get(path: str, **params):
    r = requests.get(f"{API}{path}", params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def pct(x, nd=2):
    return "—" if x is None else f"{x * 100:.{nd}f}%"


# --- chart builders --------------------------------------------------------------------
def h_bars(df, val, cat, fmt=".2%", ref=None, diverge=False, height=24):
    """Horizontal sorted bars, direct value labels, optional deviation reference rule."""
    enc_y = alt.Y(f"{cat}:N", sort="-x" if not diverge else
                  alt.EncodingSortField(val, order="descending"), title=None)
    base = alt.Chart(df).encode(y=enc_y, x=alt.X(f"{val}:Q", title=None,
                                                 axis=alt.Axis(format=fmt)))
    if diverge:
        df = df.assign(_sign=df[val] < 0)
        base = alt.Chart(df).encode(
            y=enc_y, x=alt.X(f"{val}:Q", title=None, axis=alt.Axis(format=fmt)),
            color=alt.Color("_sign:N", scale=alt.Scale(domain=[False, True], range=[LONG, LOSS]),
                            legend=None))
        bars = base.mark_bar(height=12)
        lab = base.mark_text(align="left", baseline="middle", dx=4, color="#444", fontSize=10
                             ).encode(text=alt.Text(f"{val}:Q", format="+.3f"),
                                      x=alt.X(f"{val}:Q"))
    else:
        bars = base.mark_bar(height=height, color=BAR)
        lab = base.mark_text(align="left", baseline="middle", dx=4, color="#444", fontSize=10
                             ).encode(text=alt.Text(f"{val}:Q", format=fmt))
    layers = [bars, lab]
    if ref is not None:
        layers.append(alt.Chart(pd.DataFrame({val: [ref]})).mark_rule(
            color=REF, strokeDash=[4, 3], strokeWidth=1).encode(x=f"{val}:Q"))
    return alt.layer(*layers).properties(height=max(120, 26 * len(df)))


def sparkline_multiples(df, val_fmt=".1%"):
    """One sparkline per scenario set: independent y, last point dotted (Tufte convention).

    Faceting a LAYERED chart needs one shared data source — so we flag the last point per
    set with `is_last` and filter the dot/label layers, rather than passing separate frames.
    """
    df = df.copy()
    last_idx = df.sort_values("date").groupby("set").tail(1).index
    df["is_last"] = df.index.isin(last_idx)
    line = alt.Chart().mark_line(strokeWidth=1, color=INK).encode(
        x=alt.X("date:T", axis=None), y=alt.Y("value:Q", axis=None))
    dot = alt.Chart().mark_point(color=LOSS, size=22).encode(
        x="date:T", y="value:Q").transform_filter("datum.is_last")
    txt = alt.Chart().mark_text(align="left", dx=6, color="#444", fontSize=10).encode(
        x="date:T", y="value:Q", text=alt.Text("value:Q", format=val_fmt)
    ).transform_filter("datum.is_last")
    return (alt.layer(line, dot, txt, data=df).properties(height=34, width=300)
            .facet(row=alt.Row("set:N", title=None,
                               header=alt.Header(labelAngle=0, labelAlign="left",
                                                 labelFontSize=11, labelColor="#444")))
            .resolve_scale(y="independent"))


# --- header / slicers ------------------------------------------------------------------
try:
    meta = get("/meta")
except Exception as e:
    st.error(f"Backend not reachable at {API} — start risk_api first.\n\n{e}")
    st.stop()

st.title("Barra Factor Risk · Soros 13F")
hc1, hc2, _ = st.columns([1, 1, 3])
date = hc1.selectbox("As-of date", meta["dates"], index=len(meta["dates"]) - 1)
sset = hc2.selectbox("Scenario", meta["scenario_sets"],
                     index=meta["scenario_sets"].index("HistFull") if "HistFull" in meta["scenario_sets"] else 0)

tab_risk, tab_exp, tab_val = st.tabs(["Risk", "Exposure", "Validation"])

# --- Risk ------------------------------------------------------------------------------
with tab_risk:
    r = get("/risk", date=date, set=sset)
    kpis = [("Total VaR 99", r.get("total_var")), ("Factor VaR 99", r.get("factor_var")),
            ("Specific vol", r.get("specific_vol")), ("Worst 1-day loss", r.get("worst_loss"))]
    cols = st.columns(4)
    for c, (lab, v) in zip(cols, kpis):
        c.markdown(f"<div class='kpi-w'><div class='kpi-l'>{lab}</div>"
                   f"<div class='kpi-v'>{pct(v)}</div></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='cap'>Book-level · scenario <b>{sset}</b> · as of <b>{date}</b> · "
                "Total VaR = √(Factor VaR² + (2.326·Specific vol)²)</div>", unsafe_allow_html=True)
    st.write("")

    cL, cR = st.columns([1, 1])
    with cL:
        st.subheader("Scenario comparison")
        sc = pd.DataFrame(get("/scenarios", date=date))
        if len(sc):
            href = sc.loc[sc["ScenarioSet"] == "HistFull", "Scenario VaR 99"]
            ref = float(href.iloc[0]) if len(href) else None
            st.altair_chart(h_bars(sc, "Scenario VaR 99", "ScenarioSet", ref=ref),
                            use_container_width=True)
            st.markdown("<div class='cap'>VaR 99 by mode; dashed line = HistFull baseline. "
                        "Same engine — history / event replay / hypothetical.</div>",
                        unsafe_allow_html=True)
    with cR:
        st.subheader("Risk over time")
        allts = []
        for s in meta["scenario_sets"]:
            for pt in get("/timeseries", set=s, measure="Total VaR 99"):
                allts.append({**pt, "set": s})
        ts = pd.DataFrame(allts)
        if len(ts):
            ts["date"] = pd.to_datetime(ts["date"])
            st.altair_chart(sparkline_multiples(ts), use_container_width=True)
            st.markdown("<div class='cap'>Total VaR 99 per scenario set across "
                        f"{ts['date'].nunique()} COBs; dot = latest.</div>", unsafe_allow_html=True)

    st.subheader("Scenario P&L path")
    path = get("/scenario_pnl", date=date, set=sset)
    if path.get("n", 0) > 1:
        pp = pd.DataFrame(path["points"]); pp["date"] = pd.to_datetime(pp["date"])
        line = alt.Chart(pp).mark_line(strokeWidth=0.7, color=INK).encode(
            x=alt.X("date:T", title=None), y=alt.Y("pnl:Q", title=None, axis=alt.Axis(format="%")))
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=MUTE, strokeWidth=0.5).encode(y="y:Q")
        var = alt.Chart(pd.DataFrame({"y": [-path["var99"]]})).mark_rule(
            color=REF, strokeDash=[4, 3], strokeWidth=1).encode(y="y:Q")
        wd = pd.to_datetime(path["worst"]["date"])
        worst = alt.Chart(pd.DataFrame({"date": [wd], "pnl": [path["worst"]["pnl"]]})).mark_point(
            color=LOSS, size=40).encode(x="date:T", y="pnl:Q")
        st.altair_chart((zero + line + var + worst).properties(height=240), use_container_width=True)
        st.markdown(f"<div class='cap'>Each point = one date's P&amp;L under <b>{sset}</b>; "
                    f"dashed line = −VaR 99 ({pct(path['var99'])}); "
                    f"dot = worst loss {pct(-path['worst']['pnl'])} on "
                    f"<b>{path['worst']['date']}</b>. (P&amp;L vector × its date-axis dual.)</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<div class='cap'>Hypothetical sets are a single shocked point — no path. "
                    "Pick a historical or event scenario.</div>", unsafe_allow_html=True)

    st.subheader("Risk attribution")
    by = st.radio("Break down by", list(meta["by_levels"]), horizontal=True, index=1,
                  label_visibility="collapsed")
    at = pd.DataFrame(get("/attribution", date=date, set=sset, by=by))
    if len(at):
        sort_col = "Scenario VaR 99" if "Scenario VaR 99" in at else at.columns[-1]
        at = at.sort_values(sort_col, ascending=False).head(15)
        by_col = {"country": "Country", "sector": "Sector", "issuer": "Issuer", "position": "Ticker"}
        cat = by_col.get(by, at.columns[0])
        cat = cat if cat in at.columns else at.columns[0]
        st.altair_chart(h_bars(at, "Scenario VaR 99", cat), use_container_width=True)
        st.markdown("<div class='cap'>Standalone VaR 99 per group — top 15. "
                    "VaR is <b>not</b> additive across groups.</div>", unsafe_allow_html=True)

# --- Exposure --------------------------------------------------------------------------
with tab_exp:
    st.subheader("Net factor exposure")
    ex = pd.DataFrame(get("/exposures", date=date))
    if len(ex):
        st.altair_chart(h_bars(ex, "Net exposure", "Factor", fmt="+.2f", diverge=True),
                        use_container_width=True)
        st.markdown("<div class='cap'>Signed book tilt per factor (the Barra story). "
                    "<span style='color:#3b5e8c'>slate = long</span> · "
                    "<span style='color:#9c3a2e'>red = short</span>.</div>", unsafe_allow_html=True)

    st.subheader("Position detail")
    pos = pd.DataFrame(get("/attribution", date=date, set=sset, by="position"))
    if "Net exposure" in pos:                       # held names only (non-zero book weight)
        pos = pos[pos["Net exposure"].abs() > 0]
    if len(pos):
        labels = {(row.get("Ticker") or row["Position"]): row["Position"] for _, row in pos.iterrows()}
        pick = st.selectbox("Position", sorted(labels))
        d = get("/position", date=date, position=labels[pick])
        m1, m2, m3 = st.columns(3)
        for c, (lab, v) in zip([m1, m2, m3],
                               [("Weight", pct(d.get("weight"))),
                                ("Specific var", "—" if d.get("specific_var") is None else f"{d['specific_var']:.2e}"),
                                ("Sector", d.get("sector") or "—")]):
            c.markdown(f"<div class='kpi-w'><div class='kpi-l'>{lab}</div>"
                       f"<div class='kpi-v' style='font-size:1.2rem'>{v}</div></div>", unsafe_allow_html=True)
        st.markdown(f"<div class='cap'>{d.get('issuer','')} · {d.get('ticker','')} · "
                    f"{d.get('country','')}</div>", unsafe_allow_html=True)
        ld = pd.DataFrame(d.get("loadings", []))
        if len(ld):
            st.altair_chart(h_bars(ld, "Loading", "Factor", fmt="+.2f", diverge=True),
                            use_container_width=True)

# --- Validation ------------------------------------------------------------------------
with tab_val:
    st.subheader("Cube vs Excel / pandas reconciliation")
    v = get("/validation")
    st.markdown("<div class='cap'>3-position sub-book: " +
                ", ".join(f"{b['ticker'].upper()} {b['weight']*100:.1f}%" for b in v["book"]) +
                f" · as of {v['as_of']}</div>", unsafe_allow_html=True)
    rows = []
    for x in v["rows"]:
        rows.append({
            "ScenarioSet": x["ScenarioSet"],
            "Cube VaR99": pct(x["cube_var99"]), "Ref VaR99": pct(x["ref_var99"]),
            "Δ bps": "—" if x["ref_var99"] is None else f"{(x['cube_var99'] - x['ref_var99']) * 1e4:+.1f}",
            "Cube worst": pct(x["cube_worst"]), "Ref worst": pct(x["ref_worst"]),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.markdown("<div class='cap'>Cube = Atoti engine · Ref = independent pandas/Excel formula "
                "(barra_excel_check.py) · Δ in basis points should be ≈ 0.</div>", unsafe_allow_html=True)
