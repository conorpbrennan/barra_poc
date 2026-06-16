"""
author_chart_views.py — (re)write the embedded Vega-Lite `chart` spec into the chart views.

Chart views are fully self-describing: each carries `render:"chart"`, named `queries` (each a
self-contained PIVOT query `{name, rows, cols, measures, filters}` — the same tabular query the
L1 grid runs) and a complete Vega-Lite `chart` spec (marks/encodings/scales/theme). Each spec
carries `"source": <query name>` linking the graph to ONE query. risk_pivot_app.py renders them
generically — every query is served by /pivot and its records bound as the spec's default dataset;
NO chart logic in the app. This script is the AUTHORING side: it builds specs + queries here and
saves them, so a view's graph can be regenerated/edited in one place.

    ../barra/bin/python author_chart_views.py
"""
from __future__ import annotations
import views_repo as R

FOLDER = "Public/Soros 13F filings"
SERIF = "Georgia, 'Iowan Old Style', serif"
PAL3 = ["#2a4d69", "#c46d4e", "#6b8f71"]
ACCENT = "#c0392b"

# Tufte/Few light theme — embedded in every spec so the view is self-contained (no app styling).
THEME = {
    "background": "#fdfdfb",
    "view": {"stroke": None},
    "axis": {"grid": False, "domainColor": "#d8d5cd", "tickColor": "#d8d5cd",
             "labelColor": "#44423d", "titleColor": "#1a1a1a",
             "labelFont": SERIF, "titleFont": SERIF, "titleFontWeight": "normal"},
    "legend": {"labelColor": "#1a1a1a", "titleColor": "#1a1a1a",
               "labelFont": SERIF, "titleFont": SERIF},
    "title": {"color": "#1a1a1a", "font": SERIF, "fontWeight": "normal"},
}
VL5 = "https://vega.github.io/schema/vega-lite/v5.json"
# autosize fit + a left pad so container width CONTAINS the y-axis labels (else they clip left).
FIT = {"type": "fit", "contains": "padding"}
PAD = {"left": 22, "top": 5, "right": 14, "bottom": 5}
# the scenario date rides along as an epoch-DAY int measure (the vector's date dual); turn it into a
# real Date in the spec (presentation only) — epoch-days * 86_400_000 ms, then toDate().
_TO_DATE = "toDate(datum['{}'] * 86400000)"


def line_spec(measures: list, xfield: str = "Date") -> dict:
    """Multi-series line over a pivot query's records: x = a row dim, one line per measure (folded)."""
    return {
        "$schema": VL5,
        "transform": [{"fold": measures, "as": ["Measure", "Value"]}],
        "mark": {"type": "line", "point": {"size": 22}},
        "encoding": {
            "x": {"field": xfield, "type": "temporal", "title": None},
            "y": {"field": "Value", "type": "quantitative", "axis": {"format": "%"},
                  "title": "% of NAV"},
            "color": {"field": "Measure", "type": "nominal",
                      "scale": {"domain": measures, "range": PAL3},
                      "legend": {"orient": "top", "title": None}},
            "tooltip": [{"field": xfield, "type": "temporal"},
                        {"field": "Measure", "type": "nominal"},
                        {"field": "Value", "type": "quantitative", "format": ".4f"}],
        },
        "height": 380, "autosize": FIT, "padding": PAD, "config": THEME,
    }


def bar_spec(measures: list, yfield: str) -> dict:
    """Grouped horizontal bars over a pivot query's records: y = a categorical row dim, group by measure."""
    return {
        "$schema": VL5,
        "transform": [{"fold": measures, "as": ["Measure", "Value"]}],
        "mark": "bar",
        "encoding": {
            "y": {"field": yfield, "type": "nominal", "title": None, "sort": "-x"},
            "x": {"field": "Value", "type": "quantitative", "axis": {"format": "%"},
                  "title": "% of NAV"},
            "yOffset": {"field": "Measure", "type": "nominal"},
            "color": {"field": "Measure", "type": "nominal",
                      "scale": {"domain": measures, "range": PAL3},
                      "legend": {"orient": "top", "title": None}},
            "tooltip": [{"field": yfield, "type": "nominal"},
                        {"field": "Measure", "type": "nominal"},
                        {"field": "Value", "type": "quantitative", "format": ".4f"}],
        },
        "height": 420, "autosize": FIT, "padding": PAD, "config": THEME,
    }


def pnl_path_spec() -> dict:
    """Scenario P&L PATH over the `rows=[ScenarioDay]` query records: a line of the per-day book P&L
    (`Scenario PnL at day`) vs its date (`Scenario date at day (epoch)`), with the 99% VaR rule and
    the worst-loss point. The VaR / worst markers are book-level CONSTANTS across the ScenarioDay
    rows (chart-ready, already negative), so `aggregate:min` collapses each to a single mark off the
    same query — every measure is ScenarioDay-gated, so the query is exactly the set's real days."""
    return {
        "$schema": VL5, "height": 300, "autosize": FIT, "padding": PAD, "config": THEME,
        "transform": [
            {"calculate": _TO_DATE.format("Scenario date at day (epoch)"), "as": "date"},
            {"calculate": _TO_DATE.format("Scenario worst date at day (epoch)"), "as": "worst_date"},
        ],
        "layer": [
            {"mark": {"type": "rule", "color": "#d8d5cd"}, "encoding": {"y": {"datum": 0}}},
            {"mark": {"type": "line", "color": PAL3[0], "strokeWidth": 1.3, "point": {"size": 16}},
             "encoding": {"x": {"field": "date", "type": "temporal", "title": None},
                          "y": {"field": "Scenario PnL at day", "type": "quantitative",
                                "axis": {"format": "%"}, "title": "scenario P&L"},
                          "tooltip": [{"field": "date", "type": "temporal"},
                                      {"field": "Scenario PnL at day", "type": "quantitative",
                                       "format": ".2%", "title": "P&L"}]}},
            # invisible wide per-day rules capture the cursor ANYWHERE along x (a 1.3px line is almost
            # impossible to hover) and show the same tooltip — the fix for "line tooltip never shows".
            {"mark": {"type": "rule", "opacity": 0, "strokeWidth": 8},
             "encoding": {"x": {"field": "date", "type": "temporal"},
                          "tooltip": [{"field": "date", "type": "temporal"},
                                      {"field": "Scenario PnL at day", "type": "quantitative",
                                       "format": ".2%", "title": "P&L"}]}},
            {"mark": {"type": "rule", "color": ACCENT, "strokeDash": [4, 4]},
             "encoding": {"y": {"field": "Scenario VaR line at day", "type": "quantitative",
                                "aggregate": "min"}}},
            {"mark": {"type": "point", "color": ACCENT, "size": 80, "filled": True},
             "encoding": {"x": {"field": "worst_date", "type": "temporal", "aggregate": "min"},
                          "y": {"field": "Scenario worst pnl at day", "type": "quantitative",
                                "aggregate": "min"},
                          "tooltip": [{"field": "worst_date", "type": "temporal", "title": "worst date"},
                                      {"field": "Scenario worst pnl at day", "type": "quantitative",
                                       "format": ".2%", "title": "worst P&L", "aggregate": "min"}]}},
        ]}


def pnl_sector_spec() -> dict:
    """Scenario P&L stacked by Sector over the `rows=[ScenarioDay, Sector]` query records: each
    sector's per-day P&L (`Scenario PnL at day`) stacked to the book P&L. The x axis is the scenario
    day, sorted WORST→BEST by the day's BOOK total (a Vega `sort` on the stacked sum — presentation
    only) so it reads as the sorted loss curve, but LABELLED by date (`%d %b`). The 99% VaR rule is
    the BOOK tail (`Scenario VaR line at day` is book-level via tt.total; `aggregate:min` -> one rule)."""
    return {
        "$schema": VL5, "height": 260, "autosize": FIT, "padding": PAD, "config": THEME,
        "transform": [
            {"calculate": _TO_DATE.format("Scenario date at day (epoch)"), "as": "date"},
        ],
        "layer": [
            {"mark": {"type": "area", "opacity": 0.85, "line": {"strokeWidth": 0.4}},
             "encoding": {
                 # ordinal day, ordered by the day's summed (book) P&L ascending = worst→best, but
                 # the tick LABELS are the date (epoch-day -> ms -> %d %b), angled to fit ~80 days.
                 "x": {"field": "Scenario date at day (epoch)", "type": "ordinal",
                       "sort": {"field": "Scenario PnL at day", "op": "sum", "order": "ascending"},
                       "axis": {"labelExpr": "timeFormat(toDate(datum.value * 86400000), '%d %b')",
                                "labelOverlap": True, "labelAngle": -45},
                       "title": "scenario date (worst → best)"},
                 "y": {"field": "Scenario PnL at day", "type": "quantitative", "stack": "zero",
                       "axis": {"format": "%"}, "title": "scenario P&L"},
                 "color": {"field": "Sector", "type": "nominal", "scale": {"scheme": "set2"},
                           "legend": {"orient": "top", "title": None}},
                 "tooltip": [{"field": "date", "type": "temporal", "title": "date"},
                             {"field": "Sector", "type": "nominal"},
                             {"field": "Scenario PnL at day", "type": "quantitative",
                              "format": ".2%", "title": "P&L"}]}},
            {"mark": {"type": "rule", "color": ACCENT, "strokeDash": [4, 4]},
             "encoding": {"y": {"field": "Scenario VaR line at day", "type": "quantitative",
                                "aggregate": "min"}}},
        ]}


def set_chart(slug: str, render: str, queries: list, chart) -> None:
    """`queries` is a list of named self-contained pivot queries; `chart` is a spec (or list) where
    each spec carries `"source": <name>` referencing one of them. Drops any legacy `source` key."""
    rel = f"{FOLDER}/{slug}.json"
    doc = R.load_view(rel)
    s = doc["state"]
    s.pop("source", None)                       # retire the old thin-feed abstraction
    s["render"] = render
    s["queries"] = queries
    s["chart"] = chart
    R.save_view(doc["name"], FOLDER, s)
    print(f"{slug:34} render={render} queries={[q['name'] for q in queries]}")


def _ref(name, spec):
    """Attach a query reference (by name) to a chart spec."""
    return {"source": name, **spec}


if __name__ == "__main__":
    # var-trend: one Date-series pivot query, drawn as a multi-measure line.
    set_chart("var-trend-histfull", "chart",
              [{"name": "VaR trend", "rows": ["Date"], "cols": [],
                "measures": ["Scenario VaR 99", "Total VaR 99", "Specific vol"],
                "filters": {"Book": ["Soros"], "ScenarioSet": ["HistFull"]}}],
              _ref("VaR trend", line_spec(["Scenario VaR 99", "Total VaR 99", "Specific vol"])))

    # stress-board: one ScenarioSet pivot query, drawn as grouped bars.
    set_chart("stress-board-all-scenarios", "chart",
              [{"name": "Stress board", "rows": ["ScenarioSet"], "cols": [],
                "measures": ["Scenario VaR 99", "Scenario worst loss", "Total VaR 99"],
                "filters": {"Book": ["Soros"], "Date": ["2024-12-31"]}}],
              _ref("Stress board", bar_spec(["Scenario VaR 99", "Scenario worst loss",
                                             "Total VaR 99"], "ScenarioSet")))

    # COVID: TWO structurally-different pivot queries (the only difference is `rows`), each drawn by
    # its own graph. ScenarioDay unpacks the scenario P&L vector into a per-day series in the cube.
    _covid_filters = {"Book": ["Soros"], "Date": ["2024-12-31"], "ScenarioSet": ["Evt:COVID2020"]}
    set_chart("scenario-p-l-covid-2020", "chart",
              [{"name": "Scenario P&L", "rows": ["ScenarioDay"], "cols": [],
                "measures": ["Scenario PnL at day", "Scenario date at day (epoch)",
                             "Scenario VaR line at day", "Scenario worst pnl at day",
                             "Scenario worst date at day (epoch)"],
                "filters": _covid_filters},
               {"name": "Scenario P&L by Sector", "rows": ["ScenarioDay", "Sector"], "cols": [],
                "measures": ["Scenario PnL at day", "Scenario date at day (epoch)",
                             "Scenario VaR line at day"],
                "filters": _covid_filters}],
              [_ref("Scenario P&L", pnl_path_spec()),
               _ref("Scenario P&L by Sector", pnl_sector_spec())])
