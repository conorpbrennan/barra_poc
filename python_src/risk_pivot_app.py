"""
risk_pivot_app.py
=================
Second Streamlit UI — a PROPER PIVOT over the same Atoti backend (risk_api.py).

Where risk_app.py is a curated dashboard, this is an explorer: pick Row fields, Column
fields, and Measures; pin Date / Scenario slicers; get a live pivoted matrix + heatmap.
It calls the generic /pivot endpoint, which keeps the cube's guardrails (scalar measures
only; scenario measures need a ScenarioSet context — the UI warns when that's missing).

Pivot configurations ("views") can be SAVED to an on-disk repository (`views/` at the repo
root), organized in folders, and reloaded from a collapsible tree panel. Views are pure
presentation state (the 12 control fields) serialized to JSON; file I/O is client-side here,
consistent with the cube/UI separation in CLAUDE.md (no new API endpoints).

Run on its own port, separate process:
    cd python_src
    BARRA_API=http://127.0.0.1:8010 ../barra/bin/streamlit run risk_pivot_app.py --server.port 8502
"""
from __future__ import annotations
import os
import re
import json
import shutil
import datetime as _dt
from pathlib import Path
import requests
import numpy as np
import pandas as pd
import streamlit as st
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode, GridUpdateMode

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
_VL5 = "https://vega.github.io/schema/vega-lite/v5.json"   # Vega-Lite spec schema (chart JSON)
PCT_MEASURES = {"Scenario VaR 99", "Scenario worst loss", "Scenario mean PnL",
                "Specific vol", "Total VaR 99",
                "Marginal Scenario VaR 99", "Marginal Total VaR 99",
                "Incremental Scenario VaR 99", "Incremental Total VaR 99",
                "% of Scenario VaR 99", "% of Total VaR 99"}   # fractions -> ×100 when "as %"
# (VaR sensitivity is a per-unit ∂VaR/∂exposure decimal, NOT a NAV fraction — left as-is)
# Date display format (strftime / d3-time-format codes — compatible for these common patterns),
# shared by the grid (Date labels) and the graph builder (temporal axis). fmt -> example label.
DATE_FMTS = {"%Y-%m-%d": "2024-12-31", "%d %b %Y": "31 Dec 2024", "%b %Y": "Dec 2024",
             "%b %d, %Y": "Dec 31, 2024", "%m/%d/%Y": "12/31/2024", "%Y": "2024"}
DATE_FMT_DEFAULT = "%Y-%m-%d"

# ---- chart-spec builder: individual UI items (mark / X / series) -> a Vega-Lite spec. Pure data
# (the produced spec is JSON embedded in the view); the generic renderer draws it unchanged.
_PAL = ["#2a4d69", "#c46d4e", "#6b8f71", "#9a7aa0", "#b8902a", "#7da7c8", "#8a8a8a"]
_SERIF = "Georgia, 'Iowan Old Style', serif"
_THEME = {"background": "#fdfdfb", "view": {"stroke": None},
          "axis": {"grid": False, "domainColor": "#d8d5cd", "tickColor": "#d8d5cd",
                   "labelColor": "#44423d", "titleColor": "#1a1a1a",
                   "labelFont": _SERIF, "titleFont": _SERIF, "titleFontWeight": "normal"},
          "legend": {"labelColor": "#1a1a1a", "titleColor": "#1a1a1a",
                     "labelFont": _SERIF, "titleFont": _SERIF},
          "title": {"color": "#1a1a1a", "font": _SERIF, "fontWeight": "normal"}}
_FIT = {"type": "fit", "contains": "padding"}
_PAD = {"left": 22, "top": 5, "right": 14, "bottom": 5}
MARKS = ["line", "bar", "area", "point"]
_XTYPES = ["temporal", "nominal", "ordinal", "quantitative"]
_YFMT = {"percent": "%", "decimal": ".2f", "thousands": ",.0f", "none": None}   # label -> d3 format
_ACCENT = "#c0392b"   # VaR / worst markers
# per-graph items the builder exposes. A graph carries the NAME of the query (its "source") it
# references; that query's rows/measures live in the separate Queries section and drive the
# graph's X/Series options.
_GB_FIELDS = ["source", "mark", "point", "x", "xtype", "xtitle", "meas", "yfmt", "ytitle",
              "title", "height", "legend"]


def _gb_defaults(rows: list, meas_opts: list) -> dict:
    """Appropriate defaults for a NEW graph given its query's rows/measures, so it is a complete,
    valid spec out of the box. The query NAME is seeded separately (it picks an existing query)."""
    real = [r for r in (rows or []) if r != "Measures"]
    x = real[0] if real else None
    pct = bool(meas_opts) and all(m in PCT_MEASURES for m in meas_opts)
    return {"mark": "line", "point": True,
            "x": x, "xtype": ("temporal" if x == "Date" else "nominal"), "xtitle": "",
            "meas": list(meas_opts),
            "yfmt": ("percent" if pct else "none"),
            "ytitle": ("% of NAV" if pct else "value"),
            "title": "", "height": 380, "legend": True}


def _spec_from(g: dict, date_fmt: str = DATE_FMT_DEFAULT, all_rows: list = None):
    """Reconstruct a COMPLETE Vega-Lite spec from a graph's UI items over its query's tidy records.
    Every query is a /pivot query, so this is ONE uniform path: X = a row field, Y = measure(s),
    and any row dim NOT used as X becomes a `detail` channel so the chart groups EXACTLY as the
    cube did (no implicit client-side aggregation). `date_fmt` styles any temporal axis."""
    yax = {"format": _YFMT[g["yfmt"]]} if _YFMT.get(g.get("yfmt")) else {}
    mark = {"type": g["mark"]}
    if g["mark"] in ("line", "area") and g.get("point"):
        mark["point"] = {"size": 22}
    spec = {"$schema": _VL5, "mark": mark, "height": int(g.get("height") or 380),
            "autosize": _FIT, "padding": _PAD, "config": _THEME}
    if (g.get("title") or "").strip():
        spec["title"] = g["title"].strip()

    measures = [m for m in (g.get("meas") or []) if m]
    if not g.get("x") or not measures:
        return None
    enc = {"x": {"field": g["x"], "type": g.get("xtype", "nominal"),
                 "title": (g.get("xtitle") or None)}}
    if g.get("xtype") == "temporal":                     # apply the shared date format to the axis
        enc["x"]["axis"] = {"format": date_fmt}
    if len(measures) > 1:                                # >1 measure -> fold into a colour series
        spec["transform"] = [{"fold": measures, "as": ["Measure", "Value"]}]
        enc["y"] = {"field": "Value", "type": "quantitative", "axis": yax,
                    "title": (g.get("ytitle") or None)}
        enc["color"] = {"field": "Measure", "type": "nominal",
                        "scale": {"domain": measures, "range": _PAL[:len(measures)]},
                        "legend": ({"orient": "top", "title": None} if g.get("legend") else None)}
    else:
        enc["y"] = {"field": measures[0], "type": "quantitative", "axis": yax,
                    "title": (g.get("ytitle") or None)}
    # force the chart grouping == the cube query grouping: any row dim not on X becomes `detail`,
    # so every cube row is its own mark and Vega can't implicitly aggregate over the others.
    _extra = [d for d in (all_rows or []) if d and d != g.get("x")]
    if _extra:
        enc["detail"] = [{"field": d, "type": "nominal"} for d in _extra]
    spec["encoding"] = enc
    return spec


def _build_specs(graphs: list, queries: list, date_fmt: str = DATE_FMT_DEFAULT):
    """Build the view's `chart` from per-graph items + the NAMED `queries`. Each graph names a
    query; we look it up (its rows drive the `detail` grouping), build the spec, and tag it with
    `"source": <query name>`. Returns a single spec or a list (parallel to graphs)."""
    by_name = {q["name"]: q for q in (queries or []) if isinstance(q, dict) and "name" in q}
    specs = []
    for g in (graphs or []):
        q = (by_name.get(g.get("source"))
             or (queries[0] if queries else {"name": "Query 1", "rows": []}))
        all_rows = [r for r in (q.get("rows") or []) if r != "Measures"]
        s = _spec_from(g, date_fmt, all_rows)
        if not s:
            continue
        specs.append({"source": q.get("name") or "Query 1", **s})   # graph references its query by name
    if not specs:
        return None
    return specs[0] if len(specs) == 1 else specs


def _queries_from_state(state: dict) -> list:
    """The view's `queries` (self-contained pivot queries). If a view predates the model and only
    has a legacy `source` feed-list, migrate it: a scenario_pnl feed -> a ScenarioDay query over the
    per-day measures (its breakout becomes a second row dim); a bare pivot feed -> inherit the view's
    own rows/cols/measures/filters. Lets old saved views still load."""
    qs = state.get("queries")
    if isinstance(qs, list):
        return qs
    if isinstance(qs, dict):
        return [qs]
    src = state.get("source")
    if not src:
        return []
    src_list = src if isinstance(src, list) else [src]
    vrows = [r for r in (state.get("rows") or []) if r != "Measures"]
    vflt = dict(state.get("filters") or {})
    out = []
    for i, s in enumerate(src_list):
        s = s if isinstance(s, dict) else {}
        name = s.get("name") or f"Query {i + 1}"
        if s.get("endpoint") == "scenario_pnl":
            rows = ["ScenarioDay"] + ([s["breakout"]] if s.get("breakout") else [])
            out.append({"name": name, "rows": rows, "cols": [],
                        "measures": ["Scenario PnL at day", "Scenario date at day (epoch)",
                                     "Scenario VaR line at day", "Scenario worst pnl at day",
                                     "Scenario worst date at day (epoch)"], "filters": vflt})
        else:
            out.append({"name": name, "rows": vrows, "cols": list(state.get("cols") or []),
                        "measures": list(state.get("measures") or []), "filters": vflt})
    return out


_FMT_LABEL = {v: k for k, v in _YFMT.items() if v}   # d3 format -> our label ("%"->"percent" …)


def _primary_unit(spec: dict) -> dict:
    """The main mark+encoding of a spec — descend through layer/concat to the data-bearing mark
    (the line/bar/area/point with an x field), ignoring marker layers (rules/points)."""
    if not isinstance(spec, dict):
        return {}
    if "layer" in spec:
        for lyr in spec["layer"]:
            mk = lyr.get("mark")
            mt = mk.get("type") if isinstance(mk, dict) else mk
            if mt in MARKS and ((lyr.get("encoding", {}) or {}).get("x", {}) or {}).get("field"):
                return lyr
        return spec["layer"][0] if spec["layer"] else spec
    for key in ("vconcat", "hconcat", "concat"):
        if spec.get(key):
            return _primary_unit(spec[key][0])
    return spec


def _items_from_spec(spec: dict) -> dict:
    """Best-effort REVERSE of _spec_from: read a graph's UI items back out of a Vega-Lite spec, so
    a loaded view populates the builder. (Marker layers a spec may also carry aren't represented in
    the item set — the raw JSON keeps the full spec; editing an item rebuilds from items only.)"""
    spec = spec or {}
    unit = _primary_unit(spec)
    enc = unit.get("encoding", {}) or {}
    mk = unit.get("mark")
    mtype = (mk.get("type") if isinstance(mk, dict) else mk) or "line"
    x = enc.get("x", {}) or {}
    y = enc.get("y", {}) or {}
    meas = []
    for t in (unit.get("transform", []) or []) + (spec.get("transform", []) or []):
        if isinstance(t, dict) and t.get("fold"):
            meas = list(t["fold"])
    if not meas and y.get("field") and y.get("field") != "Value":
        meas = [y["field"]]
    color = enc.get("color", {}) or {}
    legend = not (isinstance(color, dict) and color.get("legend", "keep") in (None, False))
    return {"mark": mtype if mtype in MARKS else "line",
            "point": bool(isinstance(mk, dict) and mk.get("point")),
            "x": x.get("field"), "xtype": (x.get("type") if x.get("type") in _XTYPES else "nominal"),
            "xtitle": (x.get("title") or ""), "meas": meas,
            "yfmt": _FMT_LABEL.get((y.get("axis", {}) or {}).get("format"), "none"),
            "ytitle": (y.get("title") or ""),
            "title": (spec.get("title") if isinstance(spec.get("title"), str) else ""),
            "height": int(spec.get("height") or 380), "legend": legend}


def _gb_touch() -> None:
    """on_change marker: once the user edits any builder item, the builder owns pv_chart (so a
    freshly-LOADED view's spec is preserved until the user actually starts building)."""
    st.session_state["pv_gb_touched"] = True


def _filters_changed() -> None:
    """on_change marker for the Slicers: re-bake the new filters into the queries WITHOUT rebuilding
    the chart spec — so a scenario switch re-scopes the data and keeps any authored spec (e.g. the
    COVID loss-curve sort) intact. Distinct from `_gb_touch`, which DOES rebuild the spec."""
    st.session_state["pv_qbake"] = True

# ============================================================================ view repo
# The on-disk repository (save/load/list/folders) lives in views_repo.py — a pure file/JSON
# module with no Streamlit dependency, so it can be unit-tested directly (test_views_repo.py).
from views_repo import (                                          # noqa: E402
    SECTIONS, VIEWS_ROOT, SCHEMA_VERSION, STATE_FIELDS,
    ensure_root, slugify, folder_name, list_tree, all_folders, load_view,
    save_view, make_folder, rename_folder, delete_folder, rename_view, move_view, delete_view,
    json_safe_records)


# ============================================================================ page chrome
st.set_page_config(page_title="Flex Agg ++", layout="wide",
                   initial_sidebar_state="expanded")   # pivot selector visible by default
st.markdown("""
<style>
  /* force the Tufte/Few light ground regardless of any stored dark preference */
  .stApp, section.main, [data-testid="stAppViewContainer"] { background-color: #fdfdfb !important; }
  [data-testid="stSidebar"] { background-color: #f1efe9 !important; }
  /* repository toolbar: compact, equal-size icon buttons (refresh / new-folder / save) */
  .st-key-repo_toolbar button { white-space: nowrap !important; font-size: 1.05rem !important;
       min-height: 0 !important; padding: 1px 7px !important; line-height: 1.5; }
  /* overlay a small "+" badge on the folder icon (the 2nd toolbar column) */
  .st-key-repo_toolbar [data-testid="stHorizontalBlock"] > div:nth-child(2) button { position: relative; }
  .st-key-repo_toolbar [data-testid="stHorizontalBlock"] > div:nth-child(2) button::after {
       content: "+"; position: absolute; top: -1px; right: 3px;
       font-size: .62rem; font-weight: 700; color: #2a4d69; line-height: 1; }
  /* the 💾 emoji lives in an inner markdown <p> that sets its own size, so shrink THAT (the
     button-level font-size is overridden). scale() from center also pulls the glyph off the
     left border. the dropdown caret is a separate SVG -> unaffected, stays inside the border. */
  .st-key-repo_toolbar [data-testid="stHorizontalBlock"] > div:nth-child(3)
      [data-testid="stMarkdownContainer"] p {
       display: inline-block !important; transform: scale(0.78); transform-origin: center; }
  .st-key-repo_toolbar [data-testid="stHorizontalBlock"] > div:nth-child(3) button {
       padding-left: 10px !important; }
  .stApp, .stMarkdown, p, span, label, li, td, th { color: #1a1a1a; }
  html, body, [class*="css"] { font-family: Georgia, 'Iowan Old Style', 'Times New Roman', serif; }
  /* full window width (layout="wide"); the grid fills it and refits on resize */
  .block-container { max-width: 100%; padding: 2rem 2.5rem 0; }
  /* hide the menu/footer/toolbar for a clean ground, but NOT the whole header —
     the sidebar (pivot selector) expand arrow lives there; hiding it stranded a
     collapsed sidebar with no way to reopen. */
  #MainMenu, footer, [data-testid="stToolbar"], [data-testid="stDecoration"] { visibility: hidden; }
  /* always keep the sidebar collapse/expand controls reachable */
  [data-testid="stSidebarCollapsedControl"], [data-testid="stSidebarCollapseButton"],
  [data-testid="collapsedControl"], [data-testid="stExpandSidebarButton"] {
      visibility: visible !important; }
  h1 { font-weight: 500; font-size: 1.5rem; }
  .cap { color:#8a8a8a; font-size:.78rem; font-style:italic; }
  /* loaded-view name, shown atop the render area */
  .vtitle { font-size: 1.2rem; font-weight: 600; color:#1a1a1a; margin:.1rem 0 .15rem; }
  .vtitle .vfolder { font-size:.8rem; font-weight:400; color:#8a8a8a; }
  /* saved-views panel: quiet, tree-like */
  .vpanel h3 { font-size: 1rem; font-weight: 500; margin-bottom: .2rem; }
  .vpanel .stButton button { text-align: left; font-weight: 400; border: none;
       background: transparent; padding: 1px 4px; color: #2a4d69; }
  .vpanel .stButton button:hover { background: #ecebe4; color: #1a1a1a; }
  /* tighten the folder expanders so the tree reads as a tree, not stacked cards */
  .vpanel [data-testid="stExpander"] { border: none; box-shadow: none; }
  .vpanel [data-testid="stExpander"] details { border: none; }
  .vpanel [data-testid="stExpander"] summary { font-size: .85rem; padding: 2px 0; }
  .vpanel [data-testid="stExpander"] > details > div { border-left: 1px solid #e4e2dc;
       margin-left: 7px; padding-left: 7px; }
  /* selected-view metadata box (mirrors the jpeg's tooltip card) */
  .vmeta { border: 1px solid #d8d5cd; background: #f7f6f1; border-radius: 3px;
       padding: 6px 9px; font-size: .76rem; color: #44423d; margin: .3rem 0; }
  .vmeta b { color: #1a1a1a; font-weight: 600; }
  /* sidebar multiselects (Rows / Columns / Measures / Slicers / query fields): stack each selected
     member on its OWN full-width line so long names are readable — default BaseWeb lays the chips
     out as wrapping inline tags, which is cramped and truncated in the narrow sidebar. */
  [data-testid="stSidebar"] [data-baseweb="tag"] {
      flex: 1 0 100% !important; max-width: 100% !important; margin: 1px 0 !important; }
  [data-testid="stSidebar"] [data-baseweb="tag"] span { max-width: none !important; }
  /* ---- Overview landing: Tufte/Few scorecard ---- */
  .ov-head { font-size: 1.18rem; font-weight: 600; color: #1a1a1a; margin: .1rem 0 0; }
  .ov-asof { font-weight: 400; color: #8a8a8a; font-size: .9rem; }
  .sc-lab { font-size: .7rem; letter-spacing: .03em; text-transform: uppercase; color: #8a8a8a;
            white-space: nowrap; }
  .sc-val { font-size: 1.7rem; font-weight: 500; color: #1a1a1a; line-height: 1.25; }
  .ov-status { margin: .15rem 0; }
  .ov-status .dot { font-size: .82rem; }
  .ov-status .st-lab { font-size: .86rem; color: #44423d; }
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=300)
def get(path: str, **params):
    r = requests.get(f"{API}{path}", params=params, timeout=120)
    r.raise_for_status()
    return r.json()


try:
    dims = get("/dims")
except Exception as e:
    st.error(f"Backend not reachable at {API} — start risk_api first.\n\n{e}")
    st.stop()


def render_limits_banner():
    """Desk-limit RAG strip: a red/amber/green headline plus a per-limit detail table. Book-level
    status from /limits (latest date, config scenario set); silent if limits aren't configured or
    the call fails, so it never blocks the page."""
    try:
        lim = get("/limits")
    except Exception:
        return
    if not lim.get("configured"):
        return
    checks = lim.get("checks", [])
    n_br = len(lim.get("breaches", []))
    n_am = sum(1 for c in checks if c["status"] == "amber")
    head = f"Limits — {lim['book']} · {lim['set']} · {lim['date']}"
    status = lim["status"]
    if status == "breach":
        st.error(f"🔴 {head} — {n_br} breach(es)" + (f", {n_am} warning(s)" if n_am else ""))
    elif status == "amber":
        st.warning(f"🟠 {head} — {n_am} warning(s)")
    elif status == "green":
        st.success(f"🟢 {head} — all within limits")
    else:
        st.info(f"{head} — status unknown")
    with st.expander("Limit detail", expanded=(status == "breach")):
        def _pct(v):
            return "—" if v is None else f"{v:.2%}"
        rows = [{"Limit": c["name"], "Scope": c["scope"], "Value": _pct(c["value"]),
                 "Warn": _pct(c["warn"]), "Cap": _pct(c["limit"]),
                 "Status": {"breach": "🔴 BREACH", "amber": "🟠 WARN", "green": "🟢 OK"}.get(
                     c["status"], c["status"].upper()),
                 "Detail": c.get("detail") or ""} for c in checks]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def render_dq_badge():
    """Data-quality / trust strip: a pass/warn/fail headline from /dq plus a detail expander with
    every check, the known stubs, and each frame's latest date. Silent if /dq is unreachable."""
    try:
        dq = get("/dq")
    except Exception:
        return
    s = dq.get("summary", {})
    st_ = dq.get("status", "pass")
    head = (f"Data quality — {s.get('PASS',0)} pass · {s.get('WARN',0)} warn · {s.get('FAIL',0)} fail"
            f"  ·  data through {dq.get('latest_date',{}).get('positions','?')}")
    if st_ == "fail":
        st.error(f"🔴 {head}")
    elif st_ == "warn":
        st.warning(f"🟠 {head}")
    else:
        st.success(f"🟢 {head}")
    with st.expander("Data-quality detail", expanded=(st_ == "fail")):
        stubs = dq.get("stubs", {})
        st.caption(f"Known stubs: {stubs.get('country_stub_US', 0)}/{stubs.get('n_securities', 0)} names "
                   f"carry the Country=\"US\" stub · {stubs.get('sector_unknown', 0)} 'Unknown' sector. "
                   "Country is not yet a real crosswalk (see ROADMAP Step 14).")
        icon = {"PASS": "🟢", "WARN": "🟠", "FAIL": "🔴"}
        rows = [{"": icon.get(c["level"], ""), "Check": c["name"], "Detail": c.get("detail") or ""}
                for c in sorted(dq.get("checks", []), key=lambda c: {"FAIL": 0, "WARN": 1, "PASS": 2}[c["level"]])]
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def render_backtest_badge():
    """VaR backtest (model validation): Basel traffic-light + Kupiec result for the rolling-window
    backtest of the book's daily factor-P&L. Collapsed by default; silent if /backtest is down."""
    try:
        bt = get("/backtest")
    except Exception:
        return
    if bt.get("status") != "ok":
        return
    zone = bt["basel_zone"]
    conf = int(round(bt["alpha"] * 100))
    meth = {"fhs": f"FHS λ={bt.get('lam')}", "ewma": f"EWMA λ={bt.get('lam')}",
            "equal": "equal-weight HS"}.get(bt["method"], bt["method"])
    head = (f"VaR backtest ({conf}% / {bt['window']}d, {bt['set']}, {meth}) — "
            f"{bt['exceptions']} exceptions vs {bt['expected']} expected over {bt['tested']} days")
    icon = {"green": "🟢", "amber": "🟠", "red": "🔴"}.get(zone, "")
    line = f"{icon} {head} · Basel {zone.upper()}"
    (st.error if zone == "red" else st.warning if zone == "amber" else st.success)(line)
    with st.expander("Backtest detail", expanded=(zone == "red")):
        verdict = "REJECTS the model" if bt["kupiec_reject"] else "does not reject the model"
        st.caption(
            f"Kupiec POF likelihood ratio {bt['kupiec_LR']} vs χ²(1)@95% = {bt['kupiec_crit']} — {verdict}. "
            f"Constant-portfolio backtest: the current book's exposures applied to the daily factor-return "
            f"history, rolling a {bt['window']}-day window to estimate VaR. It validates the VaR "
            f"methodology, not a live P&L track record (the 13F book has none).")
        # FHS methodology note — expandable, default closed (popover; nested expanders aren't allowed).
        if bt["method"] == "fhs":
            with st.popover("ℹ️ FHS methodology"):
                st.markdown(
                    f"**Filtered Historical Simulation** — the default VaR estimator here "
                    f"(λ={bt.get('lam')}).\n\n"
                    "- Standardise each past daily return by its EWMA volatility estimate "
                    "(zₜ = rₜ / σₜ), so the history is read as shocks per unit of that day's vol.\n"
                    "- Take the empirical quantile of those standardised residuals — this keeps the "
                    "real fat tail, with **no normality assumption**.\n"
                    "- Rescale that quantile by **today's** EWMA vol, so the estimate reacts to the "
                    "current volatility regime.\n\n"
                    "It's the middle ground. Equal-weight historical sim under-covers — it treats a "
                    "calm day like a turbulent one. Parametric EWMA reacts to vol but over-breaches "
                    "on the fat tail by assuming normality. Over the Soros book at 99%, FHS λ=0.94 "
                    "lands near the expected ~1.0% exception rate (Kupiec green) where both of those "
                    "miss.")
        ed = bt.get("exception_dates", [])
        if ed:
            st.caption(f"Exception dates ({bt.get('n_exception_dates', len(ed))}):")
            st.dataframe(pd.DataFrame({"VaR breach date": ed}), hide_index=True, use_container_width=True)


def render_drawdown():
    """Drawdown lens: max peak-to-trough of the book's simulated cumulative P&L over the scenario
    path — a path risk VaR/ES don't show. Equity curve + underwater chart. Collapsed; silent if
    /drawdown is down. Constant-portfolio what-if (the held book over history), not a live record."""
    with st.expander("📉 Drawdown (constant-portfolio, over the scenario path)", expanded=False):
        sets = members.get("ScenarioSet") or ["HistFull"]
        sset = st.selectbox("Scenario set", sets,
                            index=(sets.index("HistFull") if "HistFull" in sets else 0),
                            key="pv_dd_set")
        try:
            dd = get("/drawdown", set=sset)
        except Exception as e:
            st.info(f"Drawdown unavailable: {e}")
            return
        if dd.get("status") != "ok":
            st.info("No usable path for this set (hypothetical sets are a single shock — pick HistFull "
                    "or an event set).")
            return
        rec = "recovered " + dd["recovery_date"] if dd["recovered"] else "not yet recovered"
        st.markdown(
            f"**Max drawdown {dd['max_drawdown']:.1%}** · peak {dd['peak_date']} → trough "
            f"{dd['trough_date']} ({dd['drawdown_obs']} trading days) · {rec} · longest underwater "
            f"{dd['longest_underwater_obs']} days")
        path = pd.DataFrame(dd.get("path", []))
        if not path.empty:
            path["Date"] = pd.to_datetime(path["date"]); path = path.set_index("Date")
            c1, c2 = st.columns(2)
            with c1:
                st.caption("Equity curve — book held over the factor path (start = 1.0)")
                st.line_chart(path[["equity"]])
            with c2:
                st.caption("Underwater — drawdown from the running peak (fraction)")
                st.area_chart(path[["drawdown"]])
        st.caption("Constant-portfolio what-if: the current book applied to the set's daily factor "
                   "returns, compounded. A path lens VaR/ES miss — not a live P&L track record.")


def render_trends():
    """Risk-over-time panel: book VaR 99 / ES 97.5 and Risk HHI across the whole calendar, plus the
    top style-factor exposures over time. One /trends call per dataset (cached); collapsed."""
    with st.expander("📈 Risk trends (2016–2024)", expanded=False):
        sets = members.get("ScenarioSet") or ["HistFull"]
        sset = st.selectbox("Scenario set", sets, index=(sets.index("HistFull") if "HistFull" in sets else 0),
                            key="pv_trend_set")
        try:
            rm = get("/trends", set=sset)
            fx = get("/trends", set=sset, measures="Net exposure", by="Factor")
        except Exception as e:
            st.info(f"Trends unavailable: {e}")
            return
        df = pd.DataFrame(rm.get("records", []))
        if df.empty:
            st.info("No trend data for this set.")
            return
        df["Date"] = pd.to_datetime(df["Date"]); df = df.set_index("Date")
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Tail risk — Scenario VaR 99 & ES 97.5 (fraction of book)")
            cols = [c for c in ("Scenario VaR 99", "Scenario ES 97.5") if c in df]
            st.line_chart(df[cols])
        with c2:
            st.caption("Concentration — Risk HHI (1 / HHI ≈ effective independent bets)")
            if "Risk HHI" in df:
                st.line_chart(df[["Risk HHI"]])
        fdf = pd.DataFrame(fx.get("records", []))
        if not fdf.empty and "Factor" in fdf.columns:
            fdf = fdf[fdf["Factor"] != "Market"]                       # Market ≈ 1.0, dwarfs styles
            piv = fdf.pivot_table(index="Date", columns="Factor", values="Net exposure", aggfunc="first")
            piv.index = pd.to_datetime(piv.index)
            top = list(piv.iloc[-1].abs().sort_values(ascending=False).head(6).index)
            st.caption("Style factor exposures (Net exposure) — top 6 by latest |exposure|, Market excluded")
            st.line_chart(piv[top])


def render_universe():
    """Estimation-universe diagnostic (Phase 1): which index each Soros holding sat in, per 13F
    filing, BITEMPORALLY (S&P 500 membership read as-of the filing's report date). Leads with the
    latest book's weight outside S&P 1500 — Chris's question on whether the book reaches names an
    SP1500 estimation universe wouldn't cover. Collapsed; silent if /universe is unbuilt/down."""
    with st.expander("🌐 Estimation universe — index membership of the book", expanded=False):
        try:
            u = get("/universe")
        except Exception as e:
            st.info(f"Universe membership unavailable: {e} — run barra_universe_membership.py to build it.")
            return
        series = u.get("series", [])
        if not series:
            st.info("No membership data.")
            return
        lat = u["latest"]
        out_w, unc_w = lat["outside_sp1500"], lat["unclassified"]
        st.markdown(
            f"**{out_w:.1%} of the book sits outside the S&P 1500** as of the {lat['report_date']} "
            f"filing ({lat['n_names']} names) — the coverage an SP1500 estimation universe would miss. "
            f"In S&P 500 {lat['split'].get('S&P 500', 0):.1%} · S&P 400/600 "
            f"{lat['split'].get('S&P 400/600', 0):.1%} · unclassified {unc_w:.1%}.")

        df = pd.DataFrame(series)
        df["Date"] = pd.to_datetime(df["report_date"]); df = df.set_index("Date")
        cols = [b for b in u["buckets"] if b in df]
        st.caption("Book weight by index bucket, per filing — S&P 500 is point-in-time; S&P 1500 is "
                   "current membership; 'Unclassified' is the identity-resolution gap (kept out of the "
                   "headline), which thins coverage on older filings.")
        st.bar_chart(df[cols])

        detail = u.get("detail", [])
        if detail:
            n_out = sum(1 for d in detail if d["bucket"] == "Outside S&P 1500")
            with st.expander(f"Names outside S&P 1500 / unclassified — {lat['report_date']} "
                             f"({n_out} outside, {len(detail) - n_out} unclassified)", expanded=False):
                dd = pd.DataFrame(detail)
                dd["weight"] = dd["weight"].map(lambda v: f"{v:.2%}")
                st.dataframe(dd[["issuer", "ticker", "cusip", "weight", "bucket"]],
                             hide_index=True, use_container_width=True)
        # --- Phase 2: the DQ filtration funnel over the PIT S&P 500 ---
        st.divider()
        st.markdown("**Estimation universe — DQ filtration funnel** (point-in-time S&P 500)")
        try:
            fn = get("/funnel")
        except Exception as e:
            st.info(f"Funnel unavailable: {e} — run barra_universe_funnel.py to build it.")
        else:
            fseries = fn.get("series", [])
            if not fseries:
                st.info("No funnel data.")
            else:
                fl, fsel = fn["latest"], fn["selected_date"]
                st.markdown(
                    f"Of the **{fl['population']} point-in-time S&P 500 names** at {fsel}, "
                    f"**{fl['survivors']} survive** the data-quality filters "
                    f"({fl['held_survivors']} of them held). {fl['data_unavailable']} data-unavailable. "
                    f"Near-flat by design — the S&P 500 is already curated, so the filters confirm a "
                    f"clean input rather than carve much away.")
                fdf = pd.DataFrame(fseries)
                fdf["Date"] = pd.to_datetime(fdf["month"]); fdf = fdf.set_index("Date")
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("Population vs survivors — most of the gap is data-availability "
                               "(delisted PIT members we never pulled), not filtering; the genuine "
                               "filter drops are the small per-stage counts at right.")
                    st.line_chart(fdf[["population", "survivors", "data_unavailable"]])
                with c2:
                    st.caption(f"Drops by filter stage — {fsel}")
                    drops = {s: fl["drops"][s] for s in fn["stages"] if fl["drops"].get(s)}
                    st.bar_chart(pd.Series(drops, name="dropped")) if drops else \
                        st.caption("No names dropped by a filter this month.")
                dropped = fn.get("dropped", [])
                if dropped:
                    with st.expander(f"Names dropped by a filter — {fsel} ({len(dropped)})",
                                     expanded=False):
                        st.dataframe(pd.DataFrame(dropped)[["issuer", "ticker", "stage_dropped",
                                     "mcap", "hist_days", "adv", "n_descriptors", "held"]],
                                     hide_index=True, use_container_width=True)
                with st.expander("Filter thresholds & unavailable stages", expanded=False):
                    cfg = fn.get("config", {})
                    st.caption("Thresholds (universe_filters.json): " + ", ".join(
                        f"{k}={cfg[k]}" for k in ("min_mcap", "min_hist_days", "min_adv",
                        "min_trade_freq", "min_descriptors") if k in cfg))
                    st.caption(f"Stability buffer (anti-churn): {cfg.get('buffer', {})}")
                    st.caption("Unavailable on free data (inert, disclosed): "
                               + ", ".join(fn.get("unavailable_stages", [])))
                    st.caption("• " + fn.get("note", ""))

        # --- Phase 3: span / high-confidence check (is the book inside the model's factor space?) ---
        st.divider()
        st.markdown("**Estimation universe — span / high-confidence check** "
                    "(does the book sit inside the model's factor space?)")
        st.session_state.setdefault("pv_span_fx", "Size")
        st.session_state.setdefault("pv_span_fy", "ResidVol")
        try:
            sp = get("/span", fx=st.session_state["pv_span_fx"], fy=st.session_state["pv_span_fy"])
        except Exception as e:
            st.info(f"Span check unavailable: {e} — run barra_universe_span.py to build it.")
        else:
            if not sp.get("series"):
                st.info("No span data.")
            else:
                sl = sp["latest"]
                st.markdown(
                    f"**{sl['inside_wt']:.0%} of the book sits inside** the estimation universe's factor "
                    f"space at {sp['selected_date']} ({sl['n_inside']}/{sl['n_held']} names) — exposures "
                    f"there are well-supported. The rest is extrapolation (small, higher-vol names beyond "
                    f"anything the S&P 500 spans). With coverage loadings uncapped, off-index holdings "
                    f"show their true extreme positions: ~82% of the book sits inside on average, "
                    f"dipping since 2021 (≈59% in 2022) — Chris's intentional-vs-not question (Phase 4).")
                sdf = pd.DataFrame(sp["series"])
                sdf["Date"] = pd.to_datetime(sdf["month"]); sdf = sdf.set_index("Date")
                c1, c2 = st.columns(2)
                with c1:
                    st.caption("Book weight inside the span over time")
                    st.line_chart(sdf[["inside_wt"]])
                with c2:
                    facs = sp["factors"]
                    fc1, fc2 = st.columns(2)
                    fc1.selectbox("x factor", facs, key="pv_span_fx")
                    fc2.selectbox("y factor", facs, key="pv_span_fy")
                    sc = sp["scatter"]
                    pts = [{"x": p["x"], "y": p["y"], "group": "estimation cloud"}
                           for p in sc["cloud"] if p["x"] is not None and p["y"] is not None]
                    pts += [{"x": p["x"], "y": p["y"],
                             "group": "book · inside" if p["inside"] else "book · outside"}
                            for p in sc["held"] if p["x"] is not None and p["y"] is not None]
                    st.caption(f"{sc['fx']} vs {sc['fy']} — estimation cloud vs the book")
                    if pts:
                        st.scatter_chart(pd.DataFrame(pts), x="x", y="y", color="group")
                outs = [d for d in sp.get("detail", []) if not d["inside"]]
                if outs:
                    with st.expander(f"Holdings outside the span (extrapolation) — "
                                     f"{sp['selected_date']} ({len(outs)})", expanded=False):
                        od = pd.DataFrame(outs)
                        od["weight"] = od["weight"].map(lambda v: f"{v:.2%}")
                        od["d2"] = od["d2"].map(lambda v: f"{v:.0f}")
                        st.dataframe(od[["issuer", "ticker", "weight", "d2", "extreme"]],
                                     hide_index=True, use_container_width=True)
                st.caption("• " + sp.get("note", ""))

        with st.expander("Method & caveats", expanded=False):
            for k in ("sp500", "sp1500", "russell", "ticker", "unclassified"):
                if k in u.get("notes", {}):
                    st.caption("• " + u["notes"][k])


def render_drift():
    """Phase 4: style-drift attribution. Which factors the book drifted on since `split`, and whether
    the drift came from rotating into NEW names (leans intentional → benchmark) or HELD names' loadings
    drifting (leans unintentional → hedge). Chris's 2026-06-23 question, made empirical. Collapsed."""
    with st.expander("🧭 Style-drift attribution (intentional vs not)", expanded=False):
        try:
            d = get("/drift")
        except Exception as e:
            st.info(f"Drift unavailable: {e} — run barra_universe_drift.py to build it.")
            return
        summ = d.get("summary", [])
        if not summ:
            st.info("No drift data.")
            return
        top = summ[0]
        st.markdown(
            f"Biggest drift since {d['split']}: **{top['factor']}** "
            f"({top['early']:+.2f} → {top['late']:+.2f}, Δ {top['delta']:+.2f}). {top['lean']}")
        sdf = pd.DataFrame(d["series"]); sdf["Date"] = pd.to_datetime(sdf["month"]); sdf = sdf.set_index("Date")
        topf = [r["factor"] for r in summ[:6] if r["factor"] in sdf]
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Book net factor exposure over time (top 6 movers)")
            st.line_chart(sdf[topf])
        with c2:
            st.caption(f"Drift attribution {d['t0']} → {d['t1']} (top movers)")
            arows = [{"Factor": r["factor"],
                      **{s.replace("_", " "): round(r[f"src_{s}"], 3) for s in d["sources"]}}
                     for r in summ[:6]]
            st.bar_chart(pd.DataFrame(arows).set_index("Factor"))
        rows = [{"Factor": r["factor"], "Pre": f"{r['early']:+.2f}", "Post": f"{r['late']:+.2f}",
                 "Δ": f"{r['delta']:+.2f}", "What drove it": r["lean"].split(" — ")[0]} for r in summ]
        st.caption(f"Per-factor drift pre/post {d['split']}, ranked, with the dominant source")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("• " + d.get("note", ""))


def render_liquidity():
    """Step 11: days-to-liquidate per held name = MV / (participation × ADV). Headline = share of the
    book liquidatable within a horizon; least-liquid names; names with no ADV. Collapsed; silent if
    /liquidity is down (e.g. frames built before the Step-11 ADV column)."""
    with st.expander("💧 Liquidity — days to liquidate", expanded=False):
        c1, c2 = st.columns(2)
        part = c1.slider("Participation (% of ADV / day)", 5, 50, 20, 5, key="pv_liq_part") / 100.0
        hor = c2.slider("Horizon (days)", 1, 20, 5, 1, key="pv_liq_hor")
        try:
            lq = get("/liquidity", participation=part, horizon=float(hor))
        except Exception as e:
            st.info(f"Liquidity unavailable: {e} — rebuild frames with the Step-11 builder.")
            return
        pct = lq.get("pct_weight_within_horizon")
        wavg = lq.get("weighted_avg_days"); mx = lq.get("max_days")
        st.markdown(
            f"At **{part:.0%} of ADV/day**, **{(pct or 0):.0%} of the book by weight** can be exited "
            f"within **{hor:.0f} days** (as of {lq['date']}). Weighted-avg "
            f"{('%.1f' % wavg) if wavg is not None else '—'} days; worst "
            f"{('%.0f' % mx) if mx is not None else '—'}. {lq['n_no_adv']} names "
            f"({lq['weight_no_adv']:.1%}) have no ADV.")
        detail = lq.get("detail", [])
        if detail:
            dd = pd.DataFrame(detail)
            dd["Weight"] = dd["Weight"].map(lambda v: f"{v:.2%}")
            dd["MV"] = dd["MV"].map(lambda v: f"${v / 1e6:,.0f}M")
            dd["ADV"] = dd["ADV"].map(lambda v: f"${v / 1e6:,.1f}M")
            dd["days"] = dd["days"].map(lambda v: f"{v:.1f}")
            st.caption("Least-liquid holdings — days to exit (descending)")
            st.dataframe(dd.head(15)[["Issuer", "Ticker", "Sector", "Weight", "MV", "ADV", "days"]],
                         hide_index=True, use_container_width=True)
        if lq.get("no_adv_names"):
            with st.expander(f"Names with no ADV ({lq['n_no_adv']})", expanded=False):
                nd = pd.DataFrame(lq["no_adv_names"])
                nd["weight"] = nd["weight"].map(lambda v: f"{v:.2%}")
                st.dataframe(nd[["issuer", "ticker", "weight"]], hide_index=True,
                             use_container_width=True)
        st.caption("• " + lq.get("note", ""))


def render_whatchanged():
    """Step 9: what changed quarter-over-quarter — positions in/out/resized, the factor-exposure
    drift attributed (rotation vs loading drift), the book risk delta, plus an on-demand LLM
    'what changed' read. Collapsed; silent if /whatchanged is down."""
    with st.expander("📋 What changed (quarter-over-quarter)", expanded=False):
        try:
            wc = get("/whatchanged")
        except Exception as e:
            st.info(f"What-changed unavailable: {e}")
            return
        p = wc["positions"]
        st.markdown(f"**{wc['from']} → {wc['to']}** · {p['n_before']} → {p['n_after']} names "
                    f"({p['n_entered']} entered, {p['n_exited']} exited)")
        c1, c2 = st.columns(2)
        for col, items, label in [(c1, p["entered"], "Entered (new)"),
                                  (c2, p["exited"], "Exited (dropped)")]:
            with col:
                if items:
                    st.caption(f"{label}, by weight")
                    df = pd.DataFrame(items); df["weight"] = df["weight"].map(lambda v: f"{v:.2%}")
                    st.dataframe(df[["issuer", "ticker", "weight"]], hide_index=True,
                                 use_container_width=True)
        if p["resized"]:
            with st.expander(f"Resized held names ({len(p['resized'])})", expanded=False):
                rd = pd.DataFrame(p["resized"])
                for c in ("w0", "w1", "delta"):
                    rd[c] = rd[c].map(lambda v: f"{v:+.2%}")
                st.dataframe(rd[["issuer", "ticker", "w0", "w1", "delta"]], hide_index=True,
                             use_container_width=True)
        ea = pd.DataFrame(wc["exposure_attribution"])
        st.caption("Net factor-exposure drift by source (rotation = entered+exited; re-pricing = loading_drift)")
        cols = ["factor", "before", "after", "delta", "src_entered", "src_exited",
                "src_reweighted", "src_loading_drift"]
        show = ea[cols].copy()
        for c in cols[1:]:
            show[c] = show[c].map(lambda v: "—" if v is None else f"{v:+.2f}")
        st.dataframe(show, hide_index=True, use_container_width=True)
        st.caption("Book risk delta (HistFull-equivalent, Market included; losses positive)")
        rows = []
        for k, lab, pct in [("scenario_var_99", "Scenario VaR 99", True), ("es_975", "ES 97.5", True),
                            ("total_var_99", "Total VaR 99", True), ("specific_vol", "Specific vol", True),
                            ("risk_hhi", "Risk HHI", False), ("gross", "Gross", True), ("net", "Net", True)]:
            v = wc["risk"].get(k, {})
            def fmt(x, pct=pct):
                return "—" if x is None else (f"{x:.2%}" if pct else f"{x:.3f}")
            d = v.get("delta")
            rows.append({"Measure": lab, "Before": fmt(v.get("before")), "After": fmt(v.get("after")),
                         "Δ": "—" if d is None else (f"{d:+.2%}" if pct else f"{d:+.3f}")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("• " + wc.get("note", ""))

        st.divider()
        _cache = st.session_state.setdefault("pv_wc_cache", {})
        key = f"{wc['from']}->{wc['to']}"
        notes = st.text_input("Optional context for the read", key="pv_wc_notes")
        cached = _cache.get(key)
        if st.button("Regenerate" if cached else "🔍 What-changed commentary", key="pv_wc_btn"):
            try:
                r = requests.post(f"{API}/whatchanged/analysis", json={"notes": notes or None},
                                  stream=True, timeout=180)
                r.raise_for_status(); r.encoding = "utf-8"
                _cache[key] = st.write_stream(c for c in r.iter_content(chunk_size=None,
                                              decode_unicode=True) if c)
            except requests.HTTPError as e:
                st.error(f"Failed: {e.response.text if e.response is not None else e}")
            except requests.RequestException as e:
                st.error(f"Request failed: {e}")
        elif cached:
            st.markdown(cached)
            st.caption("Cached — click **Regenerate** for a fresh read.")


def render_ask():
    """Step 10: scoped Q&A drill-down. Ask a free-text desk question; the model answers by pulling
    its own cube slices through the same guarded pivot the grid uses (one `query_cube` tool, nothing
    else — no files, no web). Streamed, with each query shown inline. Silent if the backend is down."""
    with st.expander("💬 Ask the risk model (scoped Q&A)", expanded=False):
        st.caption("Free-text question, answered from live cube slices the model pulls itself — one "
                   "guarded `query_cube` tool, the same allowlist as the grid. It cites the numbers it "
                   "queried and can reach nothing else.")
        q = st.text_input("Question", key="pv_ask_q",
                          placeholder="e.g. Which factor drives the most Total VaR on HistFull at the latest date?")
        notes = st.text_input("Optional desk context", key="pv_ask_notes")
        _cache = st.session_state.setdefault("pv_ask_cache", {})
        if st.button("Ask", key="pv_ask_btn") and q.strip():
            try:
                r = requests.post(f"{API}/ask", json={"question": q, "notes": notes or None},
                                  stream=True, timeout=300)
                r.raise_for_status(); r.encoding = "utf-8"
                _cache[q] = st.write_stream(c for c in r.iter_content(chunk_size=None,
                                            decode_unicode=True) if c)
            except requests.HTTPError as e:
                st.error(f"Failed: {e.response.text if e.response is not None else e}")
            except requests.RequestException as e:
                st.error(f"Request failed: {e}")
        elif q.strip() and (cached := _cache.get(q)):
            st.markdown(cached)
            st.caption("Cached — click **Ask** for a fresh answer.")


def render_stress():
    """Custom & reverse stress. Custom: book P&L under user-set per-factor sigma shocks. Reverse:
    the single-factor sigma move that would breach a target loss, ranked by vulnerability."""
    with st.expander("🧪 Stress test (custom & reverse)", expanded=False):
        facs = members.get("Factor") or []
        t_custom, t_rev = st.tabs(["Custom shock", "Reverse stress"])
        with t_custom:
            st.caption("Shock factors in σ (standard deviations). Book P&L = Σ exposure × (σ × factor vol) "
                       "— the same math as the built-in Hypo scenarios, but yours.")
            base = pd.DataFrame({"Factor": facs, "Shock σ": [0.0] * len(facs)})
            edited = st.data_editor(
                base, key="pv_stress_editor", hide_index=True, use_container_width=True,
                column_config={"Factor": st.column_config.TextColumn(disabled=True),
                               "Shock σ": st.column_config.NumberColumn(format="%.2f", step=0.5)})
            if st.button("Run stress", key="pv_stress_run"):
                shocks = {r["Factor"]: float(r["Shock σ"]) for _, r in edited.iterrows() if r["Shock σ"]}
                if not shocks:
                    st.info("Enter at least one non-zero shock.")
                else:
                    try:
                        res = requests.post(f"{API}/stress", json={"shocks": shocks}, timeout=30)
                        res.raise_for_status(); res = res.json()
                    except requests.RequestException as e:
                        st.error(f"Stress failed: {e}"); res = None
                    if res:
                        tp = res["total_pnl"]
                        (st.error if tp < 0 else st.success)(f"Book P&L under shock: **{tp:+.2%}**")
                        cdf = pd.DataFrame(res["components"])
                        cdf = cdf[["factor", "exposure", "sigma", "shock_return", "pnl"]].rename(
                            columns={"factor": "Factor", "exposure": "Exposure", "sigma": "σ",
                                     "shock_return": "Shock ret", "pnl": "P&L"})
                        for c in ("Shock ret", "P&L"):
                            cdf[c] = cdf[c].map(lambda v: f"{v:+.2%}")
                        cdf["Exposure"] = cdf["Exposure"].map(lambda v: f"{v:+.3f}")
                        cdf["σ"] = cdf["σ"].map(lambda v: f"{v:+.1f}")
                        st.dataframe(cdf, hide_index=True, use_container_width=True)
        with t_rev:
            st.caption("For a target book loss, the single-factor σ move that would breach it — ranked "
                       "by |σ| (smallest = the book's most vulnerable factor).")
            loss = st.number_input("Target loss (fraction of book)", min_value=0.005, max_value=0.5,
                                   value=0.055, step=0.005, format="%.3f", key="pv_rev_loss")
            if st.button("Run reverse stress", key="pv_rev_run"):
                try:
                    res = requests.get(f"{API}/reverse_stress", params={"loss": loss}, timeout=30)
                    res.raise_for_status(); res = res.json()
                except requests.RequestException as e:
                    st.error(f"Reverse stress failed: {e}"); res = None
                if res and res.get("weakest"):
                    w = res["weakest"]
                    st.warning(f"Most vulnerable: **{w['factor']}** — a {w['sigma_to_breach']:+.1f}σ move "
                               f"alone loses {loss:.1%}.")
                    rdf = pd.DataFrame(res["factors"])[["factor", "exposure", "sigma_to_breach"]].rename(
                        columns={"factor": "Factor", "exposure": "Exposure", "sigma_to_breach": "σ to breach"})
                    rdf["Exposure"] = rdf["Exposure"].map(lambda v: f"{v:+.3f}")
                    rdf["σ to breach"] = rdf["σ to breach"].map(lambda v: f"{v:+.1f}")
                    st.dataframe(rdf, hide_index=True, use_container_width=True)


def render_whatif():
    """Pre-trade what-if: resize/drop holdings and see the book risk impact (VaR/ES/Total VaR/Specific
    vol/HHI, gross/net) before vs after. Risk recomputed from the cube's loadings/returns/specvar."""
    with st.expander("🔀 Pre-trade / what-if", expanded=False):
        st.caption("Edit the New weight per holding (0 = drop), then run. Book risk is recomputed under "
                   "the modified weights — the same engine the cube uses.")
        try:
            boot = requests.post(f"{API}/whatif", json={"trades": []}, timeout=30)
            boot.raise_for_status(); boot = boot.json()
        except requests.RequestException as e:
            st.info(f"What-if unavailable: {e}")
            return
        hold = boot.get("holdings", [])
        if not hold:
            st.info("No holdings to trade.")
            return
        base = pd.DataFrame([{"Ticker": h["ticker"], "Position": h["position"],
                              "Weight": h["weight"], "New weight": h["weight"]} for h in hold])
        edited = st.data_editor(
            base, key="pv_whatif_editor", hide_index=True, use_container_width=True, height=300,
            column_config={"Ticker": st.column_config.TextColumn(disabled=True),
                           "Position": None,                       # hide the FIGI
                           "Weight": st.column_config.NumberColumn("Current", format="%.3f", disabled=True),
                           "New weight": st.column_config.NumberColumn(format="%.3f", step=0.01, min_value=0.0)})

        # add a name from the coverage universe that isn't currently held
        held_figs = {h["position"] for h in hold}
        addable = [u for u in boot.get("universe", []) if u["position"] not in held_figs]
        by_ticker = {u["ticker"]: u["position"] for u in addable}
        adds = st.session_state.setdefault("pv_whatif_adds", {})            # position -> (ticker, weight)
        st.markdown("**Add from coverage universe**")
        c1, c2, c3 = st.columns([3, 1, 1])
        pick = c1.selectbox(f"Ticker ({len(addable)} available)", sorted(by_ticker), index=None,
                            placeholder="pick a name to add…", key="pv_whatif_addtk",
                            label_visibility="collapsed")
        addw = c2.number_input("Weight", min_value=0.0, max_value=1.0, value=0.02, step=0.01,
                               key="pv_whatif_addw", label_visibility="collapsed")
        if c3.button("Add", key="pv_whatif_addbtn") and pick:
            adds[by_ticker[pick]] = (pick, float(addw))
        if adds:
            cc1, cc2 = st.columns([4, 1])
            cc1.caption("Staged adds: " + ", ".join(f"{t} {w:.1%}" for _, (t, w) in adds.items()))
            if cc2.button("Clear adds", key="pv_whatif_clear"):
                adds.clear(); st.rerun()

        if st.button("Run what-if", key="pv_whatif_run"):
            trades = [{"position": r["Position"], "weight": float(r["New weight"])}
                      for _, r in edited.iterrows()
                      if abs(float(r["New weight"]) - float(r["Weight"])) > 1e-9]
            trades += [{"position": fig, "weight": w} for fig, (_, w) in adds.items()]
            if not trades:
                st.info("Change at least one weight to run a what-if.")
            else:
                try:
                    res = requests.post(f"{API}/whatif", json={"trades": trades}, timeout=30)
                    res.raise_for_status(); res = res.json()
                except requests.RequestException as e:
                    st.error(f"What-if failed: {e}"); res = None
                if res:
                    b, a, d = res["before"], res["after"], res["delta"]
                    spec = [("Scenario VaR 99", "scenario_var_99"), ("Scenario VaR 97.5", "scenario_var_975"),
                            ("Scenario ES 97.5", "es_975"), ("Scenario ES 99", "es_99"),
                            ("Total VaR 99", "total_var_99"), ("Specific vol", "specific_vol"),
                            ("Risk HHI", "risk_hhi"), ("Net exposure", "net"), ("Gross exposure", "gross")]

                    def cell(v, key):
                        return "—" if v is None else (f"{v:.3f}" if key == "risk_hhi" else f"{v:.2%}")

                    def dcell(v, key):
                        return "—" if v is None else (f"{v:+.3f}" if key == "risk_hhi" else f"{v:+.2%}")
                    tbl = [{"Measure": lab, "Before": cell(b.get(k), k), "After": cell(a.get(k), k),
                            "Δ": dcell(d.get(k), k)} for lab, k in spec]
                    st.dataframe(pd.DataFrame(tbl), hide_index=True, use_container_width=True)
                    st.caption("Traded: " + ", ".join(
                        f"{t['ticker']} {t['old']:.1%}→{t['new']:.1%}" for t in res["trades"]))

members = dims.get("members", {})


# ----------------------------------------------------------------------------- defaults / seed
def _default_members(d: str, opts: list) -> list:
    """The historical default member selection for a slicer dimension.
    Book -> real book(s), excluding the always-zero N/A bucket; Date -> latest;
    ScenarioSet -> a single set (scenario measures need one), preferring HistFull."""
    if not opts:
        return []
    if d == "Book":
        return [s for s in opts if s != "N/A"]
    if d == "Date":
        return [opts[-1]]
    if d == "ScenarioSet":
        return [next((s for s in opts if s == "HistFull"), opts[0])]
    return []


def seed_defaults(dims: dict) -> None:
    """One-time seed of every widget key (guarded by pv_seeded). Uses setdefault so it
    never clobbers a value already present (e.g. from a load or a prior run)."""
    ss = st.session_state
    if ss.get("pv_seeded"):
        return
    ss.setdefault("pv_rows", ["Book"])
    ss.setdefault("pv_cols", [])
    ss.setdefault("pv_measures", ["Total VaR 99", "Scenario VaR 99", "Specific vol"])
    ss.setdefault("pv_slice_dims", ["Book", "Date", "ScenarioSet"])
    ss.setdefault("pv_row_tot", False)
    ss.setdefault("pv_col_tot", False)
    ss.setdefault("pv_as_pct", True)
    ss.setdefault("pv_hide_empty", True)
    ss.setdefault("pv_heat", True)
    ss.setdefault("pv_prec", 3)
    ss.setdefault("pv_date_fmt", DATE_FMT_DEFAULT)   # Date display format (grid + graph)
    ss.setdefault("pv_render", "grid")         # grid | chart (how the view draws)
    ss.setdefault("pv_queries", [])             # named self-contained pivot queries (chart sources)
    ss.setdefault("pv_chart", None)             # the view's embedded Vega-Lite spec (chart mode)
    ss.setdefault("pv_grid_sort", [])          # ag-grid sort model [{colId,sort,sortIndex}]
    ss.setdefault("pv_load_token", 0)          # bumped on load to re-instantiate the grid
    ss.setdefault("pv_sort_applied_token", -1)  # last token whose saved sort was pushed to the grid
    for d in ss["pv_slice_dims"]:
        ss.setdefault(f"slice_{d}", _default_members(d, members.get(d, [])))
    ss["pv_seeded"] = True


def apply_pending_load(dims: dict) -> None:
    """If a view is queued in pv_pending_load, write its 12 fields to the widget keys,
    intersecting against the live cube and collecting dropped items into pv_load_dropped.
    Must run BEFORE any widget that owns those keys is created (then we are post-rerun)."""
    ss = st.session_state
    view = ss.pop("pv_pending_load", None)
    if not view:
        return
    state = view.get("state", view)
    valid_dims = set(dims["dimensions"])
    valid_meas = set(dims["measures"])
    dropped: list[str] = []

    def keep_dims(lst, allow_measures):
        out = []
        for x in (lst or []):
            if x == "Measures" and allow_measures:
                out.append(x)
            elif x in valid_dims:
                out.append(x)
            else:
                dropped.append(f"field “{x}”")
        return out

    rows = keep_dims(state.get("rows", []), True)
    cols = keep_dims(state.get("cols", []), True)
    cols = [c for c in cols if c not in rows]          # strip rows∩cols overlap
    meas = []
    for m in (state.get("measures", []) or []):
        if m in valid_meas:
            meas.append(m)
        else:
            dropped.append(f"measure “{m}”")
    slice_dims = keep_dims(state.get("slice_dims", []), False)

    ss["pv_rows"] = rows
    ss["pv_cols"] = cols
    ss["pv_measures"] = meas
    ss["pv_slice_dims"] = slice_dims
    ss["pv_row_tot"] = bool(state.get("row_tot", False))
    ss["pv_col_tot"] = bool(state.get("col_tot", False))
    ss["pv_as_pct"] = bool(state.get("as_pct", True))
    ss["pv_hide_empty"] = bool(state.get("hide_empty", True))
    ss["pv_heat"] = bool(state.get("heat", True))
    try:
        ss["pv_prec"] = int(state.get("prec", 3))
    except (TypeError, ValueError):
        ss["pv_prec"] = 3
    _df = state.get("date_fmt", DATE_FMT_DEFAULT)
    ss["pv_date_fmt"] = _df if _df in DATE_FMTS else DATE_FMT_DEFAULT
    _rnd = state.get("render", "grid")
    ss["pv_render"] = _rnd if _rnd in ("grid", "chart") else "grid"
    ss["pv_queries"] = _queries_from_state(state)   # named pivot queries (migrates legacy `source`)
    ss["pv_chart"] = state.get("chart")        # the view's embedded Vega-Lite spec (or None)

    # clear any stale per-dim slicer keys, then write only the kept members
    for k in [k for k in list(ss.keys()) if k.startswith("slice_")]:
        del ss[k]
    saved_filters = state.get("filters", {}) or {}
    for d in slice_dims:
        opts = set(members.get(d, []))
        want = saved_filters.get(d, _default_members(d, members.get(d, [])))
        kept = [m for m in (want or []) if m in opts]
        for m in (want or []):
            if m not in opts:
                dropped.append(f"{d} member “{m}”")
        ss[f"slice_{d}"] = kept

    # grid column sort: reapply the saved sort (filtered to existing columns happens at render);
    # bump the load token so the grid re-instantiates and applies it even if columns are unchanged.
    ss["pv_grid_sort"] = state.get("sort", []) or []
    ss["pv_load_token"] = int(ss.get("pv_load_token", 0)) + 1

    ss["pv_load_dropped"] = dropped
    ss["pv_loaded_name"] = view.get("name", "(view)")


# seed + apply pending load BEFORE any widget that owns these keys is created.
seed_defaults(dims)
apply_pending_load(dims)

# apply a queued section switch here (before the section radio is instantiated, so it's legal)
_ps = st.session_state.pop("pv_pending_section", None)
if _ps in SECTIONS:
    st.session_state["pv_section"] = _ps

# Persist widget state across mode switches: Streamlit drops the state of any widget that
# isn't rendered on a run (so toggling Pivot↔Repository would otherwise reset the pivot
# controls). Re-assigning each key to itself BEFORE any widget renders keeps it alive.
# Restrict to the pivot/slicer widget keys — NEVER touch button keys (st.button forbids
# session_state assignment) or transient repository widgets.
_PERSIST = {"pv_rows", "pv_cols", "pv_measures", "pv_slice_dims", "pv_row_tot",
            "pv_col_tot", "pv_as_pct", "pv_hide_empty", "pv_heat", "pv_prec", "pv_date_fmt",
            "pv_render", "pv_queries", "pv_chart", "pv_graph_json", "pv_section"}
# the builder's per-query/per-graph widget keys (pv_qry_*/pv_gb_*) must also survive a mode
# switch — but NEVER reassign a button key (st.button forbids it).
_BTN_KEYS = {"pv_gb_add", "pv_gb_rm", "pv_qry_add", "pv_qry_rm", "pv_graph_apply"}
for _k in list(st.session_state.keys()):
    if _k in _BTN_KEYS:
        continue
    if (_k in _PERSIST or _k.startswith("slice_")
            or _k.startswith("pv_gb_") or _k.startswith("pv_qry_")):
        st.session_state[_k] = st.session_state[_k]

def _fmt_pct(v, dp=1):
    return "—" if v is None else f"{v:.{dp}%}"


def _fmt_num(v, dp=1):
    return "—" if v is None else f"{v:.{dp}f}"


def _spark(vals, color="#666"):
    """A Tufte sparkline: thin line, no axes/grid/labels, an endpoint dot for the current value.
    Quiet by construction — the trend's shape is the message, the scorecard number is the value."""
    data = [{"i": i, "v": float(v)} for i, v in enumerate(vals) if v is not None]
    if len(data) < 2:
        return
    spec = {
        "height": 34, "background": "transparent",
        "layer": [
            {"mark": {"type": "line", "color": color, "strokeWidth": 1.3}},
            {"mark": {"type": "point", "filled": True, "color": color, "size": 22},
             "transform": [{"filter": f"datum.i == {data[-1]['i']}"}]},
        ],
        "encoding": {
            "x": {"field": "i", "type": "quantitative", "axis": None},
            "y": {"field": "v", "type": "quantitative", "axis": None, "scale": {"zero": False}},
        },
        "config": {"view": {"stroke": None}},
    }
    st.vega_lite_chart(spec, width="stretch")


def render_status_line():
    """The three health checks as small RAG dots on one line — color encodes status, nothing else.
    The full detail tables live in the Overview's 'Checks' drill-down."""
    parts = []
    try:
        lim = get("/limits")
        if lim.get("configured"):
            s = lim["status"]
            parts.append(("Limits", {"breach": "red", "amber": "amber", "green": "green"}.get(s, "grey"),
                          {"breach": "breach", "amber": "warning", "green": "OK"}.get(s, s)))
    except Exception:
        pass
    try:
        dq = get("/dq"); s = dq.get("status", "pass")
        parts.append(("Data quality", {"fail": "red", "warn": "amber", "pass": "green"}.get(s, "grey"),
                      {"fail": "fail", "warn": "warn", "pass": "pass"}.get(s, s)))
    except Exception:
        pass
    try:
        bt = get("/backtest")
        if bt.get("status") == "ok":
            z = bt["basel_zone"]
            parts.append(("Backtest", z if z in ("red", "amber", "green") else "grey", z))
    except Exception:
        pass
    if not parts:
        return
    dot = {"green": "#2e7d32", "amber": "#c77f00", "red": "#b03030", "grey": "#999"}
    html = "&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;".join(
        f"<span class='dot' style='color:{dot[c]}'>&#9679;</span> <span class='st-lab'>{lab} — {txt}</span>"
        for lab, c, txt in parts)
    st.markdown(f"<div class='ov-status'>{html}</div>", unsafe_allow_html=True)


def render_overview():
    """The landing page: overview-first per Few. A scorecard of the book's key risk numbers (each
    with a Tufte sparkline of its trend), a compact RAG status line, then every whole-book panel as a
    closed drill-down. Independent of the pivot — that's its own page."""
    try:
        recs = get("/trends").get("records", [])
    except Exception:
        recs = []
    latest = recs[-1] if recs else {}
    try:
        dd = get("/drawdown")
    except Exception:
        dd = {}
    try:
        before = requests.post(f"{API}/whatif", json={"trades": []}, timeout=60).json().get("before", {})
    except Exception:
        before = {}

    h1, h2 = st.columns([4, 1])
    with h1:
        st.markdown(f"<div class='ov-head'>Soros 13F &nbsp;·&nbsp; "
                    f"<span class='ov-asof'>as of {latest.get('Date', '—')}</span></div>",
                    unsafe_allow_html=True)
    with h2:
        if st.button("Open Pivot →", width="stretch", key="ov_to_pivot"):
            st.session_state.page = "Pivot"; st.rerun()

    hhi = latest.get("Risk HHI")
    cards = [
        ("Scenario VaR 99", _fmt_pct(latest.get("Scenario VaR 99")),
         [r.get("Scenario VaR 99") for r in recs], "#7a2d2d"),
        ("Expected shortfall 97.5", _fmt_pct(latest.get("Scenario ES 97.5")),
         [r.get("Scenario ES 97.5") for r in recs], "#7a2d2d"),
        ("Max drawdown", _fmt_pct(dd.get("max_drawdown")),
         [p.get("equity") for p in dd.get("path", [])], "#555"),
        ("Effective bets (1/HHI)", _fmt_num(1 / hhi if hhi else None),
         [(1 / r["Risk HHI"]) if r.get("Risk HHI") else None for r in recs], "#2a4d69"),
        ("Net / Gross", f"{_fmt_pct(before.get('net'), 0)} / {_fmt_pct(before.get('gross'), 0)}",
         None, None),
    ]
    for col, (lab, val, series, color) in zip(st.columns(len(cards)), cards):
        with col:
            st.markdown(f"<div class='sc-lab'>{lab}</div><div class='sc-val'>{val}</div>",
                        unsafe_allow_html=True)
            if series:
                _spark(series, color)
    st.divider()
    render_status_line()
    st.divider()

    st.caption("Details on demand")
    t_risk, t_univ, t_chg, t_checks = st.tabs(["Risk", "Universe", "Changes & Q&A", "Checks"])
    with t_risk:           # quantitative book-risk lenses
        render_drawdown(); render_trends(); render_liquidity(); render_stress(); render_whatif()
    with t_univ:           # estimation-universe diagnostics (Chris's review)
        render_universe(); render_drift()
    with t_chg:            # period-over-period change + the scoped-Q&A assistant
        render_whatchanged(); render_ask()
    with t_checks:         # the full health-check detail behind the status dots
        render_limits_banner(); render_dq_badge(); render_backtest_badge()


st.title("Flex Agg ++")


def _excel_links() -> str:
    """Download links for the generated Excel validation workbooks. They're built into tmp/ by
    barra_excel_section3.py / barra_excel_check.py; copy any present one into static/ so it serves
    through the same gate as the docs. Returns the <a> HTML (empty if none built yet)."""
    tmp = Path(__file__).resolve().parent.parent / "tmp"
    static = Path(__file__).resolve().parent / "static"
    books = [("barra_section3_1name_1month.xlsx", "📑 Section 3 worked example (Excel)"),
             ("barra_risk_check_3pos.xlsx", "📑 Risk-measure check, 3 names (Excel)")]
    out = []
    for fn, label in books:
        src = tmp / fn
        if not src.exists():
            continue
        try:
            dst = static / fn
            if not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
                shutil.copy2(src, dst)
        except OSError:
            continue
        out.append(f'<a href="app/static/{fn}" download '
                   f'style="border:none;font-size:14px;color:#3b5e8c">{label} ↓</a>')
    return ('&nbsp;&nbsp;·&nbsp;&nbsp;' + '&nbsp;&nbsp;·&nbsp;&nbsp;'.join(out)) if out else ""


st.markdown(
    '<a href="app/static/guide.html" target="_blank" rel="noopener" '
    'style="border:none;font-size:14px;color:#3b5e8c">📖 Dashboard guide / docs ↗</a>'
    '&nbsp;&nbsp;·&nbsp;&nbsp;'
    '<a href="app/static/barra_model_reference.html" target="_blank" rel="noopener" '
    'style="border:none;font-size:14px;color:#3b5e8c">📐 Model &amp; data reference ↗</a>'
    '&nbsp;&nbsp;·&nbsp;&nbsp;'
    '<a href="app/static/factor-model-roadmap.html" target="_blank" rel="noopener" '
    'style="border:none;font-size:14px;color:#3b5e8c">🗺 Risk-review response &amp; roadmap ↗</a>'
    + _excel_links(),
    unsafe_allow_html=True)

# ---- Top-level page switch: Overview (the landing scorecard + all whole-book tooling) vs Pivot
# (the view workspace). Default Overview. The nav lives at the top of the sidebar so it shows on
# both pages; the radio is unkeyed and synced to st.session_state.page so the "Open Pivot →" button
# can drive it too. On Overview we render and st.stop() — the pivot code below never runs.
if "page" not in st.session_state:
    st.session_state.page = "Overview"
with st.sidebar:
    _nav = st.radio("Page", ["Overview", "Pivot"], horizontal=True, label_visibility="collapsed",
                    index=["Overview", "Pivot"].index(st.session_state.page))
    if _nav != st.session_state.page:
        st.session_state.page = _nav
        st.rerun()

if st.session_state.page == "Overview":
    render_overview()
    st.stop()


def read_pivot_state():
    """The pivot fields read from session_state — used when the sidebar is showing the
    Repository (the pivot widgets aren't rendered then, so the grid reads their last values)."""
    ss = st.session_state
    rows = list(ss.get("pv_rows", ["Book"]))
    cols = [c for c in ss.get("pv_cols", []) if c not in rows]
    slice_dims = list(ss.get("pv_slice_dims", []))
    filters = {d: ss[f"slice_{d}"] for d in slice_dims if ss.get(f"slice_{d}")}
    return (rows, cols, list(ss.get("pv_measures", [])), slice_dims, filters,
            bool(ss.get("pv_row_tot", False)), bool(ss.get("pv_col_tot", False)),
            bool(ss.get("pv_as_pct", True)), bool(ss.get("pv_hide_empty", True)),
            bool(ss.get("pv_heat", True)), int(ss.get("pv_prec", 3)),
            ss.get("pv_render", "grid"), ss.get("pv_date_fmt", DATE_FMT_DEFAULT))


# ----------------------------------------------------------------------------- controls
# Pivot and Repository are orthogonal, so the left sidebar shows EXACTLY ONE at a time,
# chosen by a Pivot/Repository switch (default Pivot). repo_slot is filled later (once
# render_panel is defined) but renders here, directly under the switch.
with st.sidebar:
    mode = st.radio("Panel", ["Pivot", "Repository"], horizontal=True, key="pv_mode",
                    label_visibility="collapsed")
    show_panel = (mode == "Repository")
    repo_slot = st.container()
    if mode == "Pivot":
        st.subheader("Pivot")
        # how the view draws: the ag-grid table, or the view's own embedded Vega-Lite chart spec
        # (defined entirely in the view JSON; this app just renders it).
        _RLABEL = {"grid": "▦ Grid", "chart": "📈 Chart"}
        render = st.selectbox("Show as", ["grid", "chart"], key="pv_render",
                              format_func=lambda r: _RLABEL[r])

        # ---- Graphs: author each graph from individual ITEMS (Mark / X / Series) — like the
        # pivot's Rows/Measures. The JSON updates live as items change; once you edit an item the
        # builder owns the chart. Add graphs for multiples. (Advanced: edit the raw JSON.) On load
        # we seed the raw editor + reset "touched" so a loaded view's spec is preserved.
        _gtok = st.session_state.get("pv_load_token", 0)
        if st.session_state.get("pv_graph_seed_tok") != _gtok:
            st.session_state["pv_graph_seed_tok"] = _gtok
            _ch = st.session_state.get("pv_chart")
            _qs = st.session_state.get("pv_queries") or []
            # the editor shows the COMPLETE self-contained view: the named queries + the graphs.
            st.session_state["pv_graph_json"] = (json.dumps({"queries": _qs, "chart": _ch}, indent=2)
                                                 if _ch else "")
            st.session_state["pv_gb_touched"] = False        # don't overwrite the loaded spec
            # reverse-parse the loaded chart into the builder's per-graph items, so the UI shows
            # EVERY graph and its params. (Stays 'untouched' -> the original spec still renders
            # until the user edits an item.)
            _specs = _ch if isinstance(_ch, list) else ([_ch] if _ch else [])
            _qlist = _qs if isinstance(_qs, list) else [_qs]
            # QUERIES section: rebuild named query definitions from the loaded list
            for _k in [k for k in list(st.session_state)
                       if k.startswith(("pv_qry_name_", "pv_qry_rows_", "pv_qry_cols_", "pv_qry_meas_"))]:
                del st.session_state[_k]
            st.session_state["pv_qry_n"] = max(1, len(_qlist))
            for _j, _q in enumerate(_qlist):
                _q = _q if isinstance(_q, dict) else {}
                st.session_state[f"pv_qry_name_{_j}"] = _q.get("name") or f"Query {_j + 1}"
                st.session_state[f"pv_qry_rows_{_j}"] = list(_q.get("rows") or [])
                st.session_state[f"pv_qry_cols_{_j}"] = list(_q.get("cols") or [])
                st.session_state[f"pv_qry_meas_{_j}"] = list(_q.get("measures") or [])
            # GRAPHS section: per graph items + the NAME of the query it references
            for _k in [k for k in list(st.session_state)
                       if any(k.startswith(f"pv_gb_{_f}_") for _f in _GB_FIELDS)]:
                del st.session_state[_k]
            st.session_state["pv_gb_n"] = max(1, len(_specs))
            _names = [(_q.get("name") if isinstance(_q, dict) else None) or f"Query {_j + 1}"
                      for _j, _q in enumerate(_qlist)]
            for _gi, _sp in enumerate(_specs):
                _items = _items_from_spec(_sp)
                _nm = _sp.get("source") if isinstance(_sp, dict) else None
                _items["source"] = _nm if _nm in _names else (_names[_gi] if _gi < len(_names)
                                                              else (_names[0] if _names else "Query 1"))
                for _f, _v in _items.items():
                    st.session_state[f"pv_gb_{_f}_{_gi}"] = _v
        # the chart builder (named Queries + Graphs); collapsed in grid mode, open in chart mode.
        # The duplication the grid query would cause is avoided by hiding the top-level Rows/Columns/
        # Measures in chart mode (below) — so only ONE query editor is visible at a time.
        with st.expander("Graphs", expanded=(render == "chart")):
            st.caption("Define named **Queries** (each a self-contained pivot query), then build "
                       "**Graphs** that each reference a query by name. The View JSON updates live; "
                       "**Show as → Chart**, then Save.")
            _dim_opts = list(dims["dimensions"])
            _all_meas = list(dims["measures"])

            # ---------- QUERIES: named self-contained pivot queries ----------
            st.markdown("**Queries** — each a pivot query (Rows × Columns × Measures); the Slicers "
                        "below are baked in as each query's filters")
            # Query 1 PRE-FILLS from the grid pivot you were just looking at (one-time), so a fresh
            # chart works out of the box; edit it (or add queries) from there.
            _grid_rows = [r for r in st.session_state.get("pv_rows", []) if r != "Measures"]
            _grid_meas = list(st.session_state.get("pv_measures", []))
            # the CURRENT slicer selection (read from session — the Slicers widgets render later this
            # run, but a slicer change reruns first, so this is up to date). Baked into every query so
            # the view JSON is self-contained AND regenerates whenever a filter changes.
            _slice_now = {d: st.session_state[f"slice_{d}"]
                          for d in st.session_state.get("pv_slice_dims", [])
                          if st.session_state.get(f"slice_{d}")}
            _qry_n = st.session_state.setdefault("pv_qry_n", 1)
            _queries = []
            for _j in range(_qry_n):
                st.session_state.setdefault(f"pv_qry_name_{_j}", f"Query {_j + 1}")
                st.session_state.setdefault(f"pv_qry_rows_{_j}", _grid_rows if _j == 0 else [])
                st.session_state.setdefault(f"pv_qry_cols_{_j}", [])
                st.session_state.setdefault(f"pv_qry_meas_{_j}", _grid_meas if _j == 0 else [])
                # keep selections valid against the live cube (set BEFORE the widget instantiates)
                st.session_state[f"pv_qry_rows_{_j}"] = [r for r in st.session_state[f"pv_qry_rows_{_j}"]
                                                         if r in _dim_opts]
                st.session_state[f"pv_qry_cols_{_j}"] = [c for c in st.session_state[f"pv_qry_cols_{_j}"]
                                                         if c in _dim_opts]
                st.session_state[f"pv_qry_meas_{_j}"] = [m for m in st.session_state[f"pv_qry_meas_{_j}"]
                                                         if m in _all_meas]
                st.text_input("Query name", key=f"pv_qry_name_{_j}", on_change=_gb_touch,
                              label_visibility="collapsed", placeholder=f"query {_j + 1} name")
                # full-width, stacked (NOT side-by-side) so every selected member is readable
                st.multiselect("Rows", _dim_opts, key=f"pv_qry_rows_{_j}", on_change=_gb_touch,
                               help="Group-by dimensions. ScenarioDay unpacks the scenario P&L vector per day.")
                st.multiselect("Columns", _dim_opts, key=f"pv_qry_cols_{_j}", on_change=_gb_touch,
                               help="Optional cross-tab dimension(s); leave empty for a flat series.")
                st.multiselect("Measures", _all_meas, key=f"pv_qry_meas_{_j}", on_change=_gb_touch)
                _nm = (st.session_state.get(f"pv_qry_name_{_j}") or "").strip() or f"Query {_j + 1}"
                _queries.append({"name": _nm, "rows": list(st.session_state[f"pv_qry_rows_{_j}"]),
                                 "cols": list(st.session_state[f"pv_qry_cols_{_j}"]),
                                 "measures": list(st.session_state[f"pv_qry_meas_{_j}"]),
                                 "filters": dict(_slice_now)})
            _qa1, _qa2 = st.columns(2)
            if _qa1.button("➕ Add query", key="pv_qry_add"):
                st.session_state["pv_qry_n"] = _qry_n + 1; _gb_touch(); st.rerun()
            if _qry_n > 1 and _qa2.button("➖ Remove query", key="pv_qry_rm"):
                for _p in ("pv_qry_name_", "pv_qry_rows_", "pv_qry_cols_", "pv_qry_meas_"):
                    st.session_state.pop(f"{_p}{_qry_n - 1}", None)
                st.session_state["pv_qry_n"] = _qry_n - 1; _gb_touch(); st.rerun()
            _by_name = {q["name"]: q for q in _queries}
            _qry_names = [q["name"] for q in _queries] or ["Query 1"]

            st.divider()
            # ---------- GRAPHS: each references a query by name ----------
            st.markdown("**Graphs** — reference a query")
            _gn = st.session_state.setdefault("pv_gb_n", 1)
            _graphs = []
            for _i in range(_gn):
                st.markdown(f"<div class='cap'>Graph {_i + 1}</div>", unsafe_allow_html=True)
                if st.session_state.get(f"pv_gb_source_{_i}") not in _qry_names:
                    st.session_state[f"pv_gb_source_{_i}"] = _qry_names[0]
                _qname = st.selectbox("Query", _qry_names, key=f"pv_gb_source_{_i}", on_change=_gb_touch)
                _q = _by_name.get(_qname, {})
                _q_rows = [r for r in (_q.get("rows") or []) if r != "Measures"]
                _q_meas = list(_q.get("measures") or [])
                for _f, _v in _gb_defaults(_q_rows, _q_meas).items():
                    st.session_state.setdefault(f"pv_gb_{_f}_{_i}", _v)
                if _q_rows and st.session_state.get(f"pv_gb_x_{_i}") not in _q_rows:
                    st.session_state[f"pv_gb_x_{_i}"] = _q_rows[0]
                    st.session_state[f"pv_gb_xtype_{_i}"] = (
                        "temporal" if _q_rows[0] == "Date" else "nominal")
                _cm = [m for m in st.session_state.get(f"pv_gb_meas_{_i}", _q_meas) if m in _q_meas]
                st.session_state[f"pv_gb_meas_{_i}"] = _cm or _q_meas
                if st.session_state.get(f"pv_gb_xtype_{_i}") not in _XTYPES:
                    st.session_state[f"pv_gb_xtype_{_i}"] = "nominal"
                if st.session_state.get(f"pv_gb_yfmt_{_i}") not in _YFMT:
                    st.session_state[f"pv_gb_yfmt_{_i}"] = "none"
                _c1, _c2 = st.columns(2)
                _c1.selectbox("Mark", MARKS, key=f"pv_gb_mark_{_i}", on_change=_gb_touch)
                _c2.number_input("Height", min_value=120, max_value=900, step=20,
                                 key=f"pv_gb_height_{_i}", on_change=_gb_touch)
                if st.session_state.get(f"pv_gb_mark_{_i}") in ("line", "area"):
                    st.checkbox("Show points", key=f"pv_gb_point_{_i}", on_change=_gb_touch)
                _x1, _x2 = st.columns(2)
                _x1.selectbox("X (field)", _q_rows or ["— add a Row to the query —"],
                              key=f"pv_gb_x_{_i}", on_change=_gb_touch)
                _x2.selectbox("X type", _XTYPES, key=f"pv_gb_xtype_{_i}", on_change=_gb_touch)
                st.multiselect("Series (measures)", _q_meas, key=f"pv_gb_meas_{_i}", on_change=_gb_touch)
                _y1, _y2 = st.columns(2)
                _y1.selectbox("Y format", list(_YFMT), key=f"pv_gb_yfmt_{_i}", on_change=_gb_touch)
                _y2.checkbox("Legend", key=f"pv_gb_legend_{_i}", on_change=_gb_touch)
                _t1, _t2 = st.columns(2)
                _t1.text_input("X title", key=f"pv_gb_xtitle_{_i}", on_change=_gb_touch)
                _t2.text_input("Y title", key=f"pv_gb_ytitle_{_i}", on_change=_gb_touch)
                st.text_input("Chart title", key=f"pv_gb_title_{_i}", on_change=_gb_touch)
                _g = {_f: st.session_state.get(f"pv_gb_{_f}_{_i}") for _f in _GB_FIELDS}
                if not _q_rows:
                    _g["x"] = None
                _graphs.append(_g)
                if _i < _gn - 1:
                    st.divider()
            _ac1, _ac2 = st.columns(2)
            if _ac1.button("➕ Add graph", key="pv_gb_add"):
                st.session_state["pv_gb_n"] = _gn + 1; _gb_touch(); st.rerun()
            if _gn > 1 and _ac2.button("➖ Remove graph", key="pv_gb_rm"):
                for _f in _GB_FIELDS:
                    st.session_state.pop(f"pv_gb_{_f}_{_gn-1}", None)
                st.session_state["pv_gb_n"] = _gn - 1; _gb_touch(); st.rerun()
            _built = _build_specs(_graphs, _queries,
                                  st.session_state.get("pv_date_fmt", DATE_FMT_DEFAULT))
            # Sync the QUERIES (rows/cols/measures + baked Slicer filters) on an actual EDIT — a
            # query-item edit (`touched`) OR a Slicer change (`qbake`) — NOT on every rerun, so a
            # load / raw-JSON apply keeps the pv_queries it just set. A filter change re-bakes the
            # queries (regenerating the JSON) WITHOUT rebuilding the chart spec, so a loaded
            # hand-authored spec (e.g. the COVID loss-curve sort) survives a scenario switch. The
            # SPEC is rebuilt from the graph items ONLY when an item is edited (`touched`).
            if st.session_state.get("pv_gb_touched") or st.session_state.pop("pv_qbake", False):
                st.session_state["pv_queries"] = _queries
            if st.session_state.get("pv_gb_touched"):
                st.session_state["pv_chart"] = _built
            _chart_now = st.session_state.get("pv_chart")
            # the full, self-contained view: NAMED queries + the CURRENT chart (authored, or rebuilt).
            _bundle = {"queries": _queries, "chart": _chart_now} if (_chart_now or _built) else None
            if st.session_state.get("pv_gb_touched"):
                st.session_state["pv_graph_json"] = json.dumps(_bundle, indent=2) if _bundle else ""
            # the live View JSON is collapsed by default (kept available, not in your face)
            with st.expander("View JSON (live) — named queries + graphs referencing them", expanded=False):
                st.code(json.dumps(_bundle, indent=2) if _bundle
                        else "// define a Query (Rows + Measures), then a Graph (X + Series)",
                        language="json")
            with st.expander("Advanced — edit raw JSON", expanded=False):
                st.text_area("View JSON  { queries, chart }", key="pv_graph_json", height=240,
                             help="The whole self-contained view: `queries` (named pivot queries) and "
                                  "`chart` (one Vega-Lite spec, or a list, each referencing a query by "
                                  "name). Apply overrides the builder.")
                if st.button("Apply raw JSON", key="pv_graph_apply"):
                    _raw = (st.session_state.get("pv_graph_json") or "").strip()
                    try:
                        _p = json.loads(_raw) if _raw else None
                        if isinstance(_p, dict) and "chart" in _p:          # full {queries, chart} view
                            st.session_state["pv_chart"] = _p.get("chart")
                            st.session_state["pv_queries"] = _queries_from_state(_p)
                        else:                                               # legacy: chart spec/list only
                            st.session_state["pv_chart"] = _p
                            st.session_state["pv_queries"] = list(st.session_state.get("pv_queries") or [])
                        st.session_state["pv_gb_touched"] = False           # raw JSON now owns the view
                        st.session_state["pv_graph_msg"] = "✓ Applied."
                    except json.JSONDecodeError as _e:
                        st.session_state["pv_graph_msg"] = f"✗ Invalid JSON: {_e}"
                    st.rerun()
                _gmsg = st.session_state.pop("pv_graph_msg", None)
                if _gmsg:
                    (st.success if _gmsg.startswith("✓") else st.error)(_gmsg)

        # The single GRID query (Rows/Columns/Measures) belongs to GRID mode only — in chart mode
        # each graph's query is defined in the Queries section above, so showing these too would
        # duplicate the query controls. In chart mode we read the (unused) grid fields from session.
        if render == "grid":
            # "Measures" is a placeable pseudo-field: drop it on Rows or Columns to group by
            # measure on that axis. If left off both axes, multiple measures group as columns.
            dim_opts = ["Measures"] + dims["dimensions"]
            rows = st.multiselect("Rows", dim_opts, key="pv_rows",
                                  help="Add 'Measures' here to stack measures down the rows.")
            cols = st.multiselect("Columns", [d for d in dim_opts if d not in rows], key="pv_cols",
                                  help="Add 'Measures' here to spread measures across the columns.")
            measures = st.multiselect("Measures", dims["measures"], key="pv_measures")
            st.divider()
        else:
            rows = list(st.session_state.get("pv_rows", []))
            cols = list(st.session_state.get("pv_cols", []))
            measures = list(st.session_state.get("pv_measures", []))
        st.subheader("Slicers")
        # Slice on ANY dimension, single or multiple members (OR within a dim, AND across). Shared:
        # the grid filters on these, and every chart query bakes them in as its filters. A slicer
        # change does NOT rebuild the chart SPEC (so the authored loss-curve sort/labels survive a
        # scenario switch) — it only re-scopes the data; the queries' baked filters stay in sync below.
        slice_dims = st.multiselect("Slice on", dims["dimensions"], key="pv_slice_dims",
                                    on_change=_filters_changed,
                                    help="Filter the cube on any dimension; pick one or many "
                                         "members. Empty = keep all.")
        filters = {}
        for d in slice_dims:
            opts = members.get(d, [])
            # newly-added dims still default sensibly (latest Date, real Book, HistFull set).
            st.session_state.setdefault(f"slice_{d}", _default_members(d, opts))
            sel = st.multiselect(d, opts, key=f"slice_{d}", on_change=_filters_changed)
            if sel:
                filters[d] = sel
        st.divider()
        if render == "grid":
            row_tot = st.checkbox("Row totals (∑ over columns → Total column)", key="pv_row_tot")
            col_tot = st.checkbox("Column totals (∑ over rows → Total row)", key="pv_col_tot")
            as_pct = st.checkbox("Risk measures as %", key="pv_as_pct")
            hide_empty = st.checkbox("Hide empty rows / columns", key="pv_hide_empty",
                                     help="Drop rows/columns that are entirely blank. Totals are kept.")
            heat = st.checkbox("Heatmap", key="pv_heat")
            prec = st.slider("Decimals", 0, 6, key="pv_prec")
        else:
            row_tot = bool(st.session_state.get("pv_row_tot", False))
            col_tot = bool(st.session_state.get("pv_col_tot", False))
            as_pct = bool(st.session_state.get("pv_as_pct", True))
            hide_empty = bool(st.session_state.get("pv_hide_empty", True))
            heat = bool(st.session_state.get("pv_heat", True))
            prec = int(st.session_state.get("pv_prec", 3))
        date_fmt = st.selectbox("Date format", list(DATE_FMTS), key="pv_date_fmt",
                                format_func=lambda f: DATE_FMTS.get(f, f),
                                help="How Date members render in the grid and on temporal chart axes.")
    else:
        (rows, cols, measures, slice_dims, filters,
         row_tot, col_tot, as_pct, hide_empty, heat, prec, render, date_fmt) = read_pivot_state()
    show_totals = row_tot or col_tot


def current_state() -> dict:
    """Read the view fields straight from session_state, for saving (incl. the grid sort)."""
    ss = st.session_state
    sds = ss.get("pv_slice_dims", [])
    flt = {d: ss[f"slice_{d}"] for d in sds if ss.get(f"slice_{d}")}
    return {"rows": list(ss.get("pv_rows", [])), "cols": list(ss.get("pv_cols", [])),
            "measures": list(ss.get("pv_measures", [])), "slice_dims": list(sds),
            "filters": flt, "row_tot": bool(ss.get("pv_row_tot", False)),
            "col_tot": bool(ss.get("pv_col_tot", False)), "as_pct": bool(ss.get("pv_as_pct", True)),
            "hide_empty": bool(ss.get("pv_hide_empty", True)), "heat": bool(ss.get("pv_heat", True)),
            "prec": int(ss.get("pv_prec", 3)), "sort": list(ss.get("pv_grid_sort", [])),
            "render": ss.get("pv_render", "grid"),
            "date_fmt": ss.get("pv_date_fmt", DATE_FMT_DEFAULT),
            "queries": list(ss.get("pv_queries") or []),
            "chart": ss.get("pv_chart")}


# ----------------------------------------------------------------------------- charts (JSON-driven)
# A "chart" view is fully self-describing: it carries named `queries` (each a self-contained PIVOT
# query) and a complete Vega-Lite `chart` spec per graph. This renderer is a GENERIC PASS-THROUGH —
# it runs the query a graph names, binds the tidy records as the spec's default dataset, and hands
# the spec to Vega. There is NO chart logic here: every mark, encoding, scale, colour, axis, format
# and the theme live in the view JSON. Charts stay presentation-only (the cube computes every number).
def _chart_data(query: dict, view_filters: dict):
    """Run ONE named pivot query and return its tidy records as a DataFrame (the spec's default
    dataset). The query is self-contained — its own rows/cols/measures — and inherits the view's
    slicer filters, which a per-query `filters` may override/extend."""
    q = query or {}
    rows = [r for r in (q.get("rows") or []) if r and r != "Measures"]
    cols = [c for c in (q.get("cols") or []) if c and c != "Measures"]
    meas = list(q.get("measures") or [])
    if not rows or not meas:
        return None
    # the live Slicers WIN over a query's baked filters (per dimension) — so picking a new
    # ScenarioSet in the Slicers actually re-scopes the chart; the baked filters are just defaults
    # for dimensions the user isn't currently slicing on.
    flt = {**(q.get("filters") or {}), **(view_filters or {})}
    resp = get("/pivot", rows=",".join(rows), cols=",".join(cols), measures=",".join(meas),
               totals="false", filters=json.dumps(flt))
    recs = resp.get("records", [])
    if not recs:
        return None
    df = pd.DataFrame(recs)
    # data hygiene (same as the grid): some measures arrive non-scalar at fine granularity
    # (e.g. a dict per Factor) — coerce measure columns to numeric so the column is a clean
    # numeric type for Arrow/Vega (mixed struct+float fails Arrow serialization). Not graph logic.
    for me in meas:
        if me in df.columns:
            df[me] = pd.to_numeric(df[me], errors="coerce")
    return df


def _apply_date_fmt(node, fmt: str) -> None:
    """Apply the user's Date Format to EVERY date encoding in a spec, in place — so the selectbox
    drives the dates on ALL graphs. Two cases:
      * `type:"temporal"` field-defs   -> axis channels get `axis.format`, tooltips get `format`.
      * an `axis.labelExpr` that calls `timeFormat(..., '<fmt>')` (e.g. the sector loss curve's
        ordinal date axis, sorted worst→best) -> the quoted d3 format inside it is rewritten.
    The latter keeps such an axis's labelAngle/labelOverlap, so it stays legible at the new width."""
    if isinstance(node, dict):
        enc = node.get("encoding")
        if isinstance(enc, dict):
            for channel, fdef in enc.items():
                for d in (fdef if isinstance(fdef, list) else [fdef]):
                    if not isinstance(d, dict):
                        continue
                    if d.get("type") == "temporal":
                        if channel == "tooltip":
                            d["format"] = fmt
                        else:
                            ax = d.get("axis")
                            d["axis"] = {**ax, "format": fmt} if isinstance(ax, dict) else {"format": fmt}
                    # ordinal/quantitative date axis that formats via timeFormat() in a labelExpr
                    ax = d.get("axis")
                    if isinstance(ax, dict) and isinstance(ax.get("labelExpr"), str) \
                            and "timeFormat(" in ax["labelExpr"]:
                        ax["labelExpr"] = re.sub(r"(timeFormat\([^,]*,\s*)'[^']*'",
                                                 r"\1'" + fmt + "'", ax["labelExpr"])
        for v in node.values():
            _apply_date_fmt(v, fmt)
    elif isinstance(node, list):
        for v in node:
            _apply_date_fmt(v, fmt)


def render_spec(view_filters: dict) -> None:
    """Render the current view's embedded Vega-Lite spec(s). Generic — all graph definition is JSON.

    `chart` is a single spec or a LIST of specs (each drawn as its own chart). `queries` is a list
    of NAMED pivot queries; each spec carries `"source": <query name>` linking the graph to ONE
    query (e.g. the COVID view: graph 1 → "Scenario P&L" [rows=ScenarioDay], graph 2 → "Scenario
    P&L by Sector" [rows=ScenarioDay,Sector]). Identical queries are run once (cached). Positional
    fallback keeps a name-less spec working."""
    ss = st.session_state
    spec = ss.get("pv_chart")
    queries = ss.get("pv_queries") or []
    date_fmt = ss.get("pv_date_fmt", DATE_FMT_DEFAULT)
    if not spec:
        st.info("This view has no chart spec. A charted view stores named `queries` and a complete "
                "Vega-Lite `chart` block in its saved JSON — load a chart view, or switch "
                "**Show as → Grid**."); return
    specs = spec if isinstance(spec, list) else [spec]
    qlist = queries if isinstance(queries, list) else [queries]
    by_name = {q["name"]: q for q in qlist if isinstance(q, dict) and "name" in q}
    cache, rendered = {}, False
    for i, sp in enumerate(specs):
        sp = json.loads(json.dumps(sp))                         # deep copy before mutating
        _apply_date_fmt(sp, date_fmt)                           # the Date Format selectbox -> temporal axes
        # resolve this graph's query: by the spec's `source` NAME, else positional, else first.
        name = sp.pop("source", None) if isinstance(sp, dict) else None
        q = (by_name.get(name) or (qlist[i] if i < len(qlist) else None)
             or (qlist[0] if qlist else None))
        if not q:
            continue
        key = json.dumps(q, sort_keys=True)
        if key not in cache:
            try:
                cache[key] = _chart_data(q, view_filters)       # run this query once
            except requests.HTTPError as e:
                st.error(f"chart query rejected: {e.response.text}"); cache[key] = None
        df = cache[key]
        if df is None:
            continue
        st.vega_lite_chart(df, spec=sp, theme=None)             # records bound as the default dataset
        rendered = True
    if not rendered:
        st.info("No data for this slice — each graph's query needs row + measure fields and a "
                "matching slice (e.g. a scenario query needs a single Date + ScenarioSet).")


# ----------------------------------------------------------------------------- saved-views panel UI
def _queue_load(file_rel: str) -> None:
    st.session_state["pv_pending_load"] = load_view(file_rel)
    st.session_state["pv_selected_view"] = file_rel
    sect = file_rel.split("/", 1)[0]          # match the section to the loaded view; applied at
    if sect in SECTIONS:                      # the top of the next run, before the radio is built
        st.session_state["pv_pending_section"] = sect
    st.rerun()


def _fmt_ts(ts: str | None) -> str:
    return (ts or "").replace("T", " ").rstrip("Z")


def _sorted_views(views: list, sort: str) -> list:
    if sort == "Name ↓":
        return sorted(views, key=lambda v: v["name"].lower(), reverse=True)
    if sort == "Recent":
        return sorted(views, key=lambda v: v.get("updated") or "", reverse=True)
    return sorted(views, key=lambda v: v["name"].lower())


def _all_dests() -> list[str]:
    """Every folder across both sections (full rel paths) — used as move targets."""
    out: list[str] = []
    for s in SECTIONS:
        out.append(s)
        out.extend(all_folders(s))
    return out


def render_tree(node: dict, base: str = "", query: str = "", sort: str = "Name ↑") -> None:
    """Recurse over list_tree(): folders -> expanders (auto-open while searching), views ->
    single-click load buttons with a hover tooltip + a rename/move/delete popover. `base` is
    the rel path of `node`, so each folder's true rel path is base/<name>."""
    _sel = st.session_state.get("pv_selected_view")
    for fname, sub in node.get("folders", {}).items():
        frel = f"{base}/{fname}".strip("/")
        # keep a folder open while searching, OR when it holds the currently-selected view (so the
        # whole path down to the highlighted row stays expanded on load).
        _holds_sel = bool(_sel) and _sel.startswith(frel + "/")
        with st.expander(f"📁 {fname}", expanded=bool(query) or _holds_sel):
            render_tree(sub, base=frel, query=query, sort=sort)
            with st.popover("⚙ folder", use_container_width=False):
                nn = st.text_input("Rename folder", value=fname, key=f"frn_{frel}")
                c1, c2 = st.columns(2)
                if c1.button("Rename", key=f"frnb_{frel}"):
                    try:
                        rename_folder(frel, nn); st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
                if c2.button("Delete (empty)", key=f"fdel_{frel}"):
                    try:
                        delete_folder(frel); st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
    _sel_file = st.session_state.get("pv_selected_view")
    for v in _sorted_views(node.get("views", []), sort):
        tip = f"Folder: {v['path'] or '(section root)'}   ·   Changed: {_fmt_ts(v.get('updated')) or '—'}"
        _is_sel = (v["file"] == _sel_file)
        c1, c2 = st.columns([6, 1])
        # wrap the load button in a CSS-keyed container (slug is CSS-safe) so the SELECTED view can
        # be highlighted in the tree (see the dynamic rule in render_panel); ▣ marks it too.
        with c1.container(key=f"vbtn_{slugify(v['file'])}"):
            if st.button(f"{'▣' if _is_sel else '▦'} {v['name']}", key=f"load_{v['file']}",
                         use_container_width=True, help=tip):
                _queue_load(v["file"])
        with c2.popover("⚙"):
            if v.get("updated"):
                st.caption(f"Changed on: {_fmt_ts(v['updated'])}")
            nn = st.text_input("Rename", value=v["name"], key=f"vrn_{v['file']}")
            if st.button("Rename", key=f"vrnb_{v['file']}"):
                try:
                    rename_view(v["file"], nn); st.rerun()
                except Exception as ex:
                    st.error(str(ex))
            dest = st.selectbox("Move to", _all_dests(), key=f"vmv_{v['file']}")
            if st.button("Move", key=f"vmvb_{v['file']}"):
                try:
                    move_view(v["file"], dest); st.rerun()
                except Exception as ex:
                    st.error(str(ex))
            if st.button("✕ Delete", key=f"vdel_{v['file']}"):
                try:
                    delete_view(v["file"]); st.rerun()
                except Exception as ex:
                    st.error(str(ex))


def _render_meta_box(file_rel: str) -> None:
    """The selected/loaded view's metadata card (mirrors the jpeg's tooltip box)."""
    try:
        doc = load_view(file_rel)
    except Exception:
        st.session_state.pop("pv_selected_view", None)
        return
    st.markdown(
        f"<div class='vmeta'><b>▦ {doc.get('name', '?')}</b><br>"
        f"Folder: {doc.get('path') or '(section root)'}<br>"
        f"Created: {_fmt_ts(doc.get('created')) or '—'}<br>"
        f"Changed on: {_fmt_ts(doc.get('updated')) or '—'}</div>",
        unsafe_allow_html=True)


def render_panel() -> None:
    st.markdown("<div class='vpanel'>", unsafe_allow_html=True)
    # Public / Private section split (the two top-level repositories in the reference UI)
    section = st.radio("Section", SECTIONS, horizontal=True, key="pv_section",
                       label_visibility="collapsed")
    # toolbar: refresh · new folder · save view · sort  (keyed container -> .st-key-repo_toolbar
    # so CSS can size the icons consistently and badge the folder with a small "+")
    with st.container(key="repo_toolbar"):
        tb = st.columns([1, 1, 1, 3])
        if tb[0].button("⟳", key="pv_repo_refresh", help="Refresh the repository"):
            st.rerun()
        with tb[1].popover("📁", help="New folder"):            # "+" badge added via CSS
            st.caption(f"New folder in **{section}**")
            fnm = st.text_input("Folder name", key="pv_newfolder_name")
            opts = [section] + all_folders(section)
            fpar = st.selectbox("Parent", opts, key=f"pv_newfolder_parent_{section}",
                                format_func=lambda x: "(section root)" if x == section else x)
            if st.button("Create", key="pv_newfolder_btn"):
                if (fnm or "").strip():
                    try:
                        make_folder(fpar, fnm); st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
                else:
                    st.warning("Give the folder a name.")
        with tb[2].popover("💾", help="Save current view"):     # save current view (to section)
            opts = [section] + all_folders(section)
            # prepopulate name + folder from the loaded view so re-saving overwrites it by
            # default; re-seed when the selection changes or the field was cleared/GC'd.
            _sel = st.session_state.get("pv_selected_view")
            if _sel:
                try:
                    _sd = load_view(_sel)
                    if (st.session_state.get("pv_save_seed_for") != _sel
                            or "pv_save_name" not in st.session_state):
                        st.session_state["pv_save_name"] = _sd.get("name", "")
                        _fp = str(Path(_sel).parent)
                        if _fp in opts:
                            st.session_state[f"pv_save_folder_{section}"] = _fp
                        st.session_state["pv_save_seed_for"] = _sel
                except Exception:
                    pass
            st.caption(f"Save current pivot to **{section}**")
            nm = st.text_input("View name", key="pv_save_name")
            fld = st.selectbox("Folder", opts, key=f"pv_save_folder_{section}",
                               format_func=lambda x: "(section root)" if x == section else x)
            if st.button("Save", key="pv_save_btn"):
                if (nm or "").strip():
                    try:
                        rel = save_view(nm, fld, current_state())
                        st.session_state["pv_selected_view"] = rel
                        st.success(f"Saved → {rel}"); st.rerun()
                    except Exception as ex:
                        st.error(str(ex))
                else:
                    st.warning("Give the view a name.")
        sort = tb[3].selectbox("Sort", ["Name ↑", "Name ↓", "Recent"], key="pv_repo_sort",
                               label_visibility="collapsed")
    query = st.text_input("Search", key="pv_repo_search", placeholder="Search views…",
                          label_visibility="collapsed")
    # selected-view metadata card
    sel = st.session_state.get("pv_selected_view")
    if sel:
        # highlight the selected view's row in the tree (its load button lives in a .st-key-vbtn_<slug>
        # container). Quiet accent: tinted fill + a left rule, matching the serif/Tufte palette.
        st.markdown(
            f"<style>.st-key-vbtn_{slugify(sel)} button {{ background:#e3ebf2 !important; "
            f"color:#1a1a1a !important; font-weight:600 !important; "
            f"border-left:3px solid #2a4d69 !important; }}</style>", unsafe_allow_html=True)
        _render_meta_box(sel)
    st.divider()
    tree = list_tree(section)
    if query:
        tree = _filter_tree(tree, query.lower())
    if not tree["folders"] and not tree["views"]:
        st.caption("No matching views." if query else f"No views in {section} yet.")
    else:
        render_tree(tree, base=section, query=query, sort=sort)
    st.markdown("</div>", unsafe_allow_html=True)


def _filter_tree(node: dict, q: str) -> dict:
    """Prune the tree to folders/views whose name matches q (case-insensitive). A folder is
    kept if its name matches or it contains a match (kept folders are auto-expanded)."""
    views = [v for v in node.get("views", []) if q in v["name"].lower()]
    folders = {}
    for fname, sub in node.get("folders", {}).items():
        pruned = _filter_tree(sub, q)
        if q in fname.lower() or pruned["folders"] or pruned["views"]:
            folders[fname] = pruned if (pruned["folders"] or pruned["views"]) else sub
    return {"folders": folders, "views": views}


# the sidebar switch decides which panel shows; in Repository mode, fill the reserved slot
# (just under the switch) with the repository — render_panel is defined by now.
ensure_root()
with repo_slot:
    if mode == "Repository":
        render_panel()

# the grid takes the full main area
main_col = st.container()

# ============================================================================ everything below
# renders into main_col (the grid + caption + download).
with main_col:
    # name of the currently-loaded view (from the repository), shown atop the render.
    _sel = st.session_state.get("pv_selected_view")
    if _sel:
        try:
            _doc = load_view(_sel)
            _fld = _doc.get("path") or ""
            st.markdown(f"<div class='vtitle'>{_doc.get('name', '')}"
                        + (f" <span class='vfolder'>· {_fld}</span>" if _fld else "")
                        + "</div>", unsafe_allow_html=True)
        except Exception:
            st.session_state.pop("pv_selected_view", None)

    # surface a one-shot warning listing anything dropped on the last load
    dropped = st.session_state.pop("pv_load_dropped", None)
    if dropped:
        nm = st.session_state.get("pv_loaded_name", "view")
        st.warning(f"Loaded view “{nm}” dropped (no longer in data): "
                   + ", ".join(dropped))

    # rows/cols overlap guard (multiselect already filters, but be safe)
    cols = [c for c in cols if c not in rows]

    # Chart mode renders the view's embedded Vega-Lite spec(s) generically — each graph runs its
    # own named pivot query. Handle it before the grid's row/measure guards (a chart's queries are
    # self-contained; the grid's own rows/measures may be empty). The view's slicers flow in as the
    # query filters.
    if render == "chart":
        render_spec(filters); st.stop()

    if not measures:
        st.info("Pick at least one **Measure**."); st.stop()

    # split out the "Measures" pseudo-field; the cube is only queried on real dimensions.
    real_rows = [r for r in rows if r != "Measures"]
    real_cols = [c for c in cols if c != "Measures"]
    if not real_rows:
        st.info("Put at least one non-measure field (Issuer, Sector, …) on **Rows**."); st.stop()

    # ------------------------------------------------------------------------- query
    params = {"rows": ",".join(real_rows), "cols": ",".join(real_cols),
              "measures": ",".join(measures), "totals": str(show_totals).lower(),
              "filters": json.dumps(filters)}
    try:
        resp = get("/pivot", **params)
    except requests.HTTPError as e:
        st.error(f"Pivot rejected: {e.response.text}"); st.stop()

    if resp.get("warning"):
        st.warning(resp["warning"])

    if not resp["records"]:
        st.info("No data for this slice."); st.stop()

    _slc = "; ".join(f"{d}={', '.join(v)}" for d, v in filters.items()) or "none"
    st.markdown(f"<div class='cap'>rows: <b>{' › '.join(rows)}</b> &nbsp; "
                f"columns: <b>{' › '.join(cols) or '—'}</b> &nbsp; "
                f"measures: <b>{', '.join(measures)}</b> &nbsp; "
                f"slicers: <b>{_slc}</b></div>", unsafe_allow_html=True)

    TOTAL = "∑ Total"

    def is_pct(meas):
        return as_pct and meas in PCT_MEASURES

    def label(meas):
        return f"{meas} (%)" if is_pct(meas) else meas

    mlabels = {me: label(me) for me in measures}

    # --------------------------------------------------------------------- AgGrid
    # The grid is PRESENTATION ONLY. streamlit-aggrid runs ag-grid Community (enterprise
    # off), which has no row-grouping / aggregation / pivot engine at all — so the grid
    # cannot compute a single number. Every value, including all totals, is exactly what
    # the Atoti cube returned via /pivot. Sorting/reordering only permute display rows;
    # totals are shown as pinned rows so even sorting can't move or fold them.
    AGGRID_CSS = {
        ".ag-root-wrapper": {"border": "1px solid #e4e2dc",
                             "font-family": "Georgia, 'Iowan Old Style', serif"},
        ".ag-header": {"background-color": "#f1efe9", "border-bottom": "1px solid #d8d5cd"},
        ".ag-header-cell-label": {"font-weight": "500", "color": "#1a1a1a"},
        ".ag-cell": {"font-size": "13px"},
        ".ag-row": {"background-color": "#fdfdfb"},
        ".ag-row-pinned": {"font-weight": "600"},
    }
    # pinned (total) rows: always a neutral ground, never part of the colour scale.
    _PINNED = ("if(p.node&&p.node.rowPinned)return{backgroundColor:'#e8e6df',"
               "color:'#1a1a1a',fontWeight:'600',textAlign:'right'};")

    def _fmt_js():
        return JsCode("function(p){if(p.value===null||p.value===undefined||p.value==='')"
                      "return '—';var n=Number(p.value);if(isNaN(n))return '—';"
                      f"return n.toFixed({prec});}}")

    def _heat_js(lo, hi):
        # light→medium blue ramp; dark text below the midpoint, light text above. Capped
        # at a medium blue (never near-navy/black) so text stays readable either way.
        return JsCode(
            "function(p){" + _PINNED + "var v=Number(p.value);"
            "if(p.value===null||p.value===undefined||p.value===''||isNaN(v))"
            "return{backgroundColor:'#f1efe9',color:'#8a8a8a',textAlign:'right'};"
            f"var lo={lo},hi={hi};var t=hi>lo?(v-lo)/(hi-lo):0;if(t<0)t=0;if(t>1)t=1;"
            "var r=Math.round(247+(43-247)*t),g=Math.round(251+(123-251)*t),"
            "b=Math.round(255+(186-255)*t);"
            "return{backgroundColor:'rgb('+r+','+g+','+b+')',"
            "color:(t>0.55?'#f1f1f1':'#1a1a1a'),textAlign:'right'};}")

    def _plain_js():
        return JsCode("function(p){" + _PINNED + "return{textAlign:'right'};}")

    def show_grid(flat, value_cols, label_cols, key, pinned=None, neutral=()):
        """Render a flat dataframe in ag-grid: sortable, resizable, drag-reorderable columns.
        `flat` holds only body rows; `pinned` (optional) are total rows pinned to the bottom.
        Heatmap domains are computed over body cells only, so totals never skew the scale."""
        gb = GridOptionsBuilder.from_dataframe(flat)
        gb.configure_default_column(sortable=True, resizable=True, filter=False,
                                    suppressMovable=False, minWidth=95)
        for c in label_cols:
            gb.configure_column(c, pinned="left", suppressMovable=True, minWidth=150,
                                cellStyle={"fontWeight": "500"})
        for c in value_cols:
            vals = pd.to_numeric(flat[c], errors="coerce")
            if c in neutral or not heat:
                style = _plain_js()
            else:
                lo = float(vals.min()) if vals.notna().any() else 0.0
                hi = float(vals.max()) if vals.notna().any() else 0.0
                style = _heat_js(lo, hi)
            gb.configure_column(c, type=["numericColumn"], valueFormatter=_fmt_js(),
                                cellStyle=style)
        opts = gb.build()
        if pinned is not None and len(pinned):
            # NaN must become JSON null, NOT the literal NaN (invalid JSON -> frontend parse error).
            opts["pinnedBottomRowData"] = json_safe_records(pinned.to_dict("records"))

        token = st.session_state.get("pv_load_token", 0)
        # Reapply the saved sort ONCE, when a view is loaded (token changed), from inside
        # onFirstDataRendered — that fires only when the grid is freshly created, so the sort is
        # set at init via applyColumnState and the grid OWNS it thereafter. Pushing the sort on
        # every render instead fights the user's interactive sort -> endless flip. Interactive
        # sorts are captured below (update_on=sortChanged) for the next save.
        # Match the saved sort to a present column, tolerating the "(%)" display suffix that the
        # "Risk measures as %" toggle adds/removes — so a sort saved on "Marginal Scenario VaR 99"
        # still applies to the "Marginal Scenario VaR 99 (%)" column (and vice-versa).
        def _norm(c):
            return c[:-4] if isinstance(c, str) and c.endswith(" (%)") else c
        norm_to_actual = {_norm(c): c for c in (list(value_cols) + list(label_cols))}
        present = set(label_cols) | set(value_cols)
        sort_state = []
        for s in st.session_state.get("pv_grid_sort", []):
            if not s.get("sort"):
                continue
            cid = s.get("colId")
            actual = cid if cid in present else norm_to_actual.get(_norm(cid))
            if actual:
                sort_state.append({"colId": actual, "sort": s["sort"],
                                   "sortIndex": s.get("sortIndex", 0)})
        fresh_load = st.session_state.get("pv_sort_applied_token") != token
        st.session_state["pv_sort_applied_token"] = token
        if fresh_load and sort_state:
            opts["onFirstDataRendered"] = JsCode(
                "function(p){var a=p.api;var s=" + json.dumps(sort_state) + ";"
                "if(a.applyColumnState){a.applyColumnState({state:s,defaultState:{sort:null}});}"
                "else if(p.columnApi){p.columnApi.applyColumnState({state:s,defaultState:{sort:null}});}"
                "a.sizeColumnsToFit();}")
        else:
            opts["onFirstDataRendered"] = JsCode("function(p){p.api.sizeColumnsToFit();}")
        opts["onGridSizeChanged"] = JsCode("function(p){p.api.sizeColumnsToFit();}")

        n = len(flat) + (len(pinned) if pinned is not None else 0)
        # the load token forces a fresh grid on view-load so onFirstDataRendered re-fires and the
        # saved sort is applied even when the column set is unchanged; interactive sort keeps the key.
        sig = (f"{key}::{int(show_panel)}::{token}"
               f"::{'|'.join(label_cols)}::{'|'.join(value_cols)}")
        grid_ret = AgGrid(flat, gridOptions=opts, theme="alpine", height=min(640, 80 + 28 * n),
                          allow_unsafe_jscode=True, enable_enterprise_modules=False,
                          fit_columns_on_grid_load=True, update_mode=GridUpdateMode.NO_UPDATE,
                          update_on=["sortChanged"], custom_css=AGGRID_CSS, key=sig)
        # capture the live sort back so it can be saved with the view. Only sync when the grid
        # actually REPORTED its column state (a non-empty list); on the first render after a load
        # the component returns nothing — syncing then would wipe the just-applied sort.
        try:
            cs = getattr(grid_ret, "columns_state", None)
            if cs:
                sortm = sorted((c for c in cs if c.get("sort")),
                               key=lambda c: (c.get("sortIndex") if c.get("sortIndex") is not None else 0))
                st.session_state["pv_grid_sort"] = [
                    {"colId": c.get("colId"), "sort": c.get("sort"), "sortIndex": c.get("sortIndex")}
                    for c in sortm]
        except Exception:
            pass

    # --- ONE grid. "Measures" is just another axis field (Measure column) placed on rows
    #     or columns. Margins come from the cube; measures are NEVER aggregated across — the
    #     Measure value is always kept as part of every key, so totals stay per-measure. ----
    # Axis field lists, substituting the real "Measure" column for the "Measures" token.
    row_fields = ["Measure" if t == "Measures" else t for t in rows]
    col_fields = ["Measure" if t == "Measures" else t for t in cols]
    if "Measures" not in rows + cols and len(measures) > 1:
        col_fields = col_fields + ["Measure"]          # default: group measures across columns

    def cells_from(wide, present):
        """Melt a wide (one column per measure) cube result into long (Measure, Value) cells,
        filling every real dimension it does NOT carry (i.e. aggregated away) with TOTAL.
        Returns (cells, has_missing_measure)."""
        if wide is None or wide.empty:
            return None, True
        w = wide.copy()
        have = [me for me in measures if me in w.columns]
        if not have:
            return None, True
        for me in have:
            # some measures are non-scalar at fine granularity (e.g. Specific vol per Factor
            # arrives as a dict) — coerce to numeric so those cells become NaN -> "—".
            w[me] = pd.to_numeric(w[me], errors="coerce")
            if is_pct(me):
                w[me] = w[me] * 100.0
        idc = [d for d in present if d in w.columns]
        long = w.melt(id_vars=idc, value_vars=have, var_name="_m", value_name="Value")
        long["Measure"] = long["_m"].map(mlabels)
        for d in real_rows + real_cols:               # absent real dims were aggregated -> TOTAL
            if d not in long.columns:
                long[d] = TOTAL
        keep = list(dict.fromkeys(real_rows + real_cols + ["Measure", "Value"]))
        return long[keep], len(have) < len(measures)

    def _fmt_dates(df):
        """Render the Date label column with the chosen date format (presentation only)."""
        if df is not None and not df.empty and "Date" in df.columns:
            df = df.copy()
            try:
                df["Date"] = pd.to_datetime(df["Date"]).dt.strftime(date_fmt)
            except Exception:
                pass
        return df

    per_row = _fmt_dates(pd.DataFrame(resp.get("per_row", [])))
    per_col = _fmt_dates(pd.DataFrame(resp.get("per_col", [])))
    grand = pd.DataFrame([resp["grand"]]) if resp.get("grand") else pd.DataFrame()
    do_row_tot = row_tot and bool(real_cols)          # Total column: aggregate over real columns
    do_col_tot = col_tot and bool(real_rows)          # Total row: aggregate over real rows
    undefined = False

    pieces = [cells_from(_fmt_dates(pd.DataFrame(resp["records"])), real_rows + real_cols)[0]]
    if do_row_tot:
        p, miss = cells_from(per_row, real_rows)
        pieces.append(p); undefined |= (p is None) or miss or (p is not None and p["Value"].isna().any())
    if do_col_tot:
        src, pres = (per_col, real_cols) if real_cols else (grand, [])
        p, miss = cells_from(src, pres)
        pieces.append(p); undefined |= (p is None) or miss or (p is not None and p["Value"].isna().any())
    if do_row_tot and do_col_tot:                     # the corner cell(s), per measure
        p, _ = cells_from(grand, [])
        pieces.append(p); undefined |= (p is None) or (p is not None and p["Value"].isna().any())

    allcells = pd.concat([p for p in pieces if p is not None], ignore_index=True)
    if not col_fields:                                # measures on rows, no real columns -> 1 value col
        allcells["_v"] = "value"; col_fields = ["_v"]
    mat = allcells.pivot_table(index=row_fields, columns=col_fields, values="Value", aggfunc="first")

    def _is_total(t, fields, dims):
        pos = [i for i, f in enumerate(fields) if f in dims]
        if not pos:
            return False
        t = t if isinstance(t, tuple) else (t,)
        return all(t[i] == TOTAL for i in pos)

    col_total = [_is_total(t, col_fields, real_cols) for t in mat.columns]
    row_total = [_is_total(t, row_fields, real_rows) for t in mat.index]
    mat.columns = [" / ".join(str(x) for x in (t if isinstance(t, tuple) else (t,))) for t in mat.columns]

    if hide_empty:
        # drop all-blank body columns first, then all-blank body rows of what remains;
        # total rows/cols are always kept (an undefined total is information, not emptiness).
        ckeep = [t or mat.iloc[:, i].notna().any() for i, t in enumerate(col_total)]
        mat = mat.loc[:, ckeep]; col_total = [t for t, k in zip(col_total, ckeep) if k]
        rkeep = [t or mat.iloc[i].notna().any() for i, t in enumerate(row_total)]
        mat = mat.loc[rkeep]; row_total = [t for t, k in zip(row_total, rkeep) if k]

    neutral = {c for c, m in zip(mat.columns, col_total) if m}
    value_cols = list(mat.columns)

    body = mat.loc[[ix for ix, m in zip(mat.index, row_total) if not m]].reset_index()
    tots = mat.loc[[ix for ix, m in zip(mat.index, row_total) if m]]
    pinned = tots.reset_index() if len(tots) else None

    show_grid(body, value_cols, row_fields, key="pivot", pinned=pinned, neutral=neutral)
    st.download_button("Download CSV", mat.reset_index().to_csv(index=False).encode(),
                       "pivot.csv", "text/csv")

    # ---- Risk-analyst commentary (on-demand) -------------------------------------------------
    # POSTs the SAME query this grid ran to /analysis, which re-runs the guarded pivot and streams
    # a written read from the LLM (plain Messages API, no tools). Button-triggered only — no token
    # spend unless asked. Cached per (view, slice) in session_state; Regenerate forces a fresh call.
    st.divider()
    _an_name = st.session_state.get("pv_loaded_name", "this view")
    _an_key = json.dumps([_an_name, real_rows, real_cols, measures, filters, bool(show_totals)],
                         sort_keys=True, default=str)
    _an_cache = st.session_state.setdefault("pv_analysis_cache", {})
    with st.expander("🔍 Risk-analyst commentary", expanded=bool(_an_cache.get(_an_key))):
        _notes = st.text_input("Optional context for the analyst (e.g. a mandate limit, what you're checking)",
                               key="pv_analysis_notes")
        _cached = _an_cache.get(_an_key)
        if st.button("Regenerate analysis" if _cached else f"Analyze “{_an_name}”", key="pv_analysis_btn"):
            body_json = {"rows": ",".join(real_rows), "cols": ",".join(real_cols),
                         "measures": ",".join(measures), "filters": json.dumps(filters),
                         "totals": bool(show_totals), "name": _an_name, "notes": _notes or None}
            try:
                r = requests.post(f"{API}/analysis", json=body_json, stream=True, timeout=180)
                r.raise_for_status()
                r.encoding = "utf-8"
                text = st.write_stream(c for c in r.iter_content(chunk_size=None, decode_unicode=True) if c)
                _an_cache[_an_key] = text
            except requests.HTTPError as e:
                detail = e.response.text if e.response is not None else str(e)
                st.error(f"Analysis failed: {detail}")
            except requests.RequestException as e:
                st.error(f"Analysis request failed: {e}")
        elif _cached:
            st.markdown(_cached)
            st.caption("Cached for this view + slice — click **Regenerate** for a fresh read.")

    if undefined:
        st.caption("“—” totals are **undefined**, not zero: a VaR can’t be aggregated across "
                   "scenario sets (ragged, non-additive). Additive measures (Net exposure) total fully.")

    st.markdown("<div class='cap'>Generic cube pivot via /pivot. The grid is display-only "
                "(ag-grid Community — no client-side grouping/aggregation); sort, resize and "
                "drag-reorder columns freely. <b>Every number, including all totals, is computed "
                "by the Atoti cube</b> — totals are recomputed at the aggregated level (not summed "
                "from cells), so VaR margins are the true book-level VaR, not a sum of parts.</div>",
                unsafe_allow_html=True)
