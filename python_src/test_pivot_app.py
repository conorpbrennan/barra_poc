"""
test_pivot_app.py — UI-flow tests for risk_pivot_app.py ("Flex Agg ++") via Streamlit AppTest.

Covers: default mode/measures, Pivot↔Repository switch, state persistence across the switch,
single-click load applies a view, save sets the loaded view as selected, the Save form
prepopulates from the loaded/selected view (incl. after a Pivot round-trip), and graceful
drop of stale members on load.

Requires the FastAPI backend on http://127.0.0.1:8010 (provides /dims, /pivot); if it isn't
reachable the suite SKIPS rather than fails. Run with the project venv:
    BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_pivot_app.py
Uses a TEMP VIEWS_ROOT seeded with one view, so it never touches the real repo.
"""
from __future__ import annotations
import os
import json
import tempfile
import shutil
from pathlib import Path

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
RESULTS = []
TMP_ROOT = None


def test(fn):
    RESULTS.append(fn)
    return fn


def _backend_up():
    try:
        import requests
        return requests.get(f"{API}/dims", timeout=5).status_code == 200
    except Exception:
        return False


def _seed_repo():
    """Temp VIEWS_ROOT with Public/Examples/<one view>; point views_repo at it."""
    import views_repo as R
    global TMP_ROOT
    TMP_ROOT = Path(tempfile.mkdtemp(prefix="viewuitest_"))
    R.VIEWS_ROOT = TMP_ROOT
    R.ensure_root()
    R.make_folder("Public", "Examples")
    R.save_view("Demo View", "Public/Examples", {
        "rows": ["Sector"], "cols": [], "measures": ["Net exposure"],
        "slice_dims": ["Date"], "filters": {"Date": ["2024-12-31"]},
        "row_tot": False, "col_tot": False, "as_pct": True,
        "hide_empty": True, "heat": True, "prec": 3})


def _run():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file("risk_pivot_app.py", default_timeout=90).run()


def _ss(at, k, d=None):
    try:
        return at.session_state[k]
    except Exception:
        return d


@test
def t_default_mode_and_measures():
    at = _run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_mode") == "Pivot"
    assert _ss(at, "pv_measures") == ["Total VaR 99", "Scenario VaR 99", "Specific vol"]
    assert _ss(at, "pv_rows") == ["Book"]
    # repository hidden in Pivot mode -> no view-load buttons
    assert not [b for b in at.button if str(b.key).startswith("load_")]


@test
def t_switch_to_repository_shows_tree():
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    assert not at.exception, list(at.exception)
    loads = [b.key for b in at.button if str(b.key).startswith("load_")]
    assert any("Demo View".lower().replace(" ", "-") in k for k in loads), loads
    # pivot multiselects are hidden in Repository mode
    assert "Rows" not in [m.label for m in at.sidebar.multiselect]


@test
def t_state_persists_across_mode_switch():
    at = _run()
    at.multiselect(key="pv_rows").set_value(["Issuer"]).run()
    at.radio(key="pv_mode").set_value("Repository").run()
    assert _ss(at, "pv_rows") == ["Issuer"], "lost pivot state entering Repository"
    at.radio(key="pv_mode").set_value("Pivot").run()
    assert _ss(at, "pv_rows") == ["Issuer"], "lost pivot state returning to Pivot"
    assert not at.exception, list(at.exception)


@test
def t_single_click_load_applies_view():
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Demo").run()   # auto-expands folders
    key = [b.key for b in at.button if str(b.key).startswith("load_")][0]
    at.button(key=key).click().run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_rows") == ["Sector"], _ss(at, "pv_rows")
    assert _ss(at, "pv_measures") == ["Net exposure"]
    assert _ss(at, "pv_selected_view", "").endswith("demo-view.json")


@test
def t_selected_view_is_highlighted_in_tree():
    """Loading a view marks it selected: its tree row shows the ▣ marker, and a highlight <style>
    targeting its keyed container (.st-key-vbtn_<slug>) is emitted so the row stands out."""
    import views_repo as R
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Demo").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_")][0]
    at.button(key=key).click().run()
    sel = _ss(at, "pv_selected_view")
    # the selected row's load button carries the ▣ (filled) marker
    sel_labels = [b.label for b in at.button if str(b.key) == f"load_{sel}"]
    assert sel_labels and sel_labels[0].startswith("▣"), sel_labels
    # a highlight <style> for the selected view's keyed container is emitted
    slug = R.slugify(sel)
    styles = [m.value for m in at.markdown if f"vbtn_{slug}" in m.value and "background" in m.value]
    assert styles, "no highlight <style> emitted for the selected view"
    # the folder holding the selected view stays expanded even with NO active search
    at.text_input(key="pv_repo_search").set_value("").run()
    folder = Path(sel).parent.name                      # e.g. "Examples"
    open_folders = [e.label for e in at.expander if getattr(e.proto, "expanded", False)]
    assert any(folder in lbl for lbl in open_folders), (folder, open_folders)


@test
def t_save_form_prepopulates_after_load_and_roundtrip():
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Demo").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_")][0]
    at.button(key=key).click().run()
    # immediately after load, the save form reflects the loaded view
    assert _ss(at, "pv_save_name") == "Demo View", _ss(at, "pv_save_name")
    assert _ss(at, "pv_save_folder_Public") == "Public/Examples"
    # edit in Pivot, return to Repository -> still prepopulated (survives GC)
    at.radio(key="pv_mode").set_value("Pivot").run()
    at.multiselect(key="pv_rows").set_value(["Factor"]).run()
    at.radio(key="pv_mode").set_value("Repository").run()
    assert _ss(at, "pv_save_name") == "Demo View"
    assert _ss(at, "pv_save_folder_Public") == "Public/Examples"


@test
def t_save_new_view_becomes_selected():
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_save_name").set_value("Brand New").run()
    at.button(key="pv_save_btn").click().run()
    assert not at.exception, list(at.exception)
    sel = _ss(at, "pv_selected_view", "")
    assert sel.endswith("brand-new.json"), sel
    # re-opening save repopulates to the just-saved view
    assert _ss(at, "pv_save_name") == "Brand New"


@test
def t_graceful_drop_on_load_of_stale_view():
    import views_repo as R
    # write a view referencing a bogus measure + bogus Date member
    R.save_view("Stale", "Public", {
        "rows": ["Issuer"], "cols": [], "measures": ["Net exposure", "NOPE MEASURE"],
        "slice_dims": ["Date"], "filters": {"Date": ["1999-01-01"]},
        "row_tot": False, "col_tot": False, "as_pct": True,
        "hide_empty": True, "heat": True, "prec": 3})
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Stale").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_") and "stale" in b.key][0]
    at.button(key=key).click().run()
    assert not at.exception, list(at.exception)
    # bogus measure dropped, real one kept; bogus Date member dropped -> empty slice
    assert _ss(at, "pv_measures") == ["Net exposure"], _ss(at, "pv_measures")
    assert _ss(at, "slice_Date", []) == [], _ss(at, "slice_Date")


@test
def t_sort_persists_on_load():
    import views_repo as R
    sortmodel = [{"colId": "Net exposure", "sort": "desc", "sortIndex": 0}]
    R.save_view("Sorted View", "Public", {
        "rows": ["Issuer"], "cols": [], "measures": ["Net exposure"],
        "slice_dims": ["Date"], "filters": {"Date": ["2024-12-31"]},
        "row_tot": False, "col_tot": False, "as_pct": True,
        "hide_empty": True, "heat": True, "prec": 3, "sort": sortmodel})
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Sorted").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_") and "sorted-view" in b.key][0]
    tok = _ss(at, "pv_load_token", 0)
    at.button(key=key).click().run()
    assert not at.exception, list(at.exception)
    # the saved sort is applied to the grid-sort state, ready to push to the grid
    assert _ss(at, "pv_grid_sort") == sortmodel, _ss(at, "pv_grid_sort")
    # load token bumped so the grid re-instantiates and applies the sort
    assert _ss(at, "pv_load_token", 0) > tok


_VL5 = "https://vega.github.io/schema/vega-lite/v5.json"
_LINE_SPEC = {"$schema": _VL5, "mark": {"type": "line"},
              "encoding": {"x": {"field": "Date", "type": "temporal"},
                           "y": {"field": "Net exposure", "type": "quantitative"}}}


_SEED_QUERY = {"name": "Q1", "rows": ["Date"], "cols": [], "measures": ["Net exposure"],
               "filters": {"Book": ["Soros"], "ScenarioSet": ["HistFull"]}}


def _seed_chart_view(name, slug_unused=None):
    import views_repo as R
    R.save_view(name, "Public", {
        "rows": ["Date"], "cols": [], "measures": ["Net exposure"],
        "slice_dims": ["Book", "ScenarioSet"],
        "filters": {"Book": ["Soros"], "ScenarioSet": ["HistFull"]},
        "row_tot": False, "col_tot": False, "as_pct": True, "hide_empty": True,
        "heat": True, "prec": 3, "sort": [],
        "render": "chart", "queries": [dict(_SEED_QUERY)], "chart": _LINE_SPEC})


@test
def t_scenario_day_chart_renders_records():
    """A scenario query (rows=[ScenarioDay]) is a NORMAL pivot query: the synthetic ScenarioDay
    dimension unpacks the P&L vector per day, and the /pivot records bind as the spec's default
    dataset (no named-dataset machinery). The chart renders with a non-empty DataFrame."""
    import streamlit as st
    import views_repo as R
    import pandas as pd
    spec = {"$schema": _VL5, "source": "Path",
            "transform": [{"calculate": "toDate(datum['Scenario date at day (epoch)'] * 86400000)",
                           "as": "date"}],
            "mark": {"type": "line"},
            "encoding": {"x": {"field": "date", "type": "temporal"},
                         "y": {"field": "Scenario PnL at day", "type": "quantitative"}}}
    covid = {"Book": ["Soros"], "Date": ["2024-12-31"], "ScenarioSet": ["Evt:COVID2020"]}
    R.save_view("Scen Day", "Public", {
        "rows": ["ScenarioDay"], "cols": [], "measures": ["Scenario PnL at day"],
        "slice_dims": ["Book", "Date", "ScenarioSet"], "filters": covid,
        "row_tot": False, "col_tot": False, "as_pct": True, "hide_empty": True, "heat": True,
        "prec": 3, "sort": [], "render": "chart",
        "queries": [{"name": "Path", "rows": ["ScenarioDay"], "cols": [],
                     "measures": ["Scenario PnL at day", "Scenario date at day (epoch)"],
                     "filters": covid}], "chart": spec})
    captured = []
    orig = st.vega_lite_chart

    def _spy(*a, **k):
        captured.append(a[0] if a else None)            # the DataFrame is the 1st positional arg
        return orig(*a, **k)
    st.vega_lite_chart = _spy
    try:
        at = _run()
        at.radio(key="pv_mode").set_value("Repository").run()
        at.text_input(key="pv_repo_search").set_value("Scen Day").run()
        key = [b.key for b in at.button if str(b.key).startswith("load_") and "scen-day" in b.key][0]
        captured.clear()
        at.button(key=key).click().run()
    finally:
        st.vega_lite_chart = orig
    assert not at.exception, list(at.exception)
    assert captured, "no chart rendered"
    df = captured[0]
    assert isinstance(df, pd.DataFrame) and len(df) > 0, df
    assert "Scenario PnL at day" in df.columns, list(df.columns)


@test
def t_chart_view_renders_no_exception():
    """A view whose JSON carries render=chart + a Vega-Lite spec renders via the generic
    pass-through with no exception (the app holds zero chart logic)."""
    _seed_chart_view("Chart Line")
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Chart Line").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_") and "chart-line" in b.key][0]
    at.button(key=key).click().run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_render") == "chart"
    assert _ss(at, "pv_chart") == _LINE_SPEC          # the spec drove the render, from the JSON


@test
def t_chart_mode_without_spec_is_graceful():
    """Switching an ad-hoc pivot (no embedded spec) to chart shows guidance, never crashes."""
    at = _run()
    at.selectbox(key="pv_render").set_value("chart").run()
    assert not at.exception, list(at.exception)


@test
def t_chart_spec_persists_through_load_and_save():
    """Loading a chart view populates the spec/source/render in state, and re-saving writes
    them back verbatim — the graph definition lives entirely in the view JSON."""
    import views_repo as R
    _seed_chart_view("Persist Chart")
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Persist Chart").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_") and "persist-chart" in b.key][0]
    at.button(key=key).click().run()
    assert _ss(at, "pv_chart") == _LINE_SPEC
    # the save form is prepopulated to "Persist Chart"; re-save and confirm the JSON keeps the spec
    at.button(key="pv_save_btn").click().run()
    assert not at.exception, list(at.exception)
    saved = R.load_view("Public/persist-chart.json")["state"]
    assert saved["render"] == "chart"
    assert saved["queries"] == [dict(_SEED_QUERY)]
    assert saved["chart"] == _LINE_SPEC


@test
def t_graph_builder_untouched_preserves_chart():
    """A fresh builder is 'untouched' so it does NOT overwrite a loaded view's spec."""
    at = _run()
    assert _ss(at, "pv_gb_touched") is False
    assert _ss(at, "pv_chart") is None      # default ad-hoc view has no chart


@test
def t_graph_builder_items_drive_spec():
    """Editing an item (Mark) makes the builder own pv_chart, reflecting the chosen mark."""
    at = _run()
    at.selectbox(key="pv_gb_mark_0").set_value("bar").run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_gb_touched") is True
    ch = _ss(at, "pv_chart")
    spec = ch[0] if isinstance(ch, list) else ch
    assert spec and spec["mark"]["type"] == "bar", ch
    # the builder owns a single named query (Query 1, pre-filled from the grid pivot)
    qs = _ss(at, "pv_queries")
    assert isinstance(qs, list) and len(qs) == 1 and qs[0]["name"] == "Query 1", qs
    assert ch[0].get("source") == "Query 1" if isinstance(ch, list) else spec.get("source") == "Query 1"


@test
def t_graph_builder_reverse_parses_loaded_view():
    """Loading a chart view populates the builder items from its spec (one graph per spec),
    while leaving the original spec untouched so it still renders verbatim."""
    _seed_chart_view("Reverse Me")
    at = _run()
    at.radio(key="pv_mode").set_value("Repository").run()
    at.text_input(key="pv_repo_search").set_value("Reverse Me").run()
    key = [b.key for b in at.button if str(b.key).startswith("load_") and "reverse-me" in b.key][0]
    at.button(key=key).click().run()
    at.radio(key="pv_mode").set_value("Pivot").run()          # builder lives in Pivot mode
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_gb_n") == 1
    assert _ss(at, "pv_gb_mark_0") == "line"
    assert _ss(at, "pv_gb_x_0") == "Date"
    assert _ss(at, "pv_gb_xtype_0") == "temporal"
    assert _ss(at, "pv_gb_meas_0") == ["Net exposure"]
    assert _ss(at, "pv_gb_touched") is False                  # original spec preserved
    assert _ss(at, "pv_chart") == _LINE_SPEC


@test
def t_graph_builder_inputs_reconstruct_spec():
    """Every item input is reflected in the generated spec (full reconstruction)."""
    at = _run()
    at.selectbox(key="pv_gb_mark_0").set_value("bar").run()
    at.number_input(key="pv_gb_height_0").set_value(260).run()
    at.text_input(key="pv_gb_xtitle_0").set_value("Book").run()
    at.text_input(key="pv_gb_ytitle_0").set_value("Risk").run()
    at.selectbox(key="pv_gb_yfmt_0").set_value("decimal").run()
    at.text_input(key="pv_gb_title_0").set_value("My Chart").run()
    assert not at.exception, list(at.exception)
    ch = _ss(at, "pv_chart")
    spec = ch[0] if isinstance(ch, list) else ch
    assert spec["mark"]["type"] == "bar"
    assert spec["height"] == 260
    assert spec["title"] == "My Chart"
    assert spec["encoding"]["x"]["title"] == "Book"
    assert spec["encoding"]["y"]["title"] == "Risk"
    assert spec["encoding"]["y"]["axis"] == {"format": ".2f"}


@test
def t_date_format_default_and_applies_to_graph_axis():
    """Date format defaults to ISO and, when chosen, is baked into a temporal chart axis."""
    at = _run()
    assert _ss(at, "pv_date_fmt") == "%Y-%m-%d"
    at.multiselect(key="pv_qry_rows_0").set_value(["Date"]).run()       # drive the QUERY, not the grid
    at.multiselect(key="pv_qry_meas_0").set_value(["Net exposure"]).run()
    at.selectbox(key="pv_date_fmt").set_value("%d %b %Y").run()
    at.selectbox(key="pv_gb_mark_0").set_value("bar").run()      # touch the builder
    assert not at.exception, list(at.exception)
    ch = _ss(at, "pv_chart")
    spec = ch[0] if isinstance(ch, list) else ch
    assert spec["encoding"]["x"]["field"] == "Date"
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["x"]["axis"] == {"format": "%d %b %Y"}, spec["encoding"]["x"]


@test
def t_date_format_applies_to_all_graphs_at_render():
    """The Date Format selectbox drives dates on EVERY graph at render: a temporal axis gets
    `axis.format`, and an ordinal axis that formats via `timeFormat(...,'%d %b')` in a labelExpr
    (like the COVID sector loss curve) has that quoted format rewritten — both off the live setting."""
    import streamlit as st
    import views_repo as R
    flt = {"Book": ["Soros"], "ScenarioSet": ["HistFull"]}
    q = {"name": "Q", "rows": ["Date"], "cols": [], "measures": ["Net exposure"], "filters": flt}
    temporal = {"$schema": _VL5, "source": "Q", "mark": {"type": "line"},
                "encoding": {"x": {"field": "Date", "type": "temporal"},
                             "y": {"field": "Net exposure", "type": "quantitative"}}}
    ordinal = {"$schema": _VL5, "source": "Q", "mark": {"type": "area"},
               "encoding": {"x": {"field": "Date", "type": "ordinal",
                                  "axis": {"labelExpr": "timeFormat(toDate(datum.value), '%d %b')"}},
                            "y": {"field": "Net exposure", "type": "quantitative"}}}
    R.save_view("All Fmt", "Public", {
        "rows": ["Date"], "cols": [], "measures": ["Net exposure"],
        "slice_dims": ["Book", "ScenarioSet"], "filters": flt,
        "row_tot": False, "col_tot": False, "as_pct": True, "hide_empty": True, "heat": True,
        "prec": 3, "sort": [], "render": "chart", "date_fmt": "%d %b %Y",
        "queries": [q], "chart": [temporal, ordinal]})
    captured = []
    orig = st.vega_lite_chart

    def _spy(*a, **k):
        captured.append(k.get("spec"))
        return orig(*a, **k)
    st.vega_lite_chart = _spy
    try:
        at = _run()
        at.radio(key="pv_mode").set_value("Repository").run()
        at.text_input(key="pv_repo_search").set_value("All Fmt").run()
        key = [b.key for b in at.button if str(b.key).startswith("load_") and "all-fmt" in b.key][0]
        captured.clear()
        at.button(key=key).click().run()
    finally:
        st.vega_lite_chart = orig
    assert not at.exception, list(at.exception)
    specs = [s for s in captured if s]
    assert len(specs) == 2, f"expected 2 charts, got {len(specs)}"
    # graph 1: temporal axis carries the chosen format
    assert specs[0]["encoding"]["x"]["axis"]["format"] == "%d %b %Y", specs[0]["encoding"]["x"]
    # graph 2: the labelExpr's timeFormat() string is rewritten to the chosen format
    assert "'%d %b %Y'" in specs[1]["encoding"]["x"]["axis"]["labelExpr"], specs[1]["encoding"]["x"]


@test
def t_chart_grouping_matches_full_row_grouping():
    """With >1 row dim, the non-X dims become a `detail` channel so the chart groups exactly as
    the cube query did (no implicit client-side aggregation). One row dim -> no detail."""
    at = _run()
    at.multiselect(key="pv_qry_rows_0").set_value(["Sector", "Issuer"]).run()   # the QUERY's rows
    at.multiselect(key="pv_qry_meas_0").set_value(["Net exposure"]).run()
    at.selectbox(key="pv_gb_mark_0").set_value("bar").run()      # touch
    ch = _ss(at, "pv_chart")
    spec = ch[0] if isinstance(ch, list) else ch
    fields = {d["field"] for d in spec["encoding"].get("detail", [])}
    assert spec["encoding"]["x"]["field"] == "Sector"
    assert fields == {"Issuer"}, spec["encoding"].get("detail")
    # single row dim -> no detail
    at.multiselect(key="pv_qry_rows_0").set_value(["Sector"]).run()
    ch1 = _ss(at, "pv_chart")
    spec1 = ch1[0] if isinstance(ch1, list) else ch1
    assert "detail" not in spec1["encoding"]


@test
def t_graph_builder_new_graph_has_full_defaults():
    """Adding a graph seeds a complete, valid spec with appropriate defaults."""
    at = _run()
    at.button(key="pv_gb_add").click().run()
    g2 = _ss(at, "pv_chart")[1]
    assert g2["mark"]["type"] == "line"
    assert g2["height"] == 380
    assert "transform" in g2                 # >1 default measure -> folded series
    assert "color" in g2["encoding"]


@test
def t_builder_named_queries_and_graph_refs():
    """Define 2 named pivot queries (structurally different — a Date series and a ScenarioDay/Sector
    series), then 2 graphs each referencing one by name -> `queries` is the 2 named definitions, and
    each spec references its query by name."""
    at = _run()
    # Query 1 pre-fills from the grid; set it explicitly to a Date series
    at.multiselect(key="pv_qry_rows_0").set_value(["Date"]).run()
    at.multiselect(key="pv_qry_meas_0").set_value(["Net exposure"]).run()
    at.text_input(key="pv_qry_name_0").set_value("By date").run()
    # 2nd query: per-day scenario P&L by Sector, named
    at.button(key="pv_qry_add").click().run()
    at.text_input(key="pv_qry_name_1").set_value("By Sector").run()
    at.multiselect(key="pv_qry_rows_1").set_value(["ScenarioDay", "Sector"]).run()
    at.multiselect(key="pv_qry_meas_1").set_value(["Scenario PnL at day"]).run()
    # 2nd graph referencing the 2nd query
    at.button(key="pv_gb_add").click().run()
    at.selectbox(key="pv_gb_source_1").set_value("By Sector").run()
    assert not at.exception, list(at.exception)
    qs = _ss(at, "pv_queries")
    assert isinstance(qs, list) and len(qs) == 2, qs
    assert qs[0]["name"] == "By date" and qs[0]["rows"] == ["Date"], qs[0]
    assert qs[1]["name"] == "By Sector" and qs[1]["rows"] == ["ScenarioDay", "Sector"], qs[1]
    ch = _ss(at, "pv_chart")
    assert isinstance(ch, list) and len(ch) == 2
    assert ch[0].get("source") == "By date" and ch[1].get("source") == "By Sector"
    # the live JSON is the self-contained view: named queries + graphs referencing them by name
    bundle = json.loads(_ss(at, "pv_graph_json"))
    assert [q["name"] for q in bundle["queries"]] == ["By date", "By Sector"]


@test
def t_builder_columns_and_filters_regenerate_queries():
    """In chart mode, a query carries its Columns and the Slicers baked in as its filters; changing a
    Column or a Slicer regenerates the queries/graphs/JSON (the builder owns the view once touched)."""
    at = _run()
    at.selectbox(key="pv_render").set_value("chart").run()           # chart mode -> slicer changes regen
    at.multiselect(key="pv_qry_rows_0").set_value(["Sector"]).run()
    at.multiselect(key="pv_qry_meas_0").set_value(["Net exposure"]).run()
    qs = _ss(at, "pv_queries")
    assert qs[0]["rows"] == ["Sector"] and "cols" in qs[0] and "filters" in qs[0], qs[0]
    # default slicers include Book -> baked into the query's filters
    assert "Book" in qs[0]["filters"], qs[0]["filters"]
    # add a Column -> regenerates
    at.multiselect(key="pv_qry_cols_0").set_value(["ScenarioSet"]).run()
    assert _ss(at, "pv_queries")[0]["cols"] == ["ScenarioSet"], _ss(at, "pv_queries")[0]
    # clear the Book slicer -> the query's baked filters regenerate WITHOUT Book
    at.multiselect(key="slice_Book").set_value([]).run()
    assert "Book" not in _ss(at, "pv_queries")[0]["filters"], _ss(at, "pv_queries")[0]["filters"]


@test
def t_graph_raw_bundle_applies_queries_and_chart():
    """Applying a full {queries, chart} bundle in the raw editor sets BOTH the named queries and the
    specs (the view is entirely self-contained)."""
    view = {"queries": [{"name": "A", "rows": ["Date"], "cols": [], "measures": ["Net exposure"]},
                        {"name": "B", "rows": ["ScenarioDay", "Sector"], "cols": [],
                         "measures": ["Scenario PnL at day"]}],
            "chart": [_LINE_SPEC, {"mark": "area", "encoding": {}}]}
    at = _run()
    at.text_area(key="pv_graph_json").set_value(json.dumps(view)).run()
    at.button(key="pv_graph_apply").click().run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_queries") == view["queries"]
    assert _ss(at, "pv_chart") == view["chart"]


@test
def t_graph_builder_add_graph_makes_list():
    """➕ Add graph yields a second graph -> the view's chart becomes a list of specs."""
    at = _run()
    at.button(key="pv_gb_add").click().run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_gb_n") == 2
    ch = _ss(at, "pv_chart")
    assert isinstance(ch, list) and len(ch) == 2, ch


@test
def t_graph_raw_json_applies_and_is_graceful():
    """The Advanced raw-JSON editor still applies a spec, and malformed JSON never raises."""
    spec = {"mark": {"type": "line"},
            "encoding": {"x": {"field": "Date", "type": "temporal"},
                         "y": {"field": "Net exposure", "type": "quantitative"}}}
    at = _run()
    at.text_area(key="pv_graph_json").set_value(json.dumps(spec)).run()
    at.button(key="pv_graph_apply").click().run()
    assert not at.exception, list(at.exception)
    assert _ss(at, "pv_chart") == spec, _ss(at, "pv_chart")
    # malformed -> graceful
    at.text_area(key="pv_graph_json").set_value("{nope").run()
    at.button(key="pv_graph_apply").click().run()
    assert not at.exception, list(at.exception)


def main():
    if not _backend_up():
        print(f"SKIP: backend not reachable at {API} (start risk_api on :8010 to run UI tests)")
        raise SystemExit(0)
    _seed_repo()
    passed = failed = 0
    try:
        for fn in RESULTS:
            try:
                fn()
                print(f"PASS  {fn.__name__}")
                passed += 1
            except Exception as e:
                import traceback
                print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed += 1
    finally:
        if TMP_ROOT:
            shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
