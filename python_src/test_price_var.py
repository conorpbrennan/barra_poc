"""
test_price_var.py — the Price VaR family + /var_bridge (docs/price-var-plan.md).

  INTEG — need the live backend on :8010; SKIP if down:
    * /dims exposes PriceSet + price_dependent.
    * /pivot: Price VaR 99 with no PriceSet context carries the warning; sliced to HistFull it
      returns a sane number, close in magnitude to Total VaR 99.
    * Euler: Σ Marginal Price VaR 99 (by Position) == Price VaR 99 (manager, additive read-off).
    * `%` sums to 1: Σ % of Price VaR 99 (by Position) == 1.
    * Units context: /pivot?units=dollar on Price VaR 99 == Price VaR 99 (weight) × Manager MV.
    * Set semantics: an Evt:* PriceSet reads a DIFFERENT number from HistFull (a real window).
    * /var_bridge: the four terms sum to T4 - T0 exactly; verification diff is tiny; coverage is a
      fraction in [0, 1]; a Hypo:* set 400s (no Price mirror).
  LIVE  — opt-in only (RUN_LLM=1): stream /var_bridge/analysis and check it returns markdown.

Run:  BARRA_API=http://127.0.0.1:8010 ../barra/bin/python test_price_var.py
      RUN_LLM=1 BARRA_API=... ../barra/bin/python test_price_var.py
"""
from __future__ import annotations
import json
import os
import urllib.parse

API = os.environ.get("BARRA_API", "http://127.0.0.1:8010")
INTEG, LIVE = [], []
DATE = None


def integ(fn):
    INTEG.append(fn); return fn


def live(fn):
    LIVE.append(fn); return fn


def _backend_up():
    global DATE
    try:
        import requests
        r = requests.get(f"{API}/meta", timeout=30)
        if r.status_code != 200:
            return False
        DATE = (r.json().get("dates") or [None])[-1]
        return DATE is not None
    except Exception:
        return False


def _has_price_family() -> bool:
    import requests
    d = requests.get(f"{API}/dims", timeout=30).json()
    return "Price VaR 99" in (d.get("measures") or [])


def _pivot(rows="", measures="", filters=None):
    import requests
    q = {"rows": rows, "measures": measures, "filters": json.dumps(filters or {})}
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=120)
    r.raise_for_status()
    return r.json()


def _manager_cell(measures, price_set="HistFull"):
    j = _pivot(rows="PriceSet", measures=measures,
              filters={"Manager": ["Soros"], "Date": [DATE], "PriceSet": [price_set]})
    assert j["records"], j
    return j["records"][0]


# --------------------------------------------------------------------------- INTEG
@integ
def t_dims_exposes_priceset():
    import requests
    d = requests.get(f"{API}/dims", timeout=30).json()
    if not _has_price_family():
        print("  (Price family not built on this backend — skipping)")
        return
    assert "PriceSet" in d["dimensions"], d["dimensions"]
    assert "price_dependent" in d, d.keys()
    assert "Price VaR 99" in d["price_dependent"], d["price_dependent"]
    members = d.get("members", {}).get("PriceSet") or []
    assert "HistFull" in members, members
    assert not any(str(x).startswith("Hypo:") for x in members), members


@integ
def t_price_var_needs_priceset_context():
    if not _has_price_family():
        return
    j = _pivot(rows="ScenarioSet", measures="Price VaR 99",
              filters={"Manager": ["Soros"], "Date": [DATE], "ScenarioSet": ["HistFull"]})
    assert j.get("warning") and "PriceSet" in j["warning"], j.get("warning")


@integ
def t_price_var_sane_vs_total_var():
    if not _has_price_family():
        return
    price = _manager_cell("Price VaR 99")
    total = _manager_cell("Total VaR 99")     # ScenarioSet-dependent — but Manager/Date suffice, PriceSet ignored
    p99 = float(price["Price VaR 99"])
    assert 0 < p99 < 0.5, p99             # a sane daily-VaR-scale number (fraction of the portfolio)
    t99 = float(total.get("Total VaR 99") or 0)
    if t99:
        assert 0.2 < p99 / t99 < 5, (p99, t99)   # same order of magnitude, not a wildly different unit


@integ
def t_price_var_euler_sums():
    if not _has_price_family():
        return
    j = _pivot(rows="Position", measures="Marginal Price VaR 99",
              filters={"Manager": ["Soros"], "Date": [DATE], "PriceSet": ["HistFull"]}, )
    total = sum(float(r["Marginal Price VaR 99"]) for r in j["records"] if r.get("Marginal Price VaR 99") is not None)
    total_ = float(_manager_cell("Price VaR 99")["Price VaR 99"])
    assert abs(total - total_) < 5e-4, (total, total_)  # additive read-off vs interpolated quantile


@integ
def t_price_var_pct_sums_to_one():
    if not _has_price_family():
        return
    j = _pivot(rows="Position", measures="% of Price VaR 99",
              filters={"Manager": ["Soros"], "Date": [DATE], "PriceSet": ["HistFull"]})
    total = sum(float(r["% of Price VaR 99"]) for r in j["records"] if r.get("% of Price VaR 99") is not None)
    assert abs(total - 1.0) < 1e-6, total


@integ
def t_price_var_dollar_units():
    """Units context (2026-08-22): units=dollar slices the SAME measure name to Manager MV × base
    — there is no more "Price VaR 99 $" measure name to ask for."""
    if not _has_price_family():
        return
    base_cell = _manager_cell("Price VaR 99,Manager MV")
    var99, mv = float(base_cell["Price VaR 99"]), float(base_cell["Manager MV"])
    import requests
    q = {"rows": "PriceSet", "measures": "Price VaR 99", "units": "dollar",
         "filters": json.dumps({"Manager": ["Soros"], "Date": [DATE], "PriceSet": ["HistFull"]})}
    r = requests.get(f"{API}/pivot?{urllib.parse.urlencode(q)}", timeout=120)
    r.raise_for_status()
    jd = r.json()
    assert jd["units"] == "dollar", jd.get("units")
    assert "Price VaR 99" in (jd.get("dollar_measures") or []), jd.get("dollar_measures")
    dollar = float(jd["records"][0]["Price VaR 99"])
    assert abs(dollar - var99 * mv) < max(1.0, abs(var99 * mv) * 1e-9), (dollar, var99, mv)


@integ
def t_price_var_set_semantics():
    if not _has_price_family():
        return
    hist = float(_manager_cell("Price VaR 99", "HistFull")["Price VaR 99"])
    j = _pivot(rows="PriceSet", measures="Price VaR 99",
              filters={"Manager": ["Soros"], "Date": [DATE], "PriceSet": ["Evt:COVID2020"]})
    if not j["records"]:      # the event window may not exist on every manager's calendar
        return
    covid = float(j["records"][0]["Price VaR 99"])
    assert abs(hist - covid) > 1e-6, (hist, covid)   # a real, different window


@integ
def t_var_bridge_terms_sum_and_verification():
    import requests
    if not _has_price_family():
        return
    r = requests.get(f"{API}/var_bridge", params={"date": DATE, "manager": "Soros", "set": "HistFull"}, timeout=120)
    r.raise_for_status()
    j = r.json()
    for k in ("date", "manager", "set", "alpha", "steps", "terms", "coverage", "disagreements", "verification"):
        assert k in j, (k, j.keys())
    t0 = next(s["value"] for s in j["steps"] if s["step"] == "T0")
    t4 = next(s["value"] for s in j["steps"] if s["step"] == "T4")
    term_sum = sum(j["terms"].values())
    assert abs(term_sum - (t4 - t0)) < 1e-9, (term_sum, t4 - t0)
    assert j["verification"]["diff"] < 1e-6, j["verification"]
    at_tail = j["coverage"]["at_tail"]
    assert -1e-9 <= at_tail <= 1.0 + 1e-9, at_tail


@integ
def t_var_bridge_rejects_hypo_set():
    import requests
    if not _has_price_family():
        return
    r = requests.get(f"{API}/var_bridge", params={"date": DATE, "manager": "Soros", "set": "Hypo:MomentumCrash"},
                     timeout=60)
    assert r.status_code == 400, (r.status_code, r.text)


@integ
def t_ask_tool_allowlist_carries_price_when_built():
    """query_cube's description enumerates DIM_NAMES/MEASURE_NAMES at import time (same snapshot
    the other v2-only measures use) — PriceSet/Price VaR 99 present iff the cube built them."""
    import risk_api
    desc = risk_api.QUERY_CUBE_TOOL["description"]
    if _has_price_family():
        assert "PriceSet" in desc or "Price VaR 99" in risk_api.MEASURE_NAMES


# --------------------------------------------------------------------------- LIVE (RUN_LLM=1)
@live
def t_var_bridge_analysis_streams_markdown():
    import requests
    r = requests.post(f"{API}/var_bridge/analysis", json={"date": DATE, "manager": "Soros", "set": "HistFull"},
                      timeout=120)
    r.raise_for_status()
    text = r.text
    assert len(text) > 80, f"expected a markdown read, got {len(text)} chars"


def main():
    p = f = 0
    print("=== integration (live backend) ===")
    if _backend_up():
        for fn in INTEG:
            try:
                fn(); print(f"PASS  {fn.__name__}"); p += 1
            except Exception as e:
                import traceback
                print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
        if os.environ.get("RUN_LLM") == "1":
            print("\n=== live LLM (RUN_LLM=1) ===")
            for fn in LIVE:
                try:
                    fn(); print(f"PASS  {fn.__name__}"); p += 1
                except Exception as e:
                    import traceback
                    print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}"); traceback.print_exc(); f += 1
        else:
            print("\nSKIP live LLM (set RUN_LLM=1 to stream /var_bridge/analysis)")
    else:
        print(f"SKIP: backend not reachable at {API}")
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)


if __name__ == "__main__":
    main()
